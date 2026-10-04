"""Source-side retractions and replacements, handled as explicit relations.

A correction set is not a field edit: every operation names the old records
it retracts and the replacement records it produces, with an explicit
relation (amend 1->1, split 1->N, merge N->1, retract N->0).  Relations
must be confirmed by the user before they are applied; acceptance is
recorded in an append-only ledger so a half-saved run can be resumed by
accepting the remaining operations later.

Safety rules enforced here:

- A retraction cycle, two mutually exclusive replacements referencing the
  same old record, or a set depending on not-yet-accepted material blocks
  confirmation of the affected operations.
- Unaccepted operations are never mixed into the same pass conclusion as
  the currently effective data.
- Issues inherited through a split keep one identity (resolution is never
  counted twice); issues merged into one record keep every original
  reason.  Each inherited issue is labelled "same_fact" (the underlying
  fact is unchanged) or "new_fact" (needs fresh review).
- Retracting an accepted correction set later reports exactly which
  manual decisions lose their basis.
"""

import json
from pathlib import Path

from .engine import _cell
from .models import Batch, Record, utc_now

RELATIONS = ("amend", "split", "merge", "retract")

SAME_FACT = "same_fact"
NEW_FACT = "new_fact"


class CorrectionError(Exception):
    pass


def load_correction_set(path):
    """Parse a correction set JSON file into a validated dict."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if "set_id" not in doc:
        raise CorrectionError("correction set needs a set_id")
    ops = []
    seen = set()
    for item in doc.get("operations", ()):
        op_id = item.get("op_id")
        if not op_id:
            raise CorrectionError("every operation needs an op_id")
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
        "set_id": doc["set_id"],
        "depends_on": tuple(str(d) for d in doc.get("depends_on", ())),
        "ops": tuple(ops),
    }


class CorrectionLedger:
    """Append-only ledger of confirmations, acceptances and retractions."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "corrections.jsonl"

    def events(self):
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _append(self, event):
        self.root.mkdir(parents=True, exist_ok=True)
        event = dict(event, ts=utc_now())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def confirm(self, set_id, op_id, relation):
        return self._append({
            "event": "confirmed",
            "set_id": set_id,
            "op_id": op_id,
            "relation": relation,
        })

    def accept(self, set_id, op, run_id=None):
        return self._append({
            "event": "accepted",
            "set_id": set_id,
            "op_id": op["op_id"],
            "relation": op["relation"],
            "retracts": list(op["retracts"]),
            "records": list(op["records"]),
            "produces": list(op["produces"]),
            "run_id": run_id,
        })

    def retract_set(self, set_id):
        return self._append({"event": "set_retracted", "set_id": set_id})

    def confirmed_ops(self):
        return {
            (e["set_id"], e["op_id"])
            for e in self.events() if e["event"] == "confirmed"
        }

    def retracted_sets(self):
        return {
            e["set_id"] for e in self.events()
            if e["event"] == "set_retracted"
        }

    def active_accepted(self):
        """Accepted ops whose set has not been retracted, in order."""
        retracted = self.retracted_sets()
        return [
            e for e in self.events()
            if e["event"] == "accepted" and e["set_id"] not in retracted
        ]

    def accepted_set_ids(self):
        return {e["set_id"] for e in self.active_accepted()}


