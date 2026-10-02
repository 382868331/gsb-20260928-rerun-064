"""Incremental re-checking: re-evaluate only what a data change affects.

The check plan is derived from the actual content of the diff, not from a
blanket "something changed" flag:

- Row-level rules (required_fields, date_order) re-judge only records whose
  relevant fields were modified, plus added records.  Editing a date cell
  never touches identity rules, and vice versa.
- Cross-record rules (unique_fields) re-judge only the identity groups that
  the diff can reshuffle: any group containing a record whose key changed,
  was added, or was removed.  Groups no touched record can belong to keep
  their previous verdict, so a small identity edit no longer forces a full
  re-scan -- yet every group that could gain or lose a conflict is fully
  re-built, keeping the result identical to a complete recompute.
- A rule whose definition changed, whose dependency was re-checked in full,
  or whose previous run ended incomplete is re-checked in full: a stale
  carried conclusion is never treated as this run's verdict.

If a rule fails mid-run, its previous conclusion is kept verbatim and the
rule is reported in incomplete_rules together with the exact scope this run
did not cover; unrelated rules still finish normally.
"""

from dataclasses import dataclass

from .engine import CHECKS, DependencyError, _cell, evaluate, topo_order
from .models import RunResult, utc_now
from .values import is_bad

# Rule types whose verdicts span records; scoped by identity group.
CROSS_RECORD_TYPES = {"unique_fields", "group_sum"}

_ABSENT = object()


@dataclass(frozen=True)
class RulePlan:
    """One rule's planned scope for an incremental run.

    scope:    "skip" | "records" | "groups" | "full"
    targets:  record ids re-judged this run (empty for skip/full).
    replaces: record ids whose carried-over issues are discarded first.
    reasons:  human-readable justification, one entry per change kind.
    """

    rule_id: str
    scope: str
    targets: tuple = ()
    replaces: tuple = ()
    reasons: tuple = ()


def diff_batches(old_batch, new_batch):
    """Return (changed, added, removed) record id tuples between batches."""
    old_by_id = {r.record_id: r for r in old_batch.records}
    new_by_id = {r.record_id: r for r in new_batch.records}
    changed = tuple(
        rid for rid, record in new_by_id.items()
        if rid in old_by_id and old_by_id[rid].data != record.data
    )
    added = tuple(rid for rid in new_by_id if rid not in old_by_id)
    removed = tuple(rid for rid in old_by_id if rid not in new_by_id)
    return changed, added, removed


def field_changes(old_batch, new_batch):
    """Map each changed record id to the set of fields that differ."""
    old_by_id = {r.record_id: r for r in old_batch.records}
    changes = {}
    for record in new_batch.records:
        old = old_by_id.get(record.record_id)
        if old is None:
            continue
        diff = {
            name
            for name in set(old.data) | set(record.data)
            if old.data.get(name, _ABSENT) != record.data.get(name, _ABSENT)
        }
        if diff:
            changes[record.record_id] = diff
    return changes


def _rule_fields(rule):
    """All record fields a rule's verdict can depend on.

    Returns None when the field set cannot be determined (e.g. a broken
    rule definition); callers must then treat every change as relevant.
    """
    params = rule.params or {}
    if rule.rule_type == "date_order":
        early = params.get("early_field")
        late = params.get("late_field")
        if not early or not late:
            return None
        return set(rule.fields) | {early, late}
    if rule.rule_type == "unique_fields":
        key_fields = params.get("key_fields") or list(rule.fields)
        if not key_fields:
            return None
        return set(rule.fields) | set(key_fields)
    if rule.rule_type == "group_sum":
        key_fields = params.get("key_fields") or list(rule.fields)
        sum_field = params.get("sum_field")
        if not key_fields or not sum_field:
            return None
        return set(rule.fields) | set(key_fields) | {sum_field}
    return set(rule.fields)


def _identity_key(record, key_fields):
    values = tuple(_cell(record, name) for name in key_fields)
    if any(is_bad(value) for value in values):
        return None
    return values


def _definition_changed(rule, prev_rules):
    prev = {r.rule_id: r for r in prev_rules}.get(rule.rule_id)
    if prev is None:
        return True
    return (
        prev.rule_type != rule.rule_type
        or prev.fields != rule.fields
        or prev.params != rule.params
        or prev.depends_on != rule.depends_on
    )


