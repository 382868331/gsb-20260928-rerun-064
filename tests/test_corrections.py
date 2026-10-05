"""Corrections (retract/replace relations) handled as first-class objects."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq_platform import corrections as corr
from dq_platform.cli import main
from dq_platform.engine import evaluate
from dq_platform.loader import load_batch, load_rule_set
from dq_platform.models import Issue, Record

SAMPLES = ROOT / "samples"


def make_issue(record_id, field, rule="identity_present",
               kind="missing_value", detail=None):
    return Issue(
        rule_id=rule, rule_version_set="v1", condition="cond",
        record_id=record_id, record_index=0, field=field,
        kind=kind, detail=detail or "detail-" + record_id,
    )


def make_record(record_id, **data):
    return Record(record_id=record_id, index=0, data=dict(data, id=record_id))


def write_cset(tmp, doc):
    path = Path(tmp) / "cset.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def relation(relation_kind, retracts, records, op_id="op1", set_id="s1"):
    return {
        "set_id": set_id, "op_id": op_id, "relation": relation_kind,
        "retracts": tuple(retracts),
        "produces": tuple(r["id"] for r in records),
    }


class LoadTests(unittest.TestCase):
    def test_shape_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cset(tmp, {
                "set_id": "s1",
                "operations": [{
                    "op_id": "bad", "relation": "split",
                    "retracts": ["a"], "records": [{"id": "b"}],
                }],
            })
            with self.assertRaises(corr.CorrectionError):
                corr.load_correction_set(path)

    def test_unknown_relation_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_cset(tmp, {
                "set_id": "s1",
                "operations": [{
                    "op_id": "bad", "relation": "teleport",
                    "retracts": ["a"], "records": [],
                }],
            })
            with self.assertRaises(corr.CorrectionError):
                corr.load_correction_set(path)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = corr.CorrectionLedger(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, doc):
        return corr.load_correction_set(write_cset(self.tmp.name, doc))

    def test_retraction_cycle_blocked(self):
        cset = self.load({
            "set_id": "s1",
            "operations": [
                {"op_id": "a", "relation": "amend", "retracts": ["x"],
                 "records": [{"id": "y"}]},
                {"op_id": "b", "relation": "amend", "retracts": ["y"],
                 "records": [{"id": "x"}]},
            ],
        })
        errors, blocked = corr.validate_correction_set(cset, self.ledger)
        self.assertEqual(blocked, {"a", "b"})
        self.assertTrue(any("cycle" in e for e in errors))

    def test_conflicting_replacements_blocked(self):
        cset = self.load({
            "set_id": "s1",
            "operations": [
                {"op_id": "a", "relation": "amend", "retracts": ["x"],
                 "records": [{"id": "x1"}]},
                {"op_id": "b", "relation": "amend", "retracts": ["x"],
                 "records": [{"id": "x2"}]},
            ],
        })
        errors, blocked = corr.validate_correction_set(cset, self.ledger)
        self.assertEqual(blocked, {"a", "b"})
        self.assertTrue(any("mutually exclusive" in e for e in errors))

    def test_dependency_on_unaccepted_material_blocked(self):
        cset = self.load({
            "set_id": "s2", "depends_on": ["s1"],
            "operations": [
                {"op_id": "a", "relation": "retract", "retracts": ["x"]},
            ],
        })
        errors, blocked = corr.validate_correction_set(cset, self.ledger)
        self.assertEqual(blocked, {"a"})
        self.assertTrue(any("unaccepted" in e for e in errors))
        # Once the dependency is accepted, the block lifts.
        self.ledger.accept("s1", {
            "op_id": "base", "relation": "retract",
            "retracts": ("gone",), "records": (), "produces": (),
        })
        errors, blocked = corr.validate_correction_set(cset, self.ledger)
        self.assertEqual(errors, [])
        self.assertEqual(blocked, set())

    def test_replacement_cannot_collide_with_untargeted_current_id(self):
        batch = load_batch_write(self.tmp.name, [
            {"id": "a", "department": "g", "amount": 10},
            {"id": "b", "department": "g", "amount": 20},
        ])
        cset = self.load({
            "set_id": "s1",
            "operations": [{
                "op_id": "split-a", "relation": "split",
                "retracts": ["a"],
                "records": [
                    {"id": "x", "department": "g", "amount": 4},
                    {"id": "b", "department": "g", "amount": 6},
                ],
            }],
        })
        errors, blocked = corr.validate_acceptance_scope(
            batch, (), cset["ops"],
        )
        self.assertEqual(blocked, {"split-a"})
        self.assertTrue(any("existing identity 'b'" in e for e in errors))
        effective, _ = corr.effective_batch(batch, ())
        self.assertEqual(
            [(r.record_id, r.data["amount"]) for r in effective.records],
            [("a", 10), ("b", 20)],
        )

    def test_duplicate_outputs_are_blocked_within_one_correction(self):
        batch = load_batch_write(self.tmp.name, [
            {"id": "a", "department": "g", "amount": 10},
        ])
        cset = self.load({
            "set_id": "s1",
            "operations": [{
                "op_id": "split-a", "relation": "split",
                "retracts": ["a"],
                "records": [
                    {"id": "x", "amount": 4},
                    {"id": "x", "amount": 6},
                ],
            }],
        })
        errors, blocked = corr.validate_acceptance_scope(
            batch, (), cset["ops"],
        )
        self.assertEqual(blocked, {"split-a"})
        self.assertTrue(any("duplicate identity 'x'" in e for e in errors))

    def test_cross_operation_duplicate_output_blocks_only_later_scope(self):
        batch = load_batch_write(self.tmp.name, [
            {"id": "a", "department": "g", "amount": 4},
            {"id": "c", "department": "g", "amount": 6},
        ])
        cset = self.load({
            "set_id": "s1",
            "operations": [
                {"op_id": "split-a", "relation": "split",
                 "retracts": ["a"],
                 "records": [
                     {"id": "x", "department": "g", "amount": 2},
                     {"id": "y", "department": "g", "amount": 2},
                 ]},
                {"op_id": "amend-c-to-x", "relation": "amend",
                 "retracts": ["c"],
                 "records": [
                     {"id": "x", "department": "g", "amount": 6},
                 ]},
            ],
        })
        errors, blocked = corr.validate_acceptance_scope(
            batch, (), cset["ops"],
        )
        self.assertEqual(blocked, {"amend-c-to-x"})
        self.assertTrue(any("existing identity 'x'" in e for e in errors))

    def test_set_retraction_is_blocked_while_dependent_set_is_active(self):
        self.ledger.accept("s1", {
            "op_id": "make-z", "relation": "amend",
            "retracts": ("a",), "records": ({"id": "z"},),
            "produces": ("z",),
        })
        self.ledger.accept("s2", {
            "op_id": "change-z", "relation": "amend",
            "depends_on": ("s1",), "retracts": ("z",),
            "records": ({"id": "q"},), "produces": ("q",),
        })
        errors, blocked = corr.validate_set_retraction(
            "s1", self.ledger.active_accepted(),
        )
        self.assertEqual(blocked, {"s2"})
        self.assertTrue("still depend" in errors[0])


class CandidateChainPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def load_ops(self, doc):
        return self._load(doc)["ops"]

    def _load(self, doc):
        path = write_cset(self.tmp.name, doc)
        return corr.load_correction_set(path)

    def batch(self):
        return load_batch_write(self.tmp.name, [
            {"id": "a", "department": "g", "amount": 10},
            {"id": "z", "department": "g", "amount": 30},
        ])

    def split_merge_ops(self):
        return self.load_ops({
            "set_id": "chain",
            "operations": [
                {"op_id": "merge-a", "relation": "merge",
                 "retracts": ["a1", "a2"],
                 "records": [
                     {"id": "a", "department": "g", "amount": 10},
                 ]},
                {"op_id": "split-a", "relation": "split",
                 "retracts": ["a"],
                 "records": [
                     {"id": "a1", "department": "g", "amount": 4},
                     {"id": "a2", "department": "g", "amount": 6},
                 ]},
            ],
        })

    def test_split_then_merge_is_ordered_and_amount_conserved(self):
        plan = corr.plan_candidate_chains(
            self.batch(), (), self.split_merge_ops(),
        )
        self.assertEqual(plan["order"], ("split-a", "merge-a"))
        self.assertEqual(plan["blocked"], set())
        self.assertEqual(plan["errors"], ())
        self.assertEqual(plan["chains"][0]["status"], "legal")
        self.assertEqual(plan["group_totals"], {"g": 40})
        self.assertEqual(
            [record.record_id for record in plan["projected_records"]],
            ["z", "a"],
        )

    def test_downstream_cannot_be_selected_without_pending_upstream(self):
        plan = corr.plan_candidate_chains(
            self.batch(), (), self.split_merge_ops(),
            selected=("merge-a",),
        )
        self.assertEqual(plan["order"], ())
        self.assertIn("merge-a", plan["blocked"])
        self.assertTrue(any(
            "required prerequisite" in error for error in plan["errors"]
        ))

    def test_identity_conflict_cycle_and_missing_prerequisite_are_classified(self):
        ops = self.load_ops({
            "set_id": "cases",
            "operations": [
                {"op_id": "claim-one", "relation": "split",
                 "retracts": ["a"],
                 "records": [
                     {"id": "one", "department": "g", "amount": 4},
                     {"id": "two", "department": "g", "amount": 6},
                 ]},
                {"op_id": "claim-two", "relation": "amend",
                 "retracts": ["a"],
                 "records": [
                     {"id": "other", "department": "g", "amount": 10},
                 ]},
                {"op_id": "cycle-x", "relation": "amend",
                 "depends_on": ["cycle-z"], "retracts": ["x"],
                 "records": [{"id": "y"}]},
                {"op_id": "cycle-y", "relation": "amend",
                 "depends_on": ["cycle-x"], "retracts": ["y"],
                 "records": [{"id": "z"}]},
                {"op_id": "cycle-z", "relation": "amend",
                 "depends_on": ["cycle-y"], "retracts": ["z"],
                 "records": [{"id": "x"}]},
                {"op_id": "missing", "relation": "retract",
                 "retracts": ["not-here"]},
            ],
        })
        plan = corr.plan_candidate_chains(self.batch(), (), ops)
        statuses = {
            tuple(chain["status"] for chain in plan["chains"]
                  if "claim-one" in chain["ops"])[0],
            tuple(chain["status"] for chain in plan["chains"]
                  if "cycle-x" in chain["ops"])[0],
            tuple(chain["status"] for chain in plan["chains"]
                  if "missing" in chain["ops"])[0],
        }
        self.assertEqual(
            statuses,
            {"identity_conflict", "cycle", "missing_prerequisite"},
        )
        self.assertEqual(
            plan["blocked"],
            {"claim-one", "claim-two", "cycle-x", "cycle-y", "cycle-z",
             "missing"},
        )

    def test_competing_candidates_for_same_produced_record_are_blocked(self):
        ops = self.load_ops({
            "set_id": "competing",
            "operations": [
                {"op_id": "from-a", "relation": "amend",
                 "retracts": ["a"],
                 "records": [
                     {"id": "x", "department": "g", "amount": 10},
                 ]},
                {"op_id": "from-z", "relation": "amend",
                 "retracts": ["z"],
                 "records": [
                     {"id": "x", "department": "g", "amount": 30},
                 ]},
            ],
        })
        plan = corr.plan_candidate_chains(self.batch(), (), ops)
        self.assertEqual(plan["order"], ())
        self.assertEqual(plan["blocked"], {"from-a", "from-z"})
        self.assertTrue(any("competing candidates" in error
                            for error in plan["errors"]))

    def test_amount_failure_blocks_chain_but_keeps_independent_correction(self):
        ops = self.load_ops({
            "set_id": "amount",
            "operations": [
                {"op_id": "bad-split", "relation": "split",
                 "retracts": ["a"],
                 "records": [
                     {"id": "a1", "department": "g", "amount": 4},
                     {"id": "a2", "department": "g", "amount": 5},
                 ]},
                {"op_id": "bad-merge", "relation": "merge",
                 "retracts": ["a1", "a2"],
                 "records": [
                     {"id": "a", "department": "g", "amount": 9},
                 ]},
                {"op_id": "independent-retract", "relation": "retract",
                 "retracts": ["z"]},
            ],
        })
        plan = corr.plan_candidate_chains(self.batch(), (), ops)
        self.assertEqual(plan["order"], ("independent-retract",))
        self.assertIn("bad-split", plan["blocked"])
        self.assertIn("bad-merge", plan["blocked"])
        self.assertNotIn("independent-retract", plan["blocked"])
        self.assertEqual(
            [record.record_id for record in plan["projected_records"]],
            ["a"],
        )

    def test_acceptance_time_data_change_rechecks_actual_records(self):
        ledger = corr.CorrectionLedger(self.tmp.name)
        ledger.accept("independent", {
            "op_id": "independent-retract", "relation": "retract",
            "retracts": ("z",), "records": (), "produces": (),
        })
        plan = corr.plan_candidate_chains(
            self.batch(), ledger.active_accepted(), self.split_merge_ops(),
        )
        self.assertEqual(plan["order"], ("split-a", "merge-a"))
        self.assertEqual(
            [record.record_id for record in plan["projected_records"]],
            ["a"],
        )
        self.assertEqual(plan["group_totals"], {"g": 10})

    def test_unknown_operation_dependency_is_rejected_when_loading(self):
        with self.assertRaises(corr.CorrectionError):
            self._load({
                "set_id": "bad-dep",
                "operations": [{
                    "op_id": "op", "relation": "retract",
                    "depends_on": ["missing-op"], "retracts": ["a"],
                }],
            })


def load_batch_write(tmp_path, records):
    path = Path(tmp_path) / "sample.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return load_batch(str(path))


class MigrationTests(unittest.TestCase):
    def test_split_follows_the_fact(self):
        old = [make_record("e1")]
        new = [make_record("e1a"), make_record("e1b", name="x")]
        rel = [relation("split", ["e1"],
                        [{"id": "e1a"}, {"id": "e1b", "name": "x"}])]
        entries = corr.migrate_issues(
            [make_issue("e1", "name")], rel, old, new,
        )
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(
            entry["targets"],
            [{"record_id": "e1a", "inheritance": corr.SAME_FACT}],
        )
        self.assertFalse(entry["resolved_by_correction"])
        self.assertEqual(corr.inherited_issue_count(entries), 1)
        self.assertEqual(corr.resolution_count(entries), 0)

    def test_split_resolution_counts_once_per_origin(self):
        old = [make_record("e1", name="bad")]
        new = [make_record("e1a", name="bad"), make_record("e1b", name="bad")]
        rel = [relation("split", ["e1"],
                        [{"id": "e1a", "name": "bad"},
                         {"id": "e1b", "name": "bad"}])]
        entries = corr.migrate_issues(
            [make_issue("e1", "name", kind="malformed_value")],
            rel, old, new,
        )
        # Two inheritors, still ONE inherited problem.
        self.assertEqual(len(entries[0]["targets"]), 2)
        self.assertEqual(corr.inherited_issue_count(entries), 1)
        # And when the fact disappears everywhere: exactly one resolution.
        fixed = [make_record("e1a", name="ok"), make_record("e1b", name="ok")]
        rel2 = [relation("split", ["e1"],
                         [{"id": "e1a", "name": "ok"},
                          {"id": "e1b", "name": "ok"}])]
        entries2 = corr.migrate_issues(
            [make_issue("e1", "name", kind="malformed_value")],
            rel2, old, fixed,
        )
        self.assertEqual(corr.resolution_count(entries2), 1)

    def test_merge_keeps_each_original_reason(self):
        old = [make_record("e1"), make_record("e2")]
        new = [make_record("e12")]
        rel = [relation("merge", ["e1", "e2"], [{"id": "e12"}])]
        entries = corr.migrate_issues(
            [make_issue("e1", "name", detail="reason-one"),
             make_issue("e2", "name", detail="reason-two")],
            rel, old, new,
        )
        self.assertEqual(len(entries), 2)
        reasons = sorted(e["reasons"][0] for e in entries)
        self.assertEqual(reasons, ["reason-one", "reason-two"])
        for entry in entries:
            self.assertEqual(entry["targets"][0]["record_id"], "e12")

    def test_amend_with_changed_value_is_a_new_fact(self):
        old = [make_record("e1", name="bad")]
        new = [make_record("e1", name="worse")]
        rel = [relation("amend", ["e1"], [{"id": "e1", "name": "worse"}])]
        entries = corr.migrate_issues(
            [make_issue("e1", "name", kind="malformed_value")],
            rel, old, new,
        )
        self.assertEqual(
            entries[0]["targets"][0]["inheritance"], corr.NEW_FACT,
        )

    def test_plain_retraction_resolves(self):
        old = [make_record("e1")]
        rel = [relation("retract", ["e1"], [])]
        entries = corr.migrate_issues(
            [make_issue("e1", "name")], rel, old, [],
        )
        self.assertTrue(entries[0]["resolved_by_correction"])
        self.assertEqual(corr.resolution_count(entries), 1)


class ClassifyTests(unittest.TestCase):
    def test_value_relation_and_decision_migration_are_distinct(self):
        prev_records = [make_record("e1", amount=5), make_record("e2", v=1)]
        new_records = [make_record("e1", amount=9), make_record("e2", v=1),
                       make_record("e3", v=1)]
        prev_issues = [make_issue("e2", "v", rule="r", kind="violated")]
        new_issues = [
            make_issue("e1", "amount", rule="r", kind="violated"),
            make_issue("e3", "v", rule="r", kind="violated"),
            make_issue("e3", "w", rule="r", kind="violated"),
        ]
        migrations = [{
            "origin": ("r", "e2", "v", "violated"),
            "set_id": "s1", "op_id": "op", "relation": "amend",
            "targets": [{"record_id": "e3", "inheritance": corr.SAME_FACT}],
            "reasons": ["d"], "resolved_by_correction": False,
        }]
        changes = corr.classify_changes(
            prev_issues, new_issues, migrations, prev_records, new_records,
        )
        self.assertEqual(
            changes["value_change"], [("r", "e1", "amount", "violated")],
        )
        self.assertEqual(
            changes["decision_migration"],
            [("r", "e3", "v", "violated")],
        )
        self.assertEqual(
            changes["relation_change"], [("r", "e3", "w", "violated")],
        )


class CliFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = str(Path(self.tmp.name) / "results")
        self.cset = str(SAMPLES / "corrections_v1.json")
        self.sample = str(SAMPLES / "employees_v1.json")
        self.rules = str(SAMPLES / "rules_v1.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()

    def base_args(self):
        return ["--sample", self.sample, "--rules", self.rules,
                "--out", self.out, "--corrections", self.cset]

    def write_identity_amount_case(self):
        sample = Path(self.tmp.name) / "amounts.json"
        rules = Path(self.tmp.name) / "amount-rules.json"
        first = Path(self.tmp.name) / "corrections-first.json"
        reopened = Path(self.tmp.name) / "corrections-reopened.json"
        sample.write_text(json.dumps([
            {"id": "a", "department": "g", "amount": 10},
            {"id": "b", "department": "g", "amount": 20},
            {"id": "c", "department": "g", "amount": 0},
        ]), encoding="utf-8")
        rules.write_text(json.dumps({
            "version_set": "amount-v1",
            "rules": [
                {"id": "identity", "type": "unique_fields",
                 "params": {"key_fields": ["id"]}},
                {"id": "total", "type": "grouped_sum",
                 "params": {"group_fields": ["department"],
                            "amount_field": "amount", "max_total": 30},
                 "depends_on": ["identity"]},
            ],
        }), encoding="utf-8")
        first.write_text(json.dumps({
            "set_id": "amount-s1",
            "operations": [
                {"op_id": "independent-retract-c", "relation": "retract",
                 "retracts": ["c"]},
                {"op_id": "bad-split-a-to-b", "relation": "split",
                 "retracts": ["a"],
                 "records": [
                     {"id": "x", "department": "g", "amount": 4},
                     {"id": "b", "department": "g", "amount": 6},
                 ]},
            ],
        }), encoding="utf-8")
        reopened.write_text(json.dumps({
            "set_id": "amount-s2", "depends_on": ["amount-s1"],
            "operations": [{
                "op_id": "reopened-bad-split-a-to-b",
                "relation": "split", "retracts": ["a"],
                "records": [
                    {"id": "x", "department": "g", "amount": 4},
                    {"id": "b", "department": "g", "amount": 6},
                ],
            }],
        }), encoding="utf-8")
        return str(sample), str(rules), str(first), str(reopened)

    def test_conflicting_split_is_scoped_blocked_and_survives_reopen(self):
        sample, rules, first, reopened = self.write_identity_amount_case()
        code, out = self.run_cli(
            "--sample", sample, "--rules", rules, "--out", self.out,
            "--corrections", first,
            "--confirm-op", "independent-retract-c,bad-split-a-to-b",
            "--accept", "independent-retract-c,bad-split-a-to-b",
        )
        self.assertEqual(code, 1)
        self.assertIn("produces existing identity 'b'", out)
        self.assertIn("accepted=1", out)
        self.assertIn("[pending] op bad-split-a-to-b", out)
        self.assertIn("status=PASSED", out)
        self.assertIn("blocked scope", out)

        ledger = corr.CorrectionLedger(self.out)
        self.assertEqual(
            [e["op_id"] for e in ledger.active_accepted()],
            ["independent-retract-c"],
        )
        batch = load_batch(sample)
        effective, _ = corr.effective_batch(batch, ledger.active_accepted())
        self.assertEqual(
            [(r.record_id, r.data["amount"]) for r in effective.records],
            [("a", 10), ("b", 20)],
        )

        code, out = self.run_cli(
            "--sample", sample, "--rules", rules, "--out", self.out,
            "--corrections", reopened, "--confirm-op",
            "reopened-bad-split-a-to-b", "--accept",
            "reopened-bad-split-a-to-b",
        )
        self.assertEqual(code, 1)
        self.assertIn("produces existing identity 'b'", out)
        self.assertIn("accepted=0", out)
        self.assertEqual(
            [e["op_id"] for e in ledger.active_accepted()],
            ["independent-retract-c"],
        )
        effective, _ = corr.effective_batch(batch, ledger.active_accepted())
        self.assertEqual(
            [(r.record_id, r.data["amount"]) for r in effective.records],
            [("a", 10), ("b", 20)],
        )

    def test_unconfirmed_relation_cannot_be_accepted(self):
        code, out = self.run_cli(
            *self.base_args(), "--accept", "amend-e002",
        )
        self.assertEqual(code, 1)
        self.assertIn("needs explicit confirmation", out)
        ledger = corr.CorrectionLedger(self.out)
        self.assertEqual(ledger.active_accepted(), [])

    def test_cli_accepts_reverse_candidate_chain_in_required_order(self):
        sample = Path(self.tmp.name) / "chain.json"
        correction_file = Path(self.tmp.name) / "chain-corrections.json"
        sample.write_text(json.dumps([
            {"id": "a", "department": "g", "amount": 10},
            {"id": "z", "department": "g", "amount": 30},
        ]), encoding="utf-8")
        correction_file.write_text(json.dumps({
            "set_id": "chain", "amount_field": "amount",
            "group_fields": ["department"],
            "operations": [
                {"op_id": "merge-parts", "relation": "merge",
                 "retracts": ["a1", "a2"],
                 "records": [
                     {"id": "a", "department": "g", "amount": 10},
                 ]},
                {"op_id": "split-source", "relation": "split",
                 "retracts": ["a"],
                 "records": [
                     {"id": "a1", "department": "g", "amount": 4},
                     {"id": "a2", "department": "g", "amount": 6},
                 ]},
            ],
        }), encoding="utf-8")
        code, out = self.run_cli(
            "--sample", str(sample), "--rules", self.rules,
            "--out", self.out, "--corrections", str(correction_file),
            "--confirm-op", "merge-parts,split-source",
            "--accept", "merge-parts,split-source",
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "planned order: split-source -> merge-parts", out,
        )
        self.assertIn("projected group totals: g=40", out)
        ledger = corr.CorrectionLedger(self.out)
        self.assertEqual(
            [event["op_id"] for event in ledger.active_accepted()],
            ["split-source", "merge-parts"],
        )

    def test_partial_acceptance_then_resume_matches_full_recompute(self):
        # Accept only the amend; the split stays pending and unmixed.
        code, out = self.run_cli(
            *self.base_args(),
            "--confirm-op", "amend-e002", "--accept", "amend-e002",
        )
        self.assertIn("[pending] op split-e004", out)
        ledger = corr.CorrectionLedger(self.out)
        self.assertEqual(
            [e["op_id"] for e in ledger.active_accepted()], ["amend-e002"],
        )
        batch = load_batch(self.sample)
        eff, _ = corr.effective_batch(batch, ledger.active_accepted())
        ids = {r.record_id for r in eff.records}
        self.assertNotIn("E004a", ids)
        self.assertIn("E004", ids)  # unaccepted split not applied

        # Resume later: accept the remaining split.
        code, out = self.run_cli(
            *self.base_args(),
            "--confirm-op", "split-e004", "--accept", "split-e004",
        )
        self.assertIn("relation:split", out)
        self.assertIn("migrated:", out)

        # Stepwise acceptance must equal accepting everything at once.
        eff_stepwise, _ = corr.effective_batch(
            batch, ledger.active_accepted(),
        )
        _, rules = load_rule_set(self.rules)
        stepwise_result = evaluate(eff_stepwise, rules)
        fresh_ledger_dir = tempfile.TemporaryDirectory()
        self.addCleanup(fresh_ledger_dir.cleanup)
        fresh = corr.CorrectionLedger(fresh_ledger_dir.name)
        cset = corr.load_correction_set(self.cset)
        for op in cset["ops"]:
            fresh.accept(cset["set_id"], op)
        eff_full, _ = corr.effective_batch(batch, fresh.active_accepted())
        full_result = evaluate(eff_full, rules)
        key = lambda i: (i.rule_id, i.record_id, i.field, i.kind)
        self.assertEqual(
            sorted(map(key, stepwise_result.issues)),
            sorted(map(key, full_result.issues)),
        )
        self.assertEqual(stepwise_result.status, full_result.status)

    def test_acknowledge_then_retract_correction_loses_basis(self):
        self.run_cli(
            *self.base_args(), "--confirm-op", "amend-e002,split-e004",
            "--accept", "all",
        )
        code, out = self.run_cli(
            *self.base_args(), "--acknowledge", "identity_unique:E003",
        )
        self.assertIn("[decision] acknowledged", out)
        decisions = corr.DecisionLedger(self.out)
        self.assertEqual(len(decisions.active_decisions()), 1)

        code, out = self.run_cli(
            "--sample", self.sample, "--rules", self.rules,
            "--out", self.out, "--retract-correction", "corr-20261004-01",
        )
        self.assertEqual(code, 1)
        self.assertIn("basis-lost", out)
        self.assertEqual(decisions.active_decisions(), [])
        # The correction ledger no longer applies the retracted set.
        ledger = corr.CorrectionLedger(self.out)
        self.assertEqual(ledger.active_accepted(), [])

    def test_decision_basis_matches_accepted_set(self):
        self.run_cli(
            *self.base_args(), "--confirm-op", "amend-e002",
            "--accept", "amend-e002",
        )
        self.run_cli(
            *self.base_args(), "--acknowledge", "identity_unique:E003",
        )
        decisions = corr.DecisionLedger(self.out)
        active = decisions.active_decisions()
        self.assertEqual(active[0]["basis_sets"], ["corr-20261004-01"])


if __name__ == "__main__":
    unittest.main()
