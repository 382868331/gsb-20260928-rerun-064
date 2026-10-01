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

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class RulePlan:
    """Before/after view of what one rule will re-check this run.

    scope is one of:
      "full"    - every record group of the rule is re-judged
      "groups"  - only the named identity groups (unique_fields)
      "records" - only the listed records (row-level rules)
      "skip"    - nothing changed that this rule can observe
    """

    rule_id: str
    rule_type: str
    scope: str
    reason: str
    affected_record_ids: tuple = ()
    affected_groups: tuple = ()
    changed_record_ids: tuple = ()
    added_record_ids: tuple = ()
    removed_record_ids: tuple = ()

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
    # Per-rule check plan: which records/groups each rule re-judged and
    # why; previous_check_plan is the plan of the run being built upon.
    check_plan: tuple = ()
    previous_check_plan: tuple = ()
    # Rules whose evaluation finished this run. A rule listed in
    # incomplete_rules failed mid-run: its previous conclusion (if any)
    # is kept and the run must not be treated as covering that scope.
    completed_rules: tuple = ()
    incomplete_rules: tuple = ()

    def to_dict(self):
        d = asdict(self)
        d["issue_count"] = len(self.issues)
        return d
