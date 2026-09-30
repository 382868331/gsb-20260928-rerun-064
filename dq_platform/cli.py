"""Command line entry point for local manual samples."""

import argparse
import sys

from .engine import evaluate
from .loader import load_batch, load_rule_set
from .lifecycle import IssueLedger
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
        "--baseline-sample", default=None,
        help="previous sample snapshot; enables incremental re-check",
    )
    parser.add_argument(
        "--baseline-batch-id", default=None,
        help="batch_id under which the previous conclusion was stored",
    )
    args = parser.parse_args(argv)

    batch = load_batch(args.sample, batch_id=args.batch_id)
    version_set, rules = load_rule_set(args.rules)
    store = ResultStore(args.out)

    baseline = None
    baseline_batch = None
    if args.baseline_sample:
        baseline_batch = load_batch(
            args.baseline_sample,
            batch_id=args.baseline_batch_id
            or batch.batch_id,
        )
        baseline = store.latest_valid_for_batch(baseline_batch.batch_id)
        if baseline is None:
            print(
                "no previous valid conclusion found for batch "
                f"{baseline_batch.batch_id}; falling back to full check"
            )

    if baseline is not None:
        from .engine import evaluate_incremental
        result = evaluate_incremental(batch, rules, baseline, baseline_batch)
    else:
        result = evaluate(batch, rules)
    store.append(result)

    ledger = IssueLedger(args.out)
    events = ledger.apply_run(result, baseline=baseline)

    print(f"batch={result.batch_id} rules={version_set} status={result.status}")
    for issue in result.issues:
        print(
            f"  [{issue.kind}] record={issue.record_id} "
            f"rule={issue.rule_id} field={issue.field} :: {issue.detail}"
        )
    for error in result.errors:
        print(f"  [error] {error}")
    for item in result.incomplete_scope:
        print(
            f"  [incomplete] rule={item['rule_id']} reason={item['reason']} "
            f"records={','.join(item['record_ids']) or '(none)'}"
        )
    for event in events:
        if event["type"] in ("opened", "reopened", "condition_changed", "resolved"):
            print(
                f"  [lifecycle:{event['type']}] {event['issue_key']} :: "
                f"{event['detail']}"
            )
    if result.status == "PASSED":
        print(f"all {len(batch.records)} records passed")
        return 0
    print("check FAILED - do not treat the batch as qualified")
    return 1


if __name__ == "__main__":
    sys.exit(main())
