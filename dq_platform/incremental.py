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

from .engine import (
    CHECKS,
    CROSS_RECORD_TYPES as ENGINE_CROSS_RECORD_TYPES,
    DependencyError,
    _attach_basis,
    _blocked_record_ids,
    build_conclusions,
    _cell,
    evaluate,
    rule_group_fields,
    topo_order,
    _unit_records,
)
from .models import RunResult, utc_now
from .values import is_bad

# Rule types whose verdicts span records; scoped by identity group.
CROSS_RECORD_TYPES = ENGINE_CROSS_RECORD_TYPES

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
    blocked_targets: tuple = ()


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
               prev_incomplete=(), prev_conclusions=()):
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
    prior_units = {}
    for conclusion in prev_conclusions or ():
        prior_units.setdefault(conclusion["rule_id"], []).append(conclusion)

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
        upstream_blocked = set()
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
            if dplan.blocked_targets:
                upstream_blocked.update(dplan.blocked_targets)
            elif dplan.scope == "skip":
                upstream_blocked.update(
                    rid for unit in prior_units.get(dep, ())
                    if unit.get("status") == "blocked"
                    for rid in unit.get("records", ())
                )
        if upstream_blocked:
            reasons.append(
                "dependency basis unavailable for records: "
                + ", ".join(sorted(upstream_blocked & set(new_by_id)))
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
                blocked = tuple(upstream_blocked & set(new_by_id))
                if blocked:
                    plans[rule.rule_id] = RulePlan(
                        rule.rule_id, "records", targets=blocked,
                        replaces=(), blocked_targets=blocked,
                        reasons=tuple(reasons),
                    )
                else:
                    plans[rule.rule_id] = RulePlan(
                        rule.rule_id, "skip",
                        reasons=("no identity-relevant change",),
                    )
                continue
            key_fields = rule_group_fields(rule)
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
                blocked_targets=tuple(
                    rid for rid in targets if rid in upstream_blocked
                ),
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
            rule.rule_id, "records", targets=targets, replaces=tuple(
                rid for rid in targets if rid not in upstream_blocked
            ),
            blocked_targets=tuple(
                rid for rid in targets if rid in upstream_blocked
            ),
            reasons=tuple(reasons),
        )
    return tuple(plans[rule.rule_id] for rule in ordered)


def plan_scopes(rules, old_batch, new_batch, prev_rules=None,
                prev_incomplete=(), prev_conclusions=()):
    """Map each rule id to "full", "records", "groups" or "skip"."""
    return {
        plan.rule_id: plan.scope
        for plan in build_plan(
            rules, old_batch, new_batch,
            prev_rules=prev_rules, prev_incomplete=prev_incomplete,
            prev_conclusions=prev_conclusions,
        )
    }


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


def _all_record_ids(batch):
    if batch is None:
        return ()
    return tuple(record.record_id for record in batch.records)


def _expand_by_rule_groups(rule, record_ids, batch):
    fields = rule_group_fields(rule)
    ids = set(record_ids)
    if not fields:
        return ids
    by_id = {record.record_id: record for record in batch.records}
    affected_keys = {
        tuple(_cell(by_id[rid], name) for name in fields)
        for rid in record_ids if rid in by_id
    }
    affected_keys.discard(None)
    for record in batch.records:
        key = tuple(_cell(record, name) for name in fields)
        if key in affected_keys:
            ids.add(record.record_id)
    return ids


