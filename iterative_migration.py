#!/usr/bin/env python3
"""Iterative (накопительная) dependency migration coordinator core.

Python owns the deterministic planner/materializer/verifier and the durable
run/checkpoint/candidate/feedback state.  The Desktop coordinator decides WHEN
to call each step and dispatches the repair/feedback agent; every step here is a
bounded, idempotent CLI operation over ONE durable run directory.

Identity/authority contract (shared with desktop/electron/iterative-migration.ts):

  * Checkpoints are immutable and content-addressed by snapshot keys; the parent
    chain C0 -> C1 -> ... is never rewritten.  Acceptance is an atomic pointer
    switch (write checkpoint, then update run.json) under a single-writer lock.
  * A candidate is an exact full assignment on top of one base checkpoint.
    "Resolver success", agent prose, a snapshot and the absence of NEW errors are
    NOT PROJECT_VERIFIED.  Only verify-exact's authoritative `verify_assignment`
    run over the repaired bytes may accept a checkpoint.
  * AgentFeedback is typed (REPAIRING / READY_FOR_VERIFY /
    NEEDS_COHORT_EXPANSION / NEEDS_ALTERNATIVE / INCONCLUSIVE / INFRA_BLOCKED),
    bound to run/candidate/base checkpoint/attempt identities; a stale reply is
    rejected.  It controls scheduling, never proof.

This module does NOT launch agents, does NOT wait for human dialogs and does NOT
own the agent provider.  It reuses existing primitives (plan_progressive_extension,
verify_assignment, durable SourceSnapshot containers, _apply_assignment,
build_repair_request, manual_dependency_audit) instead of a second solver or a
competing store of truth.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from baseline_constraint_verifier import (
    BaselineVerifyConfig,
    BaselineVerifyResult,
    _apply_assignment,
    _run,
    assignment_fingerprint,
    detect_package_manager,
    discover_baseline_project_checks,
    is_structural_project_failure,
    observed_resolved_assignment,
    observed_resolved_hash,
    resolve_executable,
    structural_project_failure_signatures,
    verify_assignment,
)
from baseline_repair_handoff import build_repair_request
from block_psi_progressive_baseline import plan_progressive_extension
from package_manager_profile import (
    install_args_for_profile,
    resolve_package_manager_profile,
)
from project_topology import ProjectTopologyError, resolve_project_topology
from source_snapshot import (
    SourceSnapshot,
    capture_durable_source_snapshot,
    capture_source_snapshot,
    open_source_snapshot,
)
from verification_workspace_backend import materialize_private_tree
from prepared_workspace_fastpath import apply_tree_write_protection

SCHEMA_VERSION = 1
AUTHORITY = "ITERATIVE_MIGRATION_ORCHESTRATION"

# Machine-readable progress event emitted on stdout (never the only transport:
# the durable step result files are the transport, stdout is progress).
STATUS_EVENT = "ITERATIVE_MIGRATION_STATUS_V1"

FEEDBACK_KINDS = {
    "REPAIRING",
    "READY_FOR_VERIFY",
    "NEEDS_COHORT_EXPANSION",
    "NEEDS_ALTERNATIVE",
    "INCONCLUSIVE",
    "INFRA_BLOCKED",
}

PHASES = {
    "BASELINING",
    "BOOTSTRAP_REPAIR",
    "PLANNING",
    "MATERIALIZING",
    "PRECHECK",
    "REPAIRING",
    "VERIFYING",
    "CHECKPOINTING",
    "READY",
    "TERMINAL",
}

TERMINALS = {
    "COMPLETE",
    "PARTIAL_VERIFIED",
    "STOPPED_WITH_CHECKPOINT",
    "BLOCKED_BASELINE",
    "BLOCKED_INFRA",
    "NO_VERIFIED_UPGRADE",
}

CANDIDATE_STAGES = {
    "PLANNED",
    "MATERIALIZED",
    "PRECHECKED",
    "REPAIRING",
    "VERIFYING",
    "ACCEPTED",
    "REJECTED",
}

# Files the agent may never touch in the isolated trial.  Dependency state is
# immutable for the agent; the controller is the only writer.
FORBIDDEN_TRIAL_RELATIVES = {
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "npm-shrinkwrap.json",
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    ".pnpmfile.cjs",
}

# Declared dependency sections applied by exact assignment materialization.
DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


class IterativeMigrationError(RuntimeError):
    """Base domain error with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StaleFeedbackError(IterativeMigrationError):
    def __init__(self, message: str) -> None:
        super().__init__("STALE_FEEDBACK", message)


class InvalidInputError(IterativeMigrationError):
    def __init__(self, message: str) -> None:
        super().__init__("INVALID_INPUT", message)


class BudgetExceededError(IterativeMigrationError):
    def __init__(self, message: str) -> None:
        super().__init__("BUDGET_EXCEEDED", message)


class TrialLockedError(IterativeMigrationError):
    def __init__(self, message: str) -> None:
        super().__init__("TRIAL_LOCKED", message)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _emit_status(event: Mapping[str, Any]) -> None:
    print(f"{STATUS_EVENT} {json.dumps(dict(event), ensure_ascii=False, separators=(',', ':'))}")
    sys_stdout_flush()


def sys_stdout_flush() -> None:
    try:
        import sys

        sys.stdout.flush()
    except Exception:
        pass


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


# ---------------------------------------------------------------------------
# Run directory layout and locking (one writer per project)
# ---------------------------------------------------------------------------

RUN_FILENAME = "run.json"
LEDGER_FILENAME = "ledger.json"
CONFIG_FILENAME = "run-config.json"
CHECKPOINT_DIR = "checkpoints"
SOURCE_DIR = "sources"
TRIAL_DIR = "trial"
TRIAL_CANDIDATE_FILENAME = "candidate.json"
TRIAL_WORKSPACE = "workspace"
REPAIR_REQUESTS_FILENAME = "repair-requests.json"
REPORTS_DIR = "reports"
AUDIT_DIR = "audit"
LOCK_FILENAME = "run.lock"


def _trial_dir(run_dir: Path) -> Path:
    return run_dir / TRIAL_DIR