def build_plan(rules, old_batch, new_batch, prev_rules=None,
               prev_incomplete=()):
    """Plan each rule's re-check scope for the old->new batch diff.

    Returns RulePlan entries in dependency order.  Every plan names the
    exact record groups it re-judges and why, so the plan itself can be
    audited against the previous run before anything executes.
    """
    ordered = topo_order(rules)
    changed, added, removed = diff_batches(old_batch, new_batch)
    fchanges = field_changes(old_batch, new_batch)
    old_by_id = {r.record_id: r for r in old_batch.records}
    new_by_id = {r.record_id: r for r in new_batch.records}
    prev_incomplete = set(prev_incomplete)

    plans = {}
    for rule in ordered:
        reasons = []
        relevant = _rule_fields(rule)
        if relevant is None:
            # Field set unknown (broken definition): any change may matter.
            modified = changed
        else:
            modified = tuple(rid for rid in changed
                             if fchanges.get(rid, set()) & relevant)
        if modified:
            if relevant is None:
                reasons.append(
                    "modified records (field set undeterminable): "
                    + ", ".join(modified)
                )
            else:
                reasons.append(
                    "modified relevant fields: " + ", ".join(
                        f"{rid} ({'/'.join(sorted(fchanges[rid] & relevant))})"
                        for rid in modified
                    )
                )
        if added:
            reasons.append("added records: " + ", ".join(added))
        if removed:
            reasons.append("removed records: " + ", ".join(removed))

        dep_targets = []
        dep_full = False
        for dep in rule.depends_on:
            dplan = plans[dep]
            if dplan.scope == "full":
                dep_full = True
            elif dplan.targets:
                dep_targets.extend(dplan.targets)
                reasons.append(
                    f"dependency {dep!r} re-judged: "
                    + ", ".join(dplan.targets)
                )

        touched = tuple(dict.fromkeys((*modified, *added, *dep_targets)))

        if prev_rules is not None and _definition_changed(rule, prev_rules):
            plans[rule.rule_id] = RulePlan(
                rule.rule_id, "full",
                reasons=("rule definition changed since previous run",),
            )
            continue
        if rule.rule_id in prev_incomplete:
            plans[rule.rule_id] = RulePlan(
                rule.rule_id, "full",
                reasons=(
                    "previous run left this rule incomplete; its kept "
                    "conclusion is stale and must not count as this "
                    "run's verdict",
                ),
            )
            continue
        if dep_full:
            plans[rule.rule_id] = RulePlan(
                rule.rule_id, "full",
                reasons=("a dependency is re-checked in full",),
            )
            continue
        if rule.rule_type not in CHECKS:
            plans[rule.rule_id] = RulePlan(
                rule.rule_id, "full",
                reasons=(f"unknown rule type {rule.rule_type!r}",),
            )
            continue

        if rule.rule_type in CROSS_RECORD_TYPES:
            if not touched and not removed:
                plans[rule.rule_id] = RulePlan(
                    rule.rule_id, "skip",
                    reasons=("no identity-relevant change",),
                )
                continue
            key_fields = (rule.params.get("key_fields")
                          or list(rule.fields))
            if not key_fields:
                plans[rule.rule_id] = RulePlan(
                    rule.rule_id, "full",
                    reasons=("identity key fields are not determinable",),
                )
                continue
            affected_keys = set()
            for rid in (*touched, *removed):
                old = old_by_id.get(rid)
                if old is not None:
                    key = _identity_key(old, key_fields)
                    if key is not None:
                        affected_keys.add(key)
                new = new_by_id.get(rid)
                if new is not None:
                    key = _identity_key(new, key_fields)
                    if key is not None:
                        affected_keys.add(key)
            targets = tuple(
                rid for rid, record in new_by_id.items()
                if rid in touched
                or _identity_key(record, key_fields) in affected_keys
            )
            replaces = tuple(
                rid for rid, record in old_by_id.items()
                if rid in touched
                or _identity_key(record, key_fields) in affected_keys
            )
            plans[rule.rule_id] = RulePlan(
                rule.rule_id, "groups", targets=targets, replaces=replaces,
                reasons=tuple(reasons) + (
                    f"affected identity groups: {len(affected_keys)} "
                    f"covering {len(targets)} record(s)",
                ),
            )
            continue

        # Row-level rule: removals only drop carried issues, nothing to
        # re-judge; only records whose relevant fields changed are targets.
        if not touched:
            plans[rule.rule_id] = RulePlan(
                rule.rule_id, "skip",
                reasons=("no relevant field change",),
            )
            continue
        targets = tuple(rid for rid in touched if rid in new_by_id)
        plans[rule.rule_id] = RulePlan(
            rule.rule_id, "records", targets=targets, replaces=targets,
            reasons=tuple(reasons),
        )
    return tuple(plans[rule.rule_id] for rule in ordered)