def dependency_impact(rules, changed_rules=(), changed_records=(),
                      changed_fields=None, batch=None, old_batch=None,
                      prev_result=None):
    """Preview checks affected by rule and/or record-field modifications.

    ``changed_records`` maps a root record id to its changed fields.  A rule
    listed in ``changed_rules`` is treated as a rule-definition change and may
    affect every record. Reverse dependencies are traversed transitively;
    each downstream rule appears once, even if several paths or upstreams
    point at the same affected range.
    """
    ordered = topo_order(rules)
    by_id = {rule.rule_id: rule for rule in ordered}
    reverse = {rule.rule_id: [] for rule in ordered}
    for rule in ordered:
        for dep in rule.depends_on:
            reverse[dep].append(rule.rule_id)

    preview_batch = batch or old_batch
    all_ids = _all_record_ids(preview_batch)
    changed_records = dict(changed_records or {})
    changed_fields = changed_fields or {}
    if isinstance(changed_fields, (list, tuple, set)):
        shared_fields = set(changed_fields)
        changed_fields = {rid: shared_fields for rid in changed_records}

    affected = {}
    roots = []
    for rule_id in dict.fromkeys(changed_rules):
        rule = by_id[rule_id]
        target_ids = set(all_ids) if preview_batch is not None else {"*"}
        affected[rule_id] = {
            "rule_id": rule_id,
            "scope": "full",
            "target_ids": target_ids,
            "paths": [(rule_id,)],
            "reasons": ["rule definition is proposed to change"],
            "definition_changed": True,
        }
        roots.append(rule_id)

    for record_id, fields in changed_fields.items():
        for rule in ordered:
            relevant = _rule_fields(rule)
            if relevant is None or relevant & set(fields):
                entry = affected.setdefault(rule.rule_id, {
                    "rule_id": rule.rule_id,
                    "scope": "records",
                    "target_ids": set(),
                    "paths": [(rule.rule_id,)],
                    "reasons": [],
                    "definition_changed": False,
                })
                expanded = {record_id}
                if rule.rule_type in CROSS_RECORD_TYPES:
                    expanded = _expand_by_rule_groups(
                        rule, [record_id], preview_batch
                    )
                    if old_batch is not None and batch is not old_batch:
                        expanded |= _expand_by_rule_groups(
                            rule, [record_id], old_batch
                        )
                entry["target_ids"].update(expanded)
                entry["reasons"].append(
                    f"record {record_id} fields changed: "
                    + ",".join(sorted(fields))
                )
                roots.append(rule.rule_id)

    pending = list(dict.fromkeys(roots))
    while pending:
        parent = pending.pop(0)
        parent_entry = affected[parent]
        for child_id in reverse.get(parent, ()):
            child = by_id[child_id]
            if preview_batch is None or "*" in parent_entry["target_ids"]:
                target_ids = {"*"}
                scope = "full"
            elif parent_entry.get("definition_changed"):
                target_ids = (
                    set(all_ids) if preview_batch is not None else {"*"}
                )
                scope = "full"
            else:
                target_ids = _expand_by_rule_groups(
                    child, parent_entry["target_ids"], preview_batch
                )
                scope = "full" if target_ids == set(all_ids) else "records"
            new_paths = [
                path + (child_id,)
                for path in parent_entry["paths"]
            ]
            if child_id in affected:
                before = len(affected[child_id]["target_ids"])
                affected[child_id]["target_ids"].update(target_ids)
                is_full = (
                    scope == "full"
                    or "*" in target_ids
                    or affected[child_id]["scope"] == "full"
                )
                affected[child_id]["scope"] = "full" if is_full else "records"
                affected[child_id]["paths"].extend(new_paths)
                grew = len(affected[child_id]["target_ids"]) > before
            else:
                affected[child_id] = {
                    "rule_id": child_id,
                    "scope": scope,
                    "target_ids": target_ids,
                    "paths": new_paths,
                    "reasons": [f"affected through dependency {parent!r}"],
                    "definition_changed": False,
                }
                grew = True
            if grew:
                pending.append(child_id)

    previous_versions = {}
    blocked = {}
    missing = {}
    if prev_result is not None:
        for conclusion in prev_result.conclusions:
            rule_id = conclusion["rule_id"]
            previous_versions.setdefault(rule_id, []).append(conclusion)
            if conclusion.get("status") == "blocked":
                blocked.setdefault(rule_id, set()).update(
                    conclusion.get("records", ())
                )
            for dep in conclusion.get("missing_basis", ()):
                missing.setdefault(rule_id, set()).add(dep)

    result = []
    warnings = []
    for rule in ordered:
        if rule.rule_id not in affected:
            continue
        entry = affected[rule.rule_id]
        target_ids = entry["target_ids"]
        blocked_targets = sorted(target_ids & blocked.get(rule.rule_id, set()))
        if blocked_targets:
            warnings.append(
                f"rule {rule.rule_id!r} has stale/blocked records: "
                + ",".join(blocked_targets)
            )
        for dep in sorted(missing.get(rule.rule_id, ())):
            warnings.append(
                f"rule {rule.rule_id!r} lacks current basis from {dep!r}"
            )
        path_text = [" -> ".join(path) for path in dict.fromkeys(
            map(tuple, entry["paths"])
        )]
        result.append({
            "rule_id": rule.rule_id,
            "scope": entry["scope"],
            "affected_count": len(target_ids),
            "target_ids": tuple(sorted(target_ids)),
            "blocked_target_ids": tuple(blocked_targets),
            "paths": tuple(path_text),
            "reasons": tuple(dict.fromkeys(entry["reasons"])),
            "current_basis": trace_basis_versions(
                prev_result, rule.rule_id, target_ids
            ) if prev_result is not None else {},
        })
    return {"affected": tuple(result), "warnings": tuple(warnings)}