def _checkpoint_path(run_dir: Path, checkpoint_id: str) -> Path:
    return run_dir / CHECKPOINT_DIR / f"{checkpoint_id}.json"


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(_stable_json(value) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, *, required: bool = True) -> Optional[Dict[str, Any]]:
    if not path.exists():
        if required:
            raise InvalidInputError(f"STATE_FILE_MISSING: {path}")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidInputError(f"STATE_FILE_INVALID: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidInputError(f"STATE_FILE_NOT_OBJECT: {path}")
    return value


class _RunLock:
    """Cross-process single-writer lock with stale-owner reclaim (lease)."""

    def __init__(self, run_dir: Path, owner: str, stale_seconds: int = 120) -> None:
        self.path = run_dir / LOCK_FILENAME
        self.owner = owner
        self.stale_seconds = stale_seconds

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 15
        while True:
            try:
                handle = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                stale = self._stale()
                if stale:
                    try:
                        os.unlink(str(self.path))
                    except OSError:
                        pass
                    continue
                if time.time() > deadline:
                    raise TrialLockedError(
                        f"RUN_LOCK_BUSY: another writer {self.owner} holds {self.path}"
                    )
                time.sleep(0.2)
                continue
            except OSError as exc:
                raise IterativeMigrationError(
                    "RUN_LOCK_FILE_IO", f"cannot create lock {self.path}: {exc}"
                ) from exc
            try:
                os.write(
                    handle,
                    json.dumps(
                        {"owner": self.owner, "pid": os.getpid(), "startedAt": _now_iso()}
                    ).encode("utf-8"),
                )
            finally:
                os.close(handle)
            return

    def _stale(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            owner = str(payload.get("owner") or "")
            started = str(payload.get("startedAt") or "")
            started_at = time.strptime(started, "%Y-%m-%dT%H:%M:%SZ")
            age = time.time() - time.mktime(started_at)
            return age > self.stale_seconds
        except Exception:
            try:
                age = time.time() - self.path.stat().st_mtime
                return age > self.stale_seconds
            except OSError:
                return False

    def release(self) -> None:
        try:
            os.unlink(str(self.path))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Durable state objects (versioned, validated)
# ---------------------------------------------------------------------------

def _require_schema(value: Mapping[str, Any], kind: str) -> None:
    schema = value.get("schemaVersion")
    if int(schema or -1) != SCHEMA_VERSION:
        raise InvalidInputError(
            f"SCHEMA_MISMATCH_{kind}: expected schemaVersion={SCHEMA_VERSION} "
            f"observed={schema!r}"
        )


def load_run(run_dir: Path) -> Dict[str, Any]:
    run = _read_json(run_dir / RUN_FILENAME)
    _require_schema(run, "RUN")
    if not str(run.get("runId") or "").strip():
        raise InvalidInputError("RUN_ID_MISSING")
    return run


def save_run(run_dir: Path, run: Mapping[str, Any]) -> None:
    _write_json_atomic(run_dir / RUN_FILENAME, dict(run))


def load_checkpoint(run_dir: Path, checkpoint_id: str) -> Dict[str, Any]:
    value = _read_json(_checkpoint_path(run_dir, checkpoint_id))
    _require_schema(value, "CHECKPOINT")
    return value


def save_checkpoint(run_dir: Path, checkpoint: Mapping[str, Any]) -> None:
    checkpoint_id = str(checkpoint.get("checkpointId") or "")
    if not checkpoint_id:
        raise InvalidInputError("CHECKPOINT_ID_MISSING")
    _write_json_atomic(_checkpoint_path(run_dir, checkpoint_id), dict(checkpoint))


def load_ledger(run_dir: Path) -> Dict[str, Any]:
    value = _read_json(run_dir / LEDGER_FILENAME, required=False) or {}
    if int(value.get("schemaVersion") or 0) != SCHEMA_VERSION:
        value = {
            "schemaVersion": SCHEMA_VERSION,
            "blocks": [],
            "deferrals": [],
            "feedback": [],
            "counters": {},
        }
    return value


def save_ledger(run_dir: Path, ledger: Mapping[str, Any]) -> None:
    _write_json_atomic(run_dir / LEDGER_FILENAME, dict(ledger))


def load_config(run_dir: Path) -> Dict[str, Any]:
    value = _read_json(run_dir / CONFIG_FILENAME)
    if int(value.get("schemaVersion") or -1) != SCHEMA_VERSION:
        raise InvalidInputError("SCHEMA_MISMATCH_CONFIG")
    return value


def save_config(run_dir: Path, config: Mapping[str, Any]) -> None:
    if int(config.get("schemaVersion") or -1) != SCHEMA_VERSION:
        raise InvalidInputError("SCHEMA_MISMATCH_CONFIG")
    _write_json_atomic(run_dir / CONFIG_FILENAME, dict(config))


def load_candidate(run_dir: Path) -> Optional[Dict[str, Any]]:
    value = _read_json(_trial_dir(run_dir) / TRIAL_CANDIDATE_FILENAME, required=False)
    if value is None:
        return None
    _require_schema(value, "CANDIDATE")
    return value


def save_candidate(run_dir: Path, candidate: Mapping[str, Any]) -> None:
    _write_json_atomic(_trial_dir(run_dir) / TRIAL_CANDIDATE_FILENAME, dict(candidate))


def clear_candidate(run_dir: Path) -> None:
    candidate_path = _trial_dir(run_dir) / TRIAL_CANDIDATE_FILENAME
    if candidate_path.exists():
        try:
            os.unlink(candidate_path)
        except OSError:
            pass


def trial_workspace_root(run_dir: Path) -> Path:
    return _trial_dir(run_dir) / TRIAL_WORKSPACE


def read_repair_requests(run_dir: Path) -> List[Dict[str, Any]]:
    value = _read_json(run_dir / REPAIR_REQUESTS_FILENAME, required=False)
    if not value:
        return []
    requests = value.get("requests")
    if not isinstance(requests, list):
        return []
    return [item for item in requests if isinstance(item, dict)]


def write_repair_requests(run_dir: Path, requests: Sequence[Mapping[str, Any]]) -> None:
    if requests:
        _write_json_atomic(
            run_dir / REPAIR_REQUESTS_FILENAME, {"requests": list(requests)}
        )
    else:
        path = run_dir / REPAIR_REQUESTS_FILENAME
        if path.exists():
            try:
                os.unlink(path)
            except OSError:
                pass


def run_budget_ok(run_config: Mapping[str, Any], run: Mapping[str, Any]) -> bool:
    deadline = run.get("deadlineAt")
    if not deadline:
        return True
    try:
        # _deadline_iso writes UTC ("...Z"); parse it as UTC explicitly.  Using
        # time.mktime would interpret the UTC wall-clock as LOCAL time and shift
        # the deadline by the host's UTC offset, expiring runs prematurely.
        deadline_at = datetime.strptime(str(deadline), "%Y-%m-%dT%H:%M:%SZ")
        deadline_epoch = deadline_at.replace(tzinfo=timezone.utc).timestamp()
        return time.time() < deadline_epoch
    except (ValueError, TypeError):
        return True


def touch_heartbeat(run_dir: Path, run: Mapping[str, Any]) -> Dict[str, Any]:
    """Heartbeat records activity WITHOUT moving the deadline."""
    updated = dict(run)
    updated["heartbeatAt"] = _now_iso()
    save_run(run_dir, updated)
    return updated


# ---------------------------------------------------------------------------
# Project inventory helpers
# ---------------------------------------------------------------------------

def direct_dependency_assignment(project_dir: Path) -> Dict[str, str]:
    """Current declared versions of managed direct dependencies (exact spec)."""
    manifest = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise InvalidInputError("PROJECT_MANIFEST_INVALID")
    assignment: Dict[str, str] = {}
    for section in DEPENDENCY_SECTIONS:
        deps = manifest.get(section)
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            if isinstance(name, str) and isinstance(spec, str) and name not in assignment:
                assignment[name] = spec
    return dict(sorted(assignment.items()))


def manifest_and_lock_hashes(project_dir: Path) -> Tuple[str, str, Optional[str]]:
    manifest = project_dir / "package.json"
    if not manifest.exists():
        raise InvalidInputError(f"PROJECT_PACKAGE_JSON_MISSING: {manifest}")
    manifest_hash = _sha256_file(manifest)
    lock = find_lockfile(project_dir)
    lock_hash = _sha256_file(lock) if lock else ""
    return manifest_hash, lock_hash, (str(lock) if lock else None)


def find_lockfile(project_dir: Path) -> Optional[Path]:
    for name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json"):
        candidate = project_dir / name
        if candidate.exists():
            return candidate
    return None


def _manager_and_install(project_dir: Path) -> Tuple[str, Optional[str], List[str]]:
    """Return (manager, executable, install args) for the project topology root."""
    try:
        topology = resolve_project_topology(
            project_dir, allow_discovery=False, require_supported=True
        )
    except ProjectTopologyError as exc:
        raise IterativeMigrationError("PROJECT_TOPOLOGY", str(exc)) from exc
    manager = topology.profile.manager
    executable = resolve_executable(manager)
    install = install_args_for_profile(
        topology.profile,
        ignore_scripts=True,
        frozen=False,
    )
    return manager, executable, list(install)


def _run_install(project_dir: Path, *, timeout_seconds: int, progress_label: str) -> subprocess.CompletedProcess[str]:
    manager, executable, install_args = _manager_and_install(project_dir)
    if not executable:
        raise IterativeMigrationError(
            "INFRA_PACKAGE_MANAGER_NOT_FOUND",
            f"{manager} is not available in PATH",
        )
    env: Dict[str, str] = {
        "CI": "1",
        "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
        "YARN_ENABLE_SCRIPTS": "false",
        "npm_config_ignore_scripts": "true",
    }
    return _run(
        [executable, *install_args],
        project_dir,
        timeout_seconds=timeout_seconds,
        env=env,
        base_env=os.environ,
        progress_label=progress_label,
    )


def verify_config_from(mapping: Mapping[str, Any], run_dir: Path) -> BaselineVerifyConfig:
    """BaselineVerifyConfig from the run's verifyConfig mapping.

    `from_mapping` covers the numeric/typed fields; proof-cache, telemetry,
    registry and reuse keys are applied explicitly (they participate in proof
    identity; the trial's resolved state is shared with the coordinator).
    """
    resolved = dict(mapping or {})
    config = BaselineVerifyConfig.from_mapping(resolved)
    proof_cache_dir = str(
        (
            Path(str(resolved.get("proofCacheDir") or (run_dir / "proof-cache").resolve())).expanduser().resolve()
        )
    )
    telemetry_path = str(
        (
            Path(str(resolved.get("telemetryPath") or (run_dir / "verification-telemetry.jsonl").resolve())).expanduser().resolve()
        )
    )
    return dataclasses.replace(
        config,
        registry=str(resolved.get("registry") or ""),
        telemetry_path=telemetry_path,
        proof_cache_dir=proof_cache_dir,
        publish_durable_prepared_artifact=bool(
            resolved.get("publishDurablePreparedArtifact", False)
        ),
    )


def resolve_project_relative(project_dir: Path, source: SourceSnapshot) -> Path:
    """Return the project-relative path inside a durable snapshot container."""
    if source.project_relative and str(source.project_relative) != ".":
        return Path(source.project_relative)
    try:
        return project_dir.resolve().relative_to(source.root)
    except ValueError:
        return Path(".")


# ---------------------------------------------------------------------------
# begin: C0 «Исходный замер»
# ---------------------------------------------------------------------------

def cmd_begin(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    owner = args.owner or f"pid-{os.getpid()}"
    if (run_dir / RUN_FILENAME).exists():
        raise InvalidInputError(f"RUN_ALREADY_EXISTS: {run_dir}")

    config = build_run_config(run_dir, args)

    lock = _RunLock(run_dir, owner)
    lock.acquire()
    try:
        return _begin_locked(run_dir, config, args)
    finally:
        lock.release()


def build_run_config(run_dir: Path, args: argparse.Namespace) -> Dict[str, Any]:
    project_dir = Path(args.project_dir).expanduser().resolve()
    if not (project_dir / "package.json").exists():
        raise InvalidInputError(f"PROJECT_PACKAGE_JSON_MISSING: {project_dir}")
    project_name = (args.project_name or project_dir.name).strip()
    target_level = (args.target_level or "yellow").strip().lower()
    if target_level not in ("yellow", "green"):
        raise InvalidInputError("TARGET_LEVEL_INVALID")

    verify_raw: Dict[str, Any] = {}
    if args.verify_config:
        verify_raw = json.loads(Path(args.verify_config).read_text(encoding="utf-8"))
        if not isinstance(verify_raw, dict):
            raise InvalidInputError("VERIFY_CONFIG_INVALID")

    targets: Dict[str, str] = {}
    if args.targets_file:
        raw_targets = json.loads(Path(args.targets_file).read_text(encoding="utf-8"))
        if not isinstance(raw_targets, dict):
            raise InvalidInputError("TARGETS_FILE_INVALID")
        for name, version in raw_targets.items():
            if isinstance(name, str) and isinstance(version, str) and name.strip() and version.strip():
                targets[name] = version

    commands = tuple(str(item) for item in (verify_raw.get("commands") or []))
    if not commands:
        commands = discover_baseline_project_checks(project_dir)
    verify_config = dict(verify_raw)
    verify_config["commands"] = list(commands)

    manifest_hash, lock_hash, _lock = manifest_and_lock_hashes(project_dir)
    policy_hash = _sha256_text(
        _stable_json(
            {
                "targetLevel": target_level,
                "targets": targets,
                "commands": commands,
                "manifestHash": manifest_hash,
                "lockHash": lock_hash,
            }
        )
    )
    budget = {
        "runBudgetMinutes": _clamp_int(
            args.run_budget_minutes if args.run_budget_minutes is not None else 120,
            120, 5, 24 * 60,
        ),
        "maxRepairAttemptsPerRevision": _clamp_int(
            args.max_repair_attempts if args.max_repair_attempts is not None else 2,
            2, 1, 8,
        ),
        "maxInfraRetries": _clamp_int(
            args.max_infra_retries if args.max_infra_retries is not None else 2,
            2, 0, 8,
        ),
        "phaseTimeoutSeconds": _clamp_int(
            args.phase_timeout_seconds if args.phase_timeout_seconds is not None else 1200,
            1200, 120, 4 * 3600,
        ),
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "projectDir": str(project_dir),
        "projectName": project_name,
        "workspaceId": (args.workspace_id or "").strip(),
        "projectId": (args.project_id or project_name).strip(),
        "targetLevel": target_level,
        "targets": targets,
        "verifyConfig": verify_config,
        "policyHash": policy_hash,
        "budget": budget,
        "createdAt": _now_iso(),
        "toolBuildId": args.tool_build_id or "",
    }


def _begin_locked(run_dir: Path, config: Mapping[str, Any], args: argparse.Namespace) -> int:
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(run_dir / CONFIG_FILENAME, dict(config))
    run_id = (args.run_id or "").strip() or _new_id("iter")
    project_dir = Path(config["projectDir"])
    project_name = str(config["projectName"])
    initial_assignment = direct_dependency_assignment(project_dir)
    manifest_hash, lock_hash, lock_path = manifest_and_lock_hashes(project_dir)

    _emit_status(
        {
            "event": "begin.capture",
            "runId": run_id,
            "project": project_name,
            "managedDependencies": len(initial_assignment),
        }
    )

    snapshot_container = run_dir / SOURCE_DIR / "C0"
    snapshot = capture_durable_source_snapshot(
        project_dir, snapshot_container, timeout_seconds=1800
    )
    project_relative = resolve_project_relative(project_dir, snapshot)

    verify_config = verify_config_from(config["verifyConfig"], run_dir)
    verify_config = dataclasses.replace(
        verify_config, verification_purpose="baseline-control"
    )
    _emit_status({"event": "begin.c0-verify", "runId": run_id})
    result = verify_assignment(
        project_dir,
        initial_assignment,
        config=verify_config,
        run_project_checks=True,
        progress_label="iterative migration C0 control verification",
    )

    observed = dict(result.observed_resolved_versions or {})
    full_assignment = {
        name: observed.get(name, spec)
        for name, spec in initial_assignment.items()
    }
    status = "VERIFIED" if result.ok else classify_c0_result(result)
    checkpoint: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "checkpointId": "C0",
        "parentCheckpointId": None,
        "seq": 0,
        "status": status,
        "projectName": project_name,
        "sourceSnapshotKey": snapshot.key,
        "sourceSnapshotContainer": str(snapshot.container),
        "sourceHead": snapshot.git_head or "",
        "projectRelative": str(project_relative),
        "manifestHash": manifest_hash,
        "lockfileHash": lock_hash or "",
        "resolvedStateKey": result.resolved_state_key or "",
        "observedResolvedHash": result.observed_resolved_hash or observed_resolved_hash(observed),
        "fullAssignment": full_assignment,
        "acceptedDelta": {"added": {}, "changed": {}, "removed": {}},
        "cohortId": None,
        "verification": {
            "status": "passed" if result.ok else ("failed" if result.hard_failure else "unknown"),
            "kind": result.kind,
            "commands": list(verify_config.commands),
            "failingCommands": [
                {"command": failure.command, "exitCode": failure.exit_code}
                for failure in result.project_failures
            ],
        },
        "proofRefs": {
            "resolvedStateKey": result.resolved_state_key or "",
            "preparationProofKey": result.preparation_proof_key or "",
        },
        "audit": {"status": "UNKNOWN", "evidenceRef": ""},
        "createdAt": _now_iso(),
        "updatedAt": _now_iso(),
        "toolBuildId": config.get("toolBuildId") or "",
    }
    save_checkpoint(run_dir, checkpoint)

    run: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "workspaceId": str(config.get("workspaceId") or ""),
        "projectId": str(config.get("projectId") or ""),
        "generation": 1,
        "targetPolicyHash": str(config["policyHash"]),
        "initialSnapshotKey": snapshot.key,
        "activeCheckpointId": "C0",
        "activeCandidateId": None,
        "phase": "READY" if status == "VERIFIED" else "BOOTSTRAP_REPAIR",
        "terminal": None,
        "budgetLedger": {
            "candidateAttempts": 0,
            "repairAttempts": 0,
            "repairAttemptsForRevision": 0,
            "infraRetries": 0,
            "repeatedAttempts": 0,
        },
        "leaseOwner": "",
        "deadlineAt": _deadline_iso(int(config["budget"]["runBudgetMinutes"])),
        "heartbeatAt": _now_iso(),
        "createdAt": _now_iso(),
        "updatedAt": _now_iso(),
        "toolBuildId": config.get("toolBuildId") or "",
    }
    save_run(run_dir, run)

    if status != "VERIFIED":
        _write_bootstrap_repair_request(run_dir, checkpoint, result)
        _emit_status(
            {
                "event": "begin.red",
                "runId": run_id,
                "status": status,
                "kind": result.kind,
                "summary": result.summary,
                "failingCommands": [
                    {"command": f.command, "exitCode": f.exit_code}
                    for f in result.project_failures
                ],
            }
        )
        return 0

    _emit_status(
        {
            "event": "begin.done",
            "runId": run_id,
            "checkpointId": "C0",
            "phase": "READY",
            "managedDependencies": len(full_assignment),
        }
    )
    return 0


def classify_c0_result(result: BaselineVerifyResult) -> str:
    if result.kind == "project" or is_structural_project_failure(result):
        return "RED_SOURCE"
    if result.kind in {"infrastructure", "preparation"}:
        return "BLOCKED_INFRA"
    if result.kind == "budget":
        return "BUDGET"
    return "BLOCKED_BASELINE"


def _deadline_iso(minutes: int) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(minutes) * 60)
    )


