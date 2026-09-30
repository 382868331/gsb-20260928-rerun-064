"""Evaluation engine: dependency ordering plus cross-field/cross-record rules.

Besides full evaluation, the engine supports incremental re-checks: only
records touched since the previous valid run, plus the cross-record partners
of those records, are re-judged. When a rule fails midway, the previous valid
conclusion for its scope is retained and the unfinished scope is reported.
"""

from datetime import date, datetime
import re

from .models import Issue, RunResult, rule_signature, utc_now
from .values import MISSING, MALFORMED


class DependencyError(Exception):
    pass


def topo_order(rules):
    """Return rules in dependency order.

    Raises DependencyError for a missing dependency or a dependency cycle.
    """
    by_id = {rule.rule_id: rule for rule in rules}
    errors = []
    for rule in rules:
        for dep in rule.depends_on:
            if dep not in by_id:
                errors.append(
                    f"rule {rule.rule_id!r} depends on missing rule {dep!r}"
                )
    if errors:
        raise DependencyError("; ".join(errors))

    state = {rule_id: 0 for rule_id in by_id}  # 0 unseen, 1 visiting, 2 done
    ordered = []

    def visit(rule_id, stack):
        if state[rule_id] == 1:
            cycle = stack[stack.index(rule_id):] + [rule_id]
            raise DependencyError(
                "dependency cycle: " + " -> ".join(cycle)
            )
        if state[rule_id] == 2:
            return
        state[rule_id] = 1
        for dep in by_id[rule_id].depends_on:
            visit(dep, stack + [rule_id])
        state[rule_id] = 2
        ordered.append(by_id[rule_id])

    for rule_id in by_id:
        visit(rule_id, [])
    return ordered


def _cell(record, field_name):
    if field_name not in record.data:
        return MISSING
    value = record.data[field_name]
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return MISSING
    return value


def _parse_date(rule, record, field_name):
    value = _cell(record, field_name)
    if value is MISSING:
        return value, None
    if isinstance(value, datetime):
        return value.date(), None
    if isinstance(value, date):
        return value, None
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).date(), None
            except ValueError:
                pass
        return MALFORMED, _issue(
            rule, record, field_name, "malformed_value",
            f"{field_name}={text!r} is not a YYYY-MM-DD date",
        )
    return MALFORMED, _issue(
        rule, record, field_name, "malformed_value",
        f"{field_name}={value!r} is not a date",
    )


def _issue(rule, record, field, kind, detail):
    return Issue(
        rule_id=rule.rule_id,
        rule_version_set=rule.version_set,
        condition=rule.condition,
        record_id=record.record_id,
        record_index=record.index,
        field=field,
        kind=kind,
        detail=detail,
    )


def _check_required_fields(rule, records):
    issues = []
    for record in records:
        for field_name in rule.fields:
            if _cell(record, field_name) is MISSING:
                issues.append(_issue(
                    rule, record, field_name, "missing_value",
                    f"required field {field_name!r} is missing",
                ))
    return issues


def _check_unique_fields(rule, records):
    key_fields = rule.params.get("key_fields") or list(rule.fields)
    if not key_fields:
        raise ValueError(f"rule {rule.rule_id!r} needs key_fields")
    groups = {}
    issues = []
    for record in records:
        values = [_cell(record, name) for name in key_fields]
        # Missing identity pieces are reported, not used as a join key:
        # None/blank must not collapse rows into one "zero-like" bucket.
        if any(value is MISSING or value is MALFORMED for value in values):
            for name, value in zip(key_fields, values):
                if value is MISSING:
                    issues.append(_issue(
                        rule, record, name, "missing_value",
                        f"identity field {name!r} is missing",
                    ))
                elif value is MALFORMED:
                    issues.append(_issue(
                        rule, record, name, "malformed_value",
                        f"identity field {name!r} is malformed",
                    ))
            continue
        key = tuple(values)
        groups.setdefault(key, []).append(record)
    for key, members in groups.items():
        if len(members) > 1:
            ids = ", ".join(member.record_id for member in members)
            for record in members:
                issues.append(_issue(
                    rule, record, ",".join(key_fields), "violated",
                    f"duplicate identity {dict(zip(key_fields, key))}; "
                    f"also on records: {ids}",
                ))
    return issues


def _unique_groups(rule, records):
    """Return {key: [records]} for records with complete identity keys."""
    key_fields = rule.params.get("key_fields") or list(rule.fields)
    groups = {}
    for record in records:
        values = [_cell(record, name) for name in key_fields]
        if any(value is MISSING or value is MALFORMED for value in values):
            continue
        groups.setdefault(tuple(values), []).append(record)
    return groups


