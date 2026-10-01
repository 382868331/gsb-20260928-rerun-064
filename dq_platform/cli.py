"""Command line entry point for local manual samples."""

import argparse
import sys

from .engine import evaluate
from .incremental import compare_plans, incremental_evaluate
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
        "--lifecycle", action="store_true",
        help="update the issue lifecycle ledger (issues.jsonl) in --out",
    )
    args = parser.parse_args(argv)

    batch = load_batch(args.sample, batch_id=args.batch_id)
    version_set, rules = load_rule_set(args.rules)
    store = ResultStore(args.out)
    if args.prev_sample:
        prev_batch = load_batch(args.prev_sample, batch_id=args.prev_batch_id)
        history = store.conclusions_for_batch(prev_batch.batch_id)
        prev_result = None
        for doc in reversed(history):
            if doc["rule_version_set"] == version_set:
                prev_result = _result_from_dict(doc)
                break
        result = incremental_evaluate(prev_result, prev_batch, batch, rules)
        print(f"incremental run against batch={prev_batch.batch_id}")
    else:
        result = evaluate(batch, rules)
    store.append(result)

    if result.check_plan:
        report = compare_plans(result.previous_check_plan, result.check_plan)
        for plan in result.check_plan:
            if plan.scope == "full":
                scope_text = "all records (full re-check)"
            elif plan.scope == "groups":
                groups = (
                    " | ".join(",".join(map(str, group))
                               for group in plan.affected_groups)
                    or "invalid-identity records"
                )
                scope_text = (
                    f"records {list(plan.affected_record_ids)} in groups "
                    f"[{groups}]"
                )
            elif plan.scope == "records":
                scope_text = f"records {list(plan.affected_record_ids)}"
            else:
                scope_text = "nothing (skipped)"
            print(
                f"  [plan] rule={plan.rule_id} scope={plan.scope} "
                f"reason={plan.reason} covers {scope_text}"
            )

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
    )


if __name__ == "__main__":
    sys.exit(main())
