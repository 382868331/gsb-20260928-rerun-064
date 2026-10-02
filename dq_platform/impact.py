"""Preview which checks a field or rule change affects, along dependencies.

Given a proposed field edit or rule revision, walk the dependency graph
downstream and report every affected check exactly once.  When two
upstreams change at the same time, the shared downstream scope is the
deduplicated union of both impacts, never double-counted.  Dependency
cycles and references to missing rules raise DependencyError with the
exact cycle path or missing rule id.
"""

from dataclasses import dataclass

from .engine import DependencyError, topo_order
from .incremental import _rule_fields, diff_batches, field_changes


@dataclass(frozen=True)
class ImpactPreview:
    """Which checks a change touches, in dependency order.

    affected:  every rule whose verdict may change (direct + downstream).
    direct:    rules the change touches directly.
    records:   rule_id -> deduplicated affected record ids (only when
               both old and new batches were supplied).
    reasons:   one human-readable line per affected rule.
    """

    affected: tuple
    direct: tuple
    records: dict
    reasons: tuple


def preview_impact(rules, changed_fields=(), changed_rules=(),
                   old_batch=None, new_batch=None):
    """Return the ImpactPreview for a proposed field/rule change.

    Raises DependencyError for dependency cycles, missing dependencies,
    or changed rule ids that do not exist.
    """
    ordered = topo_order(rules)
    known = {rule.rule_id for rule in ordered}
    unknown = set(changed_rules) - known
    if unknown:
        raise DependencyError(
            "changed rule(s) not found: " + ", ".join(sorted(unknown))
        )
    changed_fields = set(changed_fields)

    direct = []
    for rule in ordered:
        relevant = _rule_fields(rule)
        if rule.rule_id in changed_rules:
            direct.append(rule.rule_id)
        elif changed_fields and (
            relevant is None or relevant & changed_fields
        ):
            direct.append(rule.rule_id)

    affected = []
    reasons = []
    for rule in ordered:
        deps_hit = [dep for dep in rule.depends_on if dep in affected]
        if rule.rule_id in direct:
            affected.append(rule.rule_id)
            reasons.append(f"{rule.rule_id}: directly changed")
        elif deps_hit:
            affected.append(rule.rule_id)
            reasons.append(
                f"{rule.rule_id}: downstream of " + ", ".join(deps_hit)
            )

    records = {}
    if old_batch is not None and new_batch is not None:
        changed, added, removed = diff_batches(old_batch, new_batch)
        fchanges = field_changes(old_batch, new_batch)
        for rule in ordered:
            if rule.rule_id not in affected:
                continue
            relevant = _rule_fields(rule)
            if relevant is None:
                scope = set(changed) | set(added) | set(removed)
            else:
                scope = {
                    rid for rid in changed
                    if fchanges.get(rid, set()) & relevant
                } | set(added) | set(removed)
            # Multiple upstreams changing at once: the shared downstream
            # scope is the deduplicated union, counted exactly once.
            for dep in rule.depends_on:
                scope |= set(records.get(dep, ()))
            records[rule.rule_id] = tuple(sorted(scope))

    return ImpactPreview(
        affected=tuple(affected),
        direct=tuple(direct),
        records=records,
        reasons=tuple(reasons),
    )


def describe_impact(preview):
    """Render an ImpactPreview as text lines."""
    if not preview.affected:
        return ["no check is affected by this change"]
    lines = []
    for rule_id, reason in zip(preview.affected, preview.reasons):
        lines.append(f"  [affected] {reason}")
        if rule_id in preview.records:
            hits = preview.records[rule_id]
            lines.append(
                f"      records ({len(hits)}): "
                + (", ".join(hits) if hits else "-")
            )
    return lines

