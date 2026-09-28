import pytest

from dq_platform import (
    CrossFieldRule,
    DateContradictionRule,
    DuplicateIdentityRule,
    QualityEngine,
    RuleDependencyError,
    load_sample,
)
from dq_platform.parser import parse_cell
from dq_platform.rules import Rule, topological_order
from dq_platform.store import Store
from dq_platform.types import CellStatus, RunStatus


def _run(tmp_path, rules, sample_name="employees.json"):
    records, _schema, raw = load_sample(f"samples/{sample_name}")
    return QualityEngine(rules).run(
        records, batch_id="B-001", rule_version_id="rv1", raw_records=raw
    )


def test_missing_and_malformed_are_not_zero():
    assert parse_cell(None, "number").status is CellStatus.MISSING
    assert parse_cell("", "number").status is CellStatus.MISSING
    bad = parse_cell("abc", "number")
    assert bad.status is CellStatus.MALFORMED
    assert bad.value is None
    bad_date = parse_cell("2020-13-40", "date")
    assert bad_date.status is CellStatus.MALFORMED


def test_sample_detects_duplicate_dates_and_bad_values(tmp_path):
    rules = [
        CrossFieldRule(
            "R100", "v1", ("age",), lambda age: age > 0, "age > 0"
        ),
        DateContradictionRule(
            "R200", "v1", "hire_date", "leave_date"
        ),
        DuplicateIdentityRule("R300", "v1", ("emp_id",)),
    ]
    report = _run(tmp_path, rules)
    assert report.status is RunStatus.FAILED

    messages = [i.message for i in report.issues]
    assert any("日期矛盾" in m for m in messages)
    dupes = [m for m in messages if "重复身份" in m]
    assert len(dupes) == 2
    assert any("格式错误" in m for m in messages)
    assert any("缺失" in m for m in messages)


def test_issue_points_to_record_and_condition():
    report = _run(
        None,
        [DateContradictionRule("R200", "v1", "hire_date", "leave_date")],
    )
    issue = next(i for i in report.issues if i.record_id == "E002")
    assert issue.condition == "hire_date <= leave_date"
    assert issue.rule_version == "v1"
    assert issue.related_record_ids == ()


def test_duplicate_issues_link_related_records():
    report = _run(None, [DuplicateIdentityRule("R300", "v1", ("emp_id",))])
    e002_issues = [i for i in report.issues if i.record_id == "E002"]
    assert e002_issues
    linked = [
        i for i in e002_issues if i.related_record_ids == ("E002",)
    ]
    assert linked  # 两条重复记录互相指回


def test_missing_dependency_raises_batch_error():
    rule = CrossFieldRule(
        "R2", "v1", ("age",), lambda age: age > 0, "age>0",
        depends_on=("R1-missing",),
    )
    with pytest.raises(RuleDependencyError):
        topological_order([rule])
    report = _run(None, [rule])
    assert report.status is RunStatus.ERROR
    assert "不存在" in report.results[0].error
    assert "全部合格" not in report.summary()


def test_dependency_cycle_is_error():
    a = CrossFieldRule("A", "v1", ("age",), lambda age: True, "x",
                       depends_on=("B",))
    b = CrossFieldRule("B", "v1", ("age",), lambda age: True, "x",
                       depends_on=("C",))
    c = CrossFieldRule("C", "v1", ("age",), lambda age: True, "x",
                       depends_on=("A",))
    with pytest.raises(RuleDependencyError, match="成环"):
        topological_order([a, b, c])
    report = _run(None, [a, b, c])
    assert report.status is RunStatus.ERROR


def test_rule_exception_never_reports_all_pass():
    class BoomRule(Rule):
        def check_row(self, record_id, cells):
            raise RuntimeError("boom")

    boom = BoomRule("BOOM", "v1", "never")
    report = _run(None, [boom])
    assert report.status is RunStatus.ERROR
    result = report.results[0]
    assert result.error and "boom" in result.error
    assert result.passed is False
    assert "执行失败" in report.summary()


def test_clean_sample_passes(tmp_path):
    sample = tmp_path / "clean.json"
    sample.write_text(
        '''{
          "id_field": "emp_id",
          "schema": {"emp_id": "string", "age": "number"},
          "records": [
            {"emp_id": "A1", "age": 30},
            {"emp_id": "A2", "age": 31}
          ]
        }''',
        encoding="utf-8",
    )
    records, _schema, raw = load_sample(sample)
    rule = CrossFieldRule("R", "v1", ("age",), lambda age: age > 0, "age>0")
    report = QualityEngine([rule]).run(
        records, "B-clean", "rv1", raw_records=raw
    )
    assert report.status is RunStatus.PASSED
    assert "全部合格" in report.summary()


def test_old_batch_conclusion_cannot_be_overwritten(tmp_path):
    store = Store(tmp_path / "reports")
    records, _schema, raw = load_sample("samples/employees.json")
    rule = CrossFieldRule("R", "v1", ("age",), lambda age: age > 0, "age>0")
    engine = QualityEngine([rule])

    first = engine.run(records, "B-IMMUTABLE", "rv1", raw_records=raw)
    store.save(first)

    revised_rule = CrossFieldRule(
        "R", "v2", ("age",), lambda age: age >= 0, "age>=0"
    )
    revised = QualityEngine([revised_rule]).run(
        records, "B-IMMUTABLE", "rv2", raw_records=raw
    )
    with pytest.raises(FileExistsError, match="不可覆盖"):
        store.save(revised)

    new_batch = QualityEngine([revised_rule]).run(
        records, "B-IMMUTABLE-RERUN", "rv2", raw_records=raw
    )
    store.save(new_batch)
    assert set(store.batches()) == {"B-IMMUTABLE", "B-IMMUTABLE-RERUN"}
    assert store.load("B-IMMUTABLE")["rule_version_id"] == "rv1"
