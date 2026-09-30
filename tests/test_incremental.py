import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq_platform.engine import evaluate
from dq_platform.incremental import (
    diff_batches,
    incremental_evaluate,
    plan_scopes,
)
from dq_platform.lifecycle import IssueTracker
from dq_platform.loader import load_batch, load_rule_set

SAMPLES = ROOT / "samples"


def issue_tuples(result):
    return sorted(
        (i.rule_id, i.record_id, i.field, i.kind, i.detail)
        for i in result.issues
    )


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.batch_v1 = load_batch(SAMPLES / "employees_v1.json")
        self.batch_v2 = load_batch(
            SAMPLES / "employees_v2.json", batch_id=self.batch_v1.batch_id
        )
        self.version_set, self.rules = load_rule_set(SAMPLES / "rules_v1.json")
        self.prev = evaluate(self.batch_v1, self.rules)

    def test_diff_finds_only_touched_records(self):
        changed, added, removed = diff_batches(self.batch_v1, self.batch_v2)
        self.assertEqual(set(changed), {"E002", "E004"})
        self.assertEqual(added, ())
        self.assertEqual(removed, ())

    def test_incremental_matches_full_recompute(self):
        incr = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, self.rules
        )
        full = evaluate(self.batch_v2, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))
        self.assertEqual(incr.status, full.status)
        self.assertEqual(incr.errors, ())
        self.assertEqual(incr.incomplete_rules, ())

    def test_only_affected_scopes_are_rerun(self):
        changed, added, removed = diff_batches(self.batch_v1, self.batch_v2)
        scopes = plan_scopes(self.rules, changed, added, removed)
        # Row-level rule: only the two edited records are re-judged.
        self.assertEqual(scopes["identity_present"], "records")
        # Cross-record uniqueness must be re-judged as a whole.
        self.assertEqual(scopes["identity_unique"], "full")
        self.assertEqual(scopes["employment_dates_consistent"], "records")

    def test_untouched_change_skips_unaffected_rules(self):
        # Edit only a date cell: identity rules have nothing to redo.
        data = [dict(r.data) for r in self.batch_v1.records]
        data[0]["leave_date"] = "2024-06-01"
        path = Path(tempfile.mkdtemp()) / "b.json"
        path.write_text(json.dumps(data))
        batch_b = load_batch(path, batch_id=self.batch_v1.batch_id)
        changed, added, removed = diff_batches(self.batch_v1, batch_b)
        scopes = plan_scopes(self.rules, changed, added, removed)
        self.assertEqual(scopes["identity_present"], "records")
        self.assertEqual(scopes["identity_unique"], "full")
        self.assertEqual(scopes["employment_dates_consistent"], "records")
        incr = incremental_evaluate(self.prev, self.batch_v1, batch_b, self.rules)
        full = evaluate(batch_b, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))

    def test_no_change_skips_everything(self):
        scopes = plan_scopes(self.rules, (), (), ())
        self.assertTrue(all(s == "skip" for s in scopes.values()))
        incr = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v1, self.rules
        )
        self.assertEqual(issue_tuples(incr), issue_tuples(self.prev))

    def test_version_set_change_forces_full_recompute(self):
        _, rules_v2 = load_rule_set(SAMPLES / "rules_v2.json")
        result = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, rules_v2
        )
        full = evaluate(self.batch_v2, rules_v2)
        self.assertEqual(issue_tuples(result), issue_tuples(full))

    def test_failure_keeps_previous_conclusion_and_marks_scope(self):
        broken = type(self.rules[2])(
            rule_id=self.rules[2].rule_id,
            rule_type=self.rules[2].rule_type,
            version_set=self.rules[2].version_set,
            fields=self.rules[2].fields,
            params={},
            depends_on=(),
        )
        rules = self.rules[:2] + (broken,)
        result = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, rules
        )
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.incomplete_rules,
                         ("employment_dates_consistent",))
        # Previous valid conclusion for the failed rule is kept verbatim.
        prev_date_issues = [
            i for i in self.prev.issues
            if i.rule_id == "employment_dates_consistent"
        ]
        now_date_issues = [
            i for i in result.issues
            if i.rule_id == "employment_dates_consistent"
        ]
        self.assertEqual(
            sorted(map(str, prev_date_issues)), sorted(map(str, now_date_issues))
        )
        self.assertTrue(result.errors)
        self.assertIn("NOT completed", result.errors[0])
        # Other rules still got an incremental, correct verdict.
        self.assertIn("identity_unique", result.completed_rules)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.batch_v1 = load_batch(SAMPLES / "employees_v1.json")
        self.batch_v2 = load_batch(
            SAMPLES / "employees_v2.json", batch_id=self.batch_v1.batch_id
        )
        _, self.rules = load_rule_set(SAMPLES / "rules_v1.json")
        _, self.rules_v2 = load_rule_set(SAMPLES / "rules_v2.json")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tracker = IssueTracker(self.tmp.name)

    def test_resolved_issue_keeps_record_and_resolving_version(self):
        first = evaluate(self.batch_v1, self.rules)
        self.tracker.update(first)
        second = incremental_evaluate(
            first, self.batch_v1, self.batch_v2, self.rules
        )
        transitions = self.tracker.update(second)
        # E002's contradictory dates are fixed in v2 -> resolved, not deleted.
        resolved = transitions["resolved"]
        self.assertIn(
            ("employment_dates_consistent", "E002",
             "hire_date,leave_date", "violated"),
            resolved,
        )
        events = self.tracker.events()
        opened = [e for e in events if e["event"] == "opened"
                  and e["key"][1] == "E002"
                  and e["key"][0] == "employment_dates_consistent"]
        resolved_events = [e for e in events if e["event"] == "resolved"
                           and e["key"][1] == "E002"
                           and e["key"][0] == "employment_dates_consistent"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(resolved_events), 1)
        # Original record and the resolving version are both retained.
        self.assertEqual(opened[0]["issue"]["record_id"], "E002")
        self.assertEqual(resolved_events[0]["rule_version_set"],
                         "rules-2024-v1")
        self.assertEqual(resolved_events[0]["run_id"], second.run_id)
        state = self.tracker.state()
        self.assertEqual(
            state[("employment_dates_consistent", "E002",
                   "hire_date,leave_date", "violated")]["status"],
            "resolved",
        )

    def test_reappearing_issue_is_reopened_not_new(self):
        first = evaluate(self.batch_v1, self.rules)
        self.tracker.update(first)
        second = incremental_evaluate(
            first, self.batch_v1, self.batch_v2, self.rules
        )
        self.tracker.update(second)
        third = incremental_evaluate(
            second, self.batch_v2, self.batch_v1, self.rules
        )
        transitions = self.tracker.update(third)
        self.assertIn(
            ("employment_dates_consistent", "E002",
             "hire_date,leave_date", "violated"),
            transitions["reopened"],
        )
        kinds = [e["event"] for e in self.tracker.events()
                 if e["key"][0] == "employment_dates_consistent"
                 and e["key"][1] == "E002"]
        self.assertEqual(kinds, ["opened", "resolved", "reopened"])

    def test_condition_change_distinguished_from_reopen(self):
        first = evaluate(self.batch_v1, self.rules)
        self.tracker.update(first)
        # Same data, revised judging condition (leave_date now required).
        second = evaluate(self.batch_v1, self.rules_v2)
        transitions = self.tracker.update(second)
        self.assertEqual(transitions["reopened"], [])
        changed_rules = {key[0] for key in transitions["condition_changed"]}
        self.assertIn("identity_unique", changed_rules)
        self.assertIn("employment_dates_consistent", changed_rules)

    def test_failed_run_marks_open_issues_unknown_not_resolved(self):
        first = evaluate(self.batch_v1, self.rules)
        self.tracker.update(first)
        broken = type(self.rules[2])(
            rule_id=self.rules[2].rule_id,
            rule_type=self.rules[2].rule_type,
            version_set=self.rules[2].version_set,
            fields=self.rules[2].fields,
            params={},
            depends_on=(),
        )
        rules = self.rules[:2] + (broken,)
        failed = incremental_evaluate(
            first, self.batch_v1, self.batch_v2, rules
        )
        transitions = self.tracker.update(failed)
        unknown_rules = {key[0] for key in transitions["unknown"]}
        self.assertEqual(unknown_rules, {"employment_dates_consistent"})
        # The failed rule's open issues are neither resolved nor reopened.
        self.assertEqual(
            [k for k in transitions["resolved"]
             if k[0] == "employment_dates_consistent"],
            [],
        )
        still_open = self.tracker.open_issues()
        self.assertIn(
            ("employment_dates_consistent", "E002",
             "hire_date,leave_date", "violated"),
            still_open,
        )


if __name__ == "__main__":
    unittest.main()
