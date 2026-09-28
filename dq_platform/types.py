from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CellStatus(Enum):
    """三态单元格：正常值 / 缺失 / 格式错误。

    缺失与格式错误绝不能被静默当成 0、空串或 False。
    """

    OK = "ok"
    MISSING = "missing"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class Cell:
    status: CellStatus
    raw: Any = None
    value: Any = None
    expected: str | None = None  # 例如 "date"、"number"

    @property
    def usable(self) -> bool:
        return self.status is CellStatus.OK

    def __post_init__(self) -> None:
        if self.status is CellStatus.OK and self.value is None:
            object.__setattr__(self, "value", self.raw)


@dataclass(frozen=True)
class Issue:
    """一条问题记录：指回具体记录与违反条件。"""

    record_id: str
    rule_id: str
    rule_version: str
    condition: str
    message: str
    fields: tuple[str, ...] = ()
    related_record_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "condition": self.condition,
            "message": self.message,
            "fields": list(self.fields),
            "related_record_ids": list(self.related_record_ids),
        }


class RunStatus(Enum):
    PASSED = "passed"          # 全部规则执行成功且无问题
    FAILED = "failed"          # 执行成功但发现数据质量问题
    ERROR = "error"            # 规则执行失败（异常/依赖问题），绝不等于合格


@dataclass
class RuleResult:
    rule_id: str
    rule_version: str
    passed: bool
    issues: list[Issue] = field(default_factory=list)
    error: str | None = None
    condition: str = ""

    @property
    def status(self) -> RunStatus:
        if self.error is not None:
            return RunStatus.ERROR
        return RunStatus.PASSED if self.passed else RunStatus.FAILED


@dataclass
class BatchReport:
    batch_id: str
    rule_version_id: str
    created_at: str
    record_count: int
    results: list[RuleResult]
    data_fingerprint: str

    @property
    def status(self) -> RunStatus:
        if any(r.status is RunStatus.ERROR for r in self.results):
            return RunStatus.ERROR
        if any(not r.passed for r in self.results):
            return RunStatus.FAILED
        return RunStatus.PASSED

    @property
    def issues(self) -> list[Issue]:
        return [i for r in self.results for i in r.issues]

    def summary(self) -> str:
        st = self.status
        if st is RunStatus.PASSED:
            return f"批次 {self.batch_id}：全部合格（{self.record_count} 条记录）"
        if st is RunStatus.FAILED:
            return (
                f"批次 {self.batch_id}：不合格，"
                f"发现 {len(self.issues)} 个问题（{self.record_count} 条记录）"
            )
        errors = [r for r in self.results if r.error is not None]
        detail = "; ".join(f"{r.rule_id}: {r.error}" for r in errors)
        return f"批次 {self.batch_id}：执行失败（{detail}），不得判定为合格"

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "rule_version_id": self.rule_version_id,
            "created_at": self.created_at,
            "record_count": self.record_count,
            "data_fingerprint": self.data_fingerprint,
            "status": self.status.value,
            "results": [
                {
                    "rule_id": r.rule_id,
                    "rule_version": r.rule_version,
                    "passed": r.passed,
                    "error": r.error,
                    "condition": r.condition,
                    "issues": [i.to_dict() for i in r.issues],
                }
                for r in self.results
            ],
        }


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
