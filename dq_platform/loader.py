"""本轮只读取本地人工样本（JSON）。后续新增数据源时在此扩展。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .parser import parse_row
from .types import Cell


def load_sample(
    path: str | Path,
) -> tuple[list[tuple[str, dict[str, Cell]]], dict[str, str], list[dict[str, Any]]]:
    """读取人工样本文件。

    文件结构::

        {
          "schema": {"字段": "string|number|date"},
          "id_field": "id",
          "records": [{...}]
        }

    返回 (解析后记录, schema, 原始记录)。
    """
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    schema: dict[str, str] = payload["schema"]
    id_field = payload.get("id_field", "id")
    raw_records: list[dict[str, Any]] = payload["records"]

    parsed: list[tuple[str, dict[str, Cell]]] = []
    for index, row in enumerate(raw_records):
        record_id = str(row.get(id_field, f"row-{index}"))
        parsed.append((record_id, parse_row(row, schema)))
    return parsed, schema, raw_records
