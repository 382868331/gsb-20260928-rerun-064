import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq_platform.corrections import (
    Acceptance,
    Correction,
    IssueMigration,
    NEW_FACT,
    Replacement,
    SAME_FACT,
    apply_corrections,
    compare_effective_results,
    decisions_without_basis,
    evaluate_corrections,
    validate_corrections,
)
from dq_platform.engine import evaluate
from dq_platform.lifecycle import BASIS_LOST, IssueTracker
from dq_platform.models import Batch, Record, RuleDef, utc_now


def batch(rows):
    return Batch(
        batch_id="correction-batch",
        source="memory",
        loaded_at=utc_now(),
        records=tuple(Record(str(row["id"]), idx, row) for idx, row in enumerate(rows)),
    )


def rules():
    version = "correction-v1"
    return (
        RuleDef("identity_present", "required_fields", version,
                fields=("id", "id_card")),
        RuleDef("identity_unique", "unique_fields", version,
                params={"key_fields": ["id_card"]},
                depends_on=("identity_present",)),
        RuleDef("amount_total", "grouped_sum", version,
                params={"group_fields": ["id_card"], "amount_field": "amount",
                        "max_total": 100},
                depends_on=("identity_unique",)),
        RuleDef("hire_present", "required_fields", version,
                fields=("id", "hire_date")),
    )


V1 = [
    {"id": "E1", "id_card": "A", "amount": 60, "hire_date": "2024-01-01"},
    {"id": "E2", "id_card": "A", "amount": 50, "hire_date": "2024-01-02"},
    {"id": "E3", "id_card": "C", "amount": 200, "hire_date": "2024-01-03"},
]


