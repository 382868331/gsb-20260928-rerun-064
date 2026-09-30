"""Issue lifecycle ledger.

Issues are tracked by a stable key across batches and rule versions.
Resolved issues are never deleted: the original observation, the run that
resolved it, recurrences and condition changes all stay on record.
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .models import utc_now


@dataclass
class IssueState:
    issue_key: str
    rule_id: str
    record_id: str
    field: str
    kind: str
    status: str  # open | resolved
    condition: str
    condition_signature: str
    detail: str
    first_seen_run_id: str
    first_seen_version_set: str
    first_seen_at: str
    last_seen_run_id: str = ""
    last_seen_version_set: str = ""
    last_seen_at: str = ""
    resolved_run_id: str = ""
    resolved_version_set: str = ""
    resolved_at: str = ""
    events: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, doc):
        return cls(**doc)


class IssueLedger:
    def __init__(self, root):
        self.root = Path(root)
        self.snapshot_path = self.root / "issues_snapshot.json"
        self.events_path = self.root / "issue_events.jsonl"
        self.states = {}
        if self.snapshot_path.exists():
            doc = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            self.states = {
                key: IssueState.from_dict(item)
                for key, item in doc.get("states", {}).items()
            }

    def get(self, issue_key):
        return self.states.get(issue_key)

    def open_issues(self):
        return [s for s in self.states.values() if s.status == "open"]

    def resolved_issues(self):
        return [s for s in self.states.values() if s.status == "resolved"]

    def _record_event(self, state, event_type, run_id, version_set, detail=""):
        event = {
            "at": utc_now(),
            "run_id": run_id,
            "version_set": version_set,
            "type": event_type,
            "issue_key": state.issue_key,
            "rule_id": state.rule_id,
            "record_id": state.record_id,
            "field": state.field,
            "kind": state.kind,
            "detail": detail,
        }
        state.events.append(event)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def apply_run(self, result, baseline=None):
        """Advance issue states with the issues observed in one run.

        ``baseline`` is the previous valid RunResult. Rules listed in
        ``incomplete_scope`` did not finish, so their issues are neither
        resolved nor treated as new observations.
        """
        incomplete_rule_ids = {
            item["rule_id"] for item in result.incomplete_scope
        }
        observed = {}
        for issue in result.issues:
            if issue.rule_id in incomplete_rule_ids:
                continue
            observed[issue.issue_key] = issue

        events = []
        now = utc_now()
        version_set = result.rule_version_set

        for issue_key, issue in observed.items():
            signature = result.rule_signatures.get(issue.rule_id, "")
            state = self.states.get(issue_key)
            if state is None:
                state = IssueState(
                    issue_key=issue_key,
                    rule_id=issue.rule_id,
                    record_id=issue.record_id,
                    field=issue.field,
                    kind=issue.kind,
                    status="open",
                    condition=issue.condition,
                    condition_signature=signature,
                    detail=issue.detail,
                    first_seen_run_id=result.run_id,
                    first_seen_version_set=version_set,
                    first_seen_at=now,
                )
                self.states[issue_key] = state
                events.append(self._record_event(
                    state, "opened", result.run_id, version_set, issue.detail
                ))
            else:
                if (
                    signature
                    and state.condition_signature
                    and signature != state.condition_signature
                ):
                    events.append(self._record_event(
                        state, "condition_changed", result.run_id, version_set,
                        f"condition {state.condition!r} -> {issue.condition!r}",
                    ))
                    state.condition = issue.condition
                    state.condition_signature = signature
                state.detail = issue.detail
                if state.status == "resolved":
                    events.append(self._record_event(
                        state, "reopened", result.run_id, version_set,
                        issue.detail,
                    ))
                    state.status = "open"
                    state.resolved_run_id = ""
                    state.resolved_version_set = ""
                    state.resolved_at = ""
                else:
                    events.append(self._record_event(
                        state, "still_open", result.run_id, version_set,
                        issue.detail,
                    ))
            state.last_seen_run_id = result.run_id
            state.last_seen_version_set = version_set
            state.last_seen_at = now

        # Only rules that ran to completion may resolve an open issue.
        # Only rules whose signatures are present ran to completion this run.
        completed_rule_ids = set(result.rule_signatures)
        for state in list(self.states.values()):
            if state.status != "open":
                continue
            if state.issue_key in observed:
                continue
            if state.rule_id not in completed_rule_ids:
                continue
            state.status = "resolved"
            state.resolved_run_id = result.run_id
            state.resolved_version_set = version_set
            state.resolved_at = now
            events.append(self._record_event(
                state, "resolved", result.run_id, version_set,
                "no longer observed after re-evaluation",
            ))

        self._save_snapshot()
        return events

    def _save_snapshot(self):
        self.root.mkdir(parents=True, exist_ok=True)
        doc = {
            "saved_at": utc_now(),
            "states": {
                key: state.to_dict() for key, state in self.states.items()
            },
        }
        tmp = self.snapshot_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.snapshot_path)
