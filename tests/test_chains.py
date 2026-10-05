"""Mutually dependent correction chain planning (round 6)."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dq_platform import chains
from dq_platform.cli import main
from dq_platform.corrections import CorrectionLedger, effective_batch
from dq_platform.loader import load_batch

SAMPLES = ROOT / "samples"


def write_json(tmp, name, doc):
    path = Path(tmp) / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def sample_doc():
    return [
        {"id": "D1-001", "department": "sales", "amount": 60},
        {"id": "D1-002", "department": "sales", "amount": 50},
        {"id": "D1-003", "department": "ops", "amount": 200},
    ]


def split_merge_ops():
    return [
        {
            "op_id": "split-d1-001",
            "relation": "split",
            "retracts": ["D1-001"],
            "records": [
                {"id": "D1-001a", "department": "sales", "amount": 25},
                {"id": "D1-001b", "department": "sales", "amount": 35},
            ],
        },
        {
            "op_id": "merge-d1-parts",
            "relation": "merge",
            "retracts": ["D1-001a", "D1-001b"],
            "records": [
                {"id": "D1-001m", "department": "sales", "amount": 60},
            ],
        },
    ]


class ChainTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sample = write_json(self.tmp.name, "sample.json", sample_doc())
        self.batch = load_batch(self.sample)
        self.ledger = CorrectionLedger(self.tmp.name)

    def write_chain(self, ops, **extra):
        doc = {"chain_id": "c1", "amount_field": "amount",
               "group_fields": ["department"], "operations": ops}
        doc.update(extra)
        return write_json(self.tmp.name, "chain.json", doc)

    def plan(self, chain_path):
        chain = chains.load_correction_chain(chain_path)
        return chains.plan_chain(
            chain, self.batch, self.ledger.active_accepted(),
        )


class PlanTests(ChainTestBase):
    def test_split_then_merge_order_and_totals(self):
        path = self.write_chain(split_merge_ops())
        plan = self.plan(path)
        self.assertEqual(plan["status"]["split-d1-001"], chains.OK)
        self.assertEqual(plan["status"]["merge-d1-parts"], chains.OK)
        self.assertEqual(
            list(plan["order"]), ["split-d1-001", "merge-d1-parts"],
        )
        ids = [r.record_id for r in plan["final_records"]]
        self.assertEqual(sorted(ids), ["D1-001m", "D1-002", "D1-003"])
        # Amount conservation: sales 60+50 -> 60+50, ops untouched.
        self.assertEqual(plan["totals_before"], plan["totals"])
        self.assertEqual(plan["totals"][("sales",)], 110)
        self.assertEqual(plan["totals"][("ops",)], 200)

    def test_two_candidates_contending_same_source_record(self):
        ops = [
            {"op_id": "amend-a", "relation": "amend", "retracts": ["D1-001"],
             "records": [{"id": "X1", "department": "sales", "amount": 60}]},
            {"op_id": "amend-b", "relation": "amend", "retracts": ["D1-001"],
             "records": [{"id": "X2", "department": "sales", "amount": 60}]},
        ]
        plan = self.plan(self.write_chain(ops))
        self.assertEqual(
            plan["status"]["amend-a"], chains.IDENTITY_CONFLICT,
        )
        self.assertEqual(
            plan["status"]["amend-b"], chains.IDENTITY_CONFLICT,
        )
        self.assertEqual(plan["order"], ())

    def test_missing_prerequisite(self):
        ops = [
            {"op_id": "amend-ghost", "relation": "amend",
             "retracts": ["D1-999"],
             "records": [{"id": "D1-999", "department": "ops", "amount": 1}]},
        ]
        plan = self.plan(self.write_chain(ops))
        self.assertEqual(
            plan["status"]["amend-ghost"], chains.MISSING_PREREQUISITE,
        )

    def test_cyclic_dependency(self):
        ops = [
            {"op_id": "op-a", "relation": "amend", "retracts": ["B"],
             "records": [{"id": "A", "department": "sales", "amount": 1}]},
            {"op_id": "op-b", "relation": "amend", "retracts": ["A"],
             "records": [{"id": "B", "department": "sales", "amount": 1}]},
        ]
        plan = self.plan(self.write_chain(ops))
        self.assertEqual(plan["status"]["op-a"], chains.CYCLE)
        self.assertEqual(plan["status"]["op-b"], chains.CYCLE)
        self.assertEqual(plan["order"], ())

    def test_partial_closure_acceptance(self):
        # Requesting only the downstream op blocks it: its prerequisite
        # is not yet effective and was not requested.
        path = self.write_chain(split_merge_ops())
        plan = self.plan(path)
        acceptable, blocked = chains.dependency_closure(
            plan, {"merge-d1-parts"},
        )
        # Closure pulls in the prerequisite automatically.
        self.assertEqual(
            acceptable, {"split-d1-001", "merge-d1-parts"},
        )
        self.assertEqual(blocked, set())

    def test_downstream_blocked_when_prerequisite_not_ok(self):
        ops = split_merge_ops() + [
            {"op_id": "rival", "relation": "amend", "retracts": ["D1-001"],
             "records": [{"id": "Z", "department": "sales", "amount": 60}]},
        ]
        plan = self.plan(self.write_chain(ops))
        self.assertEqual(
            plan["status"]["split-d1-001"], chains.IDENTITY_CONFLICT,
        )
        acceptable, blocked = chains.dependency_closure(
            plan, {"merge-d1-parts"},
        )
        self.assertIn("merge-d1-parts", blocked)
        self.assertNotIn("merge-d1-parts", acceptable)

    def test_replan_after_data_change_revalidates(self):
        path = self.write_chain(split_merge_ops())
        plan = self.plan(path)
        self.assertEqual(plan["status"]["split-d1-001"], chains.OK)
        # Source data changes before acceptance: D1-001 disappears.
        changed = [r for r in sample_doc() if r["id"] != "D1-001"]
        self.sample = write_json(self.tmp.name, "s2.json", changed)
        self.batch = load_batch(self.sample)
        plan = self.plan(path)
        self.assertEqual(
            plan["status"]["split-d1-001"], chains.MISSING_PREREQUISITE,
        )
        self.assertEqual(
            plan["status"]["merge-d1-parts"], chains.MISSING_PREREQUISITE,
        )


class AcceptanceTests(ChainTestBase):
    def run_cli(self, chain_path, *extra):
        rules = write_json(self.tmp.name, "rules.json", {
            "version_set": "v1",
            "rules": [{
                "id": "total", "type": "grouped_sum",
                "params": {"group_fields": ["department"],
                           "amount_field": "amount", "max_total": 1000},
            }],
        })
        argv = [
            "--sample", self.sample, "--rules", rules,
            "--out", self.tmp.name, "--chain", chain_path,
        ] + list(extra)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def effective_ids(self):
        eff, _ = effective_batch(self.batch, self.ledger.active_accepted())
        return sorted(r.record_id for r in eff.records)

    def test_accept_closure_in_order(self):
        path = self.write_chain(split_merge_ops())
        code, out = self.run_cli(
            path, "--confirm-op", "split-d1-001,merge-d1-parts",
            "--accept", "merge-d1-parts",
        )
        self.assertEqual(code, 0, out)
        # Closure acceptance pulled the prerequisite in, in derived order.
        events = [
            e for e in self.ledger.events() if e["event"] == "accepted"
        ]
        self.assertEqual(
            [e["op_id"] for e in events],
            ["split-d1-001", "merge-d1-parts"],
        )
        self.assertEqual(self.effective_ids(), ["D1-001m", "D1-002", "D1-003"])
        eff, _ = effective_batch(self.batch, self.ledger.active_accepted())
        totals, errors = chains.grouped_totals(
            eff.records, ("department",), "amount",
        )
        self.assertEqual(errors, [])
        self.assertEqual(totals, {("sales",): 110, ("ops",): 200})

    def test_partial_acceptance_keeps_rest_candidate(self):
        path = self.write_chain(split_merge_ops())
        code, out = self.run_cli(
            path, "--confirm-op", "split-d1-001,merge-d1-parts",
            "--accept", "split-d1-001",
        )
        self.assertEqual(code, 0, out)
        self.assertIn("[pending] op merge-d1-parts", out)
        self.assertEqual(
            self.effective_ids(), ["D1-001a", "D1-001b", "D1-002", "D1-003"],
        )
        # Second run accepts the downstream now that its prerequisite is
        # effective.
        code, out = self.run_cli(path, "--accept", "merge-d1-parts")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.effective_ids(), ["D1-001m", "D1-002", "D1-003"])

    def test_contending_candidates_not_accepted(self):
        ops = [
            {"op_id": "amend-a", "relation": "amend", "retracts": ["D1-001"],
             "records": [{"id": "X1", "department": "sales", "amount": 60}],
             "confirmed": True},
            {"op_id": "amend-b", "relation": "amend", "retracts": ["D1-001"],
             "records": [{"id": "X2", "department": "sales", "amount": 60}],
             "confirmed": True},
        ]
        path = self.write_chain(ops)
        code, out = self.run_cli(path, "--accept", "all")
        self.assertEqual(code, 1)
        self.assertIn("identity_conflict", out)
        self.assertEqual(
            self.effective_ids(), ["D1-001", "D1-002", "D1-003"],
        )

    def test_midway_retract_prerequisite(self):
        # The prerequisite split is accepted first; the downstream merge
        # is a separate candidate chain relying on its output.
        split_path = self.write_chain(split_merge_ops()[:1])
        code, _ = self.run_cli(
            split_path, "--confirm-op", "split-d1-001",
            "--accept", "split-d1-001",
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.effective_ids(), ["D1-001a", "D1-001b", "D1-002", "D1-003"],
        )
        merge_path = self.write_chain(split_merge_ops()[1:])
        plan = self.plan(merge_path)
        self.assertEqual(plan["status"]["merge-d1-parts"], chains.OK)
        # Mid-way retraction of the prerequisite correction.
        self.ledger.retract_set("c1")
        self.assertEqual(
            self.effective_ids(), ["D1-001", "D1-002", "D1-003"],
        )
        # Re-planning now marks the downstream as missing its
        # prerequisite records.
        plan = self.plan(merge_path)
        self.assertEqual(
            plan["status"]["merge-d1-parts"], chains.MISSING_PREREQUISITE,
        )
        # Amount conservation across the retract/re-plan cycle.
        self.assertEqual(plan["totals_before"][("sales",)], 110)
        self.assertEqual(plan["totals_before"][("ops",)], 200)

    def test_unrelated_records_not_overwritten_by_order(self):
        ops = [
            {"op_id": "merge-first", "relation": "merge",
             "retracts": ["P1", "P2"],
             "records": [{"id": "PM", "department": "ops", "amount": 30}]},
            {"op_id": "split-second", "relation": "split",
             "retracts": ["D1-003"],
             "records": [
                 {"id": "P1", "department": "ops", "amount": 10},
                 {"id": "P2", "department": "ops", "amount": 20},
                 {"id": "P3", "department": "ops", "amount": 170},
             ]},
        ]
        path = self.write_chain(ops)
        plan = self.plan(path)
        # Derived order places the prerequisite split before the merge,
        # even though the merge is listed first.
        self.assertEqual(
            list(plan["order"]), ["split-second", "merge-first"],
        )
        code, out = self.run_cli(
            path, "--confirm-op", "merge-first,split-second",
            "--accept", "all",
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.effective_ids(), ["D1-001", "D1-002", "P3", "PM"],
        )
        eff, _ = effective_batch(self.batch, self.ledger.active_accepted())
        totals, _ = chains.grouped_totals(
            eff.records, ("department",), "amount",
        )
        # ops total conserved: 200 -> 170 + 30; sales untouched.
        self.assertEqual(totals[("ops",)], 200)
        self.assertEqual(totals[("sales",)], 110)

    def test_accepted_independent_correction_stays_valid(self):
        independent = [
            {"op_id": "fix-d1-002", "relation": "amend",
             "retracts": ["D1-002"], "confirmed": True,
             "records": [{"id": "D1-002", "department": "sales",
                          "amount": 50}]},
        ]
        path = self.write_chain(independent)
        code, _ = self.run_cli(path, "--accept", "all")
        self.assertEqual(code, 0)
        # Data changes afterwards; the accepted op remains effective.
        changed = sample_doc()
        changed[2]["amount"] = 210
        self.sample = write_json(self.tmp.name, "s2.json", changed)
        self.batch = load_batch(self.sample)
        ids = self.effective_ids()
        self.assertEqual(ids, ["D1-001", "D1-002", "D1-003"])
        eff, _ = effective_batch(self.batch, self.ledger.active_accepted())
        totals, _ = chains.grouped_totals(
            eff.records, ("department",), "amount",
        )
        self.assertEqual(totals[("ops",)], 210)


if __name__ == "__main__":
    unittest.main()
