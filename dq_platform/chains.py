"""Planning for mutually dependent correction chains (round 6).

A correction chain groups candidate operations where one operation may
retract a record that another, not-yet-accepted candidate produces.  The
planner derives an acceptable order and the final grouped totals, and
classifies every candidate as a legal in-chain continuation, an identity
conflict, a cyclic dependency or a missing prerequisite.

Safety rules:

- Only a dependency-closed part of the chain can be accepted; the rest
  stays candidate.  A downstream op is never accepted while its
  prerequisite is not yet effective.
- Acceptance replays ops in derived order against the current effective
  data only, so unrelated records are never overwritten by reordering.
- The plan is recomputed from the live batch and ledger on every run, so
  data changed before acceptance re-validates every affected chain, while
  already accepted independent corrections stay valid.
"""

import json
from pathlib import Path

from .corrections import (
    CorrectionError,
    RELATIONS,
    effective_batch,
)
from .engine import _cell
from .models import Record
from .values import MISSING, MALFORMED

OK = "ok"
IDENTITY_CONFLICT = "identity_conflict"
CYCLE = "cycle"
MISSING_PREREQUISITE = "missing_prerequisite"

STATUSES = (OK, IDENTITY_CONFLICT, CYCLE, MISSING_PREREQUISITE)


def load_correction_chain(path):
    """Parse a correction chain JSON file into a validated dict."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if "chain_id" not in doc:
        raise CorrectionError("correction chain needs a chain_id")
    ops = []
    seen = set()
    for item in doc.get("operations", ()):
        op_id = item.get("op_id")
        if not op_id:
            raise CorrectionError("every chain operation needs an op_id")
        if op_id in seen:
            raise CorrectionError(f"duplicate op_id {op_id!r}")
        seen.add(op_id)
        relation = item.get("relation")
        if relation not in RELATIONS:
            raise CorrectionError(
                f"op {op_id!r}: unknown relation {relation!r}"
            )
        retracts = tuple(str(r) for r in item.get("retracts", ()))
        records = tuple(dict(r) for r in item.get("records", ()))
        produces = tuple(
            str(r.get("id", f"{op_id}-{idx}"))
            for idx, r in enumerate(records)
        )
        shape = {
            "retract": (len(records) == 0 and len(retracts) >= 1),
            "amend": (len(retracts) == 1 and len(records) == 1),
            "split": (len(retracts) == 1 and len(records) > 1),
            "merge": (len(retracts) > 1 and len(records) == 1),
        }[relation]
        if not shape:
            raise CorrectionError(
                f"op {op_id!r}: relation {relation!r} does not match "
                f"retracts={len(retracts)} records={len(records)}"
            )
        ops.append({
            "op_id": op_id,
            "relation": relation,
            "retracts": retracts,
            "records": records,
            "produces": produces,
            "confirmed": bool(item.get("confirmed", False)),
        })
    return {
        "chain_id": doc["chain_id"],
        "ops": tuple(ops),
        "amount_field": doc.get("amount_field"),
        "group_fields": tuple(str(f) for f in doc.get("group_fields", ())),
    }


def grouped_totals(records, group_fields, amount_field):
    """Sum amount_field per group; MISSING/MALFORMED are never treated
    as zero, they surface as an error entry instead."""
    totals = {}
    errors = []
    for record in records:
        key = tuple(
            str(_cell(record, field)) for field in group_fields
        ) or ("(all)",)
        amount = _cell(record, amount_field) if amount_field else 0
        if amount is MISSING or amount is MALFORMED:
            errors.append(
                f"record {record.record_id!r} has no usable "
                f"{amount_field!r} value"
            )
            continue
        if not isinstance(amount, (int, float)):
            errors.append(
                f"record {record.record_id!r} amount {amount!r} "
                "is not numeric"
            )
            continue
        totals[key] = totals.get(key, 0) + amount
    return totals, errors


def plan_chain(chain, batch, accepted_events):
    """Classify candidate ops and derive an acceptable order.

    Returns a plan dict with per-op status/reasons, the dependency edges
    inside the chain, a topological order of the acceptable ops, and the
    final records/grouped totals that accepting all acceptable ops would
    produce.
    """
    effective, _ = effective_batch(batch, accepted_events)
    current_ids = [record.record_id for record in effective.records]
    accepted_op_ids = {
        (event["set_id"], event["op_id"]) for event in accepted_events
    }
    chain_id = chain["chain_id"]
    pending = [
        op for op in chain["ops"]
        if (chain_id, op["op_id"]) not in accepted_op_ids
    ]

    produced_by = {}
    for op in pending:
        for rid in op["produces"]:
            produced_by.setdefault(rid, []).append(op["op_id"])

    status = {op["op_id"]: OK for op in pending}
    reasons = {op["op_id"]: [] for op in pending}

    # Identity conflicts: duplicate produced identities, contention for
    # the same source record, or producing an identity that is already
    # effective without retracting it.
    for rid, claimants in sorted(produced_by.items()):
        if len(claimants) > 1:
            for op_id in claimants:
                status[op_id] = IDENTITY_CONFLICT
                reasons[op_id].append(
                    f"produces identity {rid!r} also produced by "
                    + ", ".join(c for c in claimants if c != op_id)
                )
    retract_claims = {}
    for op in pending:
        for rid in op["retracts"]:
            if rid in produced_by:
                continue  # in-chain continuation, not contention
            other = retract_claims.setdefault(rid, op["op_id"])
            if other != op["op_id"]:
                for op_id in (other, op["op_id"]):
                    status[op_id] = IDENTITY_CONFLICT
                    reasons[op_id].append(
                        f"contends with another candidate for source "
                        f"record {rid!r}"
                    )
    for op in pending:
        own_retracts = set(op["retracts"])
        for rid in op["produces"]:
            if rid in own_retracts:
                continue
            if rid in current_ids:
                status[op["op_id"]] = IDENTITY_CONFLICT
                reasons[op["op_id"]].append(
                    f"produces identity {rid!r} that is already effective "
                    "without declaring it in retracts"
                )

    # Missing prerequisites: a retracted identity that is neither
    # currently effective nor produced inside the chain.
    edges = {op["op_id"]: set() for op in pending}
    for op in pending:
        for rid in op["retracts"]:
            producers = produced_by.get(rid)
            inbound = [p for p in producers or () if p != op["op_id"]]
            if inbound:
                edges[op["op_id"]].update(inbound)
            elif rid not in current_ids:
                if status[op["op_id"]] == OK:
                    status[op["op_id"]] = MISSING_PREREQUISITE
                reasons[op["op_id"]].append(
                    f"retracts {rid!r}, which is neither a current "
                    "effective record nor produced by another candidate"
                )

    # Cyclic dependencies among candidate ops.
    visiting, done = set(), set()
    in_cycle = set()

    def visit(node, stack):
        if node in visiting:
            in_cycle.update(stack[stack.index(node):])
            return
        if node in done:
            return
        visiting.add(node)
        for nxt in edges.get(node, ()):
            visit(nxt, stack + [nxt])
        visiting.discard(node)
        done.add(node)

    for node in edges:
        visit(node, [node])
    for op_id in sorted(in_cycle):
        status[op_id] = CYCLE
        reasons[op_id].append("part of a cyclic dependency in the chain")

    # A candidate whose in-chain prerequisite is not acceptable can never
    # become effective either; propagate the prerequisite's status.
    changed = True
    while changed:
        changed = False
        for op in pending:
            op_id = op["op_id"]
            if status[op_id] != OK:
                continue
            for dep in edges.get(op_id, ()):
                if status[dep] != OK:
                    status[op_id] = status[dep]
                    reasons[op_id].append(
                        f"prerequisite {dep!r} is not acceptable "
                        f"({status[dep]})"
                    )
                    changed = True

    # Topological order over acceptable ops (prerequisites first).
    acceptable = [
        op["op_id"] for op in pending if status[op["op_id"]] == OK
    ]
    order = []
    placed = set()

    def place(op_id):
        if op_id in placed or op_id not in acceptable:
            return
        placed.add(op_id)  # guard against re-entry
        for dep in sorted(edges.get(op_id, ())):
            place(dep)
        order.append(op_id)

    for op in pending:
        place(op["op_id"])

    # Simulate accepting the acceptable ops in derived order to obtain
    # the final records and grouped totals.  Unrelated records are
    # carried through untouched.
    by_id = {op["op_id"]: op for op in pending}
    final_records = list(effective.records)
    simulation_errors = []
    for op_id in order:
        op = by_id[op_id]
        for rid in op["retracts"]:
            matches = [
                idx for idx, record in enumerate(final_records)
                if record.record_id == rid
            ]
            if len(matches) != 1:
                simulation_errors.append(
                    f"op {op_id!r} retracts {len(matches)} record(s) "
                    f"with identity {rid!r} in derived order"
                )
                continue
            del final_records[matches[0]]
        for idx, data in enumerate(op["records"]):
            rid = str(data.get("id", f"{op_id}-{idx}"))
            final_records.append(Record(
                record_id=rid, index=len(final_records), data=data,
            ))

    totals, total_errors = grouped_totals(
        final_records, chain["group_fields"], chain["amount_field"],
    )
    before_totals, before_errors = grouped_totals(
        effective.records, chain["group_fields"], chain["amount_field"],
    )
    return {
        "chain_id": chain_id,
        "status": status,
        "reasons": reasons,
        "edges": {k: sorted(v) for k, v in edges.items()},
        "order": tuple(order),
        "pending": tuple(op["op_id"] for op in pending),
        "final_records": tuple(final_records),
        "totals": totals,
        "totals_before": before_totals,
        "errors": tuple(simulation_errors + total_errors + before_errors),
    }


def dependency_closure(plan, requested_ids):
    """Expand requested op ids to their full in-chain prerequisite
    closure.  Returns (acceptable, blocked) op id sets: an op is blocked
    when it or any prerequisite is not OK in the plan, or when a
    prerequisite is neither requested nor already effective."""
    edges = plan["edges"]
    status = plan["status"]
    closure = set()
    stack = [op_id for op_id in requested_ids if op_id in status]
    while stack:
        op_id = stack.pop()
        if op_id in closure:
            continue
        closure.add(op_id)
        stack.extend(edges.get(op_id, ()))
    blocked = set()
    for op_id in closure:
        if status.get(op_id) != OK:
            blocked.add(op_id)
            continue
        missing = [
            dep for dep in edges.get(op_id, ())
            if dep not in closure
        ]
        if missing:
            blocked.add(op_id)
    # Dependents of blocked ops are blocked too.
    changed = True
    while changed:
        changed = False
        for op_id in closure:
            if op_id in blocked:
                continue
            if any(dep in blocked for dep in edges.get(op_id, ())):
                blocked.add(op_id)
                changed = True
    return closure - blocked, blocked
