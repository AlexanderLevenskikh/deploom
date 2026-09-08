"""Ψ.5.8.2 pre-run closure for bounded causal search.

This module owns search-cost bookkeeping only. It cannot create solver clauses,
promote diagnostic evidence, or certify cohort incompatibility. Exact physical
verification remains the only authority for candidate success/failure.
"""
from __future__ import annotations

import dataclasses
from typing import Iterable, Sequence

from block_psi581_probe_budget_and_cohort import (
    CausalPhysicalProbeLedger,
    ProbePermit,
)

AUTHORITY = "DIAGNOSTIC_HINT"


@dataclasses.dataclass(frozen=True)
class CohortQueueDecision:
    queue: bool
    reason: str
    authority: str = AUTHORITY


def cohort_fallback_queue_decision(
    candidate_fingerprint: str,
    *,
    confirmed_failed_fingerprints: Iterable[str] = (),
    exact_exclusion_fingerprints: Iterable[str] = (),
) -> CohortQueueDecision:
    """Reject only an exact fallback point already known to fail.

    The decision says nothing about the inferred cohort itself. It merely prevents
    re-scheduling an exact assignment whose failure is already authoritative.
    """
    fingerprint = str(candidate_fingerprint or "").strip()
    if not fingerprint:
        return CohortQueueDecision(False, "candidate-fingerprint-missing")
    confirmed = {str(item) for item in confirmed_failed_fingerprints if str(item)}
    if fingerprint in confirmed:
        return CohortQueueDecision(False, "confirmed-failure")
    exact = {str(item) for item in exact_exclusion_fingerprints if str(item)}
    if fingerprint in exact:
        return CohortQueueDecision(False, "exact-exclusion")
    return CohortQueueDecision(True, "new-exact-candidate")


class CausalFamilyBudgetHydrator:
    """Load persisted family cost exactly once per run/project/mode/predicate.

    Either main causal search or branch continuation may be the first caller after
    resume. The first caller reconstructs *all* direct-package costs into the same
    in-memory ledger before any permit can be granted.
    """

    def __init__(self) -> None:
        self._hydrated: set[tuple[str, str, str, str]] = set()

    @staticmethod
    def _key(
        run_identity: str,
        project: str,
        mode: str,
        predicate: str,
    ) -> tuple[str, str, str, str]:
        return (
            str(run_identity),
            str(project),
            str(mode),
            str(predicate),
        )

    def ensure(
        self,
        *,
        ledger: CausalPhysicalProbeLedger,
        predicate_state_store: object,
        run_identity: str,
        project: str,
        mode: str,
        predicate: str,
        packages: Sequence[str],
    ) -> int:
        key = self._key(run_identity, project, mode, predicate)
        if key not in self._hydrated:
            for package in dict.fromkeys(str(item) for item in packages if str(item)):
                session = predicate_state_store.load_session(
                    project,
                    mode,
                    run_identity=run_identity,
                    package=package,
                    predicate=predicate,
                )
                ledger.sync(
                    run_identity=run_identity,
                    project=project,
                    mode=mode,
                    predicate=predicate,
                    package=package,
                    attempted_versions=session.attempted_versions,
                )
            self._hydrated.add(key)
        return ledger.family_used(
            run_identity=run_identity,
            project=project,
            mode=mode,
            predicate=predicate,
        )


def reserve_causal_physical_probe(
    *,
    ledger: CausalPhysicalProbeLedger,
    hydrator: CausalFamilyBudgetHydrator,
    predicate_state_store: object,
    run_identity: str,
    project: str,
    mode: str,
    predicate: str,
    package: str,
    version: str,
    family_packages: Sequence[str],
) -> ProbePermit:
    """Hydrate -> acquire -> persist reservation, all before the verifier runs.

    A crash after ``mark_attempt`` but before/during physical verification may
    conservatively consume one slot, but can never regain already-paid physical
    cost after restart. Persistence failure denies the probe rather than running
    an unaccounted experiment.
    """
    hydrator.ensure(
        ledger=ledger,
        predicate_state_store=predicate_state_store,
        run_identity=run_identity,
        project=project,
        mode=mode,
        predicate=predicate,
        packages=family_packages,
    )
    permit = ledger.try_acquire(
        run_identity=run_identity,
        project=project,
        mode=mode,
        predicate=predicate,
        package=package,
    )
    if not permit.granted:
        return permit
    try:
        predicate_state_store.mark_attempt(
            project,
            mode,
            run_identity=run_identity,
            package=package,
            predicate=predicate,
            version=version,
        )
    except Exception as exc:
        # The in-memory slot remains consumed. This is conservative and prevents
        # retry storms in the current process; no physical verifier is authorized.
        return dataclasses.replace(
            permit,
            granted=False,
            reason=f"reservation-persist-failed:{type(exc).__name__}",
        )
    return permit
