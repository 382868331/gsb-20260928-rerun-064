import json
import copy
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq_platform.engine import evaluate
from dq_platform.incremental import (
    build_plan,
    compare_plans,
    diff_records,
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
        plans = build_plan(self.rules, self.batch_v1, self.batch_v2)
        by_id = {p.rule_id: p for p in plans}
        # Row-level rule: only the two edited records are re-judged.
        # Neither edit touches id/id_card, so the required-field rule
        # has nothing to redo either.
        self.assertEqual(by_id["identity_present"].scope, "skip")
        # Identity did not change in v2: duplicate groups are untouched,
        # so uniqueness is NOT scheduled as a disguised full run.
        self.assertEqual(by_id["identity_unique"].scope, "skip")
        self.assertEqual(by_id["employment_dates_consistent"].scope,
                         "records")
        self.assertEqual(
            set(by_id["employment_dates_consistent"].affected_record_ids),
            {"E002", "E004"},
        )

    def test_date_only_change_skips_identity_rules(self):
        # Edit only a date cell: identity rules have nothing to redo,
        # including the cross-record uniqueness rule.
        data = [dict(r.data) for r in self.batch_v1.records]
        data[0]["leave_date"] = "2024-06-01"
        path = Path(tempfile.mkdtemp()) / "b.json"
        path.write_text(json.dumps(data))
        batch_b = load_batch(path, batch_id=self.batch_v1.batch_id)
        plans = build_plan(self.rules, self.batch_v1, batch_b)
        by_id = {p.rule_id: p for p in plans}
        self.assertEqual(by_id["identity_present"].scope, "skip")
        self.assertEqual(by_id["identity_unique"].scope, "skip")
        self.assertEqual(by_id["employment_dates_consistent"].scope,
                         "records")
        self.assertEqual(
            list(by_id["employment_dates_consistent"].affected_record_ids),
            ["E001"],
        )
        incr = incremental_evaluate(self.prev, self.batch_v1, batch_b, self.rules)
        full = evaluate(batch_b, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))

    def test_identity_change_limited_to_affected_groups(self):
        # E003/E004 share an id_card; move only E002 into a new value that
        # collides with E001. Only those two groups may be re-judged, and
        # the result must still equal a full recomputation (the cross-row
        # E001<->E002 conflict must not be missed).
        data = [dict(r.data) for r in self.batch_v1.records]
        data[1]["id_card"] = data[0]["id_card"]
        path = Path(tempfile.mkdtemp()) / "b.json"
        path.write_text(json.dumps(data))
        batch_b = load_batch(path, batch_id=self.batch_v1.batch_id)
        plans = build_plan(self.rules, self.batch_v1, batch_b)
        unique = next(p for p in plans if p.rule_id == "identity_unique")
        self.assertEqual(unique.scope, "groups")
        self.assertEqual(
            set(unique.affected_record_ids), {"E001", "E002"}
        )
        incr = incremental_evaluate(
            self.prev, self.batch_v1, batch_b, self.rules
        )
        full = evaluate(batch_b, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))
        # E003/E004 verdict is carried over, not recomputed.
        self.assertEqual(incr.incomplete_rules, ())

    def test_diff_records_names_changed_fields(self):
        old = {r.record_id: r for r in self.batch_v1.records}
        new = {r.record_id: r for r in self.batch_v2.records}
        self.assertEqual(diff_records(old["E002"], new["E002"]),
                         ("leave_date",))
        self.assertEqual(
            set(diff_records(old["E004"], new["E004"])),
            {"hire_date"},
        )

    def test_added_and_removed_records_affect_their_identity_group(self):
        data = [dict(r.data) for r in self.batch_v1.records
                if r.record_id != "E004"]
        data.append({
            "id": "E006", "id_card": data[0]["id_card"],
            "hire_date": "2024-09-01", "leave_date": None,
        })
        path = Path(tempfile.mkdtemp()) / "b.json"
        path.write_text(json.dumps(data))
        batch_b = load_batch(path, batch_id=self.batch_v1.batch_id)
        plans = build_plan(self.rules, self.batch_v1, batch_b)
        unique = next(p for p in plans if p.rule_id == "identity_unique")
        self.assertEqual(unique.scope, "groups")
        # Removed E004 frees the E003/E004 group (E003 alone -> no issue);
        # added E006 collides with E001 (both re-judged).
        self.assertEqual(set(unique.affected_record_ids),
                         {"E001", "E003", "E004", "E006"})
        incr = incremental_evaluate(
            self.prev, self.batch_v1, batch_b, self.rules
        )
        full = evaluate(batch_b, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))

    def test_plan_comparison_classifies_change_kinds(self):
        first = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, self.rules
        )
        modified = type(self.rules[2])(
            rule_id=self.rules[2].rule_id,
            rule_type=self.rules[2].rule_type,
            version_set=self.rules[2].version_set,
            fields=self.rules[2].fields,
            params={
                "early_field": "hire_date",
                "late_field": "leave_date",
            },
            depends_on=(),
        )
        rules_mod = self.rules[:2] + (modified,)
        baseline = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, self.rules
        )
        second = incremental_evaluate(
            baseline, self.batch_v2, self.batch_v2, rules_mod,
            prev_rules=self.rules,
        )
        report = compare_plans(second.previous_check_plan, second.check_plan)
        changed = {p.rule_id for p in report["rule_changed"]}
        self.assertIn("employment_dates_consistent", changed)
        changed_plan = next(
            p for p in second.check_plan
            if p.rule_id == "employment_dates_consistent"
        )
        self.assertEqual(changed_plan.scope, "full")
        skipped = {p.rule_id for p in report["skipped"]}
        self.assertEqual(skipped, {"identity_present", "identity_unique"})

    def test_randomized_incremental_always_matches_full_recompute(self):
        rng = random.Random(42)
        id_cards = [
            "110101199001011234", "110101199002022345",
            "110101199003033456", "110101199004044567",
            None, "", "not-a-date",
        ]
        dates = ["2024-01-01", "2024-06-01", None, "", "bad",
                 "2023-12-31", "2025-01-01"]
        base_data = [dict(r.data) for r in self.batch_v1.records]
        prev_result = self.prev
        old_batch = self.batch_v1
        for step in range(60):
            data = copy.deepcopy(base_data)
            for _ in range(rng.randint(1, 4)):
                if not data:
                    break
                row = data[rng.randrange(len(data))]
                field = rng.choice(["id_card", "hire_date", "leave_date",
                                    "id"])
                if field == "id_card":
                    row["id_card"] = rng.choice(id_cards)
                elif field in ("hire_date", "leave_date"):
                    row[field] = rng.choice(dates)
            if rng.random() < 0.3:
                data.append({
                    "id": f"E{len(data)+1:03d}",
                    "id_card": rng.choice(id_cards),
                    "hire_date": rng.choice(dates),
                    "leave_date": rng.choice(dates),
                })
            if rng.random() < 0.2 and len(data) > 2:
                del data[rng.randrange(len(data))]
            path = Path(tempfile.mkdtemp()) / f"step{step}.json"
            path.write_text(json.dumps(data))
            new_batch = load_batch(path, batch_id=self.batch_v1.batch_id)
            incr = incremental_evaluate(
                prev_result, old_batch, new_batch, self.rules,
                prev_rules=self.rules,
            )
            full = evaluate(new_batch, self.rules)
            self.assertEqual(
                issue_tuples(incr), issue_tuples(full),
                msg=f"step {step}: {data}",
            )
            self.assertEqual(incr.status, full.status, msg=f"step {step}")
            prev_result = full
            old_batch = new_batch
        first = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, self.rules
        )
        # Same version set but a changed rule definition (leave_date now
        # required): the changed rule goes full; unchanged rules skip.
        modified = type(self.rules[2])(
            rule_id=self.rules[2].rule_id,
            rule_type=self.rules[2].rule_type,
            version_set=self.rules[2].version_set,
            fields=self.rules[2].fields,
            params={
                "early_field": "hire_date",
                "late_field": "leave_date",
            },
            depends_on=(),
        )
        rules_mod = self.rules[:2] + (modified,)
        # Establish the normal plan first, then a no-data run with the
        # modified rule set: the definition change is the only reason.
        baseline = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, self.rules
        )
        second = incremental_evaluate(
            baseline, self.batch_v2, self.batch_v2, rules_mod,
            prev_rules=self.rules,
        )
        report = compare_plans(second.previous_check_plan, second.check_plan)
        changed = {p.rule_id for p in report["rule_changed"]}
        self.assertIn("employment_dates_consistent", changed)
        changed_plan = next(
            p for p in second.check_plan
            if p.rule_id == "employment_dates_consistent"
        )
        self.assertEqual(changed_plan.scope, "full")
        skipped = {p.rule_id for p in report["skipped"]}
        self.assertEqual(skipped, {"identity_present", "identity_unique"})

    def test_no_change_skips_everything(self):
        scopes = plan_scopes(
            self.rules, (), (), (),
            old_batch=self.batch_v1, new_batch=self.batch_v1,
        )
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

    def test_recovery_rechecks_only_unfinished_scope_once(self):
        broken = type(self.rules[2])(
            rule_id=self.rules[2].rule_id,
            rule_type=self.rules[2].rule_type,
            version_set=self.rules[2].version_set,
            fields=self.rules[2].fields,
            params={},
            depends_on=(),
        )
        broken_rules = self.rules[:2] + (broken,)
        failed = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, broken_rules
        )
        self.assertEqual(failed.incomplete_rules,
                         ("employment_dates_consistent",))
        # Recovery run with the rule fixed: the prior failure left a full
        # scope unfinished, so recovery completes exactly that scope.
        recovered = incremental_evaluate(
            failed, self.batch_v1, self.batch_v2, self.rules,
        )
        # The failed run scheduled the broken rule as a full re-check, so
        # recovery finishes that exact full scope (not a disguised one).
        plan = next(
            p for p in recovered.check_plan
            if p.rule_id == "employment_dates_consistent"
        )
        self.assertEqual(plan.scope, "full")
        self.assertEqual(plan.reason, "retry_unfinished_full")
        full = evaluate(self.batch_v2, self.rules)
        self.assertEqual(issue_tuples(recovered), issue_tuples(full))
        self.assertEqual(recovered.incomplete_rules, ())
        # A further run with no change skips the rule: the old failure is
        # neither carried as a failure nor re-counted as new work.
        settled = incremental_evaluate(
            recovered, self.batch_v2, self.batch_v2, self.rules,
            prev_rules=self.rules,
        )
        self.assertEqual(settled.incomplete_rules, ())
        date_plan = next(
            p for p in settled.check_plan
            if p.rule_id == "employment_dates_consistent"
        )
        self.assertEqual(date_plan.scope, "skip")
        self.assertEqual(issue_tuples(settled), issue_tuples(full))


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