def plan_scopes(rules, old_batch, new_batch, prev_rules=None,
                prev_incomplete=()):
    """Map each rule id to "full", "records", "groups" or "skip"."""
    return {
        plan.rule_id: plan.scope
        for plan in build_plan(
            rules, old_batch, new_batch,
            prev_rules=prev_rules, prev_incomplete=prev_incomplete,
        )
    }


def _blocked_records(rule, unconfirmed, old_by_id, new_by_id):
    """Record ids whose verdicts cannot be re-confirmed this run.

    ``unconfirmed`` maps a dependency rule id to the record scope it did
    not cover.  For row-level rules the blocked scope is the unconfirmed
    records themselves; for cross-record rules every identity group that
    overlaps an unconfirmed record (in either batch) is blocked, because
    a group total cannot be re-confirmed while part of its membership is
    unverified.  Carried conclusions for blocked records stay valid as
    history but must not count as this run's verdict.
    """
    blocked = set()
    for dep in rule.depends_on:
        dep_unconfirmed = unconfirmed.get(dep)
        if not dep_unconfirmed:
            continue
        if rule.rule_type not in CROSS_RECORD_TYPES:
            blocked |= set(dep_unconfirmed)
            continue
        key_fields = rule.params.get("key_fields") or list(rule.fields)
        if not key_fields:
            blocked |= set(dep_unconfirmed)
            continue
        keys = set()
        for rid in dep_unconfirmed:
            for source in (old_by_id, new_by_id):
                record = source.get(rid)
                if record is None:
                    continue
                key = _identity_key(record, key_fields)
                if key is not None:
                    keys.add(key)
        for source in (old_by_id, new_by_id):
            for rid, record in source.items():
                if _identity_key(record, key_fields) in keys:
                    blocked.add(rid)
        # Records with unverifiable identity block at least themselves.
        blocked |= set(dep_unconfirmed)
    return blocked


def describe_plan(plans, prev_result=None):
    """Render the before/after check-plan comparison as text lines."""
    previous = {}
    if prev_result is not None:
        for rid in prev_result.completed_rules:
            previous[rid] = "completed"
        for rid in prev_result.incomplete_rules:
            previous[rid] = "INCOMPLETE (kept stale conclusion)"
    lines = []
    for plan in plans:
        was = previous.get(plan.rule_id, "no previous conclusion")
        if plan.scope == "skip":
            lines.append(
                f"  [skip] {plan.rule_id} - carried over (was: {was})"
            )
        elif plan.scope == "full":
            lines.append(
                f"  [full] {plan.rule_id} - re-check ALL records "
                f"(was: {was})"
            )
        else:
            lines.append(
                f"  [{plan.scope}] {plan.rule_id} - re-check "
                f"{len(plan.targets)} record(s): "
                f"{', '.join(plan.targets) or '-'} (was: {was})"
            )
        for reason in plan.reasons:
            lines.append(f"      reason: {reason}")
    return lines


