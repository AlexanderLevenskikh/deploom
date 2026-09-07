#!/usr/bin/env python3
"""Ψ.5.4 bounded continuation of useful predicate branches.

This module owns diagnostic scheduling only.  It never mutates a solver domain,
creates a nogood, or upgrades point evidence to proof authority.

A branch may be continued only from a *fresh same-run* ProbeExecution that:
- matches the just-selected preferred version,
- physically observed the current predicate as ABSENT,
- carries one or more remaining structural predicates,
- carries the exact assignment fingerprint of that physical point.

Persisted predicate state remains useful for ranking, but cannot reconstruct a
branch assignment after restart.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Mapping, Optional, Sequence

from block_v_predicate_search import ProbeCandidate, predicate_package
from block_vf_active_search import (
    PROBE_OUTCOME_ABSENT,
    ProbeExecution,
)


@dataclasses.dataclass(frozen=True)
class PredicateBranchPolicy:
    """Bounded economic policy for diagnostic branch continuation."""

    enabled: bool = True
    max_depth: int = 2
    probe_budget: int = 2

    @classmethod
    def from_sources(
        cls,
        config: Optional[Mapping[str, object]] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> "PredicateBranchPolicy":
        raw = dict(config or {})
        env = environment if environment is not None else os.environ

        enabled = _coerce_bool(
            raw.get("predicateBranchContinuation"),
            True,
        )
        if "DEPLOOM_PREDICATE_BRANCH_CONTINUATION" in env:
            enabled = _coerce_bool(
                env.get("DEPLOOM_PREDICATE_BRANCH_CONTINUATION"),
                enabled,
            )

        max_depth = _bounded_int(
            raw.get("predicateBranchMaxDepth", 2),
            minimum=1,
            maximum=3,
            default=2,
        )
        if env.get("DEPLOOM_PREDICATE_BRANCH_MAX_DEPTH"):
            max_depth = _bounded_int(
                env.get("DEPLOOM_PREDICATE_BRANCH_MAX_DEPTH"),
                minimum=1,
                maximum=3,
                default=max_depth,
            )

        probe_budget = _bounded_int(
            raw.get("predicateBranchProbeBudget", 2),
            minimum=1,
            maximum=4,
            default=2,
        )
        if env.get("DEPLOOM_PREDICATE_BRANCH_PROBE_BUDGET"):
            probe_budget = _bounded_int(
                env.get("DEPLOOM_PREDICATE_BRANCH_PROBE_BUDGET"),
                minimum=1,
                maximum=4,
                default=probe_budget,
            )

        return cls(
            enabled=bool(enabled),
            max_depth=max_depth,
            probe_budget=probe_budget,
        )


@dataclasses.dataclass(frozen=True)
class FreshPredicateBranchSeed:
    source_package: str
    source_predicate: str
    preferred_version: str
    assignment_fingerprint: str
    other_predicates: tuple[str, ...]


def fresh_predicate_branch_seed(
    *,
    package: str,
    predicate: str,
    preferred_version: str,
    executions: Sequence[ProbeExecution],
) -> FreshPredicateBranchSeed | None:
    """Return a seed only when this invocation physically produced it."""

    normalized_preferred = str(preferred_version or "").strip()
    if not package or not predicate or not normalized_preferred:
        return None

    matches = [
        execution
        for execution in executions
        if execution.version == normalized_preferred
    ]
    if len(matches) != 1:
        # Duplicate/conflicting fresh observations should not be interpreted.
        return None
    execution = matches[0]
    if execution.outcome != PROBE_OUTCOME_ABSENT:
        return None
    if not execution.assignment_fingerprint:
        return None
    remaining = tuple(
        sorted(
            {
                str(item).strip()
                for item in execution.other_predicates
                if str(item).strip() and str(item).strip() != predicate
            }
        )
    )
    if not remaining:
        return None
    return FreshPredicateBranchSeed(
        source_package=str(package),
        source_predicate=str(predicate),
        preferred_version=normalized_preferred,
        assignment_fingerprint=execution.assignment_fingerprint,
        other_predicates=remaining,
    )


def select_followup_predicate(
    *,
    predicates: Sequence[str],
    source_package: str,
    assignment: Mapping[str, str],
    visited_packages: Sequence[str] = (),
) -> tuple[str, str] | None:
    """Choose a deterministic direct-package predicate without branch ping-pong."""

    blocked = {
        str(item).strip().lower()
        for item in visited_packages
        if str(item).strip()
    }
    if source_package:
        blocked.add(source_package.lower())

    for predicate in sorted({str(item).strip() for item in predicates if str(item).strip()}):
        package = predicate_package(predicate)
        if not package or package.lower() in blocked:
            continue
        if not str(assignment.get(package, "") or "").strip():
            continue
        return predicate, package
    return None


def choose_followup_version(
    *,
    ranked: Sequence[ProbeCandidate],
    attempted_versions: Sequence[str],
    branch_current_version: str,
    project_current_version: str,
) -> str:
    """Pick one untried non-current upgrade point.

    Returning an empty string means "stop this branch".  We intentionally do
    not steer to the project's current version here; current-version evidence
    may be useful for boundaries, but Ψ.5.4 exists to expand verified upgrade
    scope rather than silently undo an update.
    """

    attempted = {str(item) for item in attempted_versions if str(item)}
    blocked = {
        str(branch_current_version or ""),
        str(project_current_version or ""),
    }
    for candidate in ranked:
        version = str(candidate.version or "")
        if not version or version in attempted or version in blocked:
            continue
        return version
    return ""


def _coerce_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    return default


def _bounded_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
