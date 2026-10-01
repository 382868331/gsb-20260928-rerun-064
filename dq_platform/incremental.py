"""Incremental re-checking: re-evaluate only what a change actually affects.

The scope follows the content of the change, never a relabelled full run:

* Row-level rules (required_fields, date_order) re-judge only records
  whose fields the rule reads were edited, plus added records.
* unique_fields only re-judges the identity groups actually touched:
  the old and new groups of records whose key fields changed, the groups
  added records join, and the previous groups of removed records.  This
  still catches cross-row conflicts: every potential collision partner of
  a moved identity is inside the affected group.
* An edit to unrelated fields (e.g. a date) changes no identity group,
  so the uniqueness check is skipped for that run.
* A changed rule definition (or a new/deleted rule) forces that rule
  back to a full re-check; dependency invalidation widens dependents only
  to the records/groups the dependency re-covered, not to a blind full.

Each run carries a before/after check plan (RulePlan per rule) stating
which record groups it covers and why.  If a rule fails mid-run its
previous valid conclusion is kept verbatim; the error names the exact
scope this run did not cover, and unrelated rules still finish.  On the
next successful run only that scope is merged again, so a resolved issue
is never counted twice and a stale failure is never treated as a pass.
"""

from .engine import (
    CHECKS,
    DependencyError,
    evaluate,
    identity_key,
    rule_key_fields,
    topo_order,
)
from .models import RulePlan, RunResult, utc_now


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


def diff_records(old_record, new_record):
    """Field names whose values differ between two record snapshots."""
    fields = set(old_record.data) | set(new_record.data)
    return tuple(sorted(
        name for name in fields
        if old_record.data.get(name) != new_record.data.get(name)
    ))


def _read_fields(rule):
    """Record fields a row-level rule can observe."""
    if rule.rule_type == "date_order":
        names = [rule.params.get("early_field"), rule.params.get("late_field")]
        names.extend(rule.params.get("optional_fields", ()))
        fields = frozenset(name for name in names if name)
        # Misconfigured rule: no readable fields can be named, so the
        # scope cannot be narrowed safely and it must run (and fail).
        if not rule.params.get("early_field") or not rule.params.get("late_field"):
            return None
        return fields
    return frozenset(rule.fields)


def _definition_changed(rule, prev_rules):
    prev = {r.rule_id: r for r in prev_rules}.get(rule.rule_id)
    if prev is None:
        return "rule_added"
    if prev.rule_type != rule.rule_type:
        return "rule_type_changed"
    if prev.fields != rule.fields or prev.params != rule.params:
        return "rule_fields_changed"
    if prev.depends_on != rule.depends_on:
        return "rule_dependencies_changed"
    return None


def _previous_plans(prev_result):
    plans = {}
    for doc in (prev_result.check_plan if prev_result else ()):
        plan = doc if isinstance(doc, RulePlan) else RulePlan(**doc)
        plans[plan.rule_id] = plan
    return plans


