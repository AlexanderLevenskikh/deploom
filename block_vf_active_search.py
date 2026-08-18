#!/usr/bin/env python3
"""Block V-F bounded active predicate exploration.

This module owns *diagnostic experiment selection only*.  It never mutates the
solver domain and never produces a constraint.  The caller supplies a real
verification callback; only the existing Baseline confirmation/minimization
pipeline may promote evidence to solver authority.
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Mapping, Sequence

from block_v_predicate_search import (
    DiagnosticHint,
    PredicateObservation,
    PredicateProbePolicy,
    controlled_probe_assignment,
    predicate_repeat_count,
    rank_version_probes,
)

PROBE_OUTCOME_PRESENT = "present"
PROBE_OUTCOME_ABSENT = "absent"
PROBE_OUTCOME_INCONCLUSIVE = "inconclusive"


@dataclasses.dataclass(frozen=True)
class ProbeExecution:
    version: str
    outcome: str
    assignment_fingerprint: str = ""
    other_predicates: tuple[str, ...] = ()
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class ActiveSearchResult:
    activated: bool
    repeat_count: int
    observations: tuple[PredicateObservation, ...]
    attempted_versions: tuple[str, ...]
    executions: tuple[ProbeExecution, ...] = ()
    preferred_version: str = ""
    remaining_candidates: int = 0


ProbeRunner = Callable[[str, Mapping[str, str]], ProbeExecution]


def run_active_predicate_search(
    *,
    package: str,
    predicate: str,
    base_assignment: Mapping[str, str],
    project_current_version: str,
    versions: Sequence[str],
    observations: Sequence[PredicateObservation],
    attempted_versions: Sequence[str] = (),
    hints: Sequence[DiagnosticHint] = (),
    policy: PredicateProbePolicy = PredicateProbePolicy(),
    run_probe: ProbeRunner,
) -> ActiveSearchResult:
    """Run a bounded sequence of high-information exact point experiments.

    ``preferred_version`` is merely a navigation suggestion. The caller may use
    it to reorder otherwise-complete solver candidates, but must not convert it
    into a clause or remove any version from the authoritative domain.
    """
    working = list(observations)
    attempts = {str(item) for item in attempted_versions if str(item)}
    repeats = predicate_repeat_count(
        package=package, predicate=predicate, observations=working
    )
    if not policy.enabled or repeats < policy.repeat_threshold:
        ranked = rank_version_probes(
            package=package,
            predicate=predicate,
            versions=versions,
            observations=working,
            hints=hints,
        )
        return ActiveSearchResult(
            activated=False,
            repeat_count=repeats,
            observations=tuple(working),
            attempted_versions=tuple(sorted(attempts)),
            remaining_candidates=len(ranked),
        )

    executions: list[ProbeExecution] = []
    preferred = ""
    for _probe_index in range(policy.probe_budget):
        ranked = rank_version_probes(
            package=package,
            predicate=predicate,
            versions=versions,
            observations=working,
            hints=hints,
        )
        candidate = next(
            (item for item in ranked if item.version not in attempts),
            None,
        )
        if candidate is None:
            break
        attempts.add(candidate.version)
        assignment = controlled_probe_assignment(
            base_assignment,
            package=package,
            version=candidate.version,
        )
        execution = run_probe(candidate.version, assignment)
        if execution.version != candidate.version:
            raise AssertionError("PREDICATE_PROBE_RUNNER_VERSION_MISMATCH")
        if execution.outcome not in {
            PROBE_OUTCOME_PRESENT,
            PROBE_OUTCOME_ABSENT,
            PROBE_OUTCOME_INCONCLUSIVE,
        }:
            raise AssertionError("PREDICATE_PROBE_RUNNER_OUTCOME_INVALID")
        executions.append(execution)

        if execution.outcome != PROBE_OUTCOME_INCONCLUSIVE:
            point = PredicateObservation(
                package=package,
                version=candidate.version,
                predicate=predicate,
                present=execution.outcome == PROBE_OUTCOME_PRESENT,
                assignment_fingerprint=execution.assignment_fingerprint,
                other_predicates=tuple(sorted(set(execution.other_predicates))),
            )
            if point not in working:
                working.append(point)

        # Current-version evidence is valuable as the opposite side of a
        # bracket, but steering the solver to current would intentionally undo
        # an update. Keep probing for a non-current compatible point instead.
        if (
            execution.outcome == PROBE_OUTCOME_ABSENT
            and candidate.version != project_current_version
        ):
            preferred = candidate.version
            break

    remaining = rank_version_probes(
        package=package,
        predicate=predicate,
        versions=versions,
        observations=working,
        hints=hints,
    )
    return ActiveSearchResult(
        activated=True,
        repeat_count=repeats,
        observations=tuple(working),
        attempted_versions=tuple(sorted(attempts)),
        executions=tuple(executions),
        preferred_version=preferred,
        remaining_candidates=len(remaining),
    )