def _write_bootstrap_repair_request(
    run_dir: Path, checkpoint: Mapping[str, Any], result: BaselineVerifyResult
) -> None:
    from baseline_constraint_verifier import BaselineProjectFailure

    failures = list(result.project_failures)
    failing: Sequence[BaselineProjectFailure] = (
        failures
        if failures
        else (BaselineProjectFailure(str(checkpoint["verification"]["commands"][0]), 1, result.summary),)
        if checkpoint["verification"]["commands"]
        else ()
    )
    request = build_repair_request(
        project=str(checkpoint.get("projectName") or Path(run_dir).name),
        mode="iterative-bootstrap",
        assignment=checkpoint["fullAssignment"],
        result=BaselineVerifyResult(
            False,
            "project" if result.kind == "project" else result.kind,
            result.summary,
            project_failures=tuple(failing),
        ),
        snapshot_identity=str(checkpoint["sourceSnapshotKey"]),
    )
    write_repair_requests(
        run_dir,
        [dict(request.to_json()) | {"bootstrap": True, "checkpointId": "C0"}],
    )


def cmd_verify_bootstrap(args: argparse.Namespace) -> int:
    """Re-run the C0 control verification after a version-neutral bootstrap repair."""
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}")
    lock.acquire()
    try:
        if run.get("phase") != "BOOTSTRAP_REPAIR":
            raise InvalidInputError(f"PHASE_NOT_BOOTSTRAP: {run.get('phase')}")
        if not run_budget_ok(config, run):
            raise BudgetExceededError("RUN_DEADLINE_EXCEEDED")
        project_dir = Path(config["projectDir"])
        checkpoint = load_checkpoint(run_dir, "C0")
        verify_config = verify_config_from(config["verifyConfig"], run_dir)
        verify_config = dataclasses.replace(
            verify_config, verification_purpose="baseline-control"
        )
        result = verify_assignment(
            project_dir,
            checkpoint["fullAssignment"],
            config=verify_config,
            run_project_checks=True,
            progress_label="iterative migration bootstrap re-verification",
        )
        if result.ok:
            checkpoint = dict(checkpoint)
            checkpoint["status"] = "VERIFIED"
            checkpoint["updatedAt"] = _now_iso()
            checkpoint["verification"] = {
                "status": "passed",
                "kind": result.kind,
                "commands": list(verify_config.commands),
                "failingCommands": [],
            }
            checkpoint["resolvedStateKey"] = result.resolved_state_key or checkpoint.get("resolvedStateKey", "")
            checkpoint["observedResolvedHash"] = result.observed_resolved_hash or checkpoint.get("observedResolvedHash", "")
            save_checkpoint(run_dir, checkpoint)
            run = dict(run)
            run["phase"] = "READY"
            run["updatedAt"] = _now_iso()
            save_run(run_dir, run)
            write_repair_requests(run_dir, [])
            _emit_status(
                {
                    "event": "verify-bootstrap.pass",
                    "runId": run["runId"],
                    "checkpointId": "C0",
                    "phase": "READY",
                }
            )
            return 0
        _emit_status(
            {
                "event": "verify-bootstrap.red",
                "runId": run["runId"],
                "kind": result.kind,
                "summary": result.summary,
                "failingCommands": [
                    {"command": f.command, "exitCode": f.exit_code}
                    for f in result.project_failures
                ],
            }
        )
        return 0
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# plan-next
# ---------------------------------------------------------------------------

def cmd_plan_next(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}", stale_seconds=60)
    lock.acquire()
    try:
        return _plan_next_locked(run_dir, run, config)
    finally:
        lock.release()