def trace_basis_versions(result, rule_id, record_ids=None):
    """Return the upstream versions actually used by one conclusion."""
    wanted = set(record_ids or ())
    trace = {}
    for conclusion in result.conclusions:
        if conclusion["rule_id"] != rule_id:
            continue
        records = conclusion.get("records", ())
        if wanted and not wanted.intersection(records):
            continue
        trace[conclusion["version"]] = {
            "unit": conclusion.get("unit"),
            "status": conclusion.get("status"),
            "records": tuple(records),
            "basis_versions": dict(conclusion.get("basis_versions", {})),
            "missing_basis": tuple(conclusion.get("missing_basis", ())),
        }
    return trace


preview_dependency_impact = dependency_impact


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
            prev_conclusions=prev_result.conclusions,
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

    # Carried conclusions: drop records that no longer exist and rules that
    # no longer exist; everything else starts from the previous verdict.
    current_rule_ids = {rule.rule_id for rule in rules}
    merged = [
        issue for issue in prev_result.issues
        if issue.record_id not in removed_ids
        and issue.rule_id in current_rule_ids
    ]
    carried_conclusions = [
        dict(conclusion,
             records=[rid for rid in conclusion.get("records", ())
                      if rid not in removed_ids])
        for conclusion in prev_result.conclusions
        if conclusion.get("rule_id") in current_rule_ids
        and any(rid not in removed_ids
                for rid in conclusion.get("records", ()))
    ]
    errors = []
    completed = []
    incomplete = []
    new_records = {r.record_id: r for r in new_batch.records}
    run_id = f"{new_batch.batch_id}:{version_set}:{started}"

    def current_conclusions():
        grouped = {}
        for conclusion in carried_conclusions:
            grouped.setdefault(conclusion["rule_id"], []).append(conclusion)
        return grouped

    def remove_conclusions(rule_id, record_ids):
        nonlocal carried_conclusions
        record_ids = set(record_ids)
        carried_conclusions = [
            conclusion for conclusion in carried_conclusions
            if not (conclusion["rule_id"] == rule_id
                    and record_ids.intersection(conclusion.get("records", ())))
        ]

    def block_conclusions(rule_id, record_ids, missing=()):
        record_ids = set(record_ids)
        for conclusion in carried_conclusions:
            if (conclusion["rule_id"] == rule_id
                    and record_ids.intersection(conclusion.get("records", ()))):
                conclusion["status"] = "blocked"
                conclusion["scope"] = "stale"
                conclusion.setdefault("missing_basis", [])
                for dep in missing:
                    if dep not in conclusion["missing_basis"]:
                        conclusion["missing_basis"].append(dep)

    def replace_blocked_conclusions(rule, record_ids, forced_ids=None):
        record_ids = set(record_ids)
        forced_ids = set(record_ids if forced_ids is None else forced_ids)
        if not record_ids:
            return
        remove_conclusions(rule.rule_id, record_ids)
        blocked_records = tuple(
            new_records[rid] for rid in record_ids if rid in new_records
        )
        carried_conclusions.extend(build_conclusions(
            run_id, rule, blocked_records, merged, current_conclusions(),
            "blocked", scope="blocked",
            forced_records=forced_ids, force_units=True,
        ))

    def dependent_blocked_ids(rule, target_ids):
        blocked = _blocked_record_ids(rule, current_conclusions())
        blocked.update(
            issue.record_id for issue in merged
            if issue.rule_id in set(rule.depends_on)
        )
        fields = rule_group_fields(rule)
        if not fields or not blocked:
            return blocked & set(target_ids)
        if blocked.intersection(target_ids):
            return set(target_ids)
        blocked_keys = {
            tuple(_cell(new_records[rid], name) for name in fields)
            for rid in blocked if rid in new_records
        }
        return {
            rid for rid in target_ids
            if tuple(_cell(new_records[rid], name) for name in fields)
            in blocked_keys
        }

    for plan in plans:
        rule = next(r for r in rules if r.rule_id == plan.rule_id)
        if plan.scope == "skip":
            if plan.blocked_targets:
                replace_blocked_conclusions(
                    rule, plan.blocked_targets,
                    set(plan.targets) or set(plan.blocked_targets)
                )
                incomplete.append(rule.rule_id)
                errors.append(
                    f"rule {rule.rule_id!r} cannot reconfirm records "
                    f"{','.join(plan.blocked_targets)}: dependency basis is "
                    "unavailable; carried conclusion is not this run's verdict"
                )
            else:
                completed.append(rule.rule_id)
            continue
        if plan.scope == "full":
            targets = [r.record_id for r in new_batch.records]
            replaced_ids = set(targets)
        else:
            targets = [rid for rid in plan.targets if rid in new_records]
            replaced_ids = (
                set(plan.replaces) | set(targets) | set(plan.blocked_targets)
            ) - removed_ids
        issue_replaced_ids = set(replaced_ids)
        blocked = dependent_blocked_ids(rule, targets)
        executed_ids = [rid for rid in targets if rid not in blocked]
        if blocked:
            record_ids = set(targets)
            blocked_records = tuple(
                new_records[rid] for rid in record_ids if rid in new_records
            )
            remove_conclusions(rule.rule_id, record_ids)
            carried_conclusions.extend(build_conclusions(
                run_id, rule, blocked_records, merged,
                current_conclusions(), "blocked", scope="blocked",
                forced_records=record_ids, force_units=True,
            ))
            issue_replaced_ids -= blocked
            errors.append(
                f"rule {rule.rule_id!r} cannot reconfirm records "
                f"{','.join(sorted(blocked))}: upstream basis is unavailable"
            )
        handler = CHECKS.get(rule.rule_type)
        try:
            if handler is None:
                raise ValueError(f"unknown rule type {rule.rule_type!r}")
            produced = handler(
                rule, tuple(new_records[rid] for rid in executed_ids)
            )
        except Exception as exc:
            # Keep the previous valid conclusion for this rule and say
            # plainly which scope this run failed to cover.
            replace_blocked_conclusions(rule, executed_ids)
            incomplete.append(rule.rule_id)
            if plan.scope == "full":
                covered = "all records"
            else:
                covered = "records: " + ",".join(sorted(executed_ids))
            errors.append(
                f"rule {rule.rule_id!r} failed during incremental run: {exc}; "
                f"kept previous conclusion; scope NOT completed this run: "
                f"{covered}"
            )
            continue
        merged = [
            issue for issue in merged
            if not (issue.rule_id == rule.rule_id
                    and issue.record_id in issue_replaced_ids)
        ]
        produced = _attach_basis(
            tuple(merged) + tuple(produced), rule,
            _unit_records(
                rule, tuple(new_records[rid] for rid in executed_ids)
            ),
            current_conclusions(),
        )[len(merged):]
        remove_conclusions(rule.rule_id, set(targets) - set(blocked))
        carried_conclusions.extend(build_conclusions(
            run_id, rule,
            tuple(new_records[rid] for rid in executed_ids),
            tuple(merged) + tuple(produced),
            current_conclusions(), "confirmed", scope=plan.scope,
        ))
        merged.extend(produced)
        if blocked:
            incomplete.append(rule.rule_id)
        else:
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
        completed_rules=tuple(dict.fromkeys(completed)),
        incomplete_rules=tuple(dict.fromkeys(incomplete)),
        conclusions=tuple(carried_conclusions),
        plan=tuple(
            {
                "rule_id": plan.rule_id,
                "scope": plan.scope,
                "targets": list(plan.targets),
                "blocked_targets": list(plan.blocked_targets),
                "reasons": list(plan.reasons),
            }
            for plan in plans
        ),
    )
