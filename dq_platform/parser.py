"""把原始标量解析为三态 Cell。缺失/格式错误保持显式标记。"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from .types import Cell, CellStatus

_MISSING_TOKENS = {"", "na", "n/a", "null", "none", "?"}
_DATE_FORMATS = ("%Y-%m-%d",)


def _is_missing(raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, str) and raw.strip().lower() in _MISSING_TOKENS:
        return True
    return False


def parse_cell(raw: Any, expected: str = "string") -> Cell:
    if _is_missing(raw):
        return Cell(CellStatus.MISSING, raw=raw, expected=expected)

    if expected == "string":
        return Cell(CellStatus.OK, raw=raw, value=str(raw).strip())

    if expected == "number":
        if isinstance(raw, bool):
            return Cell(CellStatus.MALFORMED, raw=raw, expected="number")
        if isinstance(raw, (int, float)):
            return Cell(CellStatus.OK, raw=raw, value=float(raw))
        try:
            return Cell(CellStatus.OK, raw=raw, value=float(str(raw).strip()))
        except (TypeError, ValueError):
            return Cell(CellStatus.MALFORMED, raw=raw, expected="number")

    if expected == "date":
        if isinstance(raw, _dt.date) and not isinstance(raw, _dt.datetime):
            return Cell(CellStatus.OK, raw=raw, value=raw)
        if isinstance(raw, _dt.datetime):
            d = raw.date()
            return Cell(CellStatus.OK, raw=raw, value=d)
        text = str(raw).strip()
        for fmt in _DATE_FORMATS:
            try:
                return Cell(
                    CellStatus.OK,
                    raw=raw,
                    value=_dt.datetime.strptime(text, fmt).date(),
                    expected="date",
                )
            except ValueError:
                continue
        return Cell(CellStatus.MALFORMED, raw=raw, expected="date")

    return Cell(CellStatus.OK, raw=raw, value=raw, expected=expected)


def parse_row(
    row: dict[str, Any], schema: dict[str, str]
) -> dict[str, Cell]:
    parsed: dict[str, Cell] = {}
    for field_name in schema:
        if field_name not in row:
            parsed[field_name] = Cell(CellStatus.MISSING, expected=schema[field_name])
        else:
            parsed[field_name] = parse_cell(row[field_name], schema[field_name])
    return parsed
