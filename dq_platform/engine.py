from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from .rules import Rule, RuleDependencyError, topological_order
from .types import BatchReport, RuleResult, RunStatus, now_iso


class QualityEngine:
    """数据质量规则引擎。

    - 执行前先做依赖检查（缺失依赖 / 成环 → 整个批次 ERROR）。
    - 任一规则抛异常 → 该规则 ERROR，批次 ERROR，绝不输出“全部合格”。
    """

    def __init__(self, rules: Sequence[Rule]) -> None:
        self.rules = list(rules)

    def run(
        self,
        records: Sequence[tuple[str, dict]],
        batch_id: str,
        rule_version_id: str,
        raw_records: Sequence[dict] | None = None,
    ) -> BatchReport:
        created_at = now_iso()
        fingerprint = self._fingerprint(raw_records if raw_records is not None else records)

        try:
            ordered = topological_order(self.rules)
        except RuleDependencyError as exc:
            results = [
                RuleResult(
                    rule_id=r.rule_id,
                    rule_version=r.version,
                    passed=False,
                    error=str(exc),
                    condition=r.condition,
                )
                for r in self.rules
            ]
            return BatchReport(
                batch_id=batch_id,
                rule_version_id=rule_version_id,
                created_at=created_at,
                record_count=len(records),
                results=results,
                data_fingerprint=fingerprint,
            )

        results: list[RuleResult] = []
        row_level = [r for r in ordered if r.is_row_level()]
        dataset_level = [r for r in ordered if not r.is_row_level()]

        for rule in ordered:
            issues = []
            try:
                if rule in row_level:
                    for record_id, cells in records:
                        issues.extend(rule.check_row(record_id, cells))
                else:
                    issues.extend(rule.check_dataset(list(records)))
            except Exception as exc:  # 执行失败必须显式暴露
                results.append(
                    RuleResult(
                        rule_id=rule.rule_id,
                        rule_version=rule.version,
                        passed=False,
                        error=f"{type(exc).__name__}: {exc}",
                        condition=rule.condition,
                    )
                )
                continue
            results.append(
                RuleResult(
                    rule_id=rule.rule_id,
                    rule_version=rule.version,
                    passed=not issues,
                    issues=issues,
                    condition=rule.condition,
                )
            )

        order_index = {r.rule_id: i for i, r in enumerate(ordered)}
        results.sort(key=lambda r: order_index.get(r.rule_id, 0))

        return BatchReport(
            batch_id=batch_id,
            rule_version_id=rule_version_id,
            created_at=created_at,
            record_count=len(records),
            results=results,
            data_fingerprint=fingerprint,
        )

    @staticmethod
    def _fingerprint(records: Sequence) -> str:
        blob = json.dumps(records, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