def _plan_next_locked(run_dir: Path, run: Mapping[str, Any], config: Mapping[str, Any]) -> int:
    if not run_budget_ok(config, run):
        raise BudgetExceededError("RUN_DEADLINE_EXCEEDED")
    active_candidate = load_candidate(run_dir)
    if active_candidate is not None and active_candidate.get("stage") in {
        "PLANNED",
        "MATERIALIZED",
        "PRECHECKED",
        "REPAIRING",
        "VERIFYING",
    }:
        raise InvalidInputError(
            f"ACTIVE_CANDIDATE_IN_FLIGHT: {active_candidate.get('candidateId')} "
            f"stage={active_candidate.get('stage')}"
        )
    terminal = run.get("terminal")
    if terminal:
        raise InvalidInputError(f"RUN_TERMINAL: {terminal}")

    checkpoint_id = str(run["activeCheckpointId"])
    checkpoint = load_checkpoint(run_dir, checkpoint_id)
    if checkpoint.get("status") not in ("VERIFIED",):
        raise InvalidInputError(
            f"CHECKPOINT_NOT_VERIFIED: {checkpoint_id} status={checkpoint.get('status')}"
        )

    incumbent = {str(k): str(v) for k, v in (checkpoint.get("fullAssignment") or {}).items()}
    targets = {str(k): str(v) for k, v in (config.get("targets") or {}).items()}
    desired = {name: targets.get(name, version) for name, version in incumbent.items()}
    actionable = [name for name, version in sorted(desired.items()) if name in targets and targets[name] != incumbent.get(name)]

    if not actionable:
        # Policy is satisfied only when EVERY requested target is present in
        # the current checkpoint at its exact target version. A target that is
        # deferred, blocked or not (yet) in scope keeps the policy unmet: it
        # stays in the remainder and denominator, and the run is NOT terminal
        # with a satisfied policy.
        satisfied = bool(targets) and all(
            name in incumbent and str(targets[name]) == str(incumbent[name])
            for name in targets
        )
        terminal_name = "POLICY_SATISFIED_NEEDS_AUDIT" if satisfied else "NO_ACTIONABLE"
        run = dict(run)
        run["updatedAt"] = _now_iso()
        if not satisfied:
            run["terminal"] = terminal_name
            run["phase"] = "TERMINAL"
            save_run(run_dir, run)
        _emit_status(
            {
                "event": "plan-next.no-candidate",
                "runId": run["runId"],
                "checkpointId": checkpoint_id,
                "reason": terminal_name,
                "remainingActionable": len(actionable),
                "targets": len(targets),
            }
        )
        return 0

    ledger = load_ledger(run_dir)
    blocks = [
        entry
        for entry in ledger.get("blocks", [])
        if entry.get("kind") in {"NEGATIVE_VERIFY", "NOT_ACTIONABLE"}
        and entry.get("baseCheckpointId") == checkpoint_id
    ]
    blocked_fingerprints = {
        str(entry.get("assignmentFingerprint"))
        for entry in blocks
        if str(entry.get("assignmentFingerprint") or "").strip()
    }
    learned_nogoods = [
        dict(entry.get("nogood") or {})
        for entry in ledger.get("blocks", [])
        if entry.get("kind") == "NEGATIVE_VERIFY"
        and isinstance(entry.get("nogood"), dict)
        and entry.get("nogood")
        and entry.get("scope") == "deterministic"
    ]
    priority = tuple(str(item) for item in (config.get("priorityPackages") or ()))

    atomic_groups = _build_atomic_groups(actionable, config)
    atomic_groups = _apply_scope_expansions(atomic_groups, ledger, checkpoint_id, actionable)
    plan = plan_progressive_extension(
        incumbent=incumbent,
        desired=desired,
        atomic_groups=atomic_groups,
        blocked_fingerprints=blocked_fingerprints,
        learned_nogoods=learned_nogoods,
        fingerprint_fn=assignment_fingerprint,
        priority_packages=priority,
    )
    if plan is None:
        all_blocked = len(actionable) > 0
        reason = "SCOPE_EXHAUSTED" if all_blocked else "NO_ACTIONABLE"
        run = dict(run)
        run["terminal"] = reason
        run["phase"] = "TERMINAL"
        run["updatedAt"] = _now_iso()
        save_run(run_dir, run)
        _emit_status(
            {
                "event": "plan-next.no-candidate",
                "runId": run["runId"],
                "checkpointId": checkpoint_id,
                "reason": reason,
                "blockedFingerprints": len(blocked_fingerprints),
            }
        )
        return 0

    assignment = plan.assignment_dict
    cohort_id = _new_id("cohort")
    changed = {
        name: assignment[name]
        for name in plan.packages
        if str(incumbent.get(name)) != str(assignment.get(name))
    }
    candidate: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "candidateId": _new_id("cand"),
        "runId": run["runId"],
        "baseCheckpointId": checkpoint_id,
        "cohortId": cohort_id,
        "policyHash": str(config["policyHash"]),
        "fullAssignment": assignment,
        "delta": {"changed": dict(sorted(changed.items()))},
        "atomicGroups": [list(group) for group in plan_visible_groups(atomic_groups)],
        "allowedSourceScope": {
            "mode": "source-config",
            "forbiddenRelatives": sorted(FORBIDDEN_TRIAL_RELATIVES),
        },
        "stage": "PLANNED",
        "attemptId": 0,
        "agentSessionId": "",
        "feedbackRevision": 0,
        "budgetConsumed": {},
        "materializationRefs": {},
        "resolverContextKey": "",
        "createdAt": _now_iso(),
        "updatedAt": _now_iso(),
    }
    save_candidate(run_dir, candidate)
    run = dict(run)
    run["activeCandidateId"] = candidate["candidateId"]
    run["phase"] = "PLANNING"
    run["updatedAt"] = _now_iso()
    save_run(run_dir, run)
    _emit_status(
        {
            "event": "plan-next.candidate",
            "runId": run["runId"],
            "candidateId": candidate["candidateId"],
            "baseCheckpointId": checkpoint_id,
            "cohortPackages": sorted(changed.keys()),
            "assignmentFingerprint": assignment_fingerprint(assignment),
            "changedCount": len(changed),
        }
    )
    return 0


def plan_visible_groups(atomic_groups: Sequence[Sequence[str]]) -> Sequence[Sequence[str]]:
    return atomic_groups


def _build_atomic_groups(
    actionable: Sequence[str], config: Mapping[str, Any]
) -> List[List[str]]:
    """One atomic group per package by default.

    `cohortMaxPackages` (config) allows grouping the first N packages into one
    atomic cohort; the remainder stays as single packages.  Peer/API-coherent
    groups are not split (each package is its own group unless configured).
    """
    limit = _clamp_int(config.get("cohortMaxPackages"), 1, 1, 8)
    groups: List[List[str]] = []
    remaining = list(actionable)
    if limit > 1 and len(remaining) >= 2:
        head, remaining = remaining[:limit], remaining[limit:]
        groups.append(head)
    groups.extend([name] for name in remaining)
    return groups


def _apply_scope_expansions(
    groups: Sequence[Sequence[str]],
    ledger: Mapping[str, Any],
    base_checkpoint_id: str,
    actionable: Sequence[str],
) -> List[List[str]]:
    """Merge validated companion proposals into one atomic group.

    A companion is honored only when it is actionable in the SAME base
    checkpoint and currently planned alone.  Unknown packages are ignored
    (the planner re-validates scope; proposals are never proof).
    """
    expansion = [
        (str(item.get("candidateId") or ""), [str(c) for c in (item.get("companions") or [])])
        for item in ledger.get("scopeExpansions", [])
        if item.get("baseCheckpointId") == base_checkpoint_id
    ]
    if not expansion:
        return [list(group) for group in groups]
    merged: List[List[str]] = [list(group) for group in groups]
    for _candidate, companions in expansion:
        singles = {group[0]: i for i, group in enumerate(merged) if len(group) == 1}
        merged_companions = [c for c in companions if c in actionable]
        # Attach each validated companion to the *first* unrelated single group
        # (the original proposal package itself is not mandatory: cohesion is
        # what matters, and it is bounded by `actionable`).
        for companion in merged_companions:
            index = singles.get(companion)
            if index is None:
                continue
            # Find another single group to merge with (deterministic: first).
            for other_index, other in enumerate(merged):
                if other_index == index or len(other) != 1:
                    continue
                merged[index] = [companion, other[0]]
                break
            break
    return merged


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------

def cmd_materialize(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}", stale_seconds=300)
    lock.acquire()
    try:
        return _materialize_locked(run_dir, run, config, args)
    finally:
        lock.release()


def _materialize_locked(
    run_dir: Path, run: Mapping[str, Any], config: Mapping[str, Any], args: argparse.Namespace
) -> int:
    if not run_budget_ok(config, run):
        raise BudgetExceededError("RUN_DEADLINE_EXCEEDED")
    candidate = load_candidate(run_dir)
    if candidate is None:
        raise InvalidInputError("NO_ACTIVE_CANDIDATE")
    if candidate.get("stage") != "PLANNED":
        raise InvalidInputError(
            f"CANDIDATE_STAGE_NOT_PLANNED: {candidate.get('stage')}"
        )
    base_checkpoint = load_checkpoint(run_dir, str(candidate["baseCheckpointId"]))
    if base_checkpoint.get("status") != "VERIFIED":
        raise InvalidInputError("BASE_CHECKPOINT_NOT_VERIFIED")

    _emit_status(
        {
            "event": "materialize.start",
            "runId": run["runId"],
            "candidateId": candidate["candidateId"],
        }
    )

    snapshot = open_source_snapshot(
        Path(str(base_checkpoint["sourceSnapshotContainer"])),
        expected_key=str(base_checkpoint["sourceSnapshotKey"]),
        timeout_seconds=1800,
    )
    workspace_root = trial_workspace_root(run_dir)
    if workspace_root.exists():
        shutil.rmtree(workspace_root, ignore_errors=True)
    _materialize_trial_tree(snapshot, workspace_root, args.timeout_seconds)

    project_relative = Path(str(base_checkpoint.get("projectRelative") or "."))
    project_path = workspace_root / project_relative
    changed = _apply_assignment(project_path, candidate["fullAssignment"])

    install_result = _run_install(
        project_path,
        timeout_seconds=args.timeout_seconds,
        progress_label="iterative migration exact materialization",
    )
    if install_result.returncode != 0:
        kind = _classify_materialization_failure(install_result.stdout or "")
        run = dict(run)
        run["phase"] = "MATERIALIZING"
        run["updatedAt"] = _now_iso()
        save_run(run_dir, run)
        _emit_status(
            {
                "event": "materialize.failed",
                "runId": run["runId"],
                "candidateId": candidate["candidateId"],
                "kind": kind,
                "exitCode": install_result.returncode,
                "outputTail": (install_result.stdout or install_result.stderr or "")[-2000:],
            }
        )
        return 0

    try:
        observed = observed_resolved_assignment(project_path, candidate["fullAssignment"])
    except Exception as exc:  # drift / undeclared -> materialization not exact
        _emit_status(
            {
                "event": "materialize.observed-drift",
                "runId": run["runId"],
                "candidateId": candidate["candidateId"],
                "summary": str(exc),
            }
        )
        return 0
    observed_hash = observed_resolved_hash(observed)

    trial_snapshot = capture_source_snapshot(project_path, timeout_seconds=1800)
    candidate = dict(candidate)
    candidate["stage"] = "MATERIALIZED"
    candidate["materializationRefs"] = {
        "workspaceRoot": str(workspace_root),
        "projectRelative": str(project_relative),
        "trialSnapshotKey": trial_snapshot.key,
        "observedResolvedVersions": observed,
        "observedResolvedHash": observed_hash,
        "changedPackages": changed,
        "installExitCode": install_result.returncode,
    }
    candidate["updatedAt"] = _now_iso()
    save_candidate(run_dir, candidate)
    run = dict(run)
    run["phase"] = "MATERIALIZING"
    run["updatedAt"] = _now_iso()
    save_run(run_dir, run)
    _emit_status(
        {
            "event": "materialize.done",
            "runId": run["runId"],
            "candidateId": candidate["candidateId"],
            "observedHash": observed_hash,
            "installedPackages": len(observed),
        }
    )
    return 0


