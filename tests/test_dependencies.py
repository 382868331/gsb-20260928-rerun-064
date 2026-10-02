import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq_platform.engine import DependencyError, evaluate
from dq_platform import engine
from dq_platform.impact import describe_impact, preview_impact
from dq_platform.incremental import build_plan, incremental_evaluate
from dq_platform.lifecycle import IssueTracker
from dq_platform.loader import load_batch, load_rule_set
from dq_platform.models import RuleDef

SAMPLES = ROOT / "samples"


def issue_tuples(result):
    return sorted(
        (i.rule_id, i.record_id, i.field, i.kind, i.detail)
        for i in result.issues
    )


def batch_from(data, batch_id, name="b.json"):
    path = Path(tempfile.mkdtemp()) / name
    path.write_text(json.dumps(data))
    return load_batch(path, batch_id=batch_id)


class GroupSumTests(unittest.TestCase):
    def setUp(self):
        self.batch_v1 = load_batch(SAMPLES / "payments_v1.json")
        self.batch_v2 = load_batch(
            SAMPLES / "payments_v2.json", batch_id=self.batch_v1.batch_id
        )
        self.version_set, self.rules = load_rule_set(SAMPLES / "rules_deps.json")
        self.prev = evaluate(self.batch_v1, self.rules)

    def test_group_sum_uses_confirmed_groups_only(self):
        # P005/P006 share txn_ref T005: unconfirmed identity, so their A3
        # amounts never enter a group total.  A1 (600+500) breaches the cap.
        result = self.prev
        self.assertEqual(result.status, "FAILED")
        cap_issues = [
            i for i in result.issues if i.rule_id == "account_total_cap"
        ]
        self.assertEqual(
            sorted(i.record_id for i in cap_issues), ["P001", "P002"]
        )
        self.assertIn("total 1100 exceeds max_total 1000",
                      cap_issues[0].detail)
        # No group-sum issue references the unconfirmed A3 records.
        self.assertEqual(
            [i for i in cap_issues if i.record_id in ("P005", "P006")], []
        )

    def test_group_sum_missing_amount_not_treated_as_zero(self):
        data = [dict(r.data) for r in self.batch_v1.records]
        data[2]["amount"] = None
        batch = batch_from(data, self.batch_v1.batch_id)
        result = evaluate(batch, self.rules)
        kinds = {
            (i.record_id, i.kind)
            for i in result.issues if i.rule_id == "account_total_cap"
        }
        self.assertIn(("P003", "missing_value"), kinds)

    def test_incremental_with_dependency_matches_full_recompute(self):
        incr = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, self.rules
        )
        full = evaluate(self.batch_v2, self.rules)
        self.assertEqual(issue_tuples(incr), issue_tuples(full))
        self.assertEqual(incr.status, full.status)
        self.assertEqual(incr.errors, ())
        self.assertEqual(incr.incomplete_rules, ())
        # The downstream rule re-judged exactly the groups touched by the
        # amount edit (A1) and the identity split (A3/A4), nothing else.
        plan = {p.rule_id: p for p in
                build_plan(self.rules, self.batch_v1, self.batch_v2)}
        self.assertEqual(plan["account_total_cap"].scope, "groups")
        self.assertEqual(
            set(plan["account_total_cap"].targets),
            {"P001", "P002", "P005", "P006"},
        )
        self.assertEqual(plan["payment_dates"].scope, "skip")

    def test_identity_split_then_remerge_matches_full_recompute(self):
        # Split the duplicate identity, then merge it back: each step must
        # agree with a complete recompute of the same data.
        split = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, self.rules
        )
        self.assertEqual(
            issue_tuples(split), issue_tuples(evaluate(self.batch_v2, self.rules))
        )
        remerged = incremental_evaluate(
            split, self.batch_v2, self.batch_v1, self.rules
        )
        self.assertEqual(
            issue_tuples(remerged),
            issue_tuples(evaluate(self.batch_v1, self.rules)),
        )
        self.assertEqual(remerged.status, "FAILED")
        dup = {
            i.record_id for i in remerged.issues
            if i.rule_id == "payment_identity" and i.kind == "violated"
        }
        self.assertEqual(dup, {"P005", "P006"})


class UpstreamInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.batch_v1 = load_batch(SAMPLES / "payments_v1.json")
        self.batch_v2 = load_batch(
            SAMPLES / "payments_v2.json", batch_id=self.batch_v1.batch_id
        )
        _, self.rules = load_rule_set(SAMPLES / "rules_deps.json")
        self.prev = evaluate(self.batch_v1, self.rules)

    def _broken_identity_rule(self):
        def _boom(rule, records):
            raise RuntimeError("identity store unreachable")
        return mock.patch.dict(
            engine.CHECKS, {"unique_fields": _boom}
        )

    def test_upstream_failure_blocks_only_overlapping_groups(self):
        # Only the identity split (P006 txn_ref) changed, so the broken
        # identity rule's planned scope is exactly the T005/T006 groups.
        data = [dict(r.data) for r in self.batch_v1.records]
        data[5]["txn_ref"] = "T006"
        batch_b = batch_from(data, self.batch_v1.batch_id)
        with self._broken_identity_rule():
            result = incremental_evaluate(
                self.prev, self.batch_v1, batch_b, self.rules
            )
        self.assertEqual(result.status, "FAILED")
        # Upstream identity rule failed; the unrelated date rule finished.
        self.assertIn("payment_identity", result.incomplete_rules)
        self.assertIn("payment_dates", result.completed_rules)
        # Downstream: only groups overlapping the unconfirmed identity
        # scope (A3/A4 via P005/P006) are blocked; the A1 re-check from
        # the amount edit still ran and its verdict is current.
        self.assertIn("account_total_cap", result.incomplete_rules)
        blocked = {
            entry["rule_id"]: entry.get("blocked", [])
            for entry in result.plan
        }
        self.assertEqual(blocked["account_total_cap"], ["P005", "P006"])
        self.assertEqual(blocked["payment_identity"], [])
        # The still-reusable part (A1 over the cap) keeps its previous
        # conclusion verbatim; the blocked scope's error says exactly
        # what was not re-confirmed.
        self.assertEqual(
            sorted(
                i.record_id for i in result.issues
                if i.rule_id == "account_total_cap"
            ),
            ["P001", "P002"],
        )
        messages = " ".join(result.errors)
        self.assertIn("NOT re-confirmed", messages)
        self.assertIn("P005", messages)
        self.assertIn("P006", messages)

    def test_blocked_run_never_reports_pass(self):
        # Clean data, but upstream broken: carried conclusions must not
        # add up to "all passed".
        clean = [
            dict(r.data) for r in self.batch_v2.records
        ]
        clean_batch = batch_from(clean, self.batch_v1.batch_id)
        first = evaluate(clean_batch, self.rules)
        self.assertEqual(first.status, "PASSED")
        # Touch an identity cell so the broken rule actually runs.
        edited = [dict(r) for r in clean]
        edited[5]["txn_ref"] = "T006X"
        edited_batch = batch_from(edited, self.batch_v1.batch_id)
        with self._broken_identity_rule():
            result = incremental_evaluate(
                first, clean_batch, edited_batch, self.rules
            )
        self.assertEqual(result.status, "FAILED")
        self.assertIn("payment_identity", result.incomplete_rules)
        self.assertIn("account_total_cap", result.incomplete_rules)

    def test_resume_rechecks_unconfirmed_scope_and_counts_once(self):
        tracker_dir = tempfile.mkdtemp()
        tracker = IssueTracker(tracker_dir)
        tracker.update(self.prev)
        with self._broken_identity_rule():
            failed = incremental_evaluate(
                self.prev, self.batch_v1, self.batch_v2, self.rules
            )
        transitions = tracker.update(failed)
        # The A1 cap issue is NOT resolved while its group is unconfirmed.
        cap_key = ("account_total_cap", "P001", "amount", "violated")
        self.assertNotIn(cap_key, transitions["resolved"])
        self.assertIn(cap_key, transitions["unknown"])

        # Resume with fixed rules and no further data change: the
        # unconfirmed scope is re-checked in full and matches recompute.
        recovered = incremental_evaluate(
            failed, self.batch_v2, self.batch_v2, self.rules
        )
        full = evaluate(self.batch_v2, self.rules)
        self.assertEqual(issue_tuples(recovered), issue_tuples(full))
        self.assertEqual(recovered.status, "PASSED")
        self.assertEqual(recovered.incomplete_rules, ())
        plan = {entry["rule_id"]: entry for entry in recovered.plan}
        self.assertEqual(plan["account_total_cap"]["scope"], "full")
        self.assertEqual(plan["payment_identity"]["scope"], "full")

        transitions = tracker.update(recovered)
        # The original issue identity is resolved exactly once.
        self.assertIn(cap_key, transitions["resolved"])
        again = incremental_evaluate(
            recovered, self.batch_v2, self.batch_v2, self.rules
        )
        transitions2 = tracker.update(again)
        self.assertEqual(transitions2["resolved"], [])
        kinds = [e["event"] for e in tracker.events()
                 if tuple(e["key"]) == cap_key]
        self.assertEqual(kinds, ["opened", "resolved"])


