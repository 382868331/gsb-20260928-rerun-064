"""规则定义：支持跨字段、跨记录，以及规则间依赖（含成环检测）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any

from .types import Cell, CellStatus, Issue


class RuleDependencyError(Exception):
    """依赖缺失或依赖成环。"""


class Rule(ABC):
    def __init__(
        self,
        rule_id: str,
        version: str,
        condition: str,
        depends_on: Sequence[str] = (),
    ) -> None:
        self.rule_id = rule_id
        self.version = version
        self.condition = condition
        self.depends_on = tuple(depends_on)

    @property
    def scope(self) -> str:
        return "row" if self.is_row_level() else "dataset"

    def is_row_level(self) -> bool:  # 跨字段
        return True

    @abstractmethod
    def check_row(self, record_id: str, cells: dict[str, Cell]) -> list[Issue]:
        """行级检查；跨记录规则返回 []。"""

    def check_dataset(
        self, records: Sequence[tuple[str, dict[str, Cell]]]
    ) -> list[Issue]:
        """跨记录检查；行级规则返回 []。"""
        return []

    def _issue(
        self,
        record_id: str,
        message: str,
        fields: Iterable[str] = (),
        related: Iterable[str] = (),
    ) -> Issue:
        return Issue(
            record_id=record_id,
            rule_id=self.rule_id,
            rule_version=self.version,
            condition=self.condition,
            message=message,
            fields=tuple(fields),
            related_record_ids=tuple(related),
        )


def topological_order(rules: Sequence[Rule]) -> list[Rule]:
    """按依赖排序；缺失依赖或成环时抛 RuleDependencyError。"""
    by_id = {r.rule_id: r for r in rules}
    seen: dict[str, str] = {}  # unvisited / visiting / done
    ordered: list[Rule] = []

    def visit(rule: Rule, stack: list[str]) -> None:
        state = seen.get(rule.rule_id, "unvisited")
        if state == "done":
            return
        if state == "visiting":
            cycle_start = stack.index(rule.rule_id)
            cycle = stack[cycle_start:] + [rule.rule_id]
            raise RuleDependencyError(
                f"规则依赖成环: {' -> '.join(cycle)}"
            )
        seen[rule.rule_id] = "visiting"
        stack.append(rule.rule_id)
        for dep_id in rule.depends_on:
            dep = by_id.get(dep_id)
            if dep is None:
                raise RuleDependencyError(
                    f"规则 {rule.rule_id} 依赖的规则 {dep_id} 不存在"
                )
            visit(dep, stack)
        stack.pop()
        seen[rule.rule_id] = "done"
        ordered.append(rule)

    for rule in rules:
        visit(rule, [])
    return ordered


class CrossFieldRule(Rule):
    """跨字段规则：以可调用谓词表达。

    谓词只接收状态为 OK 的 Cell.value；任何引用字段缺失或格式错误时，
    不会把它当 0/空串，而是产生一条“数据不可用”问题。
    """

    def __init__(
        self,
        rule_id: str,
        version: str,
        fields: Sequence[str],
        predicate,
        condition: str,
        depends_on: Sequence[str] = (),
    ) -> None:
        super().__init__(rule_id, version, condition, depends_on)
        self.fields = tuple(fields)
        self._predicate = predicate

    def check_row(self, record_id: str, cells: dict[str, Cell]) -> list[Issue]:
        bad: list[str] = []
        values: dict[str, Any] = {}
        for name in self.fields:
            cell = cells.get(
                name, Cell(CellStatus.MISSING, expected=None)
            )
            if cell.status is CellStatus.MISSING:
                bad.append(f"{name} 缺失")
            elif cell.status is CellStatus.MALFORMED:
                bad.append(f"{name} 格式错误(应为 {cell.expected})")
            else:
                values[name] = cell.value
        if bad:
            return [
                self._issue(
                    record_id,
                    "字段不可用于校验：" + "；".join(bad),
                    fields=self.fields,
                )
            ]
        try:
            ok = bool(self._predicate(**values))
        except Exception as exc:  # 谓词本身失败 → 执行错误而非合格
            raise RuntimeError(f"谓词执行异常: {exc}") from exc
        if ok:
            return []
        return [
            self._issue(
                record_id,
                f"违反条件: {self.condition}",
                fields=self.fields,
            )
        ]


class DateContradictionRule(Rule):
    """跨字段：开始日期不得晚于结束日期。"""

    def __init__(
        self,
        rule_id: str,
        version: str,
        start_field: str,
        end_field: str,
        depends_on: Sequence[str] = (),
    ) -> None:
        cond = f"{start_field} <= {end_field}"
        super().__init__(rule_id, version, cond, depends_on)
        self.start_field = start_field
        self.end_field = end_field

    def check_row(self, record_id: str, cells: dict[str, Cell]) -> list[Issue]:
        start = cells.get(self.start_field, Cell(CellStatus.MISSING, expected="date"))
        end = cells.get(self.end_field, Cell(CellStatus.MISSING, expected="date"))
        problems = []
        for name, cell in ((self.start_field, start), (self.end_field, end)):
            if cell.status is CellStatus.MISSING:
                problems.append(f"{name} 缺失")
            elif cell.status is CellStatus.MALFORMED:
                problems.append(f"{name} 日期格式错误")
        if problems:
            return [
                self._issue(
                    record_id,
                    "日期不可用于比较：" + "；".join(problems),
                    fields=(self.start_field, self.end_field),
                )
            ]
        if start.value > end.value:
            return [
                self._issue(
                    record_id,
                    f"日期矛盾: {self.start_field}({start.value}) 晚于 "
                    f"{self.end_field}({end.value})",
                    fields=(self.start_field, self.end_field),
                )
            ]
        return []


class DuplicateIdentityRule(Rule):
    """跨记录：身份键在批次内不得重复。"""

    def __init__(
        self,
        rule_id: str,
        version: str,
        key_fields: Sequence[str],
        depends_on: Sequence[str] = (),
    ) -> None:
        super().__init__(
            rule_id,
            version,
            "身份键 " + "+".join(key_fields) + " 在批次内唯一",
            depends_on,
        )
        self.key_fields = tuple(key_fields)

    def is_row_level(self) -> bool:
        return False

    def check_row(self, record_id: str, cells: dict[str, Cell]) -> list[Issue]:
        return []

    def check_dataset(
        self, records: Sequence[tuple[str, dict[str, Cell]]]
    ) -> list[Issue]:
        groups: dict[tuple, list[tuple[int, str]]] = {}
        unusable: list[Issue] = []
        for index, (record_id, cells) in enumerate(records):
            key_parts: list[Any] = []
            problems: list[str] = []
            for name in self.key_fields:
                cell = cells.get(name, Cell(CellStatus.MISSING))
                if cell.status is CellStatus.MISSING:
                    problems.append(f"{name} 缺失")
                elif cell.status is CellStatus.MALFORMED:
                    problems.append(f"{name} 格式错误")
                else:
                    key_parts.append(str(cell.value))
            if problems:
                unusable.append(
                    self._issue(
                        record_id,
                        "身份键不可用于查重：" + "；".join(problems),
                        fields=self.key_fields,
                    )
                )
                continue
            groups.setdefault(tuple(key_parts), []).append((index, record_id))

        issues: list[Issue] = list(unusable)
        for key, entries in groups.items():
            if len(entries) > 1:
                for _idx, rid in entries:
                    others = [other_rid for j, other_rid in entries if j != _idx]
                    issues.append(
                        self._issue(
                            rid,
                            f"重复身份 {dict(zip(self.key_fields, key))}，"
                            f"与记录 {', '.join(others)} 冲突",
                            fields=self.key_fields,
                            related=others,
                        )
                    )
        return issues
