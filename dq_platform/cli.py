"""Command line entry point for local manual samples."""

import argparse
import sys

from .engine import evaluate
from .incremental import (
    dependency_impact,
    describe_plan,
    incremental_evaluate,
)
from .lifecycle import IssueTracker
from .loader import load_batch, load_rule_set
from .store import ResultStore


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="dq_platform",
        description="Run data quality rules against a local manual sample.",
    )
    parser.add_argument("--sample", required=True, help="local JSON sample file")
    parser.add_argument("--rules", required=True, help="rule version set JSON")
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--out", default="results", help="append-only result dir")
    parser.add_argument(
        "--prev-sample", default=None,
        help="previous sample file; re-checks only affected scope",
    )
    parser.add_argument("--prev-batch-id", default=None)
    parser.add_argument(
        "--preview-rule", action="append", default=[],
        help="preview downstream checks affected by changing one rule id",
    )
    parser.add_argument(
        "--preview-record-field", action="append", default=[],
        metavar="RECORD:FIELD",
        help="preview downstream checks affected by a record/field change",
    )
    parser.add_argument(
        "--lifecycle", action="store_true",
        help="update the issue lifecycle ledger (issues.jsonl) in --out",
    )
    args = parser.parse_args(argv)

    batch = load_batch(args.sample, batch_id=args.batch_id)
    version_set, rules = load_rule_set(args.rules)
    store = ResultStore(args.out)
    prev_result = None
    if args.prev_sample:
        prev_batch = load_batch(args.prev_sample, batch_id=args.prev_batch_id)
        for doc in reversed(store.conclusions_for_batch(prev_batch.batch_id)):
            if doc["rule_version_set"] == version_set:
                prev_result = _result_from_dict(doc)
                break
    if args.preview_rule or args.preview_record_field:
        changed_fields = {}
        for item in args.preview_record_field:
            if ":" not in item:
                raise SystemExit(f"--preview-record-field expects RECORD:FIELD: {item}")
            record_id, field_name = item.split(":", 1)
            changed_fields.setdefault(record_id, set()).add(field_name)
        preview = dependency_impact(
            rules,
            changed_rules=tuple(args.preview_rule),
            changed_fields={key: tuple(value)
            for key, value in changed_fields.items()},
            batch=batch,
            old_batch=prev_batch if args.prev_sample else None,
            prev_result=prev_result,
        )
        for entry in preview["affected"]:
            print(
                f"[impact] {entry['rule_id']} scope={entry['scope']} "
                f"affected={entry['affected_count']} records="
                f"{','.join(entry['target_ids'])}"
            )
            for path in entry["paths"]:
                print(f"  path: {path}")
            for version, basis in entry["current_basis"].items():
                print(f"  basis: version={version} status={basis['status']}")
        for warning in preview["warnings"]:
            print(f"[warning] {warning}")
        return 0 if not preview["warnings"] else 1
    if args.prev_sample:
        history = store.conclusions_for_batch(prev_batch.batch_id)
        prev_result = None
        for doc in reversed(history):
            if doc["rule_version_set"] == version_set:
                prev_result = _result_from_dict(doc)
                break
        result = incremental_evaluate(prev_result, prev_batch, batch, rules)
        print(f"incremental run against batch={prev_batch.batch_id}")
        if result.plan:
            print("check plan vs previous run:")
            for line in describe_plan(_plans_from_result(result), prev_result):
                print(line)
    else:
        result = evaluate(batch, rules)
    store.append(result)

    print(f"batch={result.batch_id} rules={version_set} status={result.status}")
    for issue in result.issues:
        print(
            f"  [{issue.kind}] record={issue.record_id} "
            f"rule={issue.rule_id} field={issue.field} :: {issue.detail}"
        )
    for error in result.errors:
        print(f"  [error] {error}")
    if result.incomplete_rules:
        print(
            "  [incomplete] scope not covered this run: "
            + ", ".join(result.incomplete_rules)
        )
    if args.lifecycle:
        tracker = IssueTracker(args.out)
        transitions = tracker.update(result)
        for kind in ("opened", "reopened", "resolved", "condition_changed",
                     "unknown"):
            keys = transitions[kind]
            if keys:
                print(f"  [lifecycle:{kind}] {len(keys)} issue(s)")
                for key in keys:
                    print(f"    rule={key[0]} record={key[1]} field={key[2]}")
    if result.status == "PASSED":
        print(f"all {len(batch.records)} records passed")
        return 0
    print("check FAILED - do not treat the batch as qualified")
    return 1


def _plans_from_result(result):
    from .incremental import RulePlan

    return tuple(
        RulePlan(
            rule_id=entry["rule_id"],
            scope=entry["scope"],
            targets=tuple(entry.get("targets", ())),
            blocked_targets=tuple(entry.get("blocked_targets", ())),
            reasons=tuple(entry.get("reasons", ())),
        )
        for entry in result.plan
    )


def _result_from_dict(doc):
    from .models import Issue, RunResult

    return RunResult(
        run_id=doc["run_id"],
        batch_id=doc["batch_id"],
        batch_checksum=doc["batch_checksum"],
        rule_version_set=doc["rule_version_set"],
        started_at=doc["started_at"],
        finished_at=doc["finished_at"],
        status=doc["status"],
        issues=tuple(Issue(**i) for i in doc["issues"]),
        errors=tuple(doc["errors"]),
        completed_rules=tuple(doc.get("completed_rules", ())),
        incomplete_rules=tuple(doc.get("incomplete_rules", ())),
        plan=tuple(doc.get("plan", ())),
        conclusions=tuple(doc.get("conclusions", ())),
    )


if __name__ == "__main__":
    sys.exit(main())
