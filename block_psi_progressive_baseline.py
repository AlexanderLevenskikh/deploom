#!/usr/bin/env python3
"""Progressive Baseline orchestration primitives.

This module is intentionally proof-neutral.  It chooses the next exact direct-
dependency assignment to *try* from a physically verified incumbent.  It never
creates compatibility authority: ordinary Baseline resolver/project verification
must still accept the full candidate before it can replace the incumbent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Collection, Mapping, Sequence

AUTHORITY = "ORCHESTRATION_POLICY"


@dataclass(frozen=True)
class ProgressiveExtensionPlan:
    assignment: tuple[tuple[str, str], ...]
    packages: tuple[str, ...]
    assignment_fingerprint: str
    remaining_distance: int
    authority: str = AUTHORITY

    @property
    def assignment_dict(self) -> dict[str, str]:
        return dict(self.assignment)


def _matches_nogood(
    assignment: Mapping[str, str],
    nogood: Mapping[str, str],
) -> bool:
    return bool(nogood) and all(
        str(assignment.get(str(name), "")) == str(version)
        for name, version in nogood.items()
    )


def _normalized_atomic_groups(
    managed_names: set[str],
    atomic_groups: Sequence[Sequence[str]],
) -> tuple[tuple[str, ...], ...]:
    """Return a deterministic disjoint partition, conservatively merging overlaps."""
    pending: list[set[str]] = []
    for raw in atomic_groups:
        group = {str(name) for name in raw if str(name) in managed_names}
        if not group:
            continue
        merged = set(group)
        survivors: list[set[str]] = []
        for existing in pending:
            if merged & existing:
                merged.update(existing)
            else:
                survivors.append(existing)
        survivors.append(merged)
        pending = survivors

    covered = set().union(*pending) if pending else set()
    pending.extend({name} for name in sorted(managed_names - covered))
    return tuple(
        tuple(sorted(group))
        for group in sorted(pending, key=lambda item: (len(item), tuple(sorted(item))))
    )


def plan_progressive_extension(
    *,
    incumbent: Mapping[str, str],
    desired: Mapping[str, str],
    atomic_groups: Sequence[Sequence[str]],
    blocked_fingerprints: Collection[str],
    learned_nogoods: Sequence[Mapping[str, str]],
    fingerprint_fn: Callable[[Mapping[str, str]], str],
    priority_packages: Collection[str] = (),
) -> ProgressiveExtensionPlan | None:
    """Choose one bounded exact improvement on top of the verified incumbent.

    The returned assignment is complete over the incumbent's managed direct
    dependency keys.  A group is moved atomically to its desired values.  Known
    failed exact points and assignments already forbidden by authoritative
    learned clauses are skipped.  No transitive package can be introduced
    because candidates are restricted to keys already present in the incumbent.
    """
    base = {str(name): str(version) for name, version in incumbent.items()}
    target = {str(name): str(version) for name, version in desired.items()}
    managed = set(base) & set(target)
    if not managed:
        return None

    deferred = {name for name in managed if base[name] != target[name]}
    if not deferred:
        return None

    priority = {str(name) for name in priority_packages}
    groups = _normalized_atomic_groups(managed, atomic_groups)
    candidates: list[tuple[tuple[int, int, tuple[str, ...]], ProgressiveExtensionPlan]] = []
    blocked = {str(value) for value in blocked_fingerprints if str(value)}

    for group in groups:
        changed = tuple(name for name in group if name in deferred)
        if not changed:
            continue
        candidate = dict(base)
        for name in changed:
            candidate[name] = target[name]

        fingerprint = str(fingerprint_fn(candidate) or "")
        if not fingerprint or fingerprint in blocked:
            continue
        if any(_matches_nogood(candidate, nogood) for nogood in learned_nogoods):
            continue

        remaining = sum(
            1 for name in managed if candidate.get(name) != target.get(name)
        )
        if remaining >= len(deferred):
            continue
        plan = ProgressiveExtensionPlan(
            assignment=tuple(sorted(candidate.items())),
            packages=changed,
            assignment_fingerprint=fingerprint,
            remaining_distance=remaining,
        )
        # Required/user-priority cohorts first, then the most bounded atomic
        # change, then lexical order for deterministic replay.
        rank = (
            0 if any(name in priority for name in changed) else 1,
            len(changed),
            changed,
        )
        candidates.append((rank, plan))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]