def _materialize_trial_tree(
    snapshot: SourceSnapshot, workspace_root: Path, timeout_seconds: int
) -> None:
    workspace_root.parent.mkdir(parents=True, exist_ok=True)
    method = materialize_private_tree(
        Path(snapshot.root),
        workspace_root,
        timeout_seconds=timeout_seconds or 1800,
        progress_label="iterative migration trial materialization",
    )
    if not workspace_root.exists():
        raise IterativeMigrationError(
            "TRIAL_MATERIALIZATION_MISSING", f"materialize_private_tree returned {method!r}"
        )
    # The sealed snapshot is captured read-only for integrity; this trial is a
    # working tree the agent and the package manager must be able to write in.
    apply_tree_write_protection(workspace_root, readonly=False)


def _classify_materialization_failure(output: str) -> str:
    lowered = output.lower()
    for marker in ("enoent", "econnrefused", "esockettimeout", "eai_again", "network", "getaddrinfo"):
        if marker in lowered:
            return "infrastructure"
    for marker in ("enotcached", "e404", "notarget", "no matching version", "unable to resolve", "unresolved"):
        if marker in lowered:
            return "resolver"
    if "fixed" in lowered or "conflict" in lowered:
        return "resolver"
    return "unknown"


# ---------------------------------------------------------------------------
# precheck (diagnostic screen inside the materialized trial)
# ---------------------------------------------------------------------------

def cmd_precheck(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}", stale_seconds=300)
    lock.acquire()
    try:
        return _precheck_locked(run_dir, run, config, args)
    finally:
        lock.release()


def _precheck_locked(
    run_dir: Path, run: Mapping[str, Any], config: Mapping[str, Any], args: argparse.Namespace
) -> int:
    if not run_budget_ok(config, run):
        raise BudgetExceededError("RUN_DEADLINE_EXCEEDED")
    candidate = load_candidate(run_dir)
    if candidate is None or candidate.get("stage") != "MATERIALIZED":
        raise InvalidInputError("CANDIDATE_NOT_MATERIALIZED")
    refs = candidate.get("materializationRefs") or {}
    workspace_root = Path(str(refs["workspaceRoot"]))
    project_path = workspace_root / str(refs.get("projectRelative") or ".")

    commands = list((config.get("verifyConfig") or {}).get("commands") or ())
    if not commands:
        commands = list(discover_baseline_project_checks(project_path))

    failing: List[Dict[str, Any]] = []
    first_failure: Optional[Dict[str, Any]] = None
    for command in commands:
        env: Dict[str, str] = {
            "CI": "1",
            "npm_config_ignore_scripts": "true",
        }
        result = _run(
            _command_argv(command),
            project_path,
            timeout_seconds=_clamp_int(args.timeout_seconds, 1200, 120, 4 * 3600),
            env=env,
            base_env=os.environ,
            progress_label=f"iterative migration precheck {command}",
        )
        failed = result.returncode != 0
        entry = {
            "command": command,
            "exitCode": result.returncode,
            "tail": (result.stdout or result.stderr or "")[-4000:] if failed else "",
        }
        if failed:
            failing.append(entry)
            if first_failure is None:
                first_failure = entry
                break  # PRECHECK may stop at the first meaningful error

    candidate = dict(candidate)
    if failing:
        candidate["stage"] = "REPAIRING"
        candidate["diagnostics"] = failing
        candidate["updatedAt"] = _now_iso()
        save_candidate(run_dir, candidate)
        _write_candidate_repair_requests(
            run_dir, run, candidate, config, failing_commands=failing, reason="project-source-config-repair"
        )
        run = dict(run)
        run["phase"] = "REPAIRING"
        run["updatedAt"] = _now_iso()
        save_run(run_dir, run)
        _emit_status(
            {
                "event": "precheck.repair-required",
                "runId": run["runId"],
                "candidateId": candidate["candidateId"],
                "failingCommands": [
                    {"command": item["command"], "exitCode": item["exitCode"]}
                    for item in failing
                ],
            }
        )
        return 0

    candidate["stage"] = "PRECHECKED"
    candidate["updatedAt"] = _now_iso()
    save_candidate(run_dir, candidate)
    run = dict(run)
    run["phase"] = "PRECHECK"
    run["updatedAt"] = _now_iso()
    save_run(run_dir, run)
    _emit_status(
        {
            "event": "precheck.pass",
            "runId": run["runId"],
            "candidateId": candidate["candidateId"],
            "commands": commands,
        }
    )
    return 0


def _command_argv(command: str) -> List[str]:
    import shlex

    return shlex.split(command)


def _write_candidate_repair_requests(
    run_dir: Path,
    run: Mapping[str, Any],
    candidate: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    failing_commands: Sequence[Mapping[str, Any]],
    reason: str,
) -> None:
    project_name = str(config.get("projectName") or "")
    assignment = candidate["fullAssignment"]
    request = {
        "schemaVersion": SCHEMA_VERSION,
        "requestId": _new_id("repair"),
        "candidateId": candidate["candidateId"],
        "runId": run["runId"],
        "baseCheckpointId": candidate["baseCheckpointId"],
        "attemptId": candidate.get("attemptId", 0),
        "project": project_name,
        "mode": "iterative",
        "assignment": [list(item) for item in sorted(assignment.items())],
        "fingerprint": assignment_fingerprint(assignment),
        "snapshotIdentity": str(
            (candidate.get("materializationRefs") or {}).get("trialSnapshotKey") or ""
        ),
        "failingCommands": [
            {"command": item["command"], "exitCode": item["exitCode"]}
            for item in failing_commands
        ],
        "diagnosticsTail": (failing_commands[0]["tail"] if failing_commands else ""),
        "reason": reason,
        "disposition": "repair-or-replan",
    }
    existing = read_repair_requests(run_dir)
    if not any(
        item.get("candidateId") == candidate["candidateId"] and item.get("attemptId") == candidate.get("attemptId")
        for item in existing
    ):
        existing.append(request)
    write_repair_requests(run_dir, existing)


# ---------------------------------------------------------------------------
# verify-exact (authoritative; the only way to accept a checkpoint)
# ---------------------------------------------------------------------------

def cmd_verify_exact(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}", stale_seconds=600)
    lock.acquire()
    try:
        return _verify_exact_locked(run_dir, run, config, args)
    finally:
        lock.release()


def _verify_exact_locked(
    run_dir: Path, run: Mapping[str, Any], config: Mapping[str, Any], args: argparse.Namespace
) -> int:
    if not run_budget_ok(config, run):
        raise BudgetExceededError("RUN_DEADLINE_EXCEEDED")
    candidate = load_candidate(run_dir)
    if candidate is None:
        raise InvalidInputError("NO_ACTIVE_CANDIDATE")
    if candidate.get("stage") not in {"PRECHECKED", "REPAIRING", "VERIFYING"}:
        raise InvalidInputError(
            f"CANDIDATE_STAGE_NOT_READY_FOR_VERIFY: {candidate.get('stage')}"
        )
    base_checkpoint = load_checkpoint(run_dir, str(candidate["baseCheckpointId"]))

    refs = candidate.get("materializationRefs") or {}
    workspace_root = Path(str(refs["workspaceRoot"]))
    project_path = workspace_root / str(refs.get("projectRelative") or ".")
    if not (project_path / "package.json").exists():
        raise InvalidInputError(f"TRIAL_PROJECT_MISSING: {project_path}")

    candidate = dict(candidate)
    candidate["stage"] = "VERIFYING"
    candidate["updatedAt"] = _now_iso()
    save_candidate(run_dir, candidate)
    run = dict(run)
    run["phase"] = "VERIFYING"
    run["updatedAt"] = _now_iso()
    save_run(run_dir, run)

    verify_config = verify_config_from(config["verifyConfig"], run_dir)
    verify_config = dataclasses.replace(
        verify_config,
        verification_purpose="intermediate-candidate",
        project_checks=config.get("verifyConfig", {}).get("projectChecks", "adaptive"),
    )
    _emit_status(
        {
            "event": "verify-exact.start",
            "runId": run["runId"],
            "candidateId": candidate["candidateId"],
        }
    )
    result = verify_assignment(
        project_path,
        candidate["fullAssignment"],
        config=verify_config,
        run_project_checks=True,
        progress_label="iterative migration verify-exact",
    )
    _emit_status(
        {
            "event": "verify-exact.result",
            "runId": run["runId"],
            "candidateId": candidate["candidateId"],
            "ok": result.ok,
            "kind": result.kind,
            "summary": result.summary,
            "projectFailures": [
                {"command": f.command, "exitCode": f.exit_code}
                for f in result.project_failures
            ],
        }
    )

    if result.ok:
        return _accept_checkpoint(
            run_dir, run, config, candidate, base_checkpoint, result
        )

    if result.kind == "project":
        failing = [
            {"command": f.command, "exitCode": f.exit_code, "tail": f.output}
            for f in result.project_failures
        ]
        candidate = load_candidate(run_dir)
        candidate = dict(candidate)
        ledger = load_ledger(run_dir)
        counters = dict(ledger.get("counters", {}))
        completed_repairs = int(counters.get("repairAttemptsForRevision", 0)) + 1
        max_attempts = int(config["budget"]["maxRepairAttemptsPerRevision"])
        counters["repairAttemptsForRevision"] = completed_repairs
        ledger = dict(ledger)
        ledger["counters"] = counters
        save_ledger(run_dir, ledger)
        if completed_repairs >= max_attempts:
            _record_block(
                ledger,
                run_dir,
                candidateId=str(candidate["candidateId"]),
                baseCheckpointId=str(candidate["baseCheckpointId"]),
                kind="NOT_ACTIONABLE",
                reason=f"REPAIR_ATTEMPTS_EXHAUSTED completed={completed_repairs} max={max_attempts}",
                assignmentFingerprint=assignment_fingerprint(candidate["fullAssignment"]),
            )
            candidate["stage"] = "REJECTED"
            candidate["updatedAt"] = _now_iso()
            save_candidate(run_dir, candidate)
            run = dict(run)
            run["activeCandidateId"] = None
            run["phase"] = "READY"
            run["updatedAt"] = _now_iso()
            save_run(run_dir, run)
            _emit_status(
                {
                    "event": "verify-exact.rejected",
                    "runId": run["runId"],
                    "candidateId": candidate["candidateId"],
                    "reason": "REPAIR_ATTEMPTS_EXHAUSTED",
                }
            )
            return 0
        candidate["attemptId"] = int(candidate.get("attemptId", 0)) + 1
        candidate["stage"] = "REPAIRING"
        candidate["updatedAt"] = _now_iso()
        save_candidate(run_dir, candidate)
        _write_candidate_repair_requests(
            run_dir,
            run,
            candidate,
            config,
            failing_commands=failing,
            reason="project-source-config-repair",
        )
        run = dict(run)
        run["phase"] = "REPAIRING"
        run["updatedAt"] = _now_iso()
        save_run(run_dir, run)
        return 0

    if result.kind in {"dependency", "preparation"}:
        # A deterministic dependency/preparation failure of the EXACT candidate
        # becomes a deterministic scoped block for the same base checkpoint.
        ledger = load_ledger(run_dir)
        _record_block(
            ledger,
            run_dir,
            candidateId=str(candidate["candidateId"]),
            baseCheckpointId=str(candidate["baseCheckpointId"]),
            kind="NEGATIVE_VERIFY",
            reason=f"kind={result.kind}: {result.summary}",
            assignmentFingerprint=assignment_fingerprint(candidate["fullAssignment"]),
        )
        candidate = load_candidate(run_dir)
        candidate = dict(candidate)
        candidate["stage"] = "REJECTED"
        candidate["updatedAt"] = _now_iso()
        save_candidate(run_dir, candidate)
        run = dict(run)
        run["activeCandidateId"] = None
        run["phase"] = "READY"
        run["updatedAt"] = _now_iso()
        save_run(run_dir, run)
        _emit_status(
            {
                "event": "verify-exact.rejected",
                "runId": run["runId"],
                "candidateId": candidate["candidateId"],
                "reason": f"DETERMINISTIC_{result.kind.upper()}",
            }
        )
        return 0

    run = dict(run)
    run["phase"] = "VERIFYING"
    run["updatedAt"] = _now_iso()
    save_run(run_dir, run)
    return 0


