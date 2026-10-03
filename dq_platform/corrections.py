"""Source retraction/replacement material and human decision migration.

A correction is a source relationship, not merely a field update.  Only
explicitly accepted material enters the effective batch; rejected branches
remain outside every passing conclusion.
"""

from dataclasses import dataclass, replace
import hashlib
import json

from .models import Batch, Record, utc_now


SAME_FACT = "same_fact"
NEW_FACT = "new_fact"
REVIEW_FACT = "review_new_fact"


@dataclass(frozen=True)
class Replacement:
    record_id: str
    data: dict
    sources: tuple = ()
    relation: str = NEW_FACT


@dataclass(frozen=True)
class Correction:
    correction_id: str
    retracted_records: tuple = ()
    replacements: tuple = ()
    depends_on: tuple = ()
    revoke_corrections: tuple = ()


@dataclass(frozen=True)
class IssueMigration:
    rule_id: str
    source_records: tuple
    target_records: tuple
    field: str
    kind: str
    relation: str = SAME_FACT
    rationale: str = ""
    source_reasons: tuple = ()
    reviewed: bool = True


@dataclass(frozen=True)
class Acceptance:
    correction_ids: tuple = ()
    rejected_replacements: tuple = ()
    migrations: tuple = ()
    reviewed_at: str = ""


@dataclass(frozen=True)
class EffectiveMaterial:
    batch: Batch
    accepted_corrections: tuple
    rejected_corrections: tuple
    rejected_replacements: tuple
    lineage: tuple
    migrations: tuple
    errors: tuple = ()
    blocked_relations: tuple = ()


def correction(obj):
    if isinstance(obj, Correction):
        return obj
    replacements = tuple(
        Replacement(
            record_id=str(item["id"]),
            data=dict(item.get("data", {})),
            sources=tuple(map(str, item.get("sources", ()))),
            relation=item.get("relation", NEW_FACT),
        )
        for item in obj.get("replacements", ())
    )
    return Correction(
        correction_id=str(obj["id"]),
        retracted_records=tuple(map(str, obj.get("retracted_records", ()))),
        replacements=replacements,
        depends_on=tuple(map(str, obj.get("depends_on", ()))),
        revoke_corrections=tuple(map(str, obj.get("revoke_corrections", ()))),
    )


def validate_corrections(corrections, accepted_ids=()):
    """Return relation errors among accepted corrections."""
    corrections = [correction(item) for item in corrections]
    by_id = {item.correction_id: item for item in corrections}
    accepted = set(accepted_ids)
    errors = []

    for item in corrections:
        if len(set(item.retracted_records)) != len(item.retracted_records):
            errors.append(f"correction {item.correction_id!r} retracts a record more than once")
        for replacement in item.replacements:
            if replacement.relation not in (SAME_FACT, NEW_FACT, REVIEW_FACT):
                errors.append(
                    f"replacement {replacement.record_id!r} relation must be "
                    f"{SAME_FACT!r}, {NEW_FACT!r} or {REVIEW_FACT!r}"
                )
            if not replacement.sources:
                errors.append(f"replacement {replacement.record_id!r} has no source record")

    state = {cid: 0 for cid in accepted}

    def visit(cid, path):
        if cid not in accepted:
            return
        if state[cid] == 1:
            cycle = path[path.index(cid):] + [cid]
            errors.append("correction dependency cycle: " + " -> ".join(cycle))
            return
        if state[cid] == 2:
            return
        state[cid] = 1
        for dep in by_id[cid].depends_on:
            if dep not in accepted:
                errors.append(
                    f"correction {cid!r} depends on "
                    f"{'missing' if dep not in by_id else 'not-yet-accepted'} "
                    f"correction {dep!r}; "
                    "affected confirmation is blocked"
                )
            elif dep in by_id:
                visit(dep, path + [cid])
        state[cid] = 2

    for cid in accepted:
        if cid in by_id:
            visit(cid, [])

    source_owners = {}
    for cid in accepted:
        item = by_id[cid]
        for old_id in item.retracted_records:
            source_owners.setdefault(old_id, set()).add(cid)
        for replacement in item.replacements:
            for old_id in replacement.sources:
                source_owners.setdefault(old_id, set()).add(cid)
    for old_id, owners in sorted(source_owners.items()):
        if len(owners) > 1:
            errors.append(
                f"record {old_id!r} is claimed by mutually exclusive corrections: "
                + ", ".join(sorted(owners))
            )

    revoked = {}
    for cid in accepted:
        for revoked_id in by_id[cid].revoke_corrections:
            revoked.setdefault(revoked_id, []).append(cid)
    for revoked_id, owners in revoked.items():
        if revoked_id not in by_id:
            errors.append(f"correction {owners[0]!r} revokes missing correction {revoked_id!r}")
        elif len(owners) > 1:
            errors.append(f"correction {revoked_id!r} is revoked by competing corrections")
    return tuple(dict.fromkeys(errors))


