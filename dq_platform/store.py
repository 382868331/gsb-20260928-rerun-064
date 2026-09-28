"""批次结论持久化：以批次号为主键，旧批次结论只读、不可被覆盖。"""
from __future__ import annotations

import json
from pathlib import Path

from .types import BatchReport


class Store:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, batch_id: str) -> Path:
        return self.directory / f"{batch_id}.json"

    def save(self, report: BatchReport) -> Path:
        path = self._path(report.batch_id)
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("rule_version_id") != report.rule_version_id:
                raise FileExistsError(
                    f"批次 {report.batch_id} 已存在且绑定规则版本 "
                    f"{existing.get('rule_version_id')}；修订规则请生成新批次，"
                    "旧批次结论不可覆盖"
                )
            raise FileExistsError(
                f"批次 {report.batch_id} 的结论已归档，不可覆盖"
            )
        path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, batch_id: str) -> dict:
        return json.loads(self._path(batch_id).read_text(encoding="utf-8"))

    def batches(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.json"))
