import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq_platform.engine import DependencyError, evaluate, topo_order
from dq_platform.loader import load_batch, load_rule_set
from dq_platform.store import ResultStore
from dq_platform.values import MISSING, MALFORMED, is_bad

SAMPLES = ROOT / "samples"


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.batch = load_batch(SAMPLES / "employees_v1.json")
        self.version_set, self.rules = load_rule_set(SAMPLES / "rules_v1.json")

    def test_duplicate_identity_detected(self):
        result = evaluate(self.batch, self.rules)
        dupes = [
            i for i in result.issues
            if i.rule_id == "identity_unique" and i.kind == "violated"
        ]
        self.assertEqual({i.record_id for i in dupes}, {"E003", "E004"})
        self.assertEqual(result.status, "FAILED")

    def test_contradictory_dates_detected(self):
        result = evaluate(self.batch, self.rules)
        bad = [
            i for i in result.issues
            if i.record_id == "E002" and i.rule_id == "employment_dates_consistent"
        ]
        self.assertEqual(len(bad), 1)
        self.assertIn("contradictory dates", bad[0].detail)

    def test_missing_and_malformed_are_not_zero_like(self):
        result = evaluate(self.batch, self.rules)
        kinds = {}
        for issue in result.issues:
            if issue.record_id == "E004":
                kinds.setdefault(issue.field, set()).add(issue.kind)
        self.assertIn("malformed_value", kinds["hire_date"])
        # The malformed hire date must never generate a contradictory-date issue
        # as if it had compared as zero/epoch.
        contradictions = [
            i for i in result.issues
            if i.record_id == "E004"
            and i.rule_id == "employment_dates_consistent"
            and i.kind == "violated"
        ]
        self.assertEqual(contradictions, [])
        # Blank optional leave_date is simply not compared, never zero-like.
        date_issues = [
            i for i in result.issues
            if i.record_id == "E004"
            and i.rule_id == "employment_dates_consistent"
        ]
        self.assertEqual(
            {(i.field, i.kind) for i in date_issues},
            {("hire_date", "malformed_value")},
        )
        with self.assertRaises(TypeError):
            bool(MISSING)
        self.assertTrue(is_bad(MALFORMED))

    def test_issues_point_to_record_and_condition(self):
        result = evaluate(self.batch, self.rules)
        for issue in result.issues:
            self.assertTrue(issue.record_id)
            self.assertGreaterEqual(issue.record_index, 0)
            self.assertTrue(issue.condition)
            self.assertTrue(issue.rule_version_set)
            self.assertIn(issue.rule_id, issue.condition + issue.rule_id)

    def test_dependency_cycle_and_missing_dependency_fail(self):
        _, cycle_rules = load_rule_set(SAMPLES / "rules_cycle.json")
        with self.assertRaises(DependencyError):
            topo_order(cycle_rules)
        result = evaluate(self.batch, cycle_rules)
        self.assertEqual(result.status, "FAILED")
        self.assertTrue(result.errors)
        self.assertIn("cycle", result.errors[0])

        _, missing_rules = load_rule_set(SAMPLES / "rules_missing_dep.json")
        result = evaluate(self.batch, missing_rules)
        self.assertEqual(result.status, "FAILED")
        self.assertIn("missing rule", result.errors[0])

    def test_execution_failure_never_reports_all_passed(self):
        _, rules = load_rule_set(SAMPLES / "rules_v1.json")
        # Corrupt a rule definition at runtime so the handler raises.
        broken = type(rules[2])(
            rule_id=rules[2].rule_id,
            rule_type=rules[2].rule_type,
            version_set=rules[2].version_set,
            fields=rules[2].fields,
            params={},
            depends_on=(),
        )
        # Replace the other rules so the handler error is the only outcome.
        result = evaluate(self.batch, (broken,))
        self.assertEqual(result.status, "FAILED")
        self.assertTrue(result.errors)

    def test_revision_does_not_overwrite_old_conclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(tmp)
            first = evaluate(self.batch, self.rules)
            store.append(first)

            # Simulate a revised rule version set on the SAME immutable batch.
            revised_doc = json.loads((SAMPLES / "rules_v1.json").read_text())
            revised_doc["version_set"] = "rules-2024-v2"
            revised_path = Path(tmp) / "rules_v2.json"
            revised_path.write_text(json.dumps(revised_doc))
            _, revised_rules = load_rule_set(revised_path)
            second = evaluate(self.batch, revised_rules)
            store.append(second)

            docs = store.conclusions_for_batch(self.batch.batch_id)
            self.assertEqual(len(docs), 2)
            self.assertEqual(
                [d["rule_version_set"] for d in docs],
                ["rules-2024-v1", "rules-2024-v2"],
            )
            self.assertNotEqual(docs[0]["run_id"], docs[1]["run_id"])
            # Old conclusion stays exactly as originally recorded.
            self.assertEqual(docs[0]["status"], first.status)
            self.assertEqual(docs[0]["batch_checksum"], self.batch.checksum)

    def test_clean_sample_passes(self):
        clean = load_batch(SAMPLES / "clean.json")
        result = evaluate(clean, self.rules)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.errors, ())
        self.assertEqual(result.status, "PASSED")


if __name__ == "__main__":
    unittest.main()