def _record_block(
    ledger: Dict[str, Any], run_dir: Path, *, candidateId: str, baseCheckpointId: str,
    kind: str, reason: str, assignmentFingerprint: str,
) -> None:
    entry = {
        "kind": kind,
        "candidateId": candidateId,
        "baseCheckpointId": baseCheckpointId,
        "assignmentFingerprint": assignmentFingerprint,
        "reason": reason,
        "createdAt": _now_iso(),
    }
    ledger = dict(ledger)
    blocks = list(ledger.get("blocks", []))
    blocks.append(entry)
    ledger["blocks"] = blocks
    save_ledger(run_dir, ledger)


def _accept_checkpoint(
    run_dir: Path,
    run: Mapping[str, Any],
    config: Mapping[str, Any],
    candidate: Mapping[str, Any],
    base_checkpoint: Mapping[str, Any],
    result: BaselineVerifyResult,
) -> int:
    candidate_id = str(candidate["candidateId"])
    base_id = str(base_checkpoint["checkpointId"])
    seq = int(base_checkpoint.get("seq", 0)) + 1
    checkpoint_id = f"C{seq}"
    workspace_root = Path(str(candidate["materializationRefs"]["workspaceRoot"]))
    project_path = workspace_root / str(candidate["materializationRefs"].get("projectRelative") or ".")
    manifest_hash, lock_hash, lock_path = manifest_and_lock_hashes(project_path)

    _emit_status(
        {
            "event": "verify-exact.accepting",
            "runId": run["runId"],
            "candidateId": candidate_id,
            "checkpointId": checkpoint_id,
            "parent": base_id,
        }
    )
    snapshot = capture_durable_source_snapshot(
        project_path, run_dir / SOURCE_DIR / checkpoint_id, timeout_seconds=1800
    )
    project_relative = resolve_project_relative(project_path, snapshot)

    observed = dict(result.observed_resolved_versions or {})
    old_assignment = {str(k): str(v) for k, v in (base_checkpoint.get("fullAssignment") or {}).items()}
    new_assignment = {str(k): str(v) for k, v in (candidate.get("fullAssignment") or {}).items()}
    accepted_delta = _accepted_delta(old_assignment, new_assignment)

    checkpoint: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "checkpointId": checkpoint_id,
        "parentCheckpointId": base_id,
        "seq": seq,
        "status": "VERIFIED",
        "sourceSnapshotKey": snapshot.key,
        "sourceSnapshotContainer": str(snapshot.container),
        "sourceHead": snapshot.git_head or "",
        "projectRelative": str(project_relative),
        "manifestHash": manifest_hash,
        "lockfileHash": lock_hash or "",
        "resolvedStateKey": result.resolved_state_key or "",
        "observedResolvedHash": result.observed_resolved_hash or observed_resolved_hash(observed),
        "fullAssignment": new_assignment,
        "acceptedDelta": accepted_delta,
        "cohortId": candidate.get("cohortId") or None,
        "verification": {
            "status": "passed",
            "kind": result.kind,
            "commands": list((config.get("verifyConfig") or {}).get("commands") or ()),
            "failingCommands": [],
        },
        "proofRefs": {
            "resolvedStateKey": result.resolved_state_key or "",
            "preparationProofKey": result.preparation_proof_key or "",
        },
        "audit": {"status": "UNKNOWN", "evidenceRef": ""},
        "createdAt": _now_iso(),
        "toolBuildId": config.get("toolBuildId") or "",
    }
    save_checkpoint(run_dir, checkpoint)

    # Atomic pointer switch: the new checkpoint is durable before the pointer moves.
    run = dict(run)
    run["activeCheckpointId"] = checkpoint_id
    run["activeCandidateId"] = None
    run["generation"] = int(run.get("generation", 1)) + 1
    run["phase"] = "READY"
    run["updatedAt"] = _now_iso()
    save_run(run_dir, run)
    clear_candidate(run_dir)
    write_repair_requests(run_dir, [])

    _emit_status(
        {
            "event": "checkpoint.accepted",
            "runId": run["runId"],
            "checkpointId": checkpoint_id,
            "parent": base_id,
            "acceptedPackages": sorted(accepted_delta["changed"].keys()),
            "deltaSummary": {
                "changed": len(accepted_delta["changed"]),
                "added": len(accepted_delta["added"]),
                "removed": len(accepted_delta["removed"]),
            },
        }
    )
    return 0


def _accepted_delta(
    old_assignment: Mapping[str, str], new_assignment: Mapping[str, str]
) -> Dict[str, Dict[str, str]]:
    added: Dict[str, str] = {}
    changed: Dict[str, str] = {}
    removed: Dict[str, str] = {}
    for name, new_version in sorted(new_assignment.items()):
        old_version = old_assignment.get(name)
        if old_version is None:
            added[name] = new_version
        elif str(old_version) != str(new_version):
            changed[name] = new_version
    for name, old_version in sorted(old_assignment.items()):
        if name not in new_assignment:
            removed[name] = old_version
    return {"added": added, "changed": changed, "removed": removed}


# ---------------------------------------------------------------------------
# apply-feedback (typed agent feedback => scheduling, never proof)
# ---------------------------------------------------------------------------

def cmd_apply_feedback(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    feedback = _read_feedback(args.feedback_file)
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}", stale_seconds=120)
    lock.acquire()
    try:
        return _apply_feedback_locked(run_dir, run, config, feedback)
    finally:
        lock.release()


def _read_feedback(feedback_file: str) -> Dict[str, Any]:
    path = Path(feedback_file).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidInputError(f"FEEDBACK_FILE_INVALID: {exc}") from exc
    if not isinstance(value, dict):
        raise InvalidInputError("FEEDBACK_NOT_OBJECT")
    return value


