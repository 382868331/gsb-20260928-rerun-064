"""Command line entry point for local manual samples."""

import argparse
import sys

from .engine import evaluate
from .corrections import (
    CorrectionLedger,
    DecisionLedger,
    check_acceptance_identities,
    check_retraction_identities,
    classify_changes,
    effective_batch,
    inherited_issue_count,
    load_correction_set,
    migrate_issues,
    resolution_count,
    validate_correction_set,
)
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
    parser.add_argument(
        "--corrections", default=None,
        help="correction set JSON with retract/replace operations",
    )
    parser.add_argument(
        "--accept", default="",
        help="comma-separated correction op ids to accept, or 'all'",
    )
    parser.add_argument(
        "--confirm-op", default="",
        help="comma-separated correction op ids to explicitly confirm",
    )
    parser.add_argument(
        "--retract-correction", default=None, metavar="SET_ID",
        help="retract an accepted correction set and show decisions "
        "that lose their basis",
    )
    parser.add_argument(
        "--acknowledge", action="append", default=[],
        metavar="RULE:RECORD[:FIELD]",
        help="record a manual decision for an open issue",
    )
    args = parser.parse_args(argv)

    batch = load_batch(args.sample, batch_id=args.batch_id)
    version_set, rules = load_rule_set(args.rules)
    store = ResultStore(args.out)
    if args.retract_correction:
        return _retract_correction(args)
    if args.corrections:
        return _run_corrections(args, batch, rules, version_set, store)
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


def _split_csv(value):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _retract_correction(args):
    ledger = CorrectionLedger(args.out)
    batch = load_batch(args.sample, batch_id=args.batch_id)
    errors, _blocked = check_retraction_identities(
        batch, ledger, args.retract_correction,
    )
    if errors:
        for error in errors:
            print(f"  [blocked] {error}")
        print(
            f"correction set {args.retract_correction} NOT retracted: "
            "doing so would leave duplicate/overwritten identities; the "
            "current effective data is preserved"
        )
        return 1
    ledger.retract_set(args.retract_correction)
    decisions = DecisionLedger(args.out)
    lost = decisions.retract_basis(args.retract_correction)
    print(f"correction set {args.retract_correction} retracted")
    for event in lost:
        print(
            f"  [basis-lost] decision {event['decision']!r} on "
            f"{':'.join(event['key'])} no longer has an accepted basis"
        )
    if not lost:
        print("  no manual decisions depended on this set")
    return 1 if lost else 0