def build_plan(rules, old_batch, new_batch, prev_rules=None,
               prev_result=None):
    """Produce the per-rule check plan for an incremental run.

    Returns a tuple of RulePlan in topological order.  Each plan names the
    records/identity groups that must be re-judged and the change that
    forces the scope.
    """
    old_by_id = {r.record_id: r for r in old_batch.records}
    new_by_id = {r.record_id: r for r in new_batch.records}
    changed, added, removed = diff_batches(old_batch, new_batch)
    changed_fields = {
        rid: diff_records(old_by_id[rid], new_by_id[rid])
        for rid in changed
    }
    prev_by_id = {r.rule_id: r for r in (prev_rules or ())}
    prior_plans = _previous_plans(prev_result)
    prior_incomplete = set(
        prev_result.incomplete_rules if prev_result is not None else ()
    )

    ordered = topo_order(rules)
    plans = {}
    for rule in ordered:
        changed_rule_records = set(changed_fields)
        target_ids = set()
        groups = set()
        reason = None

        # 1) A dependency that re-checked records invalidates exactly
        # those records for the dependent rule (prerequisite verdicts may
        # have flipped).
        dep_targets = set()
        dep_groups = set()
        for dep_id in rule.depends_on:
            dep_plan = plans.get(dep_id)
            if dep_plan is None or dep_plan.scope == "skip":
                continue
            dep_targets.update(dep_plan.affected_record_ids)
            dep_groups.update(dep_plan.affected_groups)

        change = None
        if prev_rules is not None:
            prev_rule = prev_by_id.get(rule.rule_id)
            if prev_rule is None:
                change = "rule_added"
            else:
                change = _definition_changed(rule, (prev_rule,))

        if change:
            scope = "full"
            reason = change
            target_ids = set(new_by_id)
        elif rule.rule_type not in CHECKS:
            scope = "full"
            reason = "unknown_rule_type"
            target_ids = set(new_by_id)
        elif rule.rule_type == "unique_fields":
            key_fields = frozenset(rule_key_fields(rule))
            # Own effect of the data change on identity groups: old and
            # new group of each record whose key moved, groups joined by
            # added rows, and groups left by removed rows.
            for rid in changed:
                if not (set(changed_fields[rid]) & key_fields):
                    continue
                old_key = identity_key(rule, old_by_id[rid])
                new_key = identity_key(rule, new_by_id[rid])
                if old_key == new_key:
                    continue
                if old_key is not None:
                    groups.add(old_key)
                if new_key is not None:
                    groups.add(new_key)
                target_ids.add(rid)
            for rid in added:
                new_key = identity_key(rule, new_by_id[rid])
                if new_key is not None:
                    groups.add(new_key)
                target_ids.add(rid)
            for rid in removed:
                old_key = identity_key(rule, old_by_id[rid])
                if old_key is not None:
                    groups.add(old_key)
                target_ids.add(rid)
            # Cascading from a prerequisite: a row may now enter/leave the
            # uniqueness check, so expand to its whole current group.
            for rid in dep_targets:
                if rid in new_by_id:
                    key = identity_key(rule, new_by_id[rid])
                    if key is not None:
                        groups.add(key)
                    target_ids.add(rid)
            for key in dep_groups:
                groups.add(key)
            # Retry whatever the previous run did not finish, in addition
            # to the groups this change touches.
            own_change = bool(groups or target_ids)
            retry_full = (
                rule.rule_id in prior_incomplete
                and (prior_plans.get(rule.rule_id) is not None)
                and prior_plans[rule.rule_id].scope == "full"
            )
            if retry_full:
                scope = "full"
                reason = "retry_unfinished_full"
                target_ids = set(new_by_id)
                groups = set()
            else:
                if rule.rule_id in prior_incomplete:
                    prior = prior_plans.get(rule.rule_id)
                    if prior is not None:
                        for key in prior.affected_groups:
                            groups.add(tuple(key))
                        target_ids.update(prior.affected_record_ids)
                if groups or target_ids:
                    scope = "groups"
                    if rule.rule_id in prior_incomplete and not own_change:
                        reason = "retry_unfinished_scope"
                    else:
                        reason = ("identity_groups_changed"
                                  if groups else "identity_records_changed")
                    for key in groups:
                        for rid, record in new_by_id.items():
                            if identity_key(rule, record) == key:
                                target_ids.add(rid)
                else:
                    scope = "skip"
                    reason = "unrelated_change"
        else:
            observed = _read_fields(rule)
            if observed is None:
                scope = "full"
                reason = "rule_fields_unreadable"
                target_ids = set(new_by_id)
            else:
                for rid in changed:
                    if set(changed_fields[rid]) & observed:
                        target_ids.add(rid)
                target_ids.update(added)
                target_ids.update(dep_targets)
                target_ids = {rid for rid in target_ids
                              if rid in new_by_id}
                own_change = bool(target_ids - dep_targets)
                prior = prior_plans.get(rule.rule_id)
                if (rule.rule_id in prior_incomplete and prior is not None
                        and prior.scope == "full"):
                    scope = "full"
                    reason = "retry_unfinished_full"
                    target_ids = set(new_by_id)
                elif rule.rule_id in prior_incomplete and prior is not None:
                    target_ids.update(prior.affected_record_ids)
                    scope = "records"
                    reason = ("retry_unfinished_scope" if not own_change
                              else "observed_fields_changed")
                elif target_ids:
                    scope = "records"
                    reason = ("observed_fields_changed"
                              if target_ids - dep_targets
                              else "dependency_rechecked")
                else:
                    scope = "skip"
                    reason = "unrelated_change"

        plan = RulePlan(
            rule_id=rule.rule_id,
            rule_type=rule.rule_type,
            scope=scope,
            reason=reason,
            affected_record_ids=tuple(sorted(target_ids)),
            affected_groups=tuple(
                sorted(groups, key=lambda key: tuple(map(str, key)))
            ),
            changed_record_ids=tuple(sorted(changed_rule_records)),
            added_record_ids=tuple(added),
            removed_record_ids=tuple(removed),
        )
        plans[rule.rule_id] = plan
    return tuple(plans[rule.rule_id] for rule in ordered)


