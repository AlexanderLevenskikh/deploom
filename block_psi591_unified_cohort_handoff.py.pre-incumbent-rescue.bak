"""Ψ.5.9.1 unified cohort-handoff planning.

This module is proof-neutral orchestration. It converts a bounded-search
exhaustion into one exact cohort fallback candidate using the existing cohort
inference, exact fallback construction, duplicate filtering, and Ψ.5.8.3
nested-loop control contract. The candidate remains unverified until the
ordinary package-manager/lifecycle/project-check pipeline accepts it.
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Iterable, Mapping, Optional, Sequence

AUTHORITY = "DIAGNOSTIC_HINT"


@dataclasses.dataclass(frozen=True)
class UnifiedCohortHandoffPlan:
    actionable: bool
    reason: str
    suggestion: object | None = None
    fallback: object | None = None
    assignment: tuple[tuple[str, str], ...] = ()
    fingerprint: str = ""
    queue_decision: object | None = None
    control: object | None = None
    authority: str = AUTHORITY

    @property
    def assignment_dict(self) -> dict[str, str]:
        return dict(self.assignment)


def plan_cohort_handoff(
    *,
    predicate: str,
    direct_packages: Iterable[str],
    assignment: Mapping[str, str],
    current_versions: Mapping[str, str],
    subject_consumers: Optional[Mapping[str, Iterable[str]]] = None,
    interaction_graph: Optional[Mapping[str, Iterable[str]]] = None,
    policy_by_package: Optional[Mapping[str, str]] = None,
    previous_deferred: Sequence[Mapping[str, object]] = (),
    repeated_count: int = 1,
    confirmed_failed_fingerprints: Iterable[str] = (),
    exact_exclusion_fingerprints: Iterable[str] = (),
    assignment_fingerprint_fn: Callable[[Mapping[str, str]], str] | None = None,
    focus_package: str = "",
    # Dependency injection is intentionally private/test-only. Production uses
    # the existing canonical functions loaded below.
    _infer_cohort=None,
    _build_fallback=None,
    _queue_decision=None,
    _handoff_control=None,
) -> UnifiedCohortHandoffPlan:
    """Create one proof-neutral exact handoff point for either search route.

    Both a direct predicate subject and a projected transitive predicate use this
    same function. It never adds a solver clause and never treats cohort
    membership as incompatibility evidence.
    """
    if _infer_cohort is None:
        from baseline_cohort_inference import infer_baseline_cohort
        _infer_cohort = infer_baseline_cohort
    if _build_fallback is None:
        from block_psi581_probe_budget_and_cohort import (
            build_cohort_fallback_assignment,
        )
        _build_fallback = build_cohort_fallback_assignment
    if _queue_decision is None:
        from block_psi582_pre_run_closure import cohort_fallback_queue_decision
        _queue_decision = cohort_fallback_queue_decision
    if _handoff_control is None:
        from block_psi583_duplicate_fallback_control import cohort_handoff_control
        _handoff_control = cohort_handoff_control

    if assignment_fingerprint_fn is None:
        return UnifiedCohortHandoffPlan(
            False, "assignment-fingerprint-function-missing"
        )

    suggestion = _infer_cohort(
        predicate=str(predicate or ""),
        direct_packages=tuple(direct_packages),
        focus_package=str(focus_package or ""),
        subject_consumers=subject_consumers,
        interaction_graph=interaction_graph,
        policy_by_package=policy_by_package,
        previous_deferred=previous_deferred,
        repeated_count=max(1, int(repeated_count or 1)),
    )
    if suggestion is None:
        return UnifiedCohortHandoffPlan(False, "cohort-inference-unavailable")

    fallback = _build_fallback(
        assignment=assignment,
        current_versions=current_versions,
        cohort_packages=suggestion.packages,
    )
    if fallback is None or not bool(getattr(fallback, "changed", False)):
        return UnifiedCohortHandoffPlan(
            False,
            "cohort-fallback-no-change",
            suggestion=suggestion,
            fallback=fallback,
        )

    candidate = dict(getattr(fallback, "assignment_dict"))
    fingerprint = str(assignment_fingerprint_fn(candidate) or "").strip()
    if not fingerprint:
        return UnifiedCohortHandoffPlan(
            False,
            "cohort-fallback-fingerprint-missing",
            suggestion=suggestion,
            fallback=fallback,
        )

    decision = _queue_decision(
        fingerprint,
        confirmed_failed_fingerprints=tuple(
            str(item) for item in confirmed_failed_fingerprints if str(item)
        ),
        exact_exclusion_fingerprints=tuple(
            str(item) for item in exact_exclusion_fingerprints if str(item)
        ),
    )
    control = _handoff_control(decision)

    return UnifiedCohortHandoffPlan(
        True,
        str(getattr(control, "reason", "") or "cohort-handoff-planned"),
        suggestion=suggestion,
        fallback=fallback,
        assignment=tuple(sorted((str(k), str(v)) for k, v in candidate.items())),
        fingerprint=fingerprint,
        queue_decision=decision,
        control=control,
    )
