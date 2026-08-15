#!/usr/bin/env python3
"""Transition-safety refinement for dependency compatibility cohorts.

A final dependency assignment can be valid while an intermediate branch state is
invalid.  This module treats each tentative cohort as one Boolean toggle:
``False`` keeps every member at the current version, ``True`` applies every
member's target version.  It then proves that every hard IR constraint is safe
for arbitrary combinations of those toggles.

For the IR used by Dependency Flow, a violation is a conjunction of exact toggle
requirements.  If a violation requires one tentative cohort to be applied and
another to remain current, those cohorts cannot be independent; merging one
opposite-valued pair makes that violation unreachable.  Repeating to a fixed
point yields an order-independent safe partition without enumerating 2^N branch
subsets.
"""
from __future__ import annotations

import dataclasses
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from peer_solver_model import ForbiddenCombination, PeerOptimizationModel, RequiresAny


@dataclasses.dataclass(frozen=True)
class TransitionMerge:
    left: Tuple[str, ...]
    right: Tuple[str, ...]
    reason: str
    provenance: str


@dataclasses.dataclass(frozen=True)
class TransitionSafetyResult:
    groups: Tuple[Tuple[str, ...], ...]
    merges: Tuple[TransitionMerge, ...] = ()
    unresolved: Tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.unresolved


