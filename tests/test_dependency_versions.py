import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq_platform.engine import CHECKS, evaluate
from dq_platform.incremental import (
    dependency_impact,
    incremental_evaluate,
    trace_basis_versions,
)
from dq_platform.lifecycle import IssueTracker
from dq_platform.loader import load_batch
from dq_platform.models import Record, RuleDef


def batch(records, name="employees"):
    from dq_platform.models import Batch, utc_now
    return Batch(
        batch_id=name,
        source="memory",
        loaded_at=utc_now(),
        records=tuple(
            Record(record_id=str(row.get("id", index + 1)), index=index, data=row)
            for index, row in enumerate(records)
        ),
    )


V1 = [
    {"id": "E1", "id_card": "A", "amount": 50, "hire_date": "2024-01-01"},
    {"id": "E2", "id_card": "A", "amount": 60, "hire_date": "2024-01-02"},
    {"id": "E3", "id_card": "C", "amount": 200, "hire_date": "2024-01-03"},
]


def split_rows():
    rows = [dict(row) for row in V1]
    rows[1]["id_card"] = "B"
    return rows


def merge_rows():
    rows = split_rows()
    rows[1]["id_card"] = "A"
    return rows


def rules():
    version = "dependency-v1"
    return (
        RuleDef("identity_present", "required_fields", version,
                fields=("id", "id_card")),
        RuleDef("identity_unique", "unique_fields", version,
                params={"key_fields": ["id_card"]},
                depends_on=("identity_present",)),
        RuleDef("amount_by_identity", "grouped_sum", version,
                params={"group_fields": ["id_card"],
                        "amount_field": "amount", "max_total": 100},
                depends_on=("identity_unique",)),
        RuleDef("hire_present", "required_fields", version,
                fields=("id", "hire_date")),
    )


def issue_map(result):
    return {
        (issue.rule_id, issue.record_id): issue
        for issue in result.issues
    }


def semantic_issues(result):
    return set(
        (
            issue.rule_id,
            issue.rule_version_set,
            issue.record_id,
            issue.field,
            issue.kind,
            issue.detail,
        )
        for issue in result.issues
    )