class CorrectionTests(unittest.TestCase):
    def test_split_one_record_into_two_rechecks_identity_and_group(self):
        first_batch = batch(V1)
        first = evaluate(first_batch, rules())
        correction = Correction(
            "split-E2",
            retracted_records=("E2",),
            replacements=(
                Replacement("E2", {
                    "id": "E2", "id_card": "B", "amount": 20,
                    "hire_date": "2024-01-02",
                }, sources=("E2",), relation=NEW_FACT),
                Replacement("E2A", {
                    "id": "E2A", "id_card": "B", "amount": 30,
                    "hire_date": "2024-01-02",
                }, sources=("E2",), relation=NEW_FACT),
            ),
        )
        result = evaluate_corrections(
            first, first_batch, first_batch, rules(),
            [correction], Acceptance(("split-E2",)),
        )
        effective = apply_corrections(
            first_batch, [correction], Acceptance(("split-E2",))
        ).batch
        full = evaluate(effective, rules())
        def semantic(result):
            return {
                (i.rule_id, i.record_id, i.field, i.kind, i.detail)
                for i in result.issues
            }
        self.assertEqual(semantic(result), semantic(full))
        self.assertIn("E2", {i.record_id for i in result.issues if i.rule_id == "identity_unique"})
        self.assertIn("E2A", {i.record_id for i in result.issues if i.rule_id == "identity_unique"})
        self.assertNotIn("E1", {i.record_id for i in result.issues if i.rule_id == "amount_total"})
        self.assertEqual(result.accepted_material["accepted_corrections"], ["split-E2"])

    def test_merge_two_old_records_presives_each_original_manual_reason(self):
        migration = IssueMigration(
            "hire_present", ("E1", "E2"), ("E4",), "hire_date", "missing_value",
            relation=SAME_FACT,
            rationale="source packet certifies the same two employment facts",
            source_reasons=("E1 lacked date", "E2 date was illegible"),
        )
        correction = Correction(
            "merge-E1-E2",
            retracted_records=("E1", "E2"),
            replacements=(Replacement("E4", {
                "id": "E4", "id_card": "A", "amount": 110,
                "hire_date": "2024-01-05",
            }, sources=("E1", "E2"), relation=SAME_FACT),),
        )
        material = apply_corrections(
            batch(V1), [correction],
            Acceptance(("merge-E1-E2",), migrations=(migration,)),
        )
        self.assertEqual([r.record_id for r in material.batch.records], ["E3", "E4"])
        stored = material.accepted_material if hasattr(material, "accepted_material") else material.lineage
        self.assertEqual(material.migrations[0].source_reasons,
                         ("E1 lacked date", "E2 date was illegible"))
        first = evaluate(batch(V1), rules())
        result = evaluate_corrections(
            first, batch(V1), batch(V1), rules(), [correction],
            Acceptance(("merge-E1-E2",), migrations=(migration,)),
        )
        self.assertNotIn("E1", {i.record_id for i in result.issues})
        self.assertNotIn("E2", {i.record_id for i in result.issues})
        self.assertIn("E4", {i.record_id for i in result.issues if i.rule_id == "amount_total"})

    def test_same_business_value_with_source_change_is_still_reconfirmed(self):
        correction = Correction(
            "source-reissue",
            retracted_records=("E3",),
            replacements=(Replacement("E3", dict(V1[2]), sources=("E3",), relation=NEW_FACT),),
        )
        first = evaluate(batch(V1), rules())
        result = evaluate_corrections(
            first, batch(V1), batch(V1), rules(), [correction],
            Acceptance(("source-reissue",)),
        )
        sum_units = [c for c in result.conclusions if c["rule_id"] == "amount_total"]
        self.assertTrue(all(c["scope"] != "skip" for c in sum_units if "E3" in c["records"]))
        self.assertTrue(any(
            "source relationship changed" in reason
            for plan in result.plan for reason in plan["reasons"]
            if "E3" in reason
        ))

    def test_partial_acceptance_blocks_rejected_branch_but_not_unrelated_material(self):
        correction = Correction(
            "partial-split",
            retracted_records=("E2",),
            replacements=(
                Replacement("E2", dict(V1[1], id_card="B"), sources=("E2",), relation=NEW_FACT),
                Replacement("E2A", dict(V1[1], id="E2A", id_card="B"), sources=("E2",), relation=NEW_FACT),
            ),
        )
        result = evaluate_corrections(
            evaluate(batch(V1), rules()), batch(V1), batch(V1), rules(),
            [correction],
            Acceptance(("partial-split",), rejected_replacements=(("partial-split", "E2A"),)),
        )
        self.assertEqual(result.status, "FAILED")
        blocked_by_rule = {}
        confirmed_by_rule = {}
        for conclusion in result.conclusions:
            target = blocked_by_rule if conclusion["status"] == "blocked" else confirmed_by_rule
            target.setdefault(conclusion["rule_id"], set()).update(conclusion["records"])
        self.assertIn("E2", blocked_by_rule["identity_unique"])
        self.assertIn("E2", blocked_by_rule["amount_total"])
        self.assertIn("E3", confirmed_by_rule["hire_present"])
        self.assertNotIn("E2A", [r.record_id for r in result.batch_checksum and result.issues])

    def test_cycle_missing_dependency_and_competing_claims_block_confirmation(self):
        cycle = (
            Correction("a", depends_on=("b",)),
            Correction("b", depends_on=("a",)),
        )
        self.assertTrue(any("cycle" in e for e in validate_corrections(cycle, ("a", "b"))))
        ghost = Correction("ghost")
        dependent = Correction("later", depends_on=("ghost",))
        self.assertTrue(any("not-yet-accepted" in e
                            for e in validate_corrections((ghost, dependent), ("later",))))
        competing = (
            Correction("x", replacements=(Replacement("X", {"id": "X"}, ("E1",)),)),
            Correction("y", replacements=(Replacement("Y", {"id": "Y"}, ("E1",)),)),
        )
        self.assertTrue(any("mutually exclusive" in e
                            for e in validate_corrections(competing, ("x", "y"))))

    def test_revoked_correction_shows_decisions_that_lost_basis(self):
        split = Correction(
            "split",
            retracted_records=("E2",),
            replacements=(Replacement("E2B", {
                "id": "E2B", "id_card": "B", "amount": 50,
                "hire_date": "2024-01-02",
            }, sources=("E2",), relation=NEW_FACT),),
        )
        migration = IssueMigration(
            "hire_present", ("E2",), ("E2B",), "hire_date", "missing_value",
            relation=SAME_FACT, rationale="same source-certified fact",
        )
        accepted = Acceptance(("split",), migrations=(migration,))
        first_material = apply_corrections(batch(V1), [split], accepted)
        first = evaluate(batch(V1), rules())
        current = evaluate_corrections(
            first, batch(V1), batch(V1), rules(), [split], accepted
        )
        revocation = Correction("undo", revoke_corrections=("split",))
        later_material = apply_corrections(
            batch(V1), [split, revocation], Acceptance(("split", "undo"))
        )
        lost = decisions_without_basis(current, later_material)
        self.assertEqual(lost[0]["target_records"], ["E2B"])
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = IssueTracker(tmpdir)
            events = tracker.record_basis_lost(lost, current)
            self.assertEqual(events[0]["event"], BASIS_LOST)

    def test_compare_distinguishes_values_relations_and_manual_decisions(self):
        same_source = Correction(
            "same",
            replacements=(Replacement("E3", dict(V1[2]), ("E3",), NEW_FACT),),
        )
        changed_source = Correction(
            "same",
            replacements=(Replacement("E3", dict(V1[2]), ("E3",), SAME_FACT),),
        )
        first = evaluate_corrections(
            evaluate(batch(V1), rules()), batch(V1), batch(V1), rules(),
            [same_source], Acceptance(("same",)),
        )
        second_material = apply_corrections(
            batch(V1), [changed_source], Acceptance(("same",))
        )
        second = evaluate_corrections(
            first, first.batch_checksum and batch(V1), batch(V1), rules(),
            [changed_source], Acceptance(("same",)),
        )
        comparison = compare_effective_results(first, second)
        self.assertFalse(comparison["business_value_changes"])
        self.assertTrue(comparison["source_relation_changes"])
        self.assertEqual(second_material.batch.records[2].data, V1[2])

    def test_inherited_split_issue_does_not_open_new_resolution_count(self):
        correction = Correction(
            "split-duplicate",
            retracted_records=("E2",),
            replacements=(
                Replacement("E2", {"id": "E2", "id_card": "B", "amount": 20,
                                  "hire_date": "2024-01-02"}, ("E2",), NEW_FACT),
                Replacement("E2A", {"id": "E2A", "id_card": "B", "amount": 30,
                                    "hire_date": "2024-01-02"}, ("E2",), NEW_FACT),
            ),
        )
        migration = IssueMigration(
            "identity_unique", ("E2",), ("E2", "E2A"), "id_card", "violated",
            relation=SAME_FACT, rationale="same disputed identity packet",
            source_reasons=("duplicate key",),
        )
        acceptance = Acceptance(("split-duplicate",), migrations=(migration,))
        result = evaluate_corrections(
            evaluate(batch(V1), rules()), batch(V1), batch(V1), rules(),
            [correction], acceptance,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = IssueTracker(tmpdir)
            tracker.update(evaluate(batch(V1), rules()))
            first_transitions = tracker.update(result)
            second_transitions = tracker.update(result)
            kinds = [e["event"] for e in tracker.events()]
        self.assertFalse(first_transitions["opened"])
        self.assertEqual(
            first_transitions["resolved"],
            [("identity_unique", "E1", "id_card", "violated")],
        )
        self.assertEqual(kinds.count("decision_migrated"), 1)
        self.assertFalse(second_transitions["opened"])
        self.assertFalse(second_transitions["resolved"])


if __name__ == "__main__":
    unittest.main()