def material_fingerprint(lineage):
    relationships = [
        {
            "correction_id": item.get("correction_id"),
            "record_id": item.get("record_id"),
            "sources": item.get("sources"),
            "relation": item.get("relation"),
        }
        for item in lineage
    ]
    body = json.dumps(relationships, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def apply_corrections(base_batch, corrections, acceptance=None, batch_id=None):
    """Build the effective batch from explicitly accepted corrections."""
    corrections = [correction(item) for item in corrections]
    by_id = {item.correction_id: item for item in corrections}
    acceptance = acceptance or Acceptance()
    accepted = tuple(dict.fromkeys(acceptance.correction_ids))
    rejected_replacements = {
        tuple(item) if isinstance(item, (list, tuple, set)) and len(item) == 2
        else ("*", str(item))
        for item in acceptance.rejected_replacements
    }
    revoked = {
        dep
        for cid in accepted if cid in by_id
        for dep in by_id[cid].revoke_corrections
    }
    active_accepted = tuple(cid for cid in accepted if cid not in revoked and cid in by_id)
    errors = list(validate_corrections(corrections, active_accepted))
    for cid, replacement_id in sorted(rejected_replacements):
        if cid != "*" and cid in active_accepted:
            errors.append(
                f"replacement {replacement_id!r} in accepted correction {cid!r} "
                "was not accepted; affected scope cannot pass"
            )

    retracted = set()
    replacement_records = []
    lineage = []
    replacement_ids = set()
    for cid in active_accepted:
        item = by_id[cid]
        retracted.update(item.retracted_records)
        for replacement in item.replacements:
            key = (cid, replacement.record_id)
            if key in rejected_replacements or ("*", replacement.record_id) in rejected_replacements:
                continue
            replacement_records.append(Record(replacement.record_id, 0, replacement.data))
            replacement_ids.add(replacement.record_id)
            lineage.append({
                "correction_id": cid,
                "record_id": replacement.record_id,
                "sources": list(replacement.sources),
                "relation": replacement.relation,
                "business_data": replacement.data,
            })

    records = []
    for old in base_batch.records:
        if old.record_id in retracted:
            continue
        if old.record_id in replacement_ids:
            for replacement in replacement_records:
                if replacement.record_id == old.record_id:
                    records.append(Record(old.record_id, 0, replacement.data))
                    break
            continue
        records.append(Record(old.record_id, 0, dict(old.data)))
    new_records = [r for r in replacement_records if r.record_id not in {x.record_id for x in records}]
    records.extend(new_records)
    records.sort(key=lambda record: record.record_id)
    records = tuple(Record(r.record_id, idx, r.data) for idx, r in enumerate(records))

    for cid in active_accepted:
        for old_id in by_id[cid].retracted_records:
            lineage.append({
                "correction_id": cid,
                "record_id": old_id,
                "sources": [old_id],
                "relation": "retracted",
                "business_data": None,
            })
    fingerprint = material_fingerprint(lineage)
    for entry in lineage:
        entry["material_fingerprint"] = fingerprint

    effective_batch = Batch(
        batch_id=batch_id or base_batch.batch_id,
        source=base_batch.source + "#accepted-corrections:" + fingerprint,
        loaded_at=utc_now(),
        records=records,
    )
    migrations = tuple(acceptance.migrations)
    for migration in migrations:
        if migration.relation == REVIEW_FACT and migration.reviewed:
            errors.append(
                f"migration from {migration.source_records} to {migration.target_records} "
                "is marked reviewed, but the relation requires fresh review"
            )
        if migration.relation == SAME_FACT and not migration.rationale:
            errors.append(
                f"same-fact migration for {migration.rule_id} from "
                f"{migration.source_records} to {migration.target_records} needs a rationale"
            )
        if len(migration.source_reasons) != len(set(migration.source_reasons)):
            errors.append(
                f"migration into {migration.target_records} has duplicate source reasons"
            )
    return EffectiveMaterial(
        batch=effective_batch,
        accepted_corrections=active_accepted,
        rejected_corrections=tuple(
            item.correction_id for item in corrections
            if item.correction_id not in active_accepted
        ),
        rejected_replacements=tuple(sorted(rejected_replacements)),
        lineage=tuple(lineage),
        migrations=migrations,
        errors=tuple(dict.fromkeys(errors)),
    )


def migration_index(material):
    result = {}
    for migration in material.migrations:
        for target in migration.target_records:
            key = (migration.rule_id, target, migration.field, migration.kind)
            result.setdefault(key, []).append(migration)
    return result


def source_change_ids(base_batch, material):
    """Record ids whose source relationship changed even if values did not."""
    base_ids = {r.record_id for r in base_batch.records}
    effective_ids = {r.record_id for r in material.batch.records}
    changed = set()
    for entry in material.lineage:
        changed.add(entry["record_id"])
        changed.update(entry.get("sources", ()))
    return tuple(sorted(changed & (effective_ids | base_ids)))


def material_payload(material):
    return {
        "accepted_corrections": list(material.accepted_corrections),
        "rejected_corrections": list(material.rejected_corrections),
        "rejected_replacements": [list(item) for item in material.rejected_replacements],
        "lineage": [dict(entry) for entry in material.lineage],
        "migrations": [{
            "rule_id": item.rule_id,
            "source_records": list(item.source_records),
            "target_records": list(item.target_records),
            "field": item.field,
            "kind": item.kind,
            "relation": item.relation,
            "rationale": item.rationale,
            "source_reasons": list(item.source_reasons),
            "reviewed": item.reviewed,
        } for item in material.migrations],
    }


def _affected_rule_record_ids(rules, blocked_ids, batches):
    """Expand blocked source records through each rule's grouping unit."""
    from .engine import group_key, rule_group_fields

    affected = {}
    blocked_records = {}
    for batch in batches:
        for record in batch.records:
            if record.record_id in blocked_ids:
                blocked_records[record.record_id] = record
    for rule in rules:
        fields = rule_group_fields(rule)
        if not fields:
            affected[rule.rule_id] = set(blocked_records)
            continue
        blocked_keys = {group_key(record, fields) for record in blocked_records.values()}
        blocked_keys.discard(None)
        ids = set()
        for batch in batches:
            ids.update(
                record.record_id
                for record in batch.records
                if group_key(record, fields) in blocked_keys
            )
        affected[rule.rule_id] = ids & {
            record.record_id for batch in batches for record in batch.records
        }
    return affected


def block_result_for_material(result, rules, effective_batch, material, errors,
                              context_batches=()):
    """Mark conclusions covered by invalid source relations as blocked."""
    blocked_ids = set(source_change_ids(effective_batch, material))
    if not blocked_ids:
        return replace(result, errors=tuple(dict.fromkeys(result.errors + tuple(errors))))
    affected = _affected_rule_record_ids(
        rules, blocked_ids, (effective_batch, *context_batches),
    )
    conclusions = []
    incomplete = set(result.incomplete_rules)
    for conclusion in result.conclusions:
        conclusion = dict(conclusion)
        ids = set(conclusion.get("records", ()))
        if ids & affected.get(conclusion["rule_id"], set()):
            conclusion["status"] = "blocked"
            conclusion["scope"] = "material_relation_blocked"
            conclusion.setdefault("material_errors", []).extend(errors)
            incomplete.add(conclusion["rule_id"])
        conclusions.append(conclusion)
    all_errors = tuple(dict.fromkeys(result.errors + tuple(errors) + tuple(
        f"rule {rule_id!r} has records whose correction relation cannot be confirmed: "
        + ",".join(sorted(ids))
        for rule_id, ids in sorted(affected.items()) if ids
    )))
    return replace(
        result,
        status="FAILED",
        errors=all_errors,
        incomplete_rules=tuple(dict.fromkeys([*result.incomplete_rules, *sorted(incomplete)])),
        conclusions=tuple(conclusions),
        accepted_material=material_payload(material),
    )


def evaluate_corrections(prev_result, prev_batch, base_batch, rules,
                         corrections, acceptance=None, batch_id=None):
    """Evaluate only explicitly accepted correction material."""
    from .incremental import incremental_evaluate

    material = apply_corrections(
        base_batch, corrections, acceptance=acceptance, batch_id=batch_id
    )
    changed_records = source_change_ids(prev_batch, material)
    result = incremental_evaluate(
        prev_result, prev_batch, material.batch, rules,
        material_records=changed_records,
        accepted_material=material_payload(material),
    )
    if material.errors:
        return block_result_for_material(
            result, rules, material.batch, material, material.errors,
            context_batches=(prev_batch,),
        )
    return result


def compare_effective_results(previous, current):
    """Separate business values, source relations and human decisions."""
    previous = previous.accepted_material if hasattr(previous, "accepted_material") else {}
    current = current.accepted_material if hasattr(current, "accepted_material") else {}
    prev_lineage = {(item["correction_id"],) + tuple(item["sources"]) + (item["record_id"],): item
                    for item in previous.get("lineage", ())}
    curr_lineage = {(item["correction_id"],) + tuple(item["sources"]) + (item["record_id"],): item
                    for item in current.get("lineage", ())}
    business_changed = []
    relation_changed = []
    for key in set(prev_lineage) | set(curr_lineage):
        old = prev_lineage.get(key)
        new = curr_lineage.get(key)
        if old is None or new is None:
            relation_changed.append(key)
        elif old.get("business_data") != new.get("business_data"):
            business_changed.append(key)
        elif old.get("sources") != new.get("sources") or old.get("relation") != new.get("relation"):
            relation_changed.append(key)
    old_migrations = {tuple(map(str, item["source_records"])) + tuple(map(str, item["target_records"])) + (item["rule_id"], item["field"], item["kind"])
                      for item in previous.get("migrations", ())}
    new_migrations = {tuple(map(str, item["source_records"])) + tuple(map(str, item["target_records"])) + (item["rule_id"], item["field"], item["kind"])
                      for item in current.get("migrations", ())}
    return {
        "business_value_changes": tuple(business_changed),
        "source_relation_changes": tuple(relation_changed),
        "human_decision_added": tuple(new_migrations - old_migrations),
        "human_decision_removed": tuple(old_migrations - new_migrations),
    }


def decisions_without_basis(previous, current):
    """Find prior human migrations whose accepted material disappeared."""
    current_records = {r.record_id for r in current.batch.records}
    current_corrections = set(current.accepted_corrections)
    lost = []
    payload = previous.accepted_material if hasattr(previous, "accepted_material") else {}
    for migration in payload.get("migrations", ()):
        correction_ids = set()
        for entry in payload.get("lineage", ()):
            if entry["record_id"] in migration["target_records"]:
                correction_ids.add(entry["correction_id"])
        if any(cid not in current_corrections for cid in correction_ids) or any(
            rid not in current_records for rid in migration["target_records"]
        ):
            lost.append(migration)
    return tuple(lost)