def plan_scopes(rules, changed, added, removed, prev_rules=None,
                old_batch=None, new_batch=None, prev_result=None):
    """Backward-compatible rule_id -> scope mapping for a diff."""
    if old_batch is None or new_batch is None:
        raise ValueError("old_batch and new_batch are required to plan scopes")
    plans = build_plan(rules, old_batch, new_batch,
                       prev_rules=prev_rules, prev_result=prev_result)
    return {plan.rule_id: plan.scope for plan in plans}


def compare_plans(previous, current):
    """Before/after comparison of two check plans.

    Returns a dict grouping rule ids into added, removed, definition
    changed, unchanged-but-rechecked (with the affected record groups),
    and skipped.  Each entry carries the current RulePlan so the affected
    records/groups for new, deleted, field-edited and rule-definition
    changes are explicit.
    """
    prev_by_id = {plan.rule_id: plan for plan in previous}
    cur_by_id = {plan.rule_id: plan for plan in current}
    report = {
        "rule_added": [],
        "rule_removed": [],
        "rule_changed": [],
        "data_rechecked": [],
        "skipped": [],
    }
    for plan in current:
        if plan.rule_id not in prev_by_id:
            report["rule_added"].append(plan)
        elif plan.reason in ("rule_type_changed", "rule_fields_changed",
                             "rule_dependencies_changed"):
            report["rule_changed"].append(plan)
        elif plan.scope == "skip":
            report["skipped"].append(plan)
        else:
            report["data_rechecked"].append(plan)
    for plan in previous:
        if plan.rule_id not in cur_by_id:
            report["rule_removed"].append(plan)
    return report


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
    try:
        plans = build_plan(rules, old_batch, new_batch,
                           prev_rules=prev_rules, prev_result=prev_result)
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

    plans_by_id = {plan.rule_id: plan for plan in plans}
    ordered = [rule for rule in topo_order(rules)]
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
        plan = plans_by_id[rule.rule_id]
        scope = plan.scope
        target_ids = set(plan.affected_record_ids)
        if scope == "skip":
            completed.append(rule.rule_id)
            continue
        kept = [i for i in prev_by_rule.get(rule.rule_id, [])
                if i.record_id not in removed_ids]
        # Full re-check replaces every old verdict of this rule; group/
        # record scopes replace only the re-judged records' verdicts.
        if scope == "full":
            replace_ids = {i.record_id for i in kept}
        else:
            replace_ids = target_ids
        targets = [new_records[rid] for rid in sorted(target_ids)
                   if rid in new_records]
        replaced = [i for i in kept if i.record_id in replace_ids]
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
            # Keep the previous valid conclusion verbatim for this rule
            # and name exactly which scope this run failed to cover.
            merged.extend(replaced)
            incomplete.append(rule.rule_id)
            if scope == "full":
                covered = "all records/groups"
            elif scope == "groups":
                covered = (
                    "identity groups "
                    + ";".join(
                        "|".join(map(str, group))
                        for group in plan.affected_groups
                    )
                    + " (records: "
                    + ",".join(sorted(target_ids)) + ")"
                )
            else:
                covered = "records: " + ",".join(sorted(target_ids))
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
        check_plan=plans,
        previous_check_plan=prev_result.check_plan,
        completed_rules=tuple(completed),
        incomplete_rules=tuple(incomplete),
    )