def _run_corrections(args, batch, rules, version_set, store):
    cset = load_correction_set(args.corrections)
    ledger = CorrectionLedger(args.out)
    for op_id in _split_csv(args.confirm_op):
        op = next((o for o in cset["ops"] if o["op_id"] == op_id), None)
        if op is None:
            raise SystemExit(f"unknown op to confirm: {op_id}")
        ledger.confirm(cset["set_id"], op_id, op["relation"])

    errors, blocked = validate_correction_set(cset, ledger)
    for error in errors:
        print(f"  [blocked] {error}")

    confirmed = ledger.confirmed_ops()

    def is_confirmed(op):
        return (
            op["confirmed"]
            or (cset["set_id"], op["op_id"]) in confirmed
        )

    prior_active = ledger.active_accepted()
    already = {
        e["op_id"] for e in prior_active if e["set_id"] == cset["set_id"]
    }
    pending = [op for op in cset["ops"] if op["op_id"] not in already]
    accept_arg = set(_split_csv(args.accept))
    if "all" in accept_arg:
        to_accept = [
            op for op in pending
            if is_confirmed(op) and op["op_id"] not in blocked
        ]
    else:
        unknown = accept_arg - {op["op_id"] for op in cset["ops"]}
        if unknown:
            raise SystemExit(
                "unknown op ids: " + ", ".join(sorted(unknown))
            )
        to_accept = [op for op in pending if op["op_id"] in accept_arg]
    for op in to_accept:
        if op["op_id"] in blocked:
            print(
                f"  [blocked] op {op['op_id']} cannot be confirmed; "
                "affected scope is NOT accepted"
            )
            return 1
        if not is_confirmed(op):
            print(
                f"  [blocked] op {op['op_id']} needs explicit confirmation "
                "(--confirm-op) before its relation is applied"
            )
            return 1
    identity_errors, identity_blocked = check_acceptance_identities(
        batch, ledger, cset,
        [op["op_id"] for op in to_accept],
    )
    rejected_accept = bool(identity_errors)
    for error in identity_errors:
        print(f"  [blocked] {error}")
    for op_id in sorted(identity_blocked):
        print(
            f"  [blocked] op {op_id} identity check failed; affected "
            "scope is NOT accepted and the original data is preserved"
        )
    to_accept = [op for op in to_accept if op["op_id"] not in identity_blocked]
    for op in to_accept:
        ledger.accept(cset["set_id"], op)
    skipped = [op for op in pending if op not in to_accept]
    for op in skipped:
        print(
            f"  [pending] op {op['op_id']} ({op['relation']}) not accepted; "
            "excluded from this run's effective data"
        )

    prev_effective, _ = effective_batch(batch, prior_active)
    active = ledger.active_accepted()
    eff_batch, relations = effective_batch(batch, active)
    new_relations = (
        tuple(relations[len(relations) - len(to_accept):])
        if to_accept else ()
    )

    prev_result = None
    for doc in reversed(store.conclusions_for_batch(batch.batch_id)):
        if doc["rule_version_set"] == version_set:
            prev_result = _result_from_dict(doc)
            break

    result = evaluate(eff_batch, rules)
    store.append(result)

    prev_issues = prev_result.issues if prev_result is not None else ()
    migrations = migrate_issues(
        prev_issues, new_relations,
        prev_effective.records, eff_batch.records,
    )
    changes = classify_changes(
        prev_issues, result.issues, migrations,
        prev_effective.records, eff_batch.records,
    )

    print(
        f"corrections set={cset['set_id']} accepted={len(to_accept)} "
        f"pending={len(skipped)} "
        f"effective_records={len(eff_batch.records)}"
    )
    for rel in new_relations:
        print(
            f"  [relation:{rel['relation']}] op={rel['op_id']} "
            f"retracts={','.join(rel['retracts']) or '-'} "
            f"produces={','.join(rel['produces']) or '-'}"
        )
    for entry in migrations:
        origin = ":".join(entry["origin"])
        if entry["resolved_by_correction"]:
            print(
                f"  [migrated:resolved] {origin} resolved by "
                f"{entry['relation']} op {entry['op_id']}"
            )
        for target in entry["targets"]:
            if target["inheritance"] == "same_fact":
                label = "same fact inherited"
            else:
                label = "NEW fact - needs fresh review"
            print(
                f"  [migrated:{target['inheritance']}] {origin} -> "
                f"{target['record_id']}: {label}"
            )
    if migrations:
        print(
            "  inherited open problems (dedup by origin): "
            f"{inherited_issue_count(migrations)}; resolutions from "
            f"corrections (one per origin): {resolution_count(migrations)}"
        )
    if prev_result is not None:
        for kind in ("value_change", "relation_change",
                     "decision_migration"):
            keys = changes[kind]
            if keys:
                print(f"  [change:{kind}] {len(keys)} issue(s)")
                for key in keys:
                    print(
                        f"    rule={key[0]} record={key[1]} field={key[2]}"
                    )

    print(
        f"batch={result.batch_id} rules={version_set} "
        f"status={result.status}"
    )
    for issue in result.issues:
        print(
            f"  [{issue.kind}] record={issue.record_id} "
            f"rule={issue.rule_id} field={issue.field} :: {issue.detail}"
        )
    for error in result.errors:
        print(f"  [error] {error}")

    if args.acknowledge:
        decisions = DecisionLedger(args.out)
        basis = ledger.accepted_set_ids()
        for spec in args.acknowledge:
            parts = spec.split(":")
            key = None
            for issue in result.issues:
                if (issue.rule_id == parts[0]
                        and issue.record_id == parts[1]
                        and (len(parts) < 3 or issue.field == parts[2])):
                    key = (
                        issue.rule_id, issue.record_id,
                        issue.field, issue.kind,
                    )
                    break
            if key is None:
                print(f"  [acknowledge] no open issue matches {spec}")
                continue
            decisions.confirm(key, "acknowledged", basis, result.run_id)
            print(
                f"  [decision] acknowledged {':'.join(key)} "
                f"(basis: {','.join(sorted(basis)) or 'base data'})"
            )

    if args.lifecycle:
        tracker = IssueTracker(args.out)
        transitions = tracker.update(result)
        for kind in ("opened", "reopened", "resolved", "condition_changed",
                     "unknown"):
            keys = transitions[kind]
            if keys:
                print(f"  [lifecycle:{kind}] {len(keys)} issue(s)")

    if result.status == "PASSED" and not rejected_accept:
        print(f"all {len(eff_batch.records)} records passed")
        return 0
    if rejected_accept:
        print(
            "acceptance blocked for an identity conflict - do not treat "
            "the blocked scope as part of the effective data"
        )
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
