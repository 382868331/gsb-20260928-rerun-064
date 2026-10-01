import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq_platform.engine import evaluate
from dq_platform.incremental import (
    build_plan,
    describe_plan,
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
        # v1 -> v2 edits only date cells on E002/E004: identity rules have
        # nothing to redo, the date rule re-judges exactly those records.
        scopes = plan_scopes(self.rules, self.batch_v1, self.batch_v2)
        self.assertEqual(scopes["identity_present"], "skip")
        self.assertEqual(scopes["identity_unique"], "skip")
        self.assertEqual(scopes["employment_dates_consistent"], "records")
        plans = {
            p.rule_id: p
            for p in build_plan(self.rules, self.batch_v1, self.batch_v2)
        }
        self.assertEqual(
            plans["employment_dates_consistent"].targets, ("E002", "E004")
        )

    def test_untouched_change_skips_unaffected_rules(self):
        # Edit only a date cell: identity rules are skipped entirely, not
        # re-labelled full runs.
        data = [dict(r.data) for r in self.batch_v1.records]
        data[0]["leave_date"] = "2024-06-01"
        path = Path(tempfile.mkdtemp()) / "b.json"
        path.write_text(json.dumps(data))
        batch_b = load_batch(path, batch_id=self.batch_v1.batch_id)
        scopes = plan_scopes(self.rules, self.batch_v1, batch_b)
        self.assertEqual(scopes["identity_present"], "skip")
        self.assertEqual(scopes["identity_unique"], "skip")
        self.assertEqual(scopes["employment_dates_consistent"], "records")
        incr = incremental_evaluate(self.prev, self.batch_v1, batch_b, self.rules)
        full = evaluate(batch_b, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))

    def _batch_with(self, mutate):
        data = [dict(r.data) for r in self.batch_v1.records]
        mutate(data)
        path = Path(tempfile.mkdtemp()) / "b.json"
        path.write_text(json.dumps(data))
        return load_batch(path, batch_id=self.batch_v1.batch_id)

    def test_identity_change_limited_to_affected_groups(self):
        # E001 gets a fresh unique id_card: only its own group is re-judged.
        batch_b = self._batch_with(
            lambda d: d[0].update(id_card="999999999999999999")
        )
        plans = {
            p.rule_id: p
            for p in build_plan(self.rules, self.batch_v1, batch_b)
        }
        plan = plans["identity_unique"]
        self.assertEqual(plan.scope, "groups")
        self.assertEqual(plan.targets, ("E001",))
        # The untouched duplicate group {E003, E004} is NOT re-scanned.
        self.assertNotIn("E003", plan.targets)
        self.assertNotIn("E004", plan.targets)
        incr = incremental_evaluate(self.prev, self.batch_v1, batch_b, self.rules)
        full = evaluate(batch_b, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))

    def test_identity_change_into_existing_group_finds_conflict(self):
        # E001 takes E002's id_card: the conflict must be found even though
        # only two records are re-judged.
        batch_b = self._batch_with(
            lambda d: d[0].update(id_card=d[1]["id_card"])
        )
        plans = {
            p.rule_id: p
            for p in build_plan(self.rules, self.batch_v1, batch_b)
        }
        self.assertEqual(plans["identity_unique"].scope, "groups")
        self.assertEqual(
            set(plans["identity_unique"].targets), {"E001", "E002"}
        )
        incr = incremental_evaluate(self.prev, self.batch_v1, batch_b, self.rules)
        full = evaluate(batch_b, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))
        dupes = {
            i.record_id for i in incr.issues
            if i.rule_id == "identity_unique" and i.kind == "violated"
        }
        self.assertIn("E001", dupes)
        self.assertIn("E002", dupes)

    def test_removed_record_shrinks_only_its_group(self):
        # Removing E004 dissolves the {E003, E004} duplicate: E003's carried
        # issue must be dropped without re-scanning unrelated groups.
        batch_b = self._batch_with(lambda d: d.pop(3))
        plans = {
            p.rule_id: p
            for p in build_plan(self.rules, self.batch_v1, batch_b)
        }
        plan = plans["identity_unique"]
        self.assertEqual(plan.scope, "groups")
        self.assertEqual(set(plan.targets), {"E003"})
        incr = incremental_evaluate(self.prev, self.batch_v1, batch_b, self.rules)
        full = evaluate(batch_b, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))
        self.assertEqual(
            [i for i in incr.issues
             if i.rule_id == "identity_unique" and i.kind == "violated"],
            [],
        )

    def test_plan_explains_each_change_kind(self):
        batch_b = self._batch_with(
            lambda d: d[0].update(id_card="999999999999999999")
        )
        plans = build_plan(self.rules, self.batch_v1, batch_b)
        lines = chr(10).join(describe_plan(plans, prev_result=self.prev))
        self.assertIn("identity_unique", lines)
        self.assertIn("modified relevant fields: E001 (id_card)", lines)
        self.assertIn("affected identity groups", lines)
        self.assertIn("was: completed", lines)
        # Added/removed/rule-change reasons appear when applicable.
        added = self._batch_with(
            lambda d: d.append({"id": "E006", "id_card": "1"})
        )
        add_plans = build_plan(self.rules, self.batch_v1, added)
        add_text = chr(10).join(
            r for p in add_plans for r in p.reasons
        )
        self.assertIn("added records: E006", add_text)
        removed = self._batch_with(lambda d: d.pop(0))
        rm_plans = build_plan(self.rules, self.batch_v1, removed)
        rm_text = chr(10).join(r for p in rm_plans for r in p.reasons)
        self.assertIn("removed records: E001", rm_text)
        _, rules_v2 = load_rule_set(SAMPLES / "rules_v2.json")
        ch_plans = {
            p.rule_id: p
            for p in build_plan(
                rules_v2, self.batch_v1, self.batch_v2, prev_rules=self.rules
            )
        }
        # Only the rule whose definition actually changed goes full.
        self.assertEqual(
            ch_plans["employment_dates_consistent"].scope, "full"
        )
        self.assertIn(
            "rule definition changed",
            ch_plans["employment_dates_consistent"].reasons[0],
        )

    def test_no_change_skips_everything(self):
        scopes = plan_scopes(self.rules, self.batch_v1, self.batch_v1)
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


    def _broken_date_rule(self):
        return type(self.rules[2])(
            rule_id=self.rules[2].rule_id,
            rule_type=self.rules[2].rule_type,
            version_set=self.rules[2].version_set,
            fields=self.rules[2].fields,
            params={},
            depends_on=(),
        )

    def test_recovery_rechecks_previously_failed_scope(self):
        first = evaluate(self.batch_v1, self.rules)
        rules_broken = self.rules[:2] + (self._broken_date_rule(),)
        failed = incremental_evaluate(
            first, self.batch_v1, self.batch_v2, rules_broken
        )
        self.assertEqual(failed.incomplete_rules,
                         ("employment_dates_consistent",))
        # Recovery run: no data changed since the failed run, yet the
        # previously incomplete rule must be re-checked in full -- its kept
        # conclusion is stale and must not count as this run's verdict.
        recovered = incremental_evaluate(
            failed, self.batch_v2, self.batch_v2, self.rules
        )
        full = evaluate(self.batch_v2, self.rules)
        self.assertEqual(issue_tuples(recovered), issue_tuples(full))
        self.assertEqual(recovered.status, full.status)
        self.assertEqual(recovered.errors, ())
        self.assertEqual(recovered.incomplete_rules, ())
        self.assertIn("employment_dates_consistent", recovered.completed_rules)
        plan = {entry["rule_id"]: entry for entry in recovered.plan}
        self.assertEqual(
            plan["employment_dates_consistent"]["scope"], "full"
        )
        # The stale carried issue from the failed run is gone after recovery.
        self.assertEqual(
            [i for i in recovered.issues
             if i.rule_id == "employment_dates_consistent"],
            [i for i in full.issues
             if i.rule_id == "employment_dates_consistent"],
        )

    def test_no_change_after_failure_is_not_a_pass(self):
        # A no-op incremental run on top of a failed run must not inherit
        # "everything is fine" from the stale kept conclusions.
        clean = load_batch(SAMPLES / "clean.json")
        first = evaluate(clean, self.rules)
        self.assertEqual(first.status, "PASSED")
        # A data edit makes the (broken) date rule actually run and fail.
        data = [dict(r.data) for r in clean.records]
        data[0]["leave_date"] = "2024-11-30"
        path = Path(tempfile.mkdtemp()) / "clean2.json"
        path.write_text(json.dumps(data))
        clean2 = load_batch(path, batch_id=clean.batch_id)
        rules_broken = self.rules[:2] + (self._broken_date_rule(),)
        failed = incremental_evaluate(first, clean, clean2, rules_broken)
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.incomplete_rules,
                         ("employment_dates_consistent",))
        # No further data change: the recovery run must still re-check the
        # previously failed rule instead of inheriting a stale pass/fail.
        recovered = incremental_evaluate(failed, clean2, clean2, self.rules)
        # Only now, after the rule actually ran, may the run pass.
        self.assertEqual(recovered.status, "PASSED")
        self.assertIn("employment_dates_consistent", recovered.completed_rules)


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


    def test_recovery_resolves_once_without_double_counting(self):
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
        rules_broken = self.rules[:2] + (broken,)
        failed = incremental_evaluate(
            first, self.batch_v1, self.batch_v2, rules_broken
        )
        self.tracker.update(failed)
        # Recovery: the date rule runs again and E002's fix is real now.
        recovered = incremental_evaluate(
            failed, self.batch_v2, self.batch_v2, self.rules
        )
        transitions = self.tracker.update(recovered)
        key = ("employment_dates_consistent", "E002",
               "hire_date,leave_date", "violated")
        self.assertIn(key, transitions["resolved"])
        # A further no-change run must not resolve (or open) it again.
        again = incremental_evaluate(
            recovered, self.batch_v2, self.batch_v2, self.rules
        )
        transitions2 = self.tracker.update(again)
        self.assertEqual(transitions2["resolved"], [])
        self.assertEqual(transitions2["opened"], [])
        kinds = [e["event"] for e in self.tracker.events()
                 if tuple(e["key"]) == key]
        self.assertEqual(kinds, ["opened", "resolved"])


if __name__ == "__main__":
    unittest.main()