def _baseline_duplicate_partners(baseline, rule_id, affected_ids):
    """Record IDs that shared a duplicate group with a touched record.

    A touched record may be edited away from a group (or deleted), freeing
    its former partners even though the partners themselves did not change.
    Baseline issue details enumerate every member of the duplicate group.
    """
    partners = set()
    pattern = re.compile(r"also on records: ([A-Za-z0-9_,\- ]+)")
    for issue in baseline.issues:
        if issue.rule_id != rule_id or issue.kind != "violated":
            continue
        if issue.record_id not in affected_ids:
            continue
        match = pattern.search(issue.detail)
        if match:
            partners.update(
                part.strip() for part in match.group(1).split(",")
            )
    return partners


def _rule_scope(rule, records, affected_ids, baseline, old_records=()):
    """Records whose conclusion for ``rule`` may change in an incremental run."""
    if rule.rule_type == "unique_fields":
        scope = set(affected_ids)
        by_id = {record.record_id: record for record in records}
        for group in _unique_groups(rule, records).values():
            if any(record.record_id in affected_ids for record in group):
                scope.update(record.record_id for record in group)
        # A touched record may have edited its key out of an old group; its
        # former partners in the baseline snapshot are also in scope.
        if old_records:
            for group in _unique_groups(rule, old_records).values():
                if any(record.record_id in affected_ids for record in group):
                    scope.update(record.record_id for record in group)
        if baseline is not None:
            scope.update(
                _baseline_duplicate_partners(baseline, rule.rule_id, affected_ids)
            )
        return {
            record_id: by_id[record_id]
            for record_id in scope
            if record_id in by_id
        }
    # required_fields / date_order are per-record judgements.
    return {
        record.record_id: record
        for record in records
        if record.record_id in affected_ids
    }


def _check_unique_fields_scoped(rule, records, scope_ids, emit_ids=None):
    """Re-judge uniqueness over ``records``; emit issues only for ``emit_ids``.

    Grouping always uses the complete record set (a cross-record constraint),
    while prerequisite-failed records are excluded from issue output only.
    """
    emit_ids = scope_ids if emit_ids is None else emit_ids
    key_fields = rule.params.get("key_fields") or list(rule.fields)
    groups = _unique_groups(rule, records)
    issues = []
    for record in records:
        if record.record_id not in emit_ids:
            continue
        values = [_cell(record, name) for name in key_fields]
        bad = [(name, value) for name, value in zip(key_fields, values)
               if value is MISSING or value is MALFORMED]
        if bad:
            for name, value in bad:
                kind = "missing_value" if value is MISSING else "malformed_value"
                issues.append(_issue(
                    rule, record, name, kind,
                    f"identity field {name!r} is {kind.split('_')[0]}",
                ))
            continue
    for key, members in groups.items():
        if len(members) <= 1:
            continue
        if all(record.record_id not in scope_ids for record in members):
            continue
        ids = ", ".join(member.record_id for member in members)
        for record in members:
            if record.record_id not in emit_ids:
                continue
            issues.append(_issue(
                rule, record, ",".join(key_fields), "violated",
                f"duplicate identity {dict(zip(key_fields, key))}; "
                f"also on records: {ids}",
            ))
    return issues


def _check_date_order(rule, records):
    early = rule.params.get("early_field")
    late = rule.params.get("late_field")
    optional = set(rule.params.get("optional_fields", []))
    if not early or not late:
        raise ValueError(
            f"rule {rule.rule_id!r} needs early_field and late_field"
        )
    issues = []
    for record in records:
        early_val, early_err = _parse_date(rule, record, early)
        late_val, late_err = _parse_date(rule, record, late)
        if early_err:
            issues.append(early_err)
        if late_err:
            issues.append(late_err)
        # Missing/malformed values stop here: they are never compared as zeros.
        if early_val is MISSING or late_val is MISSING:
            for name, value in ((early, early_val), (late, late_val)):
                if value is MISSING and name not in optional:
                    issues.append(_issue(
                        rule, record, name, "missing_value",
                        f"date field {name!r} is missing",
                    ))
            continue
        if early_val is MALFORMED or late_val is MALFORMED:
            continue
        if late_val < early_val:
            issues.append(_issue(
                rule, record, f"{early},{late}", "violated",
                f"contradictory dates: {late}={late_val.isoformat()} "
                f"is earlier than {early}={early_val.isoformat()}",
            ))
    return issues


