#!/usr/bin/env python3
"""Deterministic post-Executor dependency compatibility evidence.

The coding agent may report that an already-approved immutable dependency state
cannot satisfy a project gate.  Agent prose is not solver authority.  This
module turns that report into solver authority only after a deterministic
candidate-vs-control reproduction and conflict localization on the preserved
branch ref.
"""
from __future__ import annotations

import inspect
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from proven_dependency_state import validate_proven_dependency_envelope
from source_snapshot import (
    SourceCaptureError,
    open_source_snapshot,
)
from verification_workspace_backend import materialize_private_tree
from substrate_identity import tool_build_id

from baseline_constraint_verifier import (
    BaselineVerifyConfig,
    BaselineVerifyResult,
    structural_project_failure_signatures,
    verify_assignment,
)
from constraint_verify import LocalizationTimeoutError, ProgressCallback, VerificationUnit, parallel_ddmin


@dataclass(frozen=True)
class CompatibilityEvidenceAction:
    package: str
    current: str
    target: str
    action: str = "update"


@dataclass(frozen=True)
class CompatibilityEvidence:
    project: str
    project_path: Path
    branch_ref: str
    target_mode: str
    commands: Tuple[str, ...]
    actions: Tuple[CompatibilityEvidenceAction, ...]
    reason: str = ""
    materialization_proof: str = ""
    proof_envelope_key: str = ""
    exact_assignment: Tuple[Tuple[str, str], ...] = ()
    source_snapshot_locator: Path = Path()
    source_snapshot_key: str = ""
    tool_build_id: str = ""
    project_relative: Path = Path(".")


class CompatibilityEvidenceError(RuntimeError):
    pass


def load_compatibility_evidence(path: Path) -> CompatibilityEvidence:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CompatibilityEvidenceError(f"invalid compatibility evidence file: {exc}") from exc
    if not isinstance(raw, dict) or int(raw.get("schemaVersion", 0) or 0) != 2:
        raise CompatibilityEvidenceError("compatibility evidence schemaVersion must be 2")
    project = str(raw.get("project") or "").strip()
    project_path_text = str(raw.get("projectPath") or "").strip()
    project_path = Path(project_path_text).expanduser()
    branch_ref = str(raw.get("branchRef") or "").strip()
    snapshot_locator_text = str(raw.get("sourceSnapshotLocator") or "").strip()
    snapshot_key = str(raw.get("sourceSnapshotKey") or "").strip()
    evidence_build_id = str(raw.get("toolBuildId") or "").strip()
    project_relative_text = str(raw.get("projectRelative") or ".").strip()
    project_relative = Path(project_relative_text)
    target_mode = str(raw.get("targetMode") or "default").strip().lower()
    if target_mode not in {"yellow", "green", "default"}:
        raise CompatibilityEvidenceError(f"unsupported targetMode {target_mode!r}")
    commands_raw = raw.get("commands")
    commands = tuple(str(item).strip() for item in commands_raw if str(item).strip()) if isinstance(commands_raw, list) else ()
    actions_raw = raw.get("actions")
    actions = []
    if isinstance(actions_raw, list):
        for item in actions_raw:
            if not isinstance(item, dict):
                continue
            package = str(item.get("package") or "").strip()
            current = str(item.get("current") or "").strip()
            target = str(item.get("target") or "").strip()
            action = str(item.get("action") or "update").strip().lower()
            if package and current and target and action == "update":
                actions.append(CompatibilityEvidenceAction(package, current, target, action))
    if not project or not project_path_text or not branch_ref:
        raise CompatibilityEvidenceError("evidence requires project, projectPath and branchRef provenance")
    if not snapshot_locator_text or not snapshot_key:
        raise CompatibilityEvidenceError(
            "evidence requires sourceSnapshotLocator and sourceSnapshotKey"
        )
    if evidence_build_id != tool_build_id():
        raise CompatibilityEvidenceError("evidence toolBuildId is missing or stale")
    if project_relative.is_absolute() or ".." in project_relative.parts:
        raise CompatibilityEvidenceError("evidence projectRelative must stay inside the snapshot")
    if not commands:
        raise CompatibilityEvidenceError("evidence requires at least one deterministic verification command")
    if not actions:
        raise CompatibilityEvidenceError("evidence contains no update actions that can be localized")

    proof_envelope_key = ""
    exact_assignment: Tuple[Tuple[str, str], ...] = ()
    proof_envelope = raw.get("proofEnvelope")
    if proof_envelope is not None:
        if not isinstance(proof_envelope, dict):
            raise CompatibilityEvidenceError("proofEnvelope must be an object")
        valid, reason = validate_proven_dependency_envelope(proof_envelope)
        if not valid:
            raise CompatibilityEvidenceError(f"invalid proofEnvelope: {reason}")
        if str(proof_envelope.get("project") or "") != project:
            raise CompatibilityEvidenceError("proofEnvelope project does not match evidence project")
        if str(proof_envelope.get("mode") or "") != target_mode:
            raise CompatibilityEvidenceError("proofEnvelope mode does not match targetMode")
        assignment_raw = proof_envelope.get("exactDirectAssignment")
        if not isinstance(assignment_raw, dict):
            raise CompatibilityEvidenceError("proofEnvelope exactDirectAssignment missing")
        proof_envelope_key = str(proof_envelope.get("envelopeKey") or "")
        exact_assignment = tuple(
            sorted((str(name), str(version)) for name, version in assignment_raw.items())
        )

    return CompatibilityEvidence(
        project=project,
        project_path=project_path.resolve(),
        branch_ref=branch_ref,
        target_mode=target_mode,
        commands=commands,
        actions=tuple(actions),
        reason=str(raw.get("reason") or ""),
        materialization_proof=str(raw.get("materializationProof") or ""),
        proof_envelope_key=proof_envelope_key,
        exact_assignment=exact_assignment,
        source_snapshot_locator=Path(snapshot_locator_text).expanduser().resolve(),
        source_snapshot_key=snapshot_key,
        tool_build_id=evidence_build_id,
        project_relative=project_relative,
    )