def validate_correction_set(cset, ledger):
    """Return (errors, blocked_op_ids) for an incoming correction set.

    Blocks confirmation of the affected scope when: the set depends on
    unaccepted material, two replacements reference the same old record,
    or retractions form a cycle through produced records.
    """
    errors = []
    blocked = set()
    set_id = cset["set_id"]

    missing_deps = [
        dep for dep in cset["depends_on"]
        if dep not in ledger.accepted_set_ids()
    ]
    if missing_deps:
        errors.append(
            f"correction set {set_id!r} depends on unaccepted material: "
            + ", ".join(missing_deps)
        )
        blocked.update(op["op_id"] for op in cset["ops"])
        return errors, blocked

    # Mutually exclusive replacements of the same old record.
    claimant = {}
    for op in cset["ops"]:
        for rid in op["retracts"]:
            other = claimant.get(rid)
            if other is not None and other != op["op_id"]:
                errors.append(
                    f"old record {rid!r} is referenced by mutually "
                    f"exclusive replacements {other!r} and {op['op_id']!r}"
                )
                blocked.add(other)
                blocked.add(op["op_id"])
            claimant.setdefault(rid, op["op_id"])

    # Retraction cycles: op -> producer of each record it retracts.
    produced_by = {}
    for event in ledger.active_accepted():
        for rid in event.get("produces", ()):
            produced_by[rid] = (event["set_id"], event["op_id"])
    for op in cset["ops"]:
        for rid in op["produces"]:
            produced_by[rid] = (set_id, op["op_id"])
    edges = {}
    for node in set(produced_by.values()):
        edges.setdefault(node, set())
    for op in cset["ops"]:
        node = (set_id, op["op_id"])
        edges.setdefault(node, set())
        for rid in op["retracts"]:
            producer = produced_by.get(rid)
            if producer is not None and producer != node:
                edges[node].add(producer)

    visiting, done, in_cycle = set(), set(), set()

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
    for node_set, node_op in sorted(in_cycle):
        if node_set == set_id:
            blocked.add(node_op)
    if in_cycle:
        names = " -> ".join(op for _, op in sorted(in_cycle))
        errors.append(f"retraction cycle among corrections: {names}")
    return errors, blocked


def effective_batch(batch, accepted_events):
    """Rebuild the effective dataset from a base batch + accepted ops.

    Unaccepted operations contribute nothing, so pending corrections are
    never mixed into the same conclusion as the effective data.
    """
    data = {r.record_id: r.data for r in batch.records}
    order = [r.record_id for r in batch.records]
    relations = []
    for event in accepted_events:
        for rid in event.get("retracts", ()):
            if rid in data:
                del data[rid]
                order.remove(rid)
        for idx, record_data in enumerate(event.get("records", ())):
            rid = str(record_data.get("id", f"{event['op_id']}-{idx}"))
            data[rid] = record_data
            order.append(rid)
        relations.append({
            "set_id": event["set_id"],
            "op_id": event["op_id"],
            "relation": event["relation"],
            "retracts": tuple(event.get("retracts", ())),
            "produces": tuple(event.get("produces", ())),
        })
    records = tuple(
        Record(record_id=rid, index=idx, data=data[rid])
        for idx, rid in enumerate(order)
    )
    return Batch(
        batch_id=batch.batch_id,
        source=batch.source + "+corrections",
        loaded_at=utc_now(),
        records=records,
    ), relations


def identity_conflicts(base_ids, events, prior_events=()):
    """Replay retract/produce events and find identity violations.

    Each event is ``(key, retracts, produces)`` where ``key`` labels the
    event in returned errors (for example an ``op_id``).  A produced id is
    legal only when it does not already occur among the event's own outputs
    and, after the event's declared retractions, is not a live record.
    Records that were never declared retracted or merged away can therefore
    never be silently overwritten, and a duplicate identity can never enter
    the effective data.  Returns ``(errors, blocked_keys)``; blocked events
    are not applied, so unrelated events still replay cleanly.
    ``prior_events`` are already accepted events that establish the
    current live state; they are replayed without re-checking but keep
    their producer labels, so a collision discovered after save/reopen
    still names the earlier producing op.
    """
    errors = []
    blocked = set()
    live = {rid: None for rid in base_ids}
    all_events = list(prior_events) + list(events)

    for index, (key, retracts, produces) in enumerate(all_events):
        if index < len(prior_events):
            for rid in set(retracts):
                live.pop(rid, None)
            for rid in produces:
                live[rid] = key
            continue
        retract_set = set(retracts)
        remaining_live = {rid for rid in live if rid not in retract_set}

        duplicated = set()
        seen = set()
        for rid in produces:
            if rid in seen:
                duplicated.add(rid)
            seen.add(rid)

        collisions = {
            rid for rid in set(produces) if rid in remaining_live
        }

        if duplicated:
            for rid in sorted(duplicated):
                errors.append(
                    f"op {key!r} produces duplicate identity {rid!r} "
                    "within the same correction"
                )
        if collisions:
            for rid in sorted(collisions):
                producer = live[rid]
                if producer is None:
                    holder = "the current effective data"
                else:
                    holder = f"live record produced by op {producer!r}"
                errors.append(
                    f"op {key!r} would overwrite {holder} with produced "
                    f"identity {rid!r}, but {rid!r} is not declared in "
                    "retracts; the affected correction is blocked and "
                    "the original record is preserved"
                )

        if duplicated or collisions:
            blocked.add(key)
            continue

        for rid in retract_set:
            live.pop(rid, None)
        for rid in produces:
            live[rid] = key

    return errors, blocked


