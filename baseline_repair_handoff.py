"""Part E (2026-09-27): shared candidate->verify->repair->verify->accept handoff.

A `project`-kind verification failure is SNAPSHOT-LOCAL migration evidence: it
means the project source/config must be repaired for the exact SAME version
tuple, not that the tuple is dependency-incompatible. Feeding such a failure
into the solver's confirmed-failed / exact-exclusion / learned-nogood machinery
would poison the solver cache with an eternal dependency nogood for a tuple
that passes after the config repair (the acceptance scenario: "тот же набор
версий падает до config repair и проходит после него").

This module owns the handoff primitives and the cumulative-merge guard. It is
deliberately proof-neutral (like block_psi_progressive_baseline): authority is
created only by the resolver verification itself (dependency/preparation kind)
or by a FRESH verify-after-repair pass on the repaired snapshot. A proof from
before a code change is never carried to the post-repair snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def is_repairable_project_failure(result: Any) -> bool:
    """A project-kind verification failure is a repair/migration handoff.

    Dependency/preparation failures are authoritative solver evidence; a
    project failure only proves the CURRENT source/config snapshot is not
    migrated for the assignment yet. `None`/unknown kinds are never treated
    as repairable (they stay infrastructure/inconclusive).
    """
    return bool(getattr(result, "kind", None) == "project")


def _result_failing_commands(result: Any) -> Tuple[Tuple[str, int], ...]:
    """(command, exit_code) pairs from a verification result, most {()}
    specific first; falls back to the single top-level command."""
    failures = getattr(result, "project_failures", ()) or ()
    pairs: List[Tuple[str, int]] = []
    for failure in failures:
        command = str(getattr(failure, "command", "") or "").strip()
        code = getattr(failure, "exit_code", None)
        if command and isinstance(code, int):
            pairs.append((command, code))
    if not pairs:
        command = str(getattr(result, "command", "") or "").strip()
        code = getattr(result, "exit_code", None)
        if isinstance(code, int):
            pairs.append((command, code))
    return tuple(pairs)


@dataclass(frozen=True)
class RepairRequest:
    """Machine-readable repair handoff consumed by the Desktop Executor.

    The Executor repairs source/config in an isolated checkout and returns a
    machine-readable outcome; the orchestrator then reruns verification itself
    (an agent's "all green" message is never proof). If the outcome is
    replan-required, the assignment returns to the planner untouched.
    """

    project: str
    mode: str
    assignment: Tuple[Tuple[str, str], ...]
    fingerprint: str
    request_id: str
    snapshot_identity: str
    structural_signatures: Tuple[str, ...] = ()
    failing_commands: Tuple[Tuple[str, int], ...] = ()
    diagnostics_tail: str = ""
    reason: str = "project"

    @property
    def assignment_dict(self) -> Dict[str, str]:
        return dict(self.assignment)

    def to_json(self) -> Dict[str, Any]:
        return {
            "project": self.project,
            "mode": self.mode,
            "assignment": dict(self.assignment),
            "fingerprint": self.fingerprint,
            "requestId": self.request_id,
            "snapshotIdentity": self.snapshot_identity,
            "structuralSignatures": list(self.structural_signatures),
            "failingCommands": [
                {"command": command, "exitCode": code}
                for command, code in self.failing_commands
            ],
            "diagnosticsTail": self.diagnostics_tail,
            "reason": self.reason,
            "disposition": "repair-or-replan",
        }


def build_repair_request(
    *,
    project: str,
    mode: str,
    assignment: Mapping[str, str],
    result: Any,
    snapshot_identity: str,
    fingerprint: str = "",
    structural_signatures: Sequence[str] = (),
    request_id: Optional[str] = None,
    diagnostics_tail_max_chars: int = 4000,
) -> RepairRequest:
    """Wrap a repairable project failure into a machine-readable repair request.

    `snapshot_identity` binds the request to the exact source/config snapshot
    the failure was observed on, so a proof from before the code change can
    never be carried to the post-repair snapshot. `fingerprint` is the exact
    assignment fingerprint that is being deferred (not a field on the verify
    result itself).
    """
    import hashlib

    plain = ",".join(f"{name}@{version}" for name, version in sorted(assignment.items()))
    request_id = request_id or hashlib.sha256(f"{project}:{mode}:{plain}".encode("utf-8")).hexdigest()[:16]
    diagnostic = str(getattr(result, "output", "") or "").strip()
    return RepairRequest(
        project=project,
        mode=mode,
        assignment=tuple(sorted(assignment.items())),
        fingerprint=fingerprint,
        request_id=request_id,
        snapshot_identity=snapshot_identity,
        structural_signatures=tuple(structural_signatures),
        failing_commands=_result_failing_commands(result),
        diagnostics_tail=diagnostic[-diagnostics_tail_max_chars:],
        reason="project-source-config-repair",
    )


class RepairHandoffStore:
    """Run-scoped deferral registry for repairable project failures.

    Deferred fingerprints are NOT solver authority: they are excluded from
    re-planning only WHILE deferred (so the loop does not spin on the tuple),
    and a fingerprinted request can be resolved after a fresh verify-after-
    repair pass accepts the SAME assignment. Nothing here is ever learned as
    a nogood / exact exclusion / confirmed-failed assignment.
    """

    def __init__(self) -> None:
        self._deferred: Dict[Tuple[str, str], Dict[str, RepairRequest]] = {}

    def defer(self, project: str, mode: str, request: RepairRequest) -> bool:
        bucket = self._deferred.setdefault((project, mode), {})
        if request.fingerprint in bucket:
            return False
        bucket[request.fingerprint] = request
        return True

    def resolve(self, project: str, mode: str, fingerprint: str) -> Optional[RepairRequest]:
        request = self._deferred.get((project, mode), {}).pop(fingerprint, None)
        return request

    def is_deferred(self, project: str, mode: str, fingerprint: str) -> bool:
        return fingerprint in self._deferred.get((project, mode), {})

    def fingerprints(self, project: str, mode: str) -> frozenset:
        return frozenset(self._deferred.get((project, mode), {}))

    def requests(self, project: str, mode: str) -> Tuple[RepairRequest, ...]:
        bucket = self._deferred.get((project, mode), {})
        return tuple(
            bucket[fingerprint] for fingerprint in sorted(bucket)
        )

    def all_requests(self) -> Tuple[RepairRequest, ...]:
        result: List[RepairRequest] = []
        for bucket in self._deferred.values():
            result.extend(bucket[fingerprint] for fingerprint in sorted(bucket))
        return tuple(result)


def cumulative_merge_verdict(
    *,
    independently_successful: Iterable[str],
    merged_verify_passed: bool,
    merged_assignment: Mapping[str, str],
    failing_commands: Sequence[str] = (),
) -> Dict[str, Any]:
    """Cumulative-merge guard (Part E acceptance).

    Two steps that pass IN ISOLATION are never silently absorbed into a
    cumulative proof: the merged tree must pass its OWN verification on the
    merged snapshot. An incompatible cumulative merge is rejected and handed
    back to planner/repair with the failing evidence; it is never accepted.
    """
    independent = list(independently_successful)
    if merged_verify_passed:
        return {
            "accepted": True,
            "mergedVerified": True,
            "independentlySuccessful": independent,
            "mergedFingerprint": "",
        }
    return {
        "accepted": False,
        "mergedVerified": False,
        "reason": "CUMULATIVE_MERGE_INCOMPATIBLE",
        "independentlySuccessful": independent,
        "mergedAssignment": dict(merged_assignment),
        "failingCommands": list(failing_commands),
        "disposition": "replan-or-repair",
    }