def _materialize_evidence_ref(evidence: CompatibilityEvidence) -> Tuple[Path, Path]:
    """Materialize only the sealed post-Executor filesystem subject."""
    try:
        snapshot = open_source_snapshot(
            evidence.source_snapshot_locator,
            expected_key=evidence.source_snapshot_key,
        )
    except SourceCaptureError as exc:
        raise CompatibilityEvidenceError(
            f"sealed compatibility SourceSnapshot is invalid: {exc}"
        ) from exc
    if snapshot.project_relative != evidence.project_relative:
        raise CompatibilityEvidenceError(
            "compatibility evidence projectRelative does not match SourceSnapshot"
        )

    temp = Path(tempfile.mkdtemp(prefix="dependency-flow-evidence-snapshot-"))
    clone = temp / "repo"
    try:
        materialize_private_tree(
            snapshot.root,
            clone,
            timeout_seconds=1800,
            progress_label="compatibility evidence SourceSnapshot materialization",
        )
        project = (clone / snapshot.project_relative).resolve()
        try:
            project.relative_to(clone.resolve())
        except ValueError as exc:
            raise CompatibilityEvidenceError(
                "materialized compatibility project escaped its SourceSnapshot"
            ) from exc
        if not project.is_dir():
            raise CompatibilityEvidenceError(
                f"materialized compatibility project is missing: {project}"
            )
        return temp, project
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _structural(result: BaselineVerifyResult) -> set[str]:
    return set(structural_project_failure_signatures(result))