CHECKS = {
    "required_fields": _check_required_fields,
    "unique_fields": _check_unique_fields,
    "date_order": _check_date_order,
}


def evaluate(batch, rules):
    started = utc_now()
    issues = []
    errors = []
    version_set = rules[0].version_set if rules else "empty"
    try:
        ordered = topo_order(rules)
    except DependencyError as exc:
        finished = utc_now()
        return RunResult(
            run_id=f"{batch.batch_id}:{version_set}:{started}",
            batch_id=batch.batch_id,
            batch_checksum=batch.checksum,
            rule_version_set=version_set,
            started_at=started,
            finished_at=finished,
            status="FAILED",
            issues=(),
            errors=(str(exc),),
        )

    failed_records = set()
    for rule in ordered:
        # A rule is skipped for records already failed by a prerequisite.
        relevant = tuple(
            r for r in batch.records
            if not any(
                i.record_id == r.record_id and i.rule_id in rule.depends_on
                for i in issues
            )
        )
        handler = CHECKS.get(rule.rule_type)
        if handler is None:
            errors.append(f"unknown rule type {rule.rule_type!r} ({rule.rule_id})")
            continue
        try:
            produced = handler(rule, relevant)
        except Exception as exc:  # execution failure -> never "all passed"
            errors.append(f"rule {rule.rule_id!r} failed to execute: {exc}")
            continue
        issues.extend(produced)
        failed_records.update(i.record_id for i in produced)

    finished = utc_now()
    status = "FAILED" if issues or errors else "PASSED"
    return RunResult(
        run_id=f"{batch.batch_id}:{version_set}:{started}",
        batch_id=batch.batch_id,
        batch_checksum=batch.checksum,
        rule_version_set=version_set,
        started_at=started,
        finished_at=finished,
        status=status,
        issues=tuple(issues),
        errors=tuple(errors),
        rule_signatures={rule.rule_id: rule_signature(rule) for rule in ordered},
    )


def diff_batches(baseline_batch, batch):
    """Return (changed_or_added_ids, deleted_ids) between two snapshots."""
    old = {record.record_id: record for record in baseline_batch.records}
    new = {record.record_id: record for record in batch.records}
    changed = set()
    for record_id, record in new.items():
        if record_id not in old or old[record_id].data != record.data:
            changed.add(record_id)
    deleted = set(old) - set(new)
    return changed, deleted


