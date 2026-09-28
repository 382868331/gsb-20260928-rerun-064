from __future__ import annotations

import argparse
import sys

from .engine import QualityEngine
from .loader import load_sample
from .rules import CrossFieldRule, DateContradictionRule, DuplicateIdentityRule
from .store import Store
from .types import RunStatus


def build_engine() -> QualityEngine:
    return QualityEngine(
        [
            CrossFieldRule(
                rule_id="R100-age-positive",
                version="v1",
                fields=("age",),
                predicate=lambda age: age > 0,
                condition="age > 0",
            ),
            CrossFieldRule(
                rule_id="R110-service-le-age",
                version="v1",
                fields=("years_service", "age"),
                predicate=lambda years_service, age: years_service < age,
                condition="years_service < age",
                depends_on=("R100-age-positive",),
            ),
            DateContradictionRule(
                rule_id="R200-date-order",
                version="v1",
                start_field="hire_date",
                end_field="leave_date",
            ),
            DuplicateIdentityRule(
                rule_id="R300-unique-emp",
                version="v1",
                key_fields=("emp_id",),
            ),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地人工样本数据质量校验")
    parser.add_argument("sample", help="样本 JSON 文件路径")
    parser.add_argument("batch_id", help="数据批次号")
    parser.add_argument(
        "--rule-version", default="rules-v1", help="规则版本标识"
    )
    parser.add_argument("--store", default="batch_reports", help="批次归档目录")
    args = parser.parse_args(argv)

    records, _schema, raw = load_sample(args.sample)
    report = build_engine().run(
        records,
        batch_id=args.batch_id,
        rule_version_id=args.rule_version,
        raw_records=raw,
    )

    print(report.summary())
    for issue in report.issues:
        print(f"  - [{issue.record_id}] {issue.rule_id}: {issue.message}")
    for result in report.results:
        if result.error:
            print(f"  ! 规则 {result.rule_id} 执行失败: {result.error}")

    store = Store(args.store)
    try:
        path = store.save(report)
        print(f"批次结论已归档: {path}")
    except FileExistsError as exc:
        print(f"归档拒绝: {exc}", file=sys.stderr)

    return {
        RunStatus.PASSED: 0,
        RunStatus.FAILED: 2,
        RunStatus.ERROR: 3,
    }[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
