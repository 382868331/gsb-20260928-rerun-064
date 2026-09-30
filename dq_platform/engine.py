"""Evaluation engine: dependency ordering plus cross-field/cross-record rules."""

from datetime import date, datetime

from .models import Issue, RunResult, utc_now
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
            incomplete_rules=tuple(rule.rule_id for rule in rules),
        )

    completed = []
    incomplete = []
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
            incomplete.append(rule.rule_id)
            continue
        try:
            produced = handler(rule, relevant)
        except Exception as exc:  # execution failure -> never "all passed"
            errors.append(f"rule {rule.rule_id!r} failed to execute: {exc}")
            incomplete.append(rule.rule_id)
            continue
        issues.extend(produced)
        completed.append(rule.rule_id)

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
    )
