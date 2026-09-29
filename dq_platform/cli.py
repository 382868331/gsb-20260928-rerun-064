"""Command line entry point for local manual samples."""

import argparse
import sys

from .engine import evaluate
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
    args = parser.parse_args(argv)

    batch = load_batch(args.sample, batch_id=args.batch_id)
    version_set, rules = load_rule_set(args.rules)
    result = evaluate(batch, rules)
    store = ResultStore(args.out)
    store.append(result)

    print(f"batch={result.batch_id} rules={version_set} status={result.status}")
    for issue in result.issues:
        print(
            f"  [{issue.kind}] record={issue.record_id} "
            f"rule={issue.rule_id} field={issue.field} :: {issue.detail}"
        )
    for error in result.errors:
        print(f"  [error] {error}")
    if result.status == "PASSED":
        print(f"all {len(batch.records)} records passed")
        return 0
    print("check FAILED - do not treat the batch as qualified")
    return 1


if __name__ == "__main__":
    sys.exit(main())
