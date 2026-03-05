"""Drift scoring — compares contract summaries and produces a 0.0-1.0 drift score."""

from __future__ import annotations

from memory.models import ContractSummary, DriftResult

DRIFT_THRESHOLD: float = 0.5

_REMOVED_WEIGHT: float = 0.4
_MODIFIED_WEIGHT: float = 0.35
_ADDED_WEIGHT: float = 0.25
_ASSUMPTION_PENALTY_FACTOR: float = 0.1
_DEPENDENCY_PENALTY_FACTOR: float = 0.05


def _contract_key(contract_type: str, name: str) -> str:
    return f"{contract_type}:{name}"


def score_drift(previous: ContractSummary, current: ContractSummary) -> DriftResult:
    """Compare two contract summaries and compute a drift score.

    Returns a DriftResult with score in [0.0, 1.0].
    Score < DRIFT_THRESHOLD means PASS, >= means BLOCK.
    """
    prev_keys = {_contract_key(c.type.value, c.name): c for c in previous.contracts}
    curr_keys = {_contract_key(c.type.value, c.name): c for c in current.contracts}

    all_keys = set(prev_keys) | set(curr_keys)
    total = len(all_keys)

    if total == 0:
        return DriftResult(score=0.0, passed=True, detail="No contracts to compare.")

    added = sorted(set(curr_keys) - set(prev_keys))
    removed = sorted(set(prev_keys) - set(curr_keys))

    shared = set(prev_keys) & set(curr_keys)
    modified: list[str] = []
    for key in sorted(shared):
        p = prev_keys[key]
        c = curr_keys[key]
        if p.fields != c.fields or p.consumed_by != c.consumed_by or p.methods != c.methods:
            modified.append(key)

    raw = (len(removed) * _REMOVED_WEIGHT + len(modified) * _MODIFIED_WEIGHT + len(added) * _ADDED_WEIGHT) / total

    # Assumption drift penalty
    prev_assumptions = set(previous.assumptions)
    curr_assumptions = set(current.assumptions)
    assumption_changes = len(prev_assumptions.symmetric_difference(curr_assumptions))
    total_assumptions = len(prev_assumptions | curr_assumptions)
    if total_assumptions > 0:
        raw += _ASSUMPTION_PENALTY_FACTOR * (assumption_changes / total_assumptions)

    # Dependency drift penalty
    prev_deps = set(previous.dependencies)
    curr_deps = set(current.dependencies)
    dep_changes = len(prev_deps.symmetric_difference(curr_deps))
    total_deps = len(prev_deps | curr_deps)
    if total_deps > 0:
        raw += _DEPENDENCY_PENALTY_FACTOR * (dep_changes / total_deps)

    score = min(1.0, raw)

    details: list[str] = []
    if added:
        details.append(f"Added: {', '.join(added)}")
    if removed:
        details.append(f"Removed: {', '.join(removed)}")
    if modified:
        details.append(f"Modified: {', '.join(modified)}")
    if assumption_changes:
        details.append(f"Assumption changes: {assumption_changes}")
    if dep_changes:
        details.append(f"Dependency changes: {dep_changes}")

    return DriftResult(
        score=round(score, 4),
        passed=score < DRIFT_THRESHOLD,
        added=added,
        removed=removed,
        modified=modified,
        detail="; ".join(details) if details else "No drift detected.",
    )