def localize_compatibility_evidence(
    evidence: CompatibilityEvidence,
    *,
    base_config: BaselineVerifyConfig,
    verify: Callable[..., BaselineVerifyResult] = verify_assignment,
    parallelism: Optional[int] = None,
    max_checks: Optional[int] = None,
    progress: Optional[ProgressCallback] = None,
) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """Return a verified target-version nogood and its stable structural signatures.

    The preserved branch already contains the Executor's source/config migration
    plus the immutable target assignment.  The deterministic control keeps those
    source/config changes but restores every in-scope direct dependency to its
    pre-update version.  Only structural signatures newly introduced by the
    target assignment are eligible for learning.  Delta debugging then finds a
    small target subset that still reproduces the same signature.
    """
    config = BaselineVerifyConfig(
        enabled=True,
        parallelism=base_config.parallelism,
        max_iterations=base_config.max_iterations,
        max_delta_checks=base_config.max_delta_checks,
        timeout_seconds=base_config.timeout_seconds,
        attempt_timeout_seconds=base_config.attempt_timeout_seconds,
        localization_timeout_seconds=base_config.localization_timeout_seconds,
        progress_interval_seconds=base_config.progress_interval_seconds,
        project_checks="adaptive",
        commands=evidence.commands,
    )
    temp_root, evidence_project = _materialize_evidence_ref(evidence)
    try:
        def emit(event: str, **details: object) -> None:
            if progress is not None:
                progress(event, details)

        try:
            verify_supports_progress = "progress" in inspect.signature(verify).parameters
        except (TypeError, ValueError):
            verify_supports_progress = False

        def run_verify(assignment: Mapping[str, str], *, label: str, event: str) -> BaselineVerifyResult:
            kwargs = {
                "config": config,
                "run_project_checks": True,
                "remove_packages": (),
            }
            if verify_supports_progress:
                kwargs.update({
                    "progress": lambda message: emit(event, message=message),
                    "progress_label": label,
                })
            return verify(evidence_project, assignment, **kwargs)

        current_versions = {item.package: item.current for item in evidence.actions}
        target_versions = {item.package: item.target for item in evidence.actions}

        # Candidate = preserved branch's approved targets; control = same source
        # migration with only the direct dependency versions restored.
        candidate = run_verify({}, label="post-Executor candidate reproduction", event="candidate-running")
        if candidate.kind in {"infrastructure", "unknown", "dependency"}:
            raise CompatibilityEvidenceError(
                f"candidate evidence is not a deterministic project-structural failure: {candidate.kind}: {candidate.summary}"
            )
        candidate_signatures = _structural(candidate)
        if candidate.ok or not candidate_signatures:
            raise CompatibilityEvidenceError("candidate branch did not reproduce a structural project failure")

        control = run_verify(current_versions, label="post-Executor control reproduction", event="control-running")
        if control.kind in {"infrastructure", "unknown", "dependency"}:
            raise CompatibilityEvidenceError(
                f"control evidence is inconclusive: {control.kind}: {control.summary}"
            )
        control_signatures = _structural(control)
        introduced = tuple(sorted(candidate_signatures - control_signatures))
        if not introduced:
            raise CompatibilityEvidenceError(
                "candidate structural signatures are also present after restoring the pre-update direct dependency state"
            )
        expected = set(introduced)

        units = tuple(VerificationUnit(item.package, (item.package,)) for item in sorted(evidence.actions, key=lambda x: x.package))

        def subset_fails(subset: Tuple[VerificationUnit, ...]) -> bool:
            active = {unit.packages[0] for unit in subset}
            assignment = {
                name: (target_versions[name] if name in active else current_versions[name])
                for name in sorted(current_versions)
            }
            subset_label = ",".join(sorted(active))
            if verify_supports_progress:
                def subset_progress(message: str) -> None:
                    emit("localization-check-running", subset=subset_label, packages=len(active), message=message)
                result = verify(
                    evidence_project, assignment, config=config, run_project_checks=True, remove_packages=(),
                    progress=subset_progress, progress_label=f"post-Executor localization subset ({len(active)} packages)",
                )
            else:
                result = verify(evidence_project, assignment, config=config, run_project_checks=True, remove_packages=())
            if result.kind in {"infrastructure", "unknown", "dependency"}:
                raise CompatibilityEvidenceError(
                    f"evidence localization became inconclusive: {result.kind}: {result.summary}"
                )
            return bool(_structural(result) & expected)

        try:
            culprit_units = parallel_ddmin(
                units,
                subset_fails,
                parallelism=parallelism or config.parallelism,
                max_checks=max_checks or config.max_delta_checks,
                progress=progress,
                progress_interval_seconds=config.progress_interval_seconds,
                timeout_seconds=config.localization_timeout_seconds,
            )
        except LocalizationTimeoutError as exc:
            raise CompatibilityEvidenceError(f"post-Executor localization timeout: {exc}") from None
        culprit_names = sorted({name for unit in culprit_units for name in unit.packages})
        nogood = {name: target_versions[name] for name in culprit_names if name in target_versions}
        if not nogood:
            raise CompatibilityEvidenceError("localization produced an empty target nogood")

        # Final fresh reproduction of the localized literal set. This protects
        # against a flaky ddmin observation becoming solver authority.
        localized_assignment = {
            name: (target_versions[name] if name in nogood else current_versions[name])
            for name in sorted(current_versions)
        }
        proofs = []
        for proof_index in range(2):
            proof_number = proof_index + 1
            emit("reproduction-start", proof=proof_number, proofs=2, literals=len(nogood))
            if verify_supports_progress:
                proof = verify(
                    evidence_project, localized_assignment, config=config, run_project_checks=True, remove_packages=(),
                    progress=lambda message, proof_number=proof_number: emit("reproduction-running", proof=proof_number, proofs=2, message=message),
                    progress_label=f"post-Executor reproduction {proof_number}/2",
                )
            else:
                proof = verify(evidence_project, localized_assignment, config=config, run_project_checks=True, remove_packages=())
            if proof.kind in {"infrastructure", "unknown", "dependency"} or proof.ok:
                raise CompatibilityEvidenceError("localized compatibility evidence did not reproduce safely")
            stable = tuple(sorted(_structural(proof) & expected))
            if not stable:
                raise CompatibilityEvidenceError("localized structural signature disappeared during proof reproduction")
            proofs.append(stable)
            emit("reproduction-finish", proof=proof_number, proofs=2, signatures=list(stable))
        if len(set(proofs)) != 1:
            raise CompatibilityEvidenceError("localized structural signature changed across proof reproductions")
        return nogood, proofs[0]
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