def check_acceptance_identities(batch, ledger, cset, op_ids):
    """Check candidate ops against the currently effective data.

    The baseline is the base batch plus every accepted correction that is
    still active, so conflicts introduced after a save/reopen (including a
    name colliding with a record produced by an earlier accepted set) are
    caught before acceptance.
    """
    wanted = set(op_ids)
    candidates = [
        op for op in cset["ops"] if op["op_id"] in wanted
    ]
    active = ledger.active_accepted()
    prior_events = [
        (
            event["op_id"],
            tuple(event.get("retracts", ())),
            tuple(event.get("produces", ())),
        )
        for event in active
    ]
    events = [
        (
            op["op_id"],
            tuple(op["retracts"]),
            tuple(op["produces"]),
        )
        for op in candidates
    ]
    return identity_conflicts(
        [r.record_id for r in batch.records], events,
        prior_events=prior_events,
    )


def check_retraction_identities(batch, ledger, set_id):
    """Check the effective data left after retracting one correction set.

    Retracting a set can resurrect base records or expose records produced
    by independent sets; the retraction is refused when that would leave a
    produced identity colliding with a still-live record.
    """
    remaining = [
        event for event in ledger.active_accepted()
        if event["set_id"] != set_id
    ]
    events = [
        (
            f"{event['set_id']}:{event['op_id']}",
            tuple(event.get("retracts", ())),
            tuple(event.get("produces", ())),
        )
        for event in remaining
    ]
    return identity_conflicts(
        [r.record_id for r in batch.records], events,
    )


def _fact_value(record, field):
    """The value(s) an issue is about, for same-fact comparison."""
    names = field.split(",") if field else ()
    if len(names) == 1:
        return _cell(record, names[0])
    return tuple(_cell(record, name) for name in names)


def _same_value(left, right):
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(
            _same_value(a, b) for a, b in zip(left, right)
        )
    if left is right:
        return True
    try:
        return bool(left == right)
    except TypeError:
        return False


def migrate_issues(prev_issues, relations, old_records, new_records):
    """Map issues on retracted records onto their replacements.

    Every entry keeps the origin issue identity, so a split issue is one
    problem with several inheritors (never several resolutions), and a
    merge keeps each origin's own reason.
    """
    old_by_id = {r.record_id: r for r in old_records}
    new_by_id = {r.record_id: r for r in new_records}
    issues_by_record = {}
    for issue in prev_issues:
        issues_by_record.setdefault(issue.record_id, []).append(issue)

    entries = []
    for rel in relations:
        produced = [
            new_by_id[rid] for rid in rel["produces"] if rid in new_by_id
        ]
        for rid in rel["retracts"]:
            old = old_by_id.get(rid)
            for issue in issues_by_record.get(rid, ()):
                origin = (
                    issue.rule_id, issue.record_id, issue.field, issue.kind,
                )
                targets = []
                if old is not None and produced:
                    old_value = _fact_value(old, issue.field)
                    if rel["relation"] in ("amend", "merge"):
                        candidates = produced[:1]
                    else:  # split: the fact follows the value
                        candidates = produced
                    for record in candidates:
                        inheritance = (
                            SAME_FACT
                            if _same_value(
                                old_value, _fact_value(record, issue.field)
                            )
                            else NEW_FACT
                        )
                        if rel["relation"] == "split" and (
                            inheritance == NEW_FACT
                        ):
                            continue
                        targets.append({
                            "record_id": record.record_id,
                            "inheritance": inheritance,
                        })
                entries.append({
                    "origin": origin,
                    "set_id": rel["set_id"],
                    "op_id": rel["op_id"],
                    "relation": rel["relation"],
                    "targets": targets,
                    "reasons": [issue.detail],
                    "resolved_by_correction": not targets,
                })
    return entries


