"""Ψ.5.8.1 shared physical-probe budget and cohort fallback primitives.

All state here is search/performance state. It cannot create solver authority,
learn incompatibility, or certify a cohort as conflicting. Every produced
assignment still requires ordinary full Baseline verification.
"""
from __future__ import annotations

import dataclasses
from typing import Mapping, Sequence

from block_psi58_bounded_causal_search import CausalSearchPolicy

AUTHORITY = "DIAGNOSTIC_HINT"


@dataclasses.dataclass(frozen=True)
class ProbePermit:
    granted: bool
    reason: str
    package_used: int
    family_used: int
    package_remaining: int
    family_remaining: int
    authority: str = AUTHORITY


class CausalPhysicalProbeLedger:
    """One budget authority for main predicate search and branch continuation.

    Counts are scoped by (run identity, project, mode, predicate, package).
    ``sync`` reconstructs lower bounds from persisted attempted-version state;
    ``try_acquire`` reserves cost *before* physical verification.
    """

    def __init__(self, policy: CausalSearchPolicy) -> None:
        self.policy = policy
        self._counts: dict[tuple[str, str, str, str, str], int] = {}

    @staticmethod
    def _key(
        run_identity: str,
        project: str,
        mode: str,
        predicate: str,
        package: str,
    ) -> tuple[str, str, str, str, str]:
        return (
            str(run_identity),
            str(project),
            str(mode),
            str(predicate),
            str(package).lower(),
        )

    def sync(
        self,
        *,
        run_identity: str,
        project: str,
        mode: str,
        predicate: str,
        package: str,
        attempted_versions: Sequence[str],
    ) -> None:
        key = self._key(run_identity, project, mode, predicate, package)
        observed = len({str(v) for v in attempted_versions if str(v)})
        self._counts[key] = max(self._counts.get(key, 0), observed)

    def package_used(
        self,
        *,
        run_identity: str,
        project: str,
        mode: str,
        predicate: str,
        package: str,
    ) -> int:
        return self._counts.get(
            self._key(run_identity, project, mode, predicate, package), 0
        )

    def family_used(
        self,
        *,
        run_identity: str,
        project: str,
        mode: str,
        predicate: str,
    ) -> int:
        prefix = (
            str(run_identity),
            str(project),
            str(mode),
            str(predicate),
        )
        return sum(
            count
            for key, count in self._counts.items()
            if key[:4] == prefix
        )

    def try_acquire(
        self,
        *,
        run_identity: str,
        project: str,
        mode: str,
        predicate: str,
        package: str,
    ) -> ProbePermit:
        key = self._key(run_identity, project, mode, predicate, package)
        package_used = self._counts.get(key, 0)
        family_used = self.family_used(
            run_identity=run_identity,
            project=project,
            mode=mode,
            predicate=predicate,
        )
        package_remaining = max(
            0, self.policy.per_package_probe_budget - package_used
        )
        family_remaining = max(
            0, self.policy.family_probe_budget - family_used
        )
        if package_remaining <= 0:
            return ProbePermit(
                False,
                "per-package-physical-budget-exhausted",
                package_used,
                family_used,
                0,
                family_remaining,
            )
        if family_remaining <= 0:
            return ProbePermit(
                False,
                "family-physical-budget-exhausted",
                package_used,
                family_used,
                package_remaining,
                0,
            )
        self._counts[key] = package_used + 1
        return ProbePermit(
            True,
            "granted",
            package_used + 1,
            family_used + 1,
            max(0, package_remaining - 1),
            max(0, family_remaining - 1),
        )


@dataclasses.dataclass(frozen=True)
class CohortFallbackCandidate:
    assignment: tuple[tuple[str, str], ...]
    deferred_packages: tuple[str, ...]
    changed: bool
    authority: str = AUTHORITY

    @property
    def assignment_dict(self) -> dict[str, str]:
        return dict(self.assignment)


def build_cohort_fallback_assignment(
    *,
    assignment: Mapping[str, str],
    current_versions: Mapping[str, str],
    cohort_packages: Sequence[str],
) -> CohortFallbackCandidate:
    """Reset only inferred direct cohort packages to current versions.

    This is a candidate-generation operation, not evidence that those packages
    are incompatible. The rest of the desired/solver assignment is preserved.
    """
    candidate = {str(k): str(v) for k, v in assignment.items()}
    deferred: list[str] = []
    for package in cohort_packages:
        name = str(package)
        if name not in candidate or name not in current_versions:
            continue
        current = str(current_versions[name])
        if candidate[name] == current:
            continue
        candidate[name] = current
        deferred.append(name)
    normalized = tuple(sorted(candidate.items()))
    return CohortFallbackCandidate(
        assignment=normalized,
        deferred_packages=tuple(sorted(deferred)),
        changed=bool(deferred),
    )