def evaluate_incremental(batch, rules, baseline, baseline_batch):
    """Re-judge only what may have changed since ``baseline``.

    The baseline is the previous *valid* RunResult. Issues of rules that do
    not finish in this run are carried over unchanged, and the unfinished
    scope is reported in ``incomplete_scope``.
    """
    started = utc_now()
    version_set = rules[0].version_set if rules else "empty"
    try:
        ordered = topo_order(rules)
    except DependencyError as exc:
        finished = utc_now()
        return RunResult(
            run_id=f"{batch.batch_id}:{version_set}:{started}",
            batch_id=batch.batch_id,
            batch_checksum=batch.checksum,
            rule_version_set=version_set,
            started_at=started,
            finished_at=finished,
            status="FAILED",
            issues=baseline.issues,
            errors=(f"incremental run aborted, previous conclusion retained: {exc}",),
            mode="incremental",
            incomplete_scope=tuple(
                {"rule_id": rule_id,
                 "reason": "dependency graph invalid",
                 "record_ids": [record.record_id for record in batch.records]}
                for rule_id in previous_rule_ids
            ),
            rule_signatures={},
            reused_baseline_run_id=baseline.run_id,
        )

    changed_ids, deleted_ids = diff_batches(baseline_batch, batch)
    affected_ids = changed_ids | deleted_ids

    signatures = {rule.rule_id: rule_signature(rule) for rule in ordered}
    baseline_sigs = dict(baseline.rule_signatures or {})

    # A rule must be fully re-judged when its condition changed or it is new.
    full_rules = set()
    for rule in ordered:
        sig = signatures[rule.rule_id]
        if rule.rule_id not in baseline_sigs:
            full_rules.add(rule.rule_id)
        elif baseline_sigs[rule.rule_id] != sig:
            full_rules.add(rule.rule_id)

    all_record_ids = {record.record_id for record in batch.records}

    # Per-rule re-judgement scope. Dependents inherit their prerequisites'
    # scopes because a changed prerequisite verdict can change gating.
    scopes = {}
    for rule in ordered:
        if rule.rule_id in full_rules:
            scope = set(all_record_ids)
        else:
            seeds = set(affected_ids)
            for dep in rule.depends_on:
                seeds.update(scopes.get(dep, set()))
            if seeds:
                scope = set(_rule_scope(
                    rule, batch.records, seeds, baseline,
                    baseline_batch.records,
                ))
                scope.update(deleted_ids)
            else:
                scope = set()
        scopes[rule.rule_id] = scope

    # Effective per-rule issues: baseline until a rule is successfully re-judged.
    effective = {}
    by_rule_issues = {}
    for issue in baseline.issues:
        by_rule_issues.setdefault(issue.rule_id, []).append(issue)

    incomplete = []
    new_errors = []

    for rule in ordered:
        scope_ids = scopes[rule.rule_id]
        if not scope_ids and rule.rule_id in baseline_sigs:
            effective[rule.rule_id] = list(
                by_rule_issues.get(rule.rule_id, [])
            )
            continue

        # Prerequisite gating mirrors the full engine: records that currently
        # fail a prerequisite are not judged. ``effective`` already holds the
        # prerequisite's final verdict (fresh issues replace stale ones).
        prereq_issue_records = set()
        for dep in rule.depends_on:
            for issue in effective.get(dep, []):
                prereq_issue_records.add(issue.record_id)
        scoped_records = tuple(
            record for record in batch.records
            if record.record_id in scope_ids
            and record.record_id not in prereq_issue_records
        )
        handler = CHECKS.get(rule.rule_type)
        if handler is None:
            new_errors.append(
                f"unknown rule type {rule.rule_type!r} ({rule.rule_id})"
            )
            incomplete.append({
                "rule_id": rule.rule_id,
                "reason": "unknown rule type",
                "record_ids": sorted(scope_ids),
            })
            effective[rule.rule_id] = list(
                by_rule_issues.get(rule.rule_id, [])
            )
            continue
        try:
            if rule.rule_type == "unique_fields":
                produced = _check_unique_fields_scoped(
                    rule, batch.records, scope_ids,
                    scope_ids - prereq_issue_records,
                )
            else:
                produced = handler(rule, scoped_records)
        except Exception as exc:
            # Execution failure: keep the previous valid conclusion for this
            # rule and report exactly what was not completed.
            new_errors.append(
                f"rule {rule.rule_id!r} failed to execute: {exc}"
            )
            incomplete.append({
                "rule_id": rule.rule_id,
                "reason": f"execution error: {exc}",
                "record_ids": sorted(scope_ids),
            })
            effective[rule.rule_id] = list(
                by_rule_issues.get(rule.rule_id, [])
            )
            continue

        retained = [
            issue for issue in by_rule_issues.get(rule.rule_id, [])
            if issue.record_id not in scope_ids
        ]
        merged = retained + produced
        effective[rule.rule_id] = merged

    # Dependents of failed/incomplete rules cannot be trusted either.
    failed_rule_ids = {item["rule_id"] for item in incomplete}
    for rule in ordered:
        if rule.rule_id in failed_rule_ids:
            continue
        if any(dep in failed_rule_ids for dep in rule.depends_on):
            failed_rule_ids.add(rule.rule_id)
            if not any(item["rule_id"] == rule.rule_id for item in incomplete):
                incomplete.append({
                    "rule_id": rule.rule_id,
                    "reason": "prerequisite rule did not complete",
                    "record_ids": sorted(
                        issue.record_id
                        for issue in effective.get(rule.rule_id, [])
                    ) or [record.record_id for record in batch.records],
                })

    final_issues = []
    for rule in ordered:
        final_issues.extend(effective.get(rule.rule_id, []))
    # De-duplicate while preserving order.
    seen = set()
    deduped = []
    for issue in final_issues:
        if issue.issue_key in seen:
            continue
        seen.add(issue.issue_key)
        deduped.append(issue)

    finished = utc_now()
    status = "FAILED" if new_errors else (
        "FAILED" if deduped else "PASSED"
    )
    return RunResult(
        run_id=f"{batch.batch_id}:{version_set}:{started}",
        batch_id=batch.batch_id,
        batch_checksum=batch.checksum,
        rule_version_set=version_set,
        started_at=started,
        finished_at=finished,
        status=status,
        issues=tuple(deduped),
        errors=tuple(new_errors),
        mode="incremental",
        affected_records=tuple(sorted(changed_ids)),
        deleted_record_ids=tuple(sorted(deleted_ids)),
        incomplete_scope=tuple(incomplete),
        rule_signatures={
            rule_id: sig for rule_id, sig in signatures.items()
            if rule_id not in failed_rule_ids
        },
        reused_baseline_run_id=baseline.run_id,
    )
