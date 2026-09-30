import copy
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from dq_platform.engine import evaluate, evaluate_incremental
from dq_platform.lifecycle import IssueLedger
from dq_platform.loader import load_batch, load_rule_set
from dq_platform.models import RunResult
from dq_platform.store import ResultStore

SAMPLES = ROOT / "samples"


def issue_keys(result):
    return sorted(
        (i.rule_id, i.record_id, i.field, i.kind) for i in result.issues
    )


def batch_from_rows(rows, tmp, name="snapshot.json", batch_id="emp"):
    path = Path(tmp) / name
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return load_batch(path, batch_id=batch_id)


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.v1_rows = json.loads(
            (SAMPLES / "employees_v1.json").read_text(encoding="utf-8")
        )
        self.version_set, self.rules = load_rule_set(
            SAMPLES / "rules_v1.json"
        )
        self.tmp = tempfile.mkdtemp()
        self.base_batch = batch_from_rows(self.v1_rows, self.tmp)
        self.base_result = evaluate(self.base_batch, self.rules)

    def _incremental(self, rows):
        new_batch = batch_from_rows(rows, self.tmp, "snapshot2.json")
        result = evaluate_incremental(
            new_batch, self.rules, self.base_result, self.base_batch
        )
        full = evaluate(new_batch, self.rules)
        return new_batch, result, full

    def test_only_affected_cross_record_scope_but_conclusions_match_full(self):
        # Fixing the contradictory date touches one record only.
        rows = copy.deepcopy(self.v1_rows)
        rows[1]["leave_date"] = "2024-09-01"
        _, result, full = self._incremental(rows)
        self.assertEqual(result.mode, "incremental")
        self.assertEqual(result.affected_records, ("E002",))
        self.assertEqual(issue_keys(result), issue_keys(full))

    def test_duplicate_fixed_by_editing_one_member_frees_partner(self):
        # E003/E004 share an id card; fixing E004 must clear both issues.
        rows = json.loads(
            (SAMPLES / "employees_v2_fixed.json").read_text(encoding="utf-8")
        )
        _, result, full = self._incremental(rows)
        self.assertEqual(issue_keys(result), issue_keys(full))
        dupes = {
            i.record_id for i in result.issues
            if i.rule_id == "identity_unique" and i.kind == "violated"
        }
        self.assertEqual(dupes, set())
        # employees_v2_fixed fixes E002 as well; the only surviving finding
        # is E005's missing id_card.
        self.assertEqual(
            {(i.rule_id, i.record_id) for i in result.issues},
            {("identity_present", "E005")},
        )

    def test_deleted_record_removes_its_issues_and_frees_partner(self):
        rows = copy.deepcopy(self.v1_rows)
        del rows[3]  # delete E004
        _, result, full = self._incremental(rows)
        self.assertEqual(result.deleted_record_ids, ("E004",))
        self.assertEqual(issue_keys(result), issue_keys(full))
        dupes = {
            i.record_id for i in result.issues
            if i.rule_id == "identity_unique"
        }
        self.assertEqual(dupes, set())

    def test_new_collision_pulls_in_unchanged_partner(self):
        rows = copy.deepcopy(self.v1_rows)
        rows[0]["id_card"] = "110101199002022345"  # E001 collides with E002
        _, result, full = self._incremental(rows)
        self.assertEqual(issue_keys(result), issue_keys(full))
        dupes = {
            i.record_id for i in result.issues
            if i.rule_id == "identity_unique" and i.kind == "violated"
        }
        # E003/E004 still share their own id card and stay open; the new
        # E001/E002 collision is detected even though E002 did not change.
        self.assertEqual(dupes, {"E001", "E002", "E003", "E004"})

    def test_rule_condition_change_triggers_full_scope(self):
        _, v2_rules = load_rule_set(SAMPLES / "rules_v2_leave_required.json")
        result = evaluate_incremental(
            self.base_batch, v2_rules, self.base_result, self.base_batch
        )
        full = evaluate(self.base_batch, v2_rules)
        self.assertEqual(issue_keys(result), issue_keys(full))
        missing_leave = {
            i.record_id for i in result.issues
            if i.rule_id == "employment_dates_consistent"
            and i.kind == "missing_value"
        }
        # leave_date is no longer optional: E003 (null) and E004 ("") fail.
        self.assertEqual(missing_leave, {"E003", "E004"})

    def test_execution_failure_keeps_previous_conclusion_and_reports_scope(self):
        broken = type(self.rules[2])(
            rule_id=self.rules[2].rule_id,
            rule_type=self.rules[2].rule_type,
            version_set=self.rules[2].version_set,
            fields=self.rules[2].fields,
            params={},
            depends_on=(),
        )
        rows = copy.deepcopy(self.v1_rows)
        rows[1]["leave_date"] = "2024-09-01"
        new_batch = batch_from_rows(rows, self.tmp, "snapshot2.json")
        result = evaluate_incremental(
            new_batch, (broken,), self.base_result, self.base_batch
        )
        self.assertEqual(result.status, "FAILED")
        self.assertTrue(result.errors)
        scope = {item["rule_id"]: item for item in result.incomplete_scope}
        self.assertIn("employment_dates_consistent", scope)
        self.assertIn("E002", scope["employment_dates_consistent"]["record_ids"])
        # Previous valid issues for the failed rule survive verbatim,
        # including the E002 contradiction that data would have resolved.
        date_issues = {
            (i.record_id, i.kind)
            for i in result.issues
            if i.rule_id == "employment_dates_consistent"
        }
        self.assertIn(("E002", "violated"), date_issues)
        self.assertIn(("E004", "malformed_value"), date_issues)

    def test_resolved_issue_kept_with_resolution_version_and_can_reopen(self):
        ledger = IssueLedger(self.tmp)
        ledger.apply_run(self.base_result)

        fixed = json.loads(
            (SAMPLES / "employees_v2_fixed.json").read_text(encoding="utf-8")
        )
        fixed_batch = batch_from_rows(fixed, self.tmp, "snapshot2.json")
        fixed_result = evaluate(fixed_batch, self.rules)
        ledger.apply_run(fixed_result)

        resolved = [
            s for s in ledger.resolved_issues()
            if s.rule_id == "identity_unique"
        ]
        self.assertEqual({s.record_id for s in resolved}, {"E003", "E004"})
        for state in resolved:
            self.assertEqual(state.resolved_run_id, fixed_result.run_id)
            self.assertEqual(
                state.resolved_version_set, fixed_result.rule_version_set
            )
            self.assertEqual(state.first_seen_run_id, self.base_result.run_id)

        # Reintroduce the collision: the same issue reopens, it is not a new row.
        rows = copy.deepcopy(fixed)
        rows[3]["id_card"] = "110101199003033456"
        re_batch = batch_from_rows(rows, self.tmp, "snapshot3.json")
        events = ledger.apply_run(evaluate(re_batch, self.rules))
        reopened = [e for e in events if e["type"] == "reopened"]
        self.assertTrue(any(e["record_id"] == "E004" for e in reopened))
        e004 = next(
            s for s in ledger.open_issues()
            if s.rule_id == "identity_unique" and s.record_id == "E004"
        )
        self.assertEqual(e004.first_seen_run_id, self.base_result.run_id)
        self.assertEqual(e004.resolved_run_id, "")
        event_types = [e["type"] for e in e004.events]
        self.assertEqual(
            event_types,
            ["opened", "resolved", "reopened"],
        )

    def test_condition_change_distinguished_from_reappearance(self):
        ledger = IssueLedger(self.tmp)
        ledger.apply_run(self.base_result)
        _, v2_rules = load_rule_set(SAMPLES / "rules_v2_leave_required.json")
        events = ledger.apply_run(evaluate(self.base_batch, v2_rules))
        changed = [
            e for e in events
            if e["type"] == "condition_changed"
        ]
        self.assertTrue(
            any(e["rule_id"] == "employment_dates_consistent" for e in changed)
        )
        # Existing findings are still_open, not reopened or new.
        e002 = next(
            s for s in ledger.states.values()
            if s.rule_id == "employment_dates_consistent"
            and s.record_id == "E002"
        )
        self.assertEqual(e002.status, "open")
        types = [e["type"] for e in e002.events]
        self.assertEqual(types, ["opened", "condition_changed", "still_open"])

    def test_failed_run_does_not_resolve_issues_in_ledger(self):
        ledger = IssueLedger(self.tmp)
        ledger.apply_run(self.base_result)
        broken = type(self.rules[2])(
            rule_id=self.rules[2].rule_id,
            rule_type=self.rules[2].rule_type,
            version_set=self.rules[2].version_set,
            fields=self.rules[2].fields,
            params={},
            depends_on=(),
        )
        rows = copy.deepcopy(self.v1_rows)
        rows[1]["leave_date"] = "2024-09-01"
        new_batch = batch_from_rows(rows, self.tmp, "snapshot2.json")
        failed = evaluate_incremental(
            new_batch, (broken,), self.base_result, self.base_batch
        )
        events = ledger.apply_run(failed, baseline=self.base_result)
        self.assertEqual(
            [e for e in events if e["type"] == "resolved"], []
        )
        e002 = next(
            s for s in ledger.open_issues()
            if s.rule_id == "employment_dates_consistent"
            and s.record_id == "E002"
        )
        self.assertEqual(e002.status, "open")
        # A failed run records no lifecycle movement at all for this issue.
        self.assertEqual(e002.events[-1]["type"], "opened")

    def test_store_roundtrip_and_latest_valid_selection(self):
        store = ResultStore(self.tmp)
        store.append(self.base_result)
        loaded = store.latest_valid_for_batch("emp")
        self.assertEqual(loaded.rule_signatures, self.base_result.rule_signatures)
        self.assertEqual(
            issue_keys(loaded), issue_keys(self.base_result)
        )
        # A failed incremental run is never picked as the new baseline.
        broken = type(self.rules[2])(
            rule_id=self.rules[2].rule_id,
            rule_type=self.rules[2].rule_type,
            version_set=self.rules[2].version_set,
            fields=self.rules[2].fields,
            params={},
            depends_on=(),
        )
        failed = evaluate_incremental(
            self.base_batch, (broken,), self.base_result, self.base_batch
        )
        store.append(failed)
        latest = store.latest_valid_for_batch("emp")
        self.assertEqual(latest.run_id, self.base_result.run_id)
        self.assertIsInstance(latest, RunResult)


if __name__ == "__main__":
    unittest.main()