def inherited_issue_count(entries):
    """Open inherited problems, deduplicated by origin issue identity."""
    return sum(1 for e in entries if not e["resolved_by_correction"])


def resolution_count(entries):
    """Resolutions caused by corrections: one per origin, never per
    split target."""
    return sum(1 for e in entries if e["resolved_by_correction"])


def classify_changes(prev_issues, new_issues, migrations,
                     prev_records, new_records):
    """Classify issue-set differences between two runs.

    Distinguishes business value changes, source relation changes and
    manual-decision migrations, so a full recompute and a stepwise
    acceptance can be compared by cause, not just by diff.
    """
    def key(issue):
        return (issue.rule_id, issue.record_id, issue.field, issue.kind)

    prev_keys = {key(i) for i in prev_issues}
    new_keys = {key(i) for i in new_issues}
    target_kind = {}
    relation_records = set()
    for entry in migrations:
        origin = entry["origin"]
        relation_records.add(origin[1])
        for target in entry["targets"]:
            relation_records.add(target["record_id"])
            target_kind[
                (origin[0], target["record_id"], origin[2], origin[3])
            ] = target["inheritance"]
    prev_by_id = {r.record_id: r for r in prev_records}
    new_by_id = {r.record_id: r for r in new_records}
    result = {"value_change": [], "relation_change": [],
              "decision_migration": []}
    for issue_key in sorted(new_keys - prev_keys):
        record_id = issue_key[1]
        inheritance = target_kind.get(issue_key)
        if inheritance == SAME_FACT:
            result["decision_migration"].append(issue_key)
        elif inheritance == NEW_FACT:
            result["relation_change"].append(issue_key)
        elif (record_id in prev_by_id and record_id in new_by_id
                and prev_by_id[record_id].data != new_by_id[record_id].data):
            result["value_change"].append(issue_key)
        elif record_id in relation_records:
            result["relation_change"].append(issue_key)
        else:
            result["value_change"].append(issue_key)
    return result


class DecisionLedger:
    """Append-only manual review decisions tied to their accepted basis."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "decisions.jsonl"

    def events(self):
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _append(self, event):
        self.root.mkdir(parents=True, exist_ok=True)
        event = dict(event, ts=utc_now())
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def confirm(self, issue_key, decision, basis_sets, run_id):
        return self._append({
            "event": "decision",
            "key": list(issue_key),
            "decision": decision,
            "basis_sets": sorted(basis_sets),
            "run_id": run_id,
        })

    def active_decisions(self):
        lost = {
            tuple(e["key"]) for e in self.events()
            if e["event"] == "basis_lost"
        }
        return [
            e for e in self.events()
            if e["event"] == "decision" and tuple(e["key"]) not in lost
        ]

    def retract_basis(self, set_id):
        """Mark every decision based on set_id as having lost its basis."""
        lost = []
        already = {
            tuple(e["key"]) for e in self.events()
            if e["event"] == "basis_lost"
        }
        for event in self.events():
            if event["event"] != "decision":
                continue
            if set_id not in event.get("basis_sets", ()):
                continue
            if tuple(event["key"]) in already:
                continue
            self._append({
                "event": "basis_lost",
                "key": event["key"],
                "lost_set": set_id,
                "decision": event["decision"],
            })
            lost.append(event)
        return lost
