"""Read-only loaders for local manual samples and rule files."""

import json
from pathlib import Path

from .models import Batch, Record, RuleDef, utc_now


def load_batch(path, batch_id=None, id_field="id"):
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"sample must be a JSON array of records: {path}")
    records = tuple(
        Record(
            record_id=str(item.get(id_field, f"row-{idx}")),
            index=idx,
            data=item,
        )
        for idx, item in enumerate(raw)
    )
    return Batch(
        batch_id=batch_id or path.stem,
        source=str(path),
        loaded_at=utc_now(),
        records=records,
    )


def load_rule_set(path):
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    version_set = doc["version_set"]
    rules = []
    seen = set()
    for item in doc["rules"]:
        rule = RuleDef(
            rule_id=item["id"],
            rule_type=item["type"],
            version_set=version_set,
            fields=tuple(item.get("fields", ())),
            params=item.get("params", {}),
            depends_on=tuple(item.get("depends_on", [])),
        )
        if rule.rule_id in seen:
            raise ValueError(f"duplicate rule id in {path}: {rule.rule_id}")
        seen.add(rule.rule_id)
        rules.append(rule)
    return version_set, tuple(rules)
