"""Evaluation engine and versioned, dependency-aware conclusions."""

from dataclasses import replace
from datetime import date, datetime
import hashlib
import json

from .models import Issue, RunResult, utc_now
from .values import MISSING, MALFORMED


CROSS_RECORD_TYPES = {"unique_fields", "grouped_sum"}


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


def _number_value(rule, record, field_name):
    value = _cell(record, field_name)
    if value is MISSING:
        return value, None
    if isinstance(value, bool):
        return MALFORMED, _issue(
            rule, record, field_name, "malformed_value",
            f"{field_name}={value!r} is not numeric",
        )
    if isinstance(value, (int, float)):
        return value, None
    try:
        return float(value), None
    except (TypeError, ValueError):
        return MALFORMED, _issue(
            rule, record, field_name, "malformed_value",
            f"{field_name}={value!r} is not numeric",
        )


def _check_grouped_sum(rule, records):
    params = rule.params or {}
    group_fields = params.get("group_fields")
    amount_field = params.get("amount_field")
    if not group_fields or not amount_field:
        raise ValueError(
            f"rule {rule.rule_id!r} needs group_fields and amount_field"
        )
    groups = {}
    issues = []
    for record in records:
        key_values = [_cell(record, name) for name in group_fields]
        if any(value is MISSING or value is MALFORMED
               for value in key_values):
            for name, value in zip(group_fields, key_values):
                if value is MISSING:
                    issues.append(_issue(
                        rule, record, name, "missing_value",
                        f"group field {name!r} is missing",
                    ))
                elif value is MALFORMED:
                    issues.append(_issue(
                        rule, record, name, "malformed_value",
                        f"group field {name!r} is malformed",
                    ))
            continue
        amount, amount_error = _number_value(rule, record, amount_field)
        if amount_error:
            issues.append(amount_error)
            continue
        groups.setdefault(tuple(key_values), []).append((record, amount))

    min_total = params.get("min_total")
    max_total = params.get("max_total")
    for key, members in groups.items():
        total = sum(amount for _, amount in members)
        bound = None
        if min_total is not None and total < min_total:
            bound = f"minimum {min_total}"
        if max_total is not None and total > max_total:
            bound = f"maximum {max_total}"
        if bound is not None:
            ids = ", ".join(record.record_id for record, _ in members)
            for record, _ in members:
                issues.append(_issue(
                    rule, record, amount_field, "violated",
                    f"group {dict(zip(group_fields, key))} total {total} "
                    f"violates {bound}; records: {ids}",
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
    "grouped_sum": _check_grouped_sum,
}


def rule_group_fields(rule):
    params = rule.params or {}
    if rule.rule_type == "unique_fields":
        return tuple(params.get("key_fields") or rule.fields)
    if rule.rule_type == "grouped_sum":
        return tuple(params.get("group_fields") or ())
    return ()


def _stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def group_key(record, fields):
    values = tuple(_cell(record, name) for name in fields)
    if any(value is MISSING or value is MALFORMED for value in values):
        return None
    return values


def _unit_id(rule, records):
    fields = rule_group_fields(rule)
    if not fields:
        return records[0].record_id if len(records) == 1 else None
    key = group_key(records[0], fields)
    if key is None or any(group_key(record, fields) != key
                          for record in records[1:]):
        return None
    return hashlib.sha256(_stable((fields, key)).encode("utf-8")).hexdigest()[:12]


def _conclusion_version(run_id, rule, unit, status, issue_data, basis):
    payload = _stable((
        run_id, rule.rule_id, rule.condition, unit, status,
        issue_data, basis,
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _issue_data(issues):
    return sorted(
        (i.record_id, i.field, i.kind, i.detail) for i in issues
    )


def _unit_records(rule, records):
    fields = rule_group_fields(rule)
    if not fields:
        return [(record.record_id, (record,)) for record in records]
    groups = {}
    for record in records:
        key = group_key(record, fields)
        groups.setdefault(key, []).append(record)
    return [
        (_unit_id(rule, members), tuple(members))
        for members in groups.values()
    ]


def _blocked_record_ids(rule, conclusions):
    blocked = set()
    for dep in rule.depends_on:
        for conclusion in conclusions.get(dep, ()):
            if conclusion.get("status") == "blocked":
                blocked.update(conclusion.get("records", ()))
    return blocked


def _failed_upstream_record_ids(rule, issues, conclusions):
    failed = _blocked_record_ids(rule, conclusions)
    dep_rules = set(rule.depends_on)
    failed.update(
        issue.record_id for issue in issues
        if issue.rule_id in dep_rules
    )
    return failed


def _basis_versions(rule, unit_records, conclusions):
    record_set = {record.record_id for record in unit_records}
    basis = {}
    for dep in rule.depends_on:
        versions = set()
        for conclusion in conclusions.get(dep, ()):
            if record_set & set(conclusion.get("records", ())):
                version = conclusion.get("version")
                if version:
                    versions.add(version)
        if versions:
            basis[dep] = sorted(versions)
        else:
            basis[dep] = None
    return basis


def build_conclusions(run_id, rule, records, issues, conclusions, status,
                      scope="full", forced_records=(), force_units=False):
    result = []
    issues_by_record = {}
    for issue in issues:
        if issue.rule_id == rule.rule_id:
            issues_by_record.setdefault(issue.record_id, []).append(issue)
    blocked_records = _blocked_record_ids(rule, conclusions)
    forced_records = set(forced_records)
    for unit, members in _unit_records(rule, tuple(records)):
        member_ids = [record.record_id for record in members]
        unit_status = "blocked" if (
            force_units
            or status == "blocked"
            or any(rid in blocked_records or rid in forced_records
                   for rid in member_ids)
        ) else status
        unit_issues = [
            issue for rid in member_ids
            for issue in issues_by_record.get(rid, [])
        ]
        basis = _basis_versions(rule, members, conclusions)
        missing_basis = [dep for dep, version in basis.items()
                         if version is None]
        if missing_basis:
            unit_status = "blocked"
        version = _conclusion_version(
            run_id, rule, unit, unit_status,
            _issue_data(unit_issues), basis,
        )
        result.append({
            "rule_id": rule.rule_id,
            "scope": scope,
            "unit": unit,
            "status": unit_status,
            "records": member_ids,
            "issues": len(unit_issues),
            "basis_versions": basis,
            "missing_basis": missing_basis,
            "version": version,
        })
    return tuple(result)


def _attach_basis(issues, rule, unit_records, conclusions):
    by_record = {}
    for _, members in unit_records:
        basis = _basis_versions(rule, members, conclusions)
        for record in members:
            by_record[record.record_id] = basis
    return tuple(
        replace(issue, basis_versions=by_record.get(issue.record_id, {}))
        if issue.rule_id == rule.rule_id else issue
        for issue in issues
    )


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
            incomplete_rules=tuple(rule.rule_id for rule in rules),
        )

    completed = []
    incomplete = []
    all_conclusions = {}
    for rule in ordered:
        handler = CHECKS.get(rule.rule_type)
        if handler is None:
            errors.append(f"unknown rule type {rule.rule_type!r} ({rule.rule_id})")
            incomplete.append(rule.rule_id)
            all_conclusions[rule.rule_id] = build_conclusions(
                f"{batch.batch_id}:{version_set}:{started}",
                rule, batch.records, issues, all_conclusions, "blocked",
            )
            continue
        blocked = _failed_upstream_record_ids(rule, issues, all_conclusions)
        relevant = tuple(
            record for record in batch.records
            if record.record_id not in blocked
        )
        try:
            produced = handler(rule, relevant)
        except Exception as exc:  # execution failure -> never "all passed"
            errors.append(f"rule {rule.rule_id!r} failed to execute: {exc}")
            incomplete.append(rule.rule_id)
            all_conclusions[rule.rule_id] = build_conclusions(
                f"{batch.batch_id}:{version_set}:{started}",
                rule, batch.records, issues, all_conclusions, "blocked",
            )
            continue
        produced = _attach_basis(
            (*issues, *produced), rule, _unit_records(rule, relevant),
            all_conclusions,
        )[len(issues):]
        issues.extend(produced)
        completed.append(rule.rule_id)
        all_conclusions[rule.rule_id] = build_conclusions(
            f"{batch.batch_id}:{version_set}:{started}",
            rule, batch.records, issues, all_conclusions, "confirmed",
        )

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
        completed_rules=tuple(completed),
        incomplete_rules=tuple(incomplete),
        conclusions=tuple(
            conclusion
            for rule in ordered
            for conclusion in all_conclusions.get(rule.rule_id, ())
        ),
    )