def incremental_evaluate(prev_result, old_batch, new_batch, rules,
                         prev_rules=None):
    """Re-check new_batch against rules, reusing prev_result where safe.

    Falls back to a full evaluation when there is no comparable baseline
    (different rule version set or no previous result).
    """
    version_set = rules[0].version_set if rules else "empty"
    if prev_result is None or prev_result.rule_version_set != version_set:
        return evaluate(new_batch, rules)

    started = utc_now()
    _, _, removed = diff_batches(old_batch, new_batch)
    removed_ids = set(removed)

    try:
        plans = build_plan(
            rules, old_batch, new_batch,
            prev_rules=prev_rules,
            prev_incomplete=prev_result.incomplete_rules,
        )
    except DependencyError as exc:
        finished = utc_now()
        return RunResult(
            run_id=f"{new_batch.batch_id}:{version_set}:{started}",
            batch_id=new_batch.batch_id,
            batch_checksum=new_batch.checksum,
            rule_version_set=version_set,
            started_at=started,
            finished_at=finished,
            status="FAILED",
            issues=(),
            errors=(str(exc),),
            incomplete_rules=tuple(rule.rule_id for rule in rules),
        )

    prev_by_rule = {}
    for issue in prev_result.issues:
        prev_by_rule.setdefault(issue.rule_id, []).append(issue)

    # Carried conclusions: drop records that no longer exist and rules that
    # no longer exist; everything else starts from the previous verdict.
    current_rule_ids = {rule.rule_id for rule in rules}
    merged = [
        issue for issue in prev_result.issues
        if issue.record_id not in removed_ids
        and issue.rule_id in current_rule_ids
    ]
    errors = []
    completed = []
    incomplete = []
    new_records = {r.record_id: r for r in new_batch.records}
    old_records = {r.record_id: r for r in old_batch.records}
    run_id = f"{new_batch.batch_id}:{version_set}:{started}"
    plans_by_id = {plan.rule_id: plan for plan in plans}
    # Records each rule failed to (re-)confirm this run; downstream rules
    # use it to tell reusable carried conclusions apart from scope that
    # cannot be re-confirmed because its upstream basis is stale.
    unconfirmed = {}
    extras = {}

    for plan in plans:
        rule = next(r for r in rules if r.rule_id == plan.rule_id)
        blocked = _blocked_records(rule, unconfirmed, old_records, new_records)
        upstream = {}
        for dep in rule.depends_on:
            dep_plan = plans_by_id[dep]
            if dep_plan.scope == "skip" and dep not in unconfirmed:
                upstream[dep] = {
                    "run_id": prev_result.run_id,
                    "use": "carried",
                }
            else:
                upstream[dep] = {"run_id": run_id, "use": "current"}
        extras[rule.rule_id] = {
            "blocked": sorted(blocked),
            "upstream": upstream,
        }
        if plan.scope == "skip":
            if blocked:
                # The carried conclusion rests on an upstream scope that
                # was not updated this run: keep it as history, but say
                # plainly it is not re-confirmed and never report a pass.
                incomplete.append(rule.rule_id)
                unconfirmed[rule.rule_id] = set(blocked)
                errors.append(
                    f"rule {rule.rule_id!r}: upstream scope not updated; "
                    f"carried conclusion for {len(blocked)} record(s) is "
                    f"NOT re-confirmed this run: "
                    + ",".join(sorted(blocked))
                )
                continue
            completed.append(rule.rule_id)
            continue
        if plan.scope == "full":
            targets = [r.record_id for r in new_batch.records]
            replaced_ids = {
                i.record_id for i in prev_by_rule.get(rule.rule_id, [])
            } - removed_ids
        else:
            targets = [rid for rid in plan.targets if rid in new_records]
            replaced_ids = set(plan.replaces) - removed_ids
        planned_targets = list(targets)
        targets = [rid for rid in targets if rid not in blocked]
        replaced_ids = replaced_ids - blocked
        replaced = [i for i in prev_by_rule.get(rule.rule_id, [])
                    if i.record_id in replaced_ids]
        merged = [i for i in merged if i not in replaced]
        relevant = tuple(
            new_records[rid] for rid in targets
            if not any(
                i.record_id == rid and i.rule_id in rule.depends_on
                for i in merged
            )
        )
        handler = CHECKS.get(rule.rule_type)
        try:
            if handler is None:
                raise ValueError(f"unknown rule type {rule.rule_type!r}")
            produced = handler(rule, relevant)
        except Exception as exc:
            # Keep the previous valid conclusion for this rule and say
            # plainly which scope this run failed to cover.
            merged.extend(replaced)
            incomplete.append(rule.rule_id)
            unconfirmed[rule.rule_id] = set(planned_targets) | set(blocked)
            if plan.scope == "full":
                covered = "all records"
            else:
                covered = "records: " + ",".join(sorted(planned_targets))
            errors.append(
                f"rule {rule.rule_id!r} failed during incremental run: {exc}; "
                f"kept previous conclusion; scope NOT completed this run: "
                f"{covered}"
            )
            continue
        merged.extend(produced)
        if blocked:
            # Partially re-confirmed: the blocked slice keeps its previous
            # conclusion as history but is not this run's verdict.
            incomplete.append(rule.rule_id)
            unconfirmed[rule.rule_id] = set(blocked)
            errors.append(
                f"rule {rule.rule_id!r}: upstream scope not updated; "
                f"{len(blocked)} record(s) NOT re-confirmed this run: "
                + ",".join(sorted(blocked))
            )
        else:
            completed.append(rule.rule_id)

    finished = utc_now()
    status = "FAILED" if merged or errors else "PASSED"
    return RunResult(
        run_id=run_id,
        batch_id=new_batch.batch_id,
        batch_checksum=new_batch.checksum,
        rule_version_set=version_set,
        started_at=started,
        finished_at=finished,
        status=status,
        issues=tuple(merged),
        errors=tuple(errors),
        completed_rules=tuple(completed),
        incomplete_rules=tuple(incomplete),
        plan=tuple(
            {
                "rule_id": plan.rule_id,
                "scope": plan.scope,
                "targets": list(plan.targets),
                "reasons": list(plan.reasons),
                "blocked": extras[plan.rule_id]["blocked"],
                "upstream": extras[plan.rule_id]["upstream"],
            }
            for plan in plans
        ),
    )