class DependencyVersionTests(unittest.TestCase):
    def test_split_and_merge_match_full_recompute_and_trace_versions(self):
        first_batch = batch(V1)
        split_batch = batch(split_rows())
        merged_batch = batch(merge_rows())
        rule_set = rules()

        first = evaluate(first_batch, rule_set)
        split = incremental_evaluate(
            first, first_batch, split_batch, rule_set
        )
        merged = incremental_evaluate(
            split, split_batch, merged_batch, rule_set
        )

        self.assertEqual(
            semantic_issues(split),
            semantic_issues(evaluate(split_batch, rule_set)),
        )
        self.assertEqual(
            semantic_issues(merged),
            semantic_issues(evaluate(merged_batch, rule_set)),
        )
        self.assertIn(("amount_by_identity", "E3"), issue_map(split))
        self.assertNotIn(("amount_by_identity", "E1"), issue_map(split))
        self.assertNotIn(("amount_by_identity", "E2"), issue_map(split))
        self.assertNotIn(("amount_by_identity", "E1"), issue_map(merged))
        self.assertNotIn(("amount_by_identity", "E2"), issue_map(merged))
        self.assertIn(("identity_unique", "E1"), issue_map(merged))
        self.assertIn(("identity_unique", "E2"), issue_map(merged))

        sum_conclusions = [
            c for c in split.conclusions if c["rule_id"] == "amount_by_identity"
        ]
        self.assertEqual(
            {c["status"] for c in sum_conclusions}, {"confirmed"}
        )
        traced = trace_basis_versions(
            split, "amount_by_identity", {"E1", "E2"}
        )
        self.assertEqual(len(traced), 2)
        identity_versions = set()
        for item in traced.values():
            self.assertIn("identity_unique", item["basis_versions"])
            identity_versions.update(item["basis_versions"]["identity_unique"])
        self.assertEqual(len(identity_versions), 2)

    def test_upstream_interruption_blocks_reconfirmation_but_keeps_unrelated_scope(self):
        first_batch = batch(V1)
        split_batch = batch(split_rows())
        rule_set = rules()
        first = evaluate(first_batch, rule_set)

        original = CHECKS["required_fields"]

        def fail_identity(rule, records):
            if rule.rule_id == "identity_present":
                raise RuntimeError("identity service unavailable")
            return original(rule, records)

        CHECKS["required_fields"] = fail_identity
        try:
            interrupted = incremental_evaluate(
                first, first_batch, split_batch, rule_set
            )
        finally:
            CHECKS["required_fields"] = original

        self.assertEqual(interrupted.status, "FAILED")
        self.assertIn("identity_present", interrupted.incomplete_rules)
        self.assertIn("identity_unique", interrupted.incomplete_rules)
        self.assertIn("amount_by_identity", interrupted.incomplete_rules)
        self.assertNotIn("hire_present", interrupted.incomplete_rules)
        self.assertIn("hire_present", interrupted.completed_rules)
        blocked_sum = {
            tuple(c["records"])
            for c in interrupted.conclusions
            if c["rule_id"] == "amount_by_identity"
            and c["status"] == "blocked"
        }
        self.assertTrue({("E1",), ("E2",)} & blocked_sum)
        confirmed_c = [
            c for c in interrupted.conclusions
            if c["rule_id"] == "amount_by_identity"
            and c["records"] == ["E3"]
        ]
        self.assertEqual(confirmed_c[0]["status"], "confirmed")
        self.assertTrue(any(
            "upstream basis is unavailable" in error
            for error in interrupted.errors
        ))

        recovered = incremental_evaluate(
            interrupted, split_batch, split_batch, rule_set
        )
        self.assertEqual(
            semantic_issues(recovered),
            semantic_issues(evaluate(split_batch, rule_set)),
        )
        self.assertEqual(recovered.incomplete_rules, ())
        self.assertTrue(all(
            c["status"] == "confirmed" for c in recovered.conclusions
        ))

    def test_preview_deduplicates_multiple_paths_and_reports_missing_basis(self):
        first_batch = batch(V1)
        split_batch = batch(split_rows())
        rule_set = rules()
        first = evaluate(first_batch, rule_set)
        original = CHECKS["required_fields"]

        def fail_identity(rule, records):
            if rule.rule_id == "identity_present":
                raise RuntimeError("boom")
            return original(rule, records)

        CHECKS["required_fields"] = fail_identity
        try:
            interrupted = incremental_evaluate(
                first, first_batch, split_batch, rule_set
            )
        finally:
            CHECKS["required_fields"] = original

        preview = dependency_impact(
            rule_set,
            changed_fields={"E2": {"id_card"}},
            old_batch=first_batch,
            batch=split_batch,
            prev_result=interrupted,
        )
        affected = {entry["rule_id"]: entry for entry in preview["affected"]}
        self.assertEqual(len(affected["amount_by_identity"]["target_ids"]), 2)
        self.assertEqual(affected["amount_by_identity"]["affected_count"], 2)
        self.assertTrue(preview["warnings"])
        self.assertTrue(
            affected["amount_by_identity"]["current_basis"]
        )

        rule_preview = dependency_impact(
            rule_set, changed_rules=("identity_present",)
        )
        affected = {entry["rule_id"]: entry
                    for entry in rule_preview["affected"]}
        self.assertEqual(affected["identity_unique"]["scope"], "full")
        self.assertEqual(affected["amount_by_identity"]["scope"], "full")

    def test_recovery_resolves_only_reconfirmed_identities_once(self):
        first_batch = batch(V1)
        split_batch = batch(split_rows())
        rule_set = rules()
        first = evaluate(first_batch, rule_set)
        original = CHECKS["required_fields"]

        def fail_identity(rule, records):
            if rule.rule_id == "identity_present":
                raise RuntimeError("boom")
            return original(rule, records)

        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = IssueTracker(tmpdir)
            tracker.update(first)
            CHECKS["required_fields"] = fail_identity
            try:
                interrupted = incremental_evaluate(
                    first, first_batch, split_batch, rule_set
                )
            finally:
                CHECKS["required_fields"] = original
            blocked_transitions = tracker.update(interrupted)
            recovered = incremental_evaluate(
                interrupted, split_batch, split_batch, rule_set
            )
            recovery_transitions = tracker.update(recovered)
            again = incremental_evaluate(
                recovered, split_batch, split_batch, rule_set
            )
            again_transitions = tracker.update(again)

        self.assertTrue(blocked_transitions["unknown"])
        self.assertFalse(any(
            key[0] == "identity_unique"
            for key in blocked_transitions["resolved"]
        ))
        self.assertEqual(again_transitions["resolved"], [])
        self.assertEqual(again_transitions["opened"], [])


if __name__ == "__main__":
    unittest.main()