class _UnionFind:
    def __init__(self, names: Iterable[str]):
        self.parent = {name: name for name in names}
        self.members: Dict[str, Set[str]] = {name: {name} for name in names}

    def find(self, name: str) -> str:
        parent = self.parent[name]
        if parent != name:
            self.parent[name] = self.find(parent)
        return self.parent[name]

    def union(self, left: str, right: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            members = tuple(sorted(self.members[left_root]))
            return members, members
        left_members = tuple(sorted(self.members[left_root]))
        right_members = tuple(sorted(self.members[right_root]))
        # Stable root selection makes the resulting partition deterministic and
        # independent of Python hash/set iteration order.
        if (len(left_members), left_members) > (len(right_members), right_members):
            left_root, right_root = right_root, left_root
            left_members, right_members = right_members, left_members
        self.parent[right_root] = left_root
        self.members[left_root].update(self.members.pop(right_root))
        return left_members, right_members

    def group(self, name: str) -> Tuple[str, ...]:
        return tuple(sorted(self.members[self.find(name)]))

    def groups(self) -> Tuple[Tuple[str, ...], ...]:
        unique = {self.find(name) for name in self.parent}
        return tuple(sorted((tuple(sorted(self.members[root])) for root in unique), key=lambda item: item))


def _exact_literal_toggle(
    name: str,
    version: str,
    current: Mapping[str, str],
    target: Mapping[str, str],
) -> Tuple[str, Optional[bool]]:
    """Return (kind, toggle): kind is impossible/always/toggle."""
    current_version = current[name]
    target_version = target[name]
    if current_version == target_version:
        return ("always", None) if version == current_version else ("impossible", None)
    if version == current_version:
        return "toggle", False
    if version == target_version:
        return "toggle", True
    return "impossible", None


def _provider_bad_toggle(
    name: str,
    allowed_versions: Sequence[str],
    current: Mapping[str, str],
    target: Mapping[str, str],
) -> Tuple[str, Optional[bool]]:
    current_bad = current[name] not in allowed_versions
    target_bad = target[name] not in allowed_versions
    if not current_bad and not target_bad:
        return "impossible", None
    if current_bad and target_bad:
        return "always", None
    return "toggle", bool(target_bad)


def _collapse_toggle_conditions(
    conditions: Iterable[Tuple[str, str, Optional[bool]]],
    union_find: _UnionFind,
) -> Tuple[str, Dict[str, bool]]:
    required: Dict[str, bool] = {}
    for name, kind, toggle in conditions:
        if kind == "impossible":
            return "impossible", {}
        if kind == "always":
            continue
        root = union_find.find(name)
        desired = bool(toggle)
        if root in required and required[root] != desired:
            # Members of one cohort switch together, therefore this violation
            # cannot be reached after the existing merge.
            return "impossible", {}
        required[root] = desired
    return "possible", required


def _forbidden_toggle_pattern(
    constraint: ForbiddenCombination,
    current: Mapping[str, str],
    target: Mapping[str, str],
    union_find: _UnionFind,
) -> Tuple[str, Dict[str, bool]]:
    conditions = []
    for name, version in constraint.literals:
        if name not in current or name not in target:
            return "impossible", {}
        kind, toggle = _exact_literal_toggle(name, version, current, target)
        conditions.append((name, kind, toggle))
    return _collapse_toggle_conditions(conditions, union_find)


def _requirement_toggle_pattern(
    requirement: RequiresAny,
    current: Mapping[str, str],
    target: Mapping[str, str],
    union_find: _UnionFind,
) -> Tuple[str, Dict[str, bool]]:
    trigger_name, trigger_version = requirement.trigger
    provider = requirement.provider
    if trigger_name not in current or provider not in current or trigger_name not in target or provider not in target:
        return "impossible", {}
    trigger_kind, trigger_toggle = _exact_literal_toggle(
        trigger_name, trigger_version, current, target
    )
    provider_kind, provider_toggle = _provider_bad_toggle(
        provider, requirement.allowed_versions, current, target
    )
    return _collapse_toggle_conditions(
        (
            (trigger_name, trigger_kind, trigger_toggle),
            (provider, provider_kind, provider_toggle),
        ),
        union_find,
    )


def _choose_opposite_pair(required: Mapping[str, bool], union_find: _UnionFind) -> Optional[Tuple[str, str]]:
    false_roots = [root for root, value in required.items() if not value]
    true_roots = [root for root, value in required.items() if value]
    if not false_roots or not true_roots:
        return None
    candidates = []
    for left in false_roots:
        for right in true_roots:
            left_group = union_find.group(left)
            right_group = union_find.group(right)
            candidates.append((len(left_group) + len(right_group), left_group, right_group, left, right))
    candidates.sort()
    return candidates[0][3], candidates[0][4]


def refine_transition_safe_groups(
    model: PeerOptimizationModel,
    current_assignment: Mapping[str, str],
    target_assignment: Mapping[str, str],
    *,
    initial_groups: Optional[Iterable[Iterable[str]]] = None,
) -> TransitionSafetyResult:
    """Return a deterministic order-independent transition-safe partition.

    ``initial_groups`` may contain peer/nogood cohorts already known to be
    atomic.  Every model package not present there starts as a singleton.
    Merging only ever makes execution more conservative; it never changes the
    final version assignment.
    """
    names = [package.name for package in model.packages]
    expected = set(names)
    if set(current_assignment) & expected != expected:
        missing = sorted(expected - set(current_assignment))
        raise ValueError(f"transition current assignment is missing: {', '.join(missing)}")
    if set(target_assignment) & expected != expected:
        missing = sorted(expected - set(target_assignment))
        raise ValueError(f"transition target assignment is missing: {', '.join(missing)}")

    union_find = _UnionFind(names)
    for group in initial_groups or ():
        members = sorted(set(group) & expected)
        if len(members) < 2:
            continue
        anchor = members[0]
        for name in members[1:]:
            union_find.union(anchor, name)

    merges: List[TransitionMerge] = []
    unresolved: List[str] = []

    # Each successful merge strictly decreases the number of groups, so this
    # loop is bounded by N-1 merges even for cyclic/hypergraph constraints.
    while True:
        changed = False
        unresolved = []
        checks = []
        for constraint in model.constraints:
            checks.append((
                constraint.reason or "FORBIDDEN_COMBINATION",
                constraint.provenance,
                _forbidden_toggle_pattern(constraint, current_assignment, target_assignment, union_find),
            ))
        for requirement in model.requirements:
            checks.append((
                requirement.reason or "REQUIRES_ALLOWED_VERSION",
                requirement.provenance,
                _requirement_toggle_pattern(requirement, current_assignment, target_assignment, union_find),
            ))

        for reason, provenance, (kind, required) in checks:
            if kind == "impossible":
                continue
            pair = _choose_opposite_pair(required, union_find)
            if pair is None:
                # A violation with no opposite toggle values cannot be repaired
                # by coupling branches. It means either the all-current or the
                # all-target endpoint itself violates the IR (or the constraint
                # is unconditional), which is a model/solver invariant breach.
                if not required:
                    unresolved.append(f"UNCONDITIONAL_TRANSITION_CONSTRAINT: {reason}")
                else:
                    values = sorted(set(required.values()))
                    endpoint = "target" if values == [True] else "current" if values == [False] else "unknown"
                    unresolved.append(f"INVALID_{endpoint.upper()}_ENDPOINT: {reason}")
                continue
            left_root, right_root = pair
            left_group, right_group = union_find.union(left_root, right_root)
            merges.append(TransitionMerge(left_group, right_group, reason, provenance))
            changed = True
            break
        if not changed:
            break

    return TransitionSafetyResult(
        groups=union_find.groups(),
        merges=tuple(merges),
        unresolved=tuple(dict.fromkeys(unresolved)),
    )


def transition_assignment_for_groups(
    current_assignment: Mapping[str, str],
    target_assignment: Mapping[str, str],
    groups: Sequence[Sequence[str]],
    applied_group_indexes: Iterable[int],
) -> Dict[str, str]:
    """Materialize one transition-hypercube vertex; intended for tests/proofs."""
    result = dict(current_assignment)
    applied = set(applied_group_indexes)
    for index, group in enumerate(groups):
        if index not in applied:
            continue
        for name in group:
            if name in target_assignment:
                result[name] = target_assignment[name]
    return result