def _apply_feedback_locked(
    run_dir: Path, run: Mapping[str, Any], config: Mapping[str, Any], feedback: Mapping[str, Any]
) -> int:
    _require_schema(feedback, "FEEDBACK")
    candidate = load_candidate(run_dir)
    if candidate is None:
        raise StaleFeedbackError(
            f"STALE_FEEDBACK_NO_CANDIDATE: run={feedback.get('runId')}"
        )
    candidate_run = str(candidate.get("runId") or "")
    if candidate_run != str(feedback.get("runId") or ""):
        raise StaleFeedbackError(
            f"STALE_FEEDBACK_RUN: candidate run={candidate_run} feedback run={feedback.get('runId')}"
        )
    if candidate["candidateId"] != str(feedback.get("candidateId") or ""):
        raise StaleFeedbackError(
            f"STALE_FEEDBACK_CANDIDATE: active={candidate['candidateId']} feedback={feedback.get('candidateId')}"
        )
    if candidate["baseCheckpointId"] != str(feedback.get("baseCheckpointId") or ""):
        raise StaleFeedbackError(
            f"STALE_FEEDBACK_BASE: active base={candidate['baseCheckpointId']} feedback={feedback.get('baseCheckpointId')}"
        )
    attempt_id = int(feedback.get("attemptId", -1))
    if attempt_id != int(candidate.get("attemptId", 0)):
        raise StaleFeedbackError(
            f"STALE_FEEDBACK_ATTEMPT: active attempt={candidate.get('attemptId')} feedback={attempt_id}"
        )
    kind = str(feedback.get("kind") or "").strip().upper()
    if kind not in FEEDBACK_KINDS:
        raise InvalidInputError(f"FEEDBACK_KIND_INVALID: {kind}")

    changed_files = feedback.get("changedFiles")
    if changed_files is not None:
        if not isinstance(changed_files, list) or not all(
            isinstance(item, str) for item in changed_files
        ):
            raise InvalidInputError("FEEDBACK_CHANGED_FILES_INVALID")
        _validate_changed_files(run_dir, candidate, changed_files)

    ledger = load_ledger(run_dir)
    feedback_record = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": str(feedback["runId"]),
        "candidateId": candidate["candidateId"],
        "baseCheckpointId": candidate["baseCheckpointId"],
        "attemptId": attempt_id,
        "kind": kind,
        "reason": str(feedback.get("reason") or ""),
        "changedFiles": changed_files or [],
        "diagnosticsRefs": feedback.get("diagnosticsRefs") or [],
        "proposedScope": feedback.get("proposedScope") or {},
        "proposedConstraints": feedback.get("proposedConstraints") or {},
        "createdAt": _now_iso(),
    }
    ledger = dict(ledger)
    feedback_list = list(ledger.get("feedback", []))
    feedback_list.append(feedback_record)
    ledger["feedback"] = feedback_list

    run = dict(run)
    if kind in {"READY_FOR_VERIFY"}:
        run["phase"] = "VERIFYING"
    elif kind in {"NEEDS_COHORT_EXPANSION", "NEEDS_ALTERNATIVE", "INCONCLUSIVE", "INFRA_BLOCKED"}:
        # Scheduling conclusions: the candidate is concluded and freed so the
        # planner can produce a new revision from the SAME base checkpoint.
        # They never carry proof authority: nothing here changes the accepted
        # checkpoint.
        candidate = dict(candidate)
        candidate["stage"] = "REJECTED"
        candidate["updatedAt"] = _now_iso()
        save_candidate(run_dir, candidate)
        run["activeCandidateId"] = None
        run["phase"] = "READY"
        if kind == "NEEDS_COHORT_EXPANSION":
            proposed = feedback.get("proposedScope") or {}
            companions = (
                proposed.get("companions")
                if isinstance(proposed, dict)
                else None
            )
            if companions is not None:
                if not isinstance(companions, list) or not all(
                    isinstance(c, str) for c in companions
                ):
                    raise InvalidInputError("FEEDBACK_COMPANIONS_INVALID")
                _record_scope_expansion(
                    ledger, candidate, list(companions), str(feedback.get("reason") or "")
                )
        if kind == "INCONCLUSIVE":
            _append_deferral(ledger, candidate, str(feedback.get("reason") or "inconclusive"))
    elif kind == "REPAIRING":
        run["phase"] = "REPAIRING"

    run = dict(run)
    run["updatedAt"] = _now_iso()
    save_run(run_dir, run)
    save_ledger(run_dir, ledger)
    _emit_status(
        {
            "event": "feedback.accepted",
            "runId": run["runId"],
            "candidateId": candidate["candidateId"],
            "kind": kind,
            "attemptId": attempt_id,
        }
    )
    return 0


def _record_scope_expansion(
    ledger: Dict[str, Any],
    candidate: Mapping[str, Any],
    companions: Sequence[str],
    reason: str,
) -> None:
    """Record a proposed companion (cohort expansion) as deterministic scope input.

    The planner re-validates the proposal (package must be in the managed set)
    before it can affect group construction; the proposal is never proof.
    """
    ledger["scopeExpansions"] = list(ledger.get("scopeExpansions", [])) + [
        {
            "baseCheckpointId": candidate["baseCheckpointId"],
            "candidateId": candidate["candidateId"],
            "package": "",
            "companions": [str(item) for item in companions],
            "reason": reason,
            "createdAt": _now_iso(),
        }
    ]


def _append_deferral(
    ledger: Dict[str, Any], candidate: Mapping[str, Any], reason: str
) -> None:
    """Bounded deferral: a later revision may retry with a fresh reason."""
    ledger["deferrals"] = list(ledger.get("deferrals", [])) + [
        {
            "baseCheckpointId": candidate["baseCheckpointId"],
            "candidateId": candidate["candidateId"],
            "cohortId": candidate.get("cohortId"),
            "reason": reason,
            "createdAt": _now_iso(),
        }
    ]


def _validate_changed_files(
    run_dir: Path, candidate: Mapping[str, Any], changed_files: Sequence[str]
) -> None:
    workspace_root = Path(
        str((candidate.get("materializationRefs") or {}).get("workspaceRoot") or "")
    ).resolve()
    allowed_scope = candidate.get("allowedSourceScope") or {}
    forbidden = set(allowed_scope.get("forbiddenRelatives") or FORBIDDEN_TRIAL_RELATIVES)
    workspace_root_str = str(workspace_root).lower()
    for raw in changed_files:
        value = str(raw).replace("\\", "/")
        candidate_path = (workspace_root / value).resolve()
        if not str(candidate_path).lower().startswith(workspace_root_str):
            raise InvalidInputError(
                f"FEEDBACK_PATH_OUTSIDE_TRIAL: {raw}"
            )
        relative = candidate_path.relative_to(workspace_root)
        parts = relative.parts
        if parts and parts[0] in {"node_modules"}:
            raise InvalidInputError(f"FEEDBACK_FORBIDDEN_DEPENDENCY_PATH: {raw}")
        if parts and parts[-1] in forbidden:
            raise InvalidInputError(
                f"FEEDBACK_FORBIDDEN_MANIFEST_LOCK_PATH: {raw}"
            )


# ---------------------------------------------------------------------------
# status, finish, audit
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    checkpoint_id = str(run["activeCheckpointId"])
    checkpoint = load_checkpoint(run_dir, checkpoint_id)
    candidate = load_candidate(run_dir)
    ledger = load_ledger(run_dir)
    repair_requests = read_repair_requests(run_dir)
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "run": run,
        "activeCheckpoint": checkpoint,
        "candidate": candidate,
        "ledgerSummary": {
            "blocks": len(ledger.get("blocks", [])),
            "deferrals": len(ledger.get("deferrals", [])),
            "feedback": len(ledger.get("feedback", [])),
            "counters": ledger.get("counters", {}),
        },
        "openRepairRequests": repair_requests,
        "config": {
            "projectName": config.get("projectName"),
            "targetLevel": config.get("targetLevel"),
            "budget": config.get("budget"),
            "verifyConfigCommands": config.get("verifyConfig", {}).get("commands", []),
        },
    }
    status_path = run_dir / "status.json"
    _write_json_atomic(status_path, payload)
    _emit_status({"event": "status.ready", "runId": run["runId"], "statusPath": str(status_path)})
    print(_stable_json(payload))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    checkpoints = _all_checkpoints(run_dir)
    terminal = run.get("terminal")
    reports_dir = run_dir / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = _build_migration_report(run, config, checkpoints)
    guide = _build_developer_upgrade_guide(config, checkpoints)
    (reports_dir / "MIGRATION_REPORT.md").write_text(report, encoding="utf-8")
    (reports_dir / "DEVELOPER_UPGRADE_GUIDE.md").write_text(guide, encoding="utf-8")
    _emit_status(
        {
            "event": "finish.done",
            "runId": run["runId"],
            "terminal": terminal,
            "checkpoints": [c["checkpointId"] for c in checkpoints],
            "reportsDir": str(reports_dir),
        }
    )
    return 0


def _all_checkpoints(run_dir: Path) -> List[Dict[str, Any]]:
    checkpoint_ids = []
    for path in sorted((run_dir / CHECKPOINT_DIR).glob("C*.json")):
        try:
            checkpoint_ids.append(path.stem)
        except Exception:
            continue
    return [load_checkpoint(run_dir, cid) for cid in checkpoint_ids]


def _build_migration_report(
    run: Mapping[str, Any], config: Mapping[str, Any], checkpoints: Sequence[Mapping[str, Any]]
) -> str:
    lines: List[str] = []
    lines.append("# MIGRATION_REPORT")
    lines.append("")
    lines.append(f"- Run: `{run.get('runId')}`")
    lines.append(f"- Project: `{config.get('projectName')}`")
    lines.append(f"- Target level: `{config.get('targetLevel')}`")
    lines.append(f"- Terminal: `{run.get('terminal') or 'in-progress'}`")
    lines.append(f"- Active checkpoint: `{run.get('activeCheckpointId')}`")
    lines.append("")
    lines.append("## Checkpoints")
    lines.append("")
    lines.append("| Checkpoint | Parent | Status | Changed packages | Audit |")
    lines.append("| --- | --- | --- | --- | --- |")
    for checkpoint in checkpoints:
        delta = checkpoint.get("acceptedDelta") or {}
        changed = list((delta.get("changed") or {}).items())
        parent_state = {}
        for parent in checkpoints:
            if parent.get("checkpointId") == checkpoint.get("parentCheckpointId"):
                parent_state = parent.get("fullAssignment") or {}
                break
        summary = (
            ", ".join(
                f"`{name}` {parent_state.get(name, '<unknown>')}→{new}"
                for name, new in changed
            )
            or "-"
        )
        lines.append(
            f"| {checkpoint.get('checkpointId')} | {checkpoint.get('parentCheckpointId') or '-'} "
            f"| {checkpoint.get('status')} | {summary} | {checkpoint.get('audit', {}).get('status', 'UNKNOWN')} |"
        )
    lines.append("")
    active = checkpoints[-1] if checkpoints else {}
    lines.append("## Accepted dependency state")
    lines.append("")
    lines.append("```json")
    lines.append(_stable_json(active.get("fullAssignment") or {}))
    lines.append("```")
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    for checkpoint in checkpoints:
        lines.append(
            f"- {checkpoint.get('checkpointId')}: sourceSnapshotKey=`{checkpoint.get('sourceSnapshotKey')}`, "
            f"manifestHash=`{checkpoint.get('manifestHash')}`, lockfileHash=`{checkpoint.get('lockfileHash') or '-'}`, "
            f"resolvedStateKey=`{checkpoint.get('resolvedStateKey') or '-'}`"
        )
    lines.append("")
    return "\n".join(lines)


