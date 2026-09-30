"""Core data model: batches, rule versions, issues and run results."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib
import json


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Record:
    record_id: str
    index: int
    data: dict


@dataclass(frozen=True)
class Batch:
    """An immutable snapshot of a local manual sample."""

    batch_id: str
    source: str
    loaded_at: str
    records: tuple

    @property
    def checksum(self):
        body = json.dumps(
            [r.data for r in self.records], ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()[:16]


@dataclass(frozen=True)
class RuleDef:
    rule_id: str
    rule_type: str
    version_set: str
    fields: tuple = ()
    params: dict = field(default_factory=dict)
    depends_on: tuple = ()

    @property
    def condition(self):
        target = ",".join(self.fields) or "(records)"
        if self.rule_type == "date_order":
            early = self.params.get("early_field", "?")
            late = self.params.get("late_field", "?")
            optional = sorted(self.params.get("optional_fields", []))
            suffix = f";optional={','.join(optional)}" if optional else ""
            return f"date_order[{early} <= {late}{suffix}]"
        if self.rule_type == "unique_fields":
            keys = self.params.get("key_fields") or list(self.fields)
            return f"unique_fields[{','.join(keys)}]"
        return f"{self.rule_type}[{target}]"


@dataclass(frozen=True)
class Issue:
    rule_id: str
    rule_version_set: str
    condition: str
    record_id: str
    record_index: int
    field: str
    kind: str  # violated | missing_value | malformed_value
    detail: str
    issue_key: str = ""

    def __post_init__(self):
        if not self.issue_key:
            object.__setattr__(
                self,
                "issue_key",
                f"{self.rule_id}|{self.record_id}|{self.field}|{self.kind}",
            )

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class RunResult:
    """Conclusion for one batch x one immutable rule version set.

    ``status`` is FAILED whenever evaluation errors occur, even if no issue
    rows were produced, so callers can never report "all passed".
    """

    run_id: str
    batch_id: str
    batch_checksum: str
    rule_version_set: str
    started_at: str
    finished_at: str
    status: str  # PASSED | FAILED
    issues: tuple
    errors: tuple
    mode: str = "full"  # full | incremental
    affected_records: tuple = ()
    deleted_record_ids: tuple = ()
    incomplete_scope: tuple = ()
    rule_signatures: dict = field(default_factory=dict)
    reused_baseline_run_id: str = ""

    def to_dict(self):
        d = asdict(self)
        d["issue_count"] = len(self.issues)
        return d

    @classmethod
    def from_dict(cls, doc):
        issues = tuple(Issue(**item) for item in doc.get("issues", []))
        errors = tuple(doc.get("errors", []))
        kwargs = dict(
            run_id=doc["run_id"],
            batch_id=doc["batch_id"],
            batch_checksum=doc["batch_checksum"],
            rule_version_set=doc["rule_version_set"],
            started_at=doc["started_at"],
            finished_at=doc["finished_at"],
            status=doc["status"],
            issues=issues,
            errors=errors,
        )
        for optional, default in (
            ("mode", "full"),
            ("affected_records", ()),
            ("deleted_record_ids", ()),
            ("incomplete_scope", ()),
            ("rule_signatures", {}),
            ("reused_baseline_run_id", ""),
        ):
            kwargs[optional] = doc.get(optional, default)
        return cls(**kwargs)


def rule_signature(rule):
    """Stable fingerprint of a rule's judgement condition (identity excluded)."""
    body = json.dumps(
        {
            "rule_type": rule.rule_type,
            "fields": list(rule.fields),
            "params": rule.params,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