class UpstreamVersionTraceTests(unittest.TestCase):
    def setUp(self):
        self.batch_v1 = load_batch(SAMPLES / "payments_v1.json")
        self.batch_v2 = load_batch(
            SAMPLES / "payments_v2.json", batch_id=self.batch_v1.batch_id
        )
        _, self.rules = load_rule_set(SAMPLES / "rules_deps.json")
        self.prev = evaluate(self.batch_v1, self.rules)

    def test_downstream_conclusion_traces_upstream_version(self):
        # Only an amount edit: identity is carried, the downstream rule
        # must record that it used the previous identity run.
        data = [dict(r.data) for r in self.batch_v1.records]
        data[1]["amount"] = 300
        batch_b = batch_from(data, self.batch_v1.batch_id)
        result = incremental_evaluate(
            self.prev, self.batch_v1, batch_b, self.rules
        )
        entry = {e["rule_id"]: e for e in result.plan}["account_total_cap"]
        upstream = entry["upstream"]["payment_identity"]
        self.assertEqual(upstream["use"], "carried")
        self.assertEqual(upstream["run_id"], self.prev.run_id)

    def test_rejudged_upstream_traces_current_run(self):
        result = incremental_evaluate(
            self.prev, self.batch_v1, self.batch_v2, self.rules
        )
        entry = {e["rule_id"]: e for e in result.plan}["account_total_cap"]
        upstream = entry["upstream"]["payment_identity"]
        self.assertEqual(upstream["use"], "current")
        self.assertEqual(upstream["run_id"], result.run_id)


class ImpactPreviewTests(unittest.TestCase):
    def setUp(self):
        self.batch_v1 = load_batch(SAMPLES / "payments_v1.json")
        self.batch_v2 = load_batch(
            SAMPLES / "payments_v2.json", batch_id=self.batch_v1.batch_id
        )
        _, self.rules = load_rule_set(SAMPLES / "rules_deps.json")

    def test_field_change_propagates_along_dependencies(self):
        preview = preview_impact(self.rules, changed_fields=["txn_ref"])
        self.assertEqual(preview.direct, ("payment_identity",))
        self.assertEqual(
            preview.affected, ("payment_identity", "account_total_cap")
        )
        self.assertNotIn("payment_dates", preview.affected)

    def test_amount_change_hits_downstream_only(self):
        preview = preview_impact(self.rules, changed_fields=["amount"])
        self.assertEqual(preview.affected, ("account_total_cap",))

    def test_rule_change_propagates_downstream(self):
        preview = preview_impact(self.rules, changed_rules=["payment_identity"])
        self.assertEqual(
            preview.affected, ("payment_identity", "account_total_cap")
        )

    def test_multiple_upstream_changes_count_scope_once(self):
        # txn_ref and amount both changed on the same records: the shared
        # downstream scope is the deduplicated union, never double-counted.
        preview = preview_impact(
            self.rules,
            changed_fields=["txn_ref", "amount"],
            old_batch=self.batch_v1,
            new_batch=self.batch_v2,
        )
        records = preview.records["account_total_cap"]
        self.assertEqual(records, tuple(sorted(set(records))))
        self.assertEqual(records, ("P002", "P006"))
        lines = "\n".join(describe_impact(preview))
        self.assertIn("account_total_cap", lines)

    def test_cycle_and_missing_dependency_have_clear_feedback(self):
        cyclic = (
            RuleDef("a", "required_fields", "v", fields=("x",),
                    depends_on=("b",)),
            RuleDef("b", "required_fields", "v", fields=("y",),
                    depends_on=("a",)),
        )
        with self.assertRaises(DependencyError) as ctx:
            preview_impact(cyclic, changed_fields=["x"])
        self.assertIn("dependency cycle", str(ctx.exception))

        missing = (
            RuleDef("a", "required_fields", "v", fields=("x",),
                    depends_on=("ghost",)),
        )
        with self.assertRaises(DependencyError) as ctx:
            preview_impact(missing, changed_fields=["x"])
        self.assertIn("depends on missing rule", str(ctx.exception))

        with self.assertRaises(DependencyError) as ctx:
            preview_impact(self.rules, changed_rules=["ghost"])
        self.assertIn("changed rule(s) not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