def _build_developer_upgrade_guide(
    config: Mapping[str, Any], checkpoints: Sequence[Mapping[str, Any]]
) -> str:
    upgraded: Dict[str, Dict[str, str]] = {}
    for checkpoint in checkpoints:
        delta = checkpoint.get("acceptedDelta") or {}
        for name, new_version in (delta.get("changed") or {}).items():
            old = "<unknown>"
            parent_id = checkpoint.get("parentCheckpointId")
            if parent_id:
                parent = next(
                    (c for c in checkpoints if c.get("checkpointId") == parent_id), None
                )
                if parent:
                    old = str((parent.get("fullAssignment") or {}).get(name, old))
            upgraded[name] = {"from": old, "to": str(new_version)}
    lines: List[str] = []
    lines.append("# DEVELOPER_UPGRADE_GUIDE")
    lines.append("")
    if not upgraded:
        lines.append(
            "Проверенных обновлений зависимостей на текущий момент нет. "
            "Agents/report: уточнять фактическую адаптацию после первого принятого upgrade."
        )
    else:
        lines.append("## Accepted upgrades")
        lines.append("")
        lines.append("| Package | From | To |")
        lines.append("| --- | --- | --- |")
        for name, versions in sorted(upgraded.items()):
            lines.append(f"| `{name}` | `{versions['from']}` | `{versions['to']}` |")
        lines.append("")
        lines.append(
            "Возможности, breaking changes и ограничения toolchain описываются по мере "
            "реальных upgrade-сессий (см. diagnostics/checkpoint evidence)."
        )
    lines.append("")
    return "\n".join(lines)


def cmd_audit(args: argparse.Namespace) -> int:
    """Independent audit of the ACTIVE checkpoint's exact dependency state."""
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}", stale_seconds=600)
    lock.acquire()
    try:
        return _audit_locked(run_dir, run, config, args)
    finally:
        lock.release()


def _audit_locked(
    run_dir: Path, run: Mapping[str, Any], config: Mapping[str, Any], args: argparse.Namespace
) -> int:
    checkpoint = load_checkpoint(run_dir, str(run["activeCheckpointId"]))
    snapshot = open_source_snapshot(
        Path(str(checkpoint["sourceSnapshotContainer"])),
        expected_key=str(checkpoint["sourceSnapshotKey"]),
        timeout_seconds=1800,
    )
    project_path = Path(snapshot.root) / str(checkpoint.get("projectRelative") or ".")
    try:
        from manual_dependency_audit import build_report
    except Exception as exc:
        raise IterativeMigrationError("AUDIT_UNAVAILABLE", f"{exc}") from exc
    audit_workspace = run_dir / AUDIT_DIR / f"{checkpoint['checkpointId']}"
    audit_workspace.mkdir(parents=True, exist_ok=True)
    report = build_report(
        project_path,
        str(config.get("projectName") or ""),
        registry=args.registry or "",
        lag_months=int(
            (args.lag_months or config.get("auditPolicy", {}).get("lagMonths", 12))
        ),
        dashboard_state=None,
        audit_workspace=audit_workspace,
        target_level=str(config.get("targetLevel") or "yellow"),
        min_lag_ok_pct=int(
            (args.min_lag_ok_pct or config.get("auditPolicy", {}).get("minLagOkPct", 80))
        ),
        max_known_high=int((args.max_known_high if args.max_known_high is not None
                            else config.get("auditPolicy", {}).get("maxKnownHigh", 1))),
        yarn_audit_engine=args.yarn_audit_engine or "auto",
    )
    evidence_ref = str(audit_workspace)
    audit_status = _audit_status_from_report(report)
    checkpoint = dict(checkpoint)
    checkpoint["audit"] = {"status": audit_status, "evidenceRef": evidence_ref}
    save_checkpoint(run_dir, checkpoint)
    _emit_status(
        {
            "event": "audit.done",
            "runId": run["runId"],
            "checkpointId": checkpoint["checkpointId"],
            "status": audit_status,
            "evidenceRef": evidence_ref,
        }
    )
    return 0


def _audit_status_from_report(report: Mapping[str, Any]) -> str:
    if not report:
        return "UNKNOWN"
    if report.get("auditComplete") is False:
        return "UNKNOWN"
    exit_code = 0
    from manual_dependency_audit import audit_command_exit_code

    try:
        exit_code = int(audit_command_exit_code(report))
    except Exception:
        exit_code = 1
    return "PASS" if exit_code == 0 else "FAIL"


def cmd_export_task(args: argparse.Namespace) -> int:
    """Publish the ТЗ (task) artifact from durable state only.

    Never starts a Baseline and never runs project checks: the builder reads
    run.json / run-config.json / checkpoints / ledger.  Insufficient durable
    JSON is a diagnosed InvalidInputError listing the missing fields, so the
    UI can show the reason and a concrete next action.
    """
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "run.json").exists():
        raise InvalidInputError("RUN_STATE_MISSING: no run.json under the run dir")
    from iterative_task import TaskExportError, export_task_artifact, verify_task_input_errors

    languages = ["ru", "en"] if args.language == "both" else [args.language]
    if args.check_only:
        missing = verify_task_input_errors(run_dir)
        if missing:
            raise InvalidInputError(
                "TASK_INPUT_INSUFFICIENT: " + "; ".join(missing)
            )
        _emit_status(
            {
                "event": "export-task.check",
                "runId": str(json.loads((run_dir / "run.json").read_text(encoding="utf-8")).get("runId") or ""),
                "sufficient": True,
            }
        )
        return 0
    try:
        summary = export_task_artifact(run_dir, languages=languages)
    except TaskExportError as exc:
        raise InvalidInputError(f"{exc.code}: {exc.summary}")
    _emit_status(summary)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iterative_migration",
        description="Iterative (накопительная) dependency migration steps.",
    )
    parser.add_argument("--run-dir", required=True, help="Durable run directory")
    parser.add_argument("--owner", default="", help="Writer/lease owner id")
    sub = parser.add_subparsers(dest="command", required=True)

    begin = sub.add_parser("begin", help="Захват C0 (Исходный замер)")
    begin.add_argument("--project-dir", required=True)
    begin.add_argument("--project-name", default="")
    begin.add_argument("--workspace-id", default="")
    begin.add_argument("--project-id", default="")
    begin.add_argument("--run-id", default="")
    begin.add_argument("--target-level", choices=("yellow", "green"), default="yellow")
    begin.add_argument("--targets-file", default="", help="JSON {package: exact version} policy targets")
    begin.add_argument("--verify-config", default="", help="BaselineVerifyConfig JSON file (commands, projectChecks, ...)")
    begin.add_argument("--tool-build-id", default="")
    begin.add_argument("--run-budget-minutes", type=int, default=None)
    begin.add_argument("--max-repair-attempts", type=int, default=None)
    begin.add_argument("--max-infra-retries", type=int, default=None)
    begin.add_argument("--phase-timeout-seconds", type=int, default=None)

    verify_bootstrap = sub.add_parser("verify-bootstrap", help="Повторная контрольная проверка C0 после bootstrap repair")
    verify_bootstrap.add_argument("--timeout-seconds", type=int, default=1800)

    plan_next = sub.add_parser("plan-next", help="Выбрать следующий небольшой шаг от активного checkpoint")

    materialize = sub.add_parser("materialize", help="Физическая материализация точных версий в isolated trial")
    materialize.add_argument("--timeout-seconds", type=int, default=1200)

    precheck = sub.add_parser("precheck", help="Диагностический прогон проверок в trial (PRECHECK)")
    precheck.add_argument("--timeout-seconds", type=int, default=1200)

    verify_exact = sub.add_parser("verify-exact", help="Авторитетная полная проверка того же exact candidate на repaired bytes")

    apply_feedback = sub.add_parser("apply-feedback", help="Проверить и принять typed feedback агента")
    apply_feedback.add_argument("--feedback-file", required=True)

    status = sub.add_parser("status", help="Машинный статус run")
    finish = sub.add_parser("finish", help="Сформировать MIGRATION_REPORT.md / DEVELOPER_UPGRADE_GUIDE.md")

    audit = sub.add_parser("audit", help="Независимый аудит активного checkpoint")
    audit.add_argument("--registry", default="")
    audit.add_argument("--lag-months", type=int, default=None)
    audit.add_argument("--min-lag-ok-pct", type=int, default=None)
    audit.add_argument("--max-known-high", type=int, default=None)
    audit.add_argument("--yarn-audit-engine", default="auto")

    export_task = sub.add_parser("export-task", help="Сформировать ТЗ (task artifact) из durable state без Baseline")
    export_task.add_argument("--language", choices=("ru", "en", "both"), default="both")
    export_task.add_argument(
        "--check-only", action="store_true",
        help="Only diagnose input sufficiency; never produces an artifact",
    )

    return parser


COMMANDS = {
    "begin": cmd_begin,
    "verify-bootstrap": cmd_verify_bootstrap,
    "plan-next": cmd_plan_next,
    "materialize": cmd_materialize,
    "precheck": cmd_precheck,
    "verify-exact": cmd_verify_exact,
    "apply-feedback": cmd_apply_feedback,
    "status": cmd_status,
    "finish": cmd_finish,
    "audit": cmd_audit,
    "export-task": cmd_export_task,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        configure_utf8_stdio()
        return int(COMMANDS[args.command](args) or 0)
    except (StaleFeedbackError, InvalidInputError) as exc:
        print(f"ITERATIVE_MIGRATION_FAILURE_V1 {json.dumps({'code': exc.code, 'summary': str(exc)})}")
        return 2
    except BudgetExceededError as exc:
        print(f"ITERATIVE_MIGRATION_FAILURE_V1 {json.dumps({'code': exc.code, 'summary': str(exc)})}")
        return 4
    except IterativeMigrationError as exc:
        print(f"ITERATIVE_MIGRATION_FAILURE_V1 {json.dumps({'code': exc.code, 'summary': str(exc)})}")
        return 5
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        import traceback

        traceback.print_exc()
        print(
            f"ITERATIVE_MIGRATION_FAILURE_V1 "
            f"{json.dumps({'code': 'INTERNAL', 'summary': f'{type(exc).__name__}: {exc}'})}"
        )
        return 1


def configure_utf8_stdio() -> None:
    import sys

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
