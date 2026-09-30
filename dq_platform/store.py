"""Append-only result store.

Conclusions are immutable: re-checking after a rule revision creates a new
run and never overwrites an earlier batch conclusion.
"""

import json
from pathlib import Path

from .models import RunResult


class ResultStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "runs.jsonl"

    def append(self, result):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
        return self.path

    def load_all(self):
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def load_results(self):
        return [RunResult.from_dict(doc) for doc in self.load_all()]

    def conclusions_for_batch(self, batch_id):
        return [
            doc for doc in self.load_all() if doc["batch_id"] == batch_id
        ]

    def latest_valid_for_batch(self, batch_id):
        """Most recent run that completed every rule (no incomplete scope)."""
        candidates = [
            result for result in self.load_results()
            if result.batch_id == batch_id and not result.incomplete_scope
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda result: result.started_at)
        return candidates[-1]
