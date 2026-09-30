"""Incremental re-checking: re-evaluate only what a data change affects.

Row-level rules (required_fields, date_order) are re-run only for changed
or added records; cross-record rules (unique_fields) are re-run in full
because a single edited cell can reshuffle duplicate groups.  Anything a
dependency invalidates is widened to a full re-run of the dependent rule.

If a rule fails mid-run, its previous conclusion is kept verbatim and the
rule is reported in incomplete_rules, so callers know exactly which scope
this run did not cover.
"""

from .engine import CHECKS, DependencyError, evaluate, topo_order
from .models import RunResult, utc_now

# Rule types whose verdicts span records, so any data change forces a
# full re-evaluation of that rule.
CROSS_RECORD_TYPES = {"unique_fields"}


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


def plan_scopes(rules, changed, added, removed, prev_rules=None):
    """Map each rule id to "full", "records" or "skip" for this diff."""
    ordered = topo_order(rules)
    touched = bool(changed or added or removed)
    scopes = {}
    for rule in ordered:
        deps_full = any(scopes[d] == "full" for d in rule.depends_on)
        deps_active = any(scopes[d] != "skip" for d in rule.depends_on)
        if prev_rules is not None and _definition_changed(rule, prev_rules):
            scope = "full"
        elif deps_full:
            scope = "full"
        elif not touched and not deps_active:
            scope = "skip"
        elif rule.rule_type in CROSS_RECORD_TYPES or rule.rule_type not in CHECKS:
            scope = "full"
        else:
            scope = "records"
        scopes[rule.rule_id] = scope
    return scopes


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
    changed, added, removed = diff_batches(old_batch, new_batch)
    removed_ids = set(removed)
    recheck_ids = set(changed) | set(added)

    try:
        scopes = plan_scopes(rules, changed, added, removed,
                             prev_rules=prev_rules)
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

    ordered = topo_order(rules)
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

    for rule in ordered:
        scope = scopes[rule.rule_id]
        if scope == "skip":
            completed.append(rule.rule_id)
            continue
        kept = [i for i in prev_by_rule.get(rule.rule_id, [])
                if i.record_id not in removed_ids]
        if scope == "records":
            # Only changed/added records are re-judged; verdicts for
            # untouched records carry over unchanged.
            targets = [new_records[rid] for rid in recheck_ids
                       if rid in new_records]
            replaced = [i for i in kept if i.record_id in recheck_ids]
        else:
            targets = list(new_batch.records)
            replaced = kept
        merged = [i for i in merged if i not in replaced]
        relevant = tuple(
            r for r in targets
            if not any(
                i.record_id == r.record_id and i.rule_id in rule.depends_on
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
            covered = (
                "all records" if scope == "full"
                else "records: " + ",".join(sorted(recheck_ids))
            )
            errors.append(
                f"rule {rule.rule_id!r} failed during incremental run: {exc}; "
                f"kept previous conclusion; scope NOT completed this run: "
                f"{covered}"
            )
            continue
        merged.extend(produced)
        completed.append(rule.rule_id)

    finished = utc_now()
    status = "FAILED" if merged or errors else "PASSED"
    return RunResult(
        run_id=f"{new_batch.batch_id}:{version_set}:{started}",
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
    )
