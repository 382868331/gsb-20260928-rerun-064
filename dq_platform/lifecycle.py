"""Issue lifecycle tracking on top of immutable run conclusions.

Events are appended to issues.jsonl and never rewritten, so a resolved
issue keeps its original record plus the version that resolved it; a
reappearing issue is a "reopened" event, not a new identity.  When a run
leaves a rule incomplete (execution failure), that rule's open issues are
reported as "unknown" instead of being silently resolved.
"""

import json
from pathlib import Path

from .models import utc_now
from .corrections import SAME_FACT

OPENED = "opened"
RESOLVED = "resolved"
REOPENED = "reopened"
CONDITION_CHANGED = "condition_changed"
DECISION_MIGRATED = "decision_migrated"
BASIS_LOST = "basis_lost"


def issue_key(issue):
    if isinstance(issue, dict):
        return (issue["rule_id"], issue["record_id"], issue["field"],
                issue["kind"])
    return (issue.rule_id, issue.record_id, issue.field, issue.kind)


def _issue_dict(issue):
    if isinstance(issue, dict):
        return dict(issue)
    return issue.to_dict()


class IssueTracker:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "issues.jsonl"

    def events(self):
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def state(self):
        """Replay the ledger into {key: latest open/resolved state}."""
        state = {}
        for event in self.events():
            key = tuple(event["key"])
            if event["event"] in (OPENED, REOPENED):
                state[key] = {
                    "status": "open",
                    "issue": event["issue"],
                    "rule_version_set": event["rule_version_set"],
                    "since_run": event["run_id"],
                }
            elif event["event"] == CONDITION_CHANGED:
                if key in state and state[key]["status"] == "open":
                    state[key]["rule_version_set"] = event["rule_version_set"]
            elif event["event"] == DECISION_MIGRATED:
                migration = event.get("migration", {})
                field = migration.get("field", key[2])
                kind = migration.get("kind", key[3])
                for record_id in migration.get("source_records", ()):
                    source_key = (key[0], record_id, field, kind)
                    state[source_key] = {
                        "status": "migrated",
                        "issue": state.get(source_key, {}).get("issue", {}),
                        "migration": migration,
                    }
                for record_id in migration.get("target_records", ()):
                    target_key = (key[0], record_id, field, kind)
                    state[target_key] = {
                        "status": "inherited",
                        "issue": migration,
                        "rule_version_set": event["rule_version_set"],
                        "since_run": event["run_id"],
                    }
            elif event["event"] == BASIS_LOST:
                migration = event.get("migration", {})
                for record_id in migration.get("target_records", ()):
                    lost_key = (
                        key[0], record_id,
                        migration.get("field", key[2]),
                        migration.get("kind", key[3]),
                    )
                    if lost_key in state:
                        state[lost_key]["status"] = "open"
                        state[lost_key]["basis_lost"] = True
            elif event["event"] == RESOLVED:
                state[key] = {
                    "status": "resolved",
                    "issue": event["issue"],
                    "resolved_in_run": event["run_id"],
                    "resolved_version": event["rule_version_set"],
                }
        return state

    def open_issues(self):
        return {k: v for k, v in self.state().items()
                if v["status"] == "open"}

    def update(self, result):
        """Fold one run result into the ledger; return the transitions."""
        current = result.to_dict()
        completed = set(current.get("completed_rules") or ())
        incomplete = set(current.get("incomplete_rules") or ())
        blocked_records = {}
        covered_records = {}
        for conclusion in current.get("conclusions") or ():
            rule_id = conclusion["rule_id"]
            target = (blocked_records if conclusion.get("status") == "blocked"
                      else covered_records)
            target.setdefault(rule_id, set()).update(
                conclusion.get("records", ())
            )
        present = {}
        for issue in current["issues"]:
            rule_id = issue["rule_id"]
            if (rule_id in completed
                    or issue["record_id"] in covered_records.get(rule_id, set())):
                present[issue_key(issue)] = issue

        migrated_targets = set()
        migration_events = []
        existing_migrations = {
            (json.dumps(e.get("migration", {}), sort_keys=True, ensure_ascii=False))
            for e in self.events()
            if e["event"] == DECISION_MIGRATED
        }
        accepted_material = current.get("accepted_material") or {}
        for migration in accepted_material.get("migrations", ()):
            target_keys = {
                (migration["rule_id"], target,
                 migration["field"], migration["kind"])
                for target in migration["target_records"]
            }
            migration_body = json.dumps(migration, sort_keys=True, ensure_ascii=False)
            if migration_body in existing_migrations:
                continue
            migration_events.append(self._migration_event(migration, result))
            if migration.get("relation") == SAME_FACT and target_keys & set(present):
                migrated_targets.update(target_keys)

        state = self.state()
        open_keys = {k for k, v in state.items() if v["status"] == "open"}
        new_events = []
        transitions = {"opened": [], "reopened": [], "resolved": [],
                       "condition_changed": [], "unknown": []}

        for key, issue in present.items():
            if key in migrated_targets:
                continue
            if key in open_keys:
                if state[key]["rule_version_set"] != result.rule_version_set:
                    # Same problem, but the judging condition changed.
                    new_events.append(self._event(
                        CONDITION_CHANGED, key, issue, result,
                        extra={"from_version": state[key]["rule_version_set"]},
                    ))
                    transitions["condition_changed"].append(key)
                continue
            event = REOPENED if key in state else OPENED
            new_events.append(self._event(event, key, issue, result))
            transitions[event].append(key)

        for key in sorted(open_keys - set(present)):
            rule_id = state[key]["issue"]["rule_id"]
            record_id = key[1]
            migrated_source = any(
                key[0] == migration.get("rule_id")
                and record_id in migration.get("source_records", ())
                and key[2] == migration.get("field")
                and key[3] == migration.get("kind")
                for migration in accepted_material.get("migrations", ())
                if migration.get("relation") == SAME_FACT
            )
            if migrated_source:
                continue
            if record_id in blocked_records.get(rule_id, set()):
                # Execution failed: previous conclusion stays valid and the
                # issue is explicitly NOT judged by this run.
                transitions["unknown"].append(key)
                continue
            if rule_id not in completed and record_id not in covered_records.get(
                rule_id, set()
            ):
                continue
            new_events.append(self._event(
                RESOLVED, key, state[key]["issue"], result,
            ))
            transitions["resolved"].append(key)

        new_events.extend(migration_events)
        if new_events:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                for event in new_events:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return transitions

    def _event(self, kind, key, issue, result, extra=None):
        event = {
            "ts": utc_now(),
            "event": kind,
            "key": list(key),
            "issue": _issue_dict(issue),
            "run_id": result.run_id,
            "batch_id": result.batch_id,
            "rule_version_set": result.rule_version_set,
        }
        if extra:
            event.update(extra)
        return event

    def record_migrations(self, result):
        """Record confirmed human issue inheritance decisions once."""
        payload = result.accepted_material or {}
        existing = {
            (e["event"], tuple(e.get("migration", {}).get("target_records", [])),
             tuple(e.get("migration", {}).get("source_records", [])),
             e.get("migration", {}).get("rule_id"),
             e.get("migration", {}).get("field"),
             e.get("migration", {}).get("kind"))
            for e in self.events()
        }
        events = []
        for migration in payload.get("migrations", ()):
            identity = (
                DECISION_MIGRATED,
                tuple(migration["target_records"]),
                tuple(migration["source_records"]),
                migration["rule_id"],
                migration["field"],
                migration["kind"],
            )
            if identity in existing:
                continue
            event = self._migration_event(migration, result)
            events.append(event)
        if events:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return tuple(events)

    def _migration_event(self, migration, result):
        return {
            "ts": utc_now(),
            "event": DECISION_MIGRATED,
            "key": [
                migration["rule_id"],
                ",".join(migration["target_records"]),
                migration["field"],
                migration["kind"],
            ],
            "source_keys": [
                [migration["rule_id"], source,
                 migration["field"], migration["kind"]]
                for source in migration["source_records"]
            ],
            "run_id": result.run_id,
            "batch_id": result.batch_id,
            "rule_version_set": result.rule_version_set,
            "migration": migration,
        }

    def record_basis_lost(self, lost_migrations, result):
        """Mark prior human decisions whose accepted source was revoked."""
        events = []
        for migration in lost_migrations:
            events.append({
                "ts": utc_now(),
                "event": BASIS_LOST,
                "key": [
                    migration["rule_id"],
                    ",".join(migration["target_records"]),
                    migration["field"],
                    migration["kind"],
                ],
                "run_id": result.run_id,
                "batch_id": result.batch_id,
                "rule_version_set": result.rule_version_set,
                "migration": migration,
            })
        if events:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return tuple(events)
