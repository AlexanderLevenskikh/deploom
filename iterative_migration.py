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
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # pragma: no cover - import parity with project_runtime
    from semantic_version import Version  # type: ignore
except Exception:  # pragma: no cover
    Version = None  # type: ignore

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
    project_check_command_argv,
    structural_project_failure_signatures,
    verify_assignment,
)
from baseline_repair_handoff import build_repair_request
from verification_proof import is_fixed_manifest_spec
from iterative_target_deferrals import record_unavailable_targets, unavailable_targets
from iterative_restart import TRANSACTION as RESTART_TRANSACTION, RestartArchiveError, archive_for_restart, recover_restart_archive
from block_psi_progressive_baseline import plan_progressive_extension
from package_manager_profile import (
    install_args_for_profile,
    resolve_package_manager_profile,
)
from project_topology import ProjectTopologyError, resolve_project_topology
from source_snapshot import (
    SourceCaptureError,
    SourceSnapshot,
    capture_durable_source_snapshot,
    capture_source_snapshot,
    incomplete_submodules,
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


class ProjectUnreadyError(IterativeMigrationError):
    """A fixable local readiness blocker found BEFORE the expensive phases.

    ``code`` is the durable machine-readable blocker (e.g.
    SOURCE_SUBMODULE_INCOMPLETE); ``command`` is the exact Git command for the
    confirmed repository root, offered to the user but NEVER executed
    automatically — it may touch private Git and alters the checkout.
    """

    def __init__(self, code: str, message: str, command: str = "") -> None:
        super().__init__(code, message)
        self.command = command
        self.fixable = True


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
# P1#4: the standalone "Проверить проект" verdict. Written by begin --check-only
# (a real prep AND control verification of the CURRENT dependencies) WITHOUT a
# run: no run.json/snapshot/checkpoint are created, so the later "Начать
# обновление" begin is a fresh run. The status handler exposes its `ok` so the
# UI can offer a distinct "ready → start" state instead of a single button that
# silently starts the migration.
PROJECT_CHECK_FILENAME = "project-check.json"
CHECKPOINT_DIR = "checkpoints"
SOURCE_DIR = "sources"
TRIAL_DIR = "trial"
TRIAL_CANDIDATE_FILENAME = "candidate.json"
TRIAL_WORKSPACE = "workspace"
BOOTSTRAP_DIR = "bootstrap"
REPAIR_REQUESTS_FILENAME = "repair-requests.json"
REPORTS_DIR = "reports"
AUDIT_DIR = "audit"
LOCK_FILENAME = "run.lock"
# P1 (v0.2.163 #1/#2): the durable handoff between a finished repair run and
# the NEXT migration. `archive-repair-run` writes it and keeps the repaired C0
# source snapshot (sources/C0) in place; `begin --adopt-repair-source` consumes
# it so the new migration continues from the VERIFIED FIXED bytes instead of
# re-capturing the still-red developer checkout. The archive itself is a
# recoverable transaction: an interrupted archive rolls back — it must never
# leave run.json archived while a checkpoint or the source snapshot is not.
REPAIR_HANDOFF_FILENAME = "repair-handoff.json"
REPAIR_ARCHIVE_DIR = "repair-archive"
REPAIR_ARCHIVE_TRANSACTION = "repair-archive-transaction.json"
# Machine-readable result envelope for the archive step (Electron parses it).
ARCHIVE_RESULT_EVENT = "ITERATIVE_ARCHIVE_RESULT_V1"


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


def _process_owner_dead(pid: int) -> bool:
    """Reclaim a killed writer without signalling a Windows process."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87  # nonexistent PID; denied is not dead
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value != 259
    finally:
        kernel.CloseHandle(handle)


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
            pid = int(payload.get("pid") or 0)
            if pid > 0:
                # A slow living verifier still owns the lock after two minutes.
                # Desktop restart must not archive files beneath an orphan child.
                return _process_owner_dead(pid)
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


def bootstrap_workspace_root(run_dir: Path) -> Path:
    """Version-neutral C0 repair workspace: a private materialization of the C0
    source snapshot (original, un-modified dependencies). The source/config
    repair agent works HERE, never on the developer checkout."""
    return _trial_dir(run_dir) / BOOTSTRAP_DIR


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
    # Fixed Git/file/workspace/alias inputs stay in the captured manifest and
    # proof identity, but must never be sent to the exact-version writer.
    # Mixed registry/fixed declarations still reach its conflict guard.
    fixed_only = {
        name for name in assignment
        if all(is_fixed_manifest_spec(str(manifest[section][name]))
               for section in DEPENDENCY_SECTIONS
               if isinstance(manifest.get(section), dict) and name in manifest[section])
    }
    return {name: spec for name, spec in sorted(assignment.items()) if name not in fixed_only}


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


def _runtime_env(config: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """Child env carrying the EXPLICIT project/CI Node runtime, or None when the
    user left the setting empty (empty never claims CI compatibility)."""
    runtime = config.get("runtime") or {}
    node_path = str(runtime.get("nodePath") or "").strip()
    if not node_path:
        return None
    from project_runtime import runtime_env_for_path

    return runtime_env_for_path(node_path)


def _runtime_contract_hash(config: Mapping[str, Any]) -> str:
    """Durable identity of the run's runtime contract ('' when unset)."""
    runtime = config.get("runtime") or {}
    return str(runtime.get("contractHash") or "")


def _assert_runtime_unchanged(config: Mapping[str, Any]) -> None:
    """D3.4: a step must not silently continue an old run under a CHANGED
    runtime. The requested Node is re-resolved against the current installation
    set; if it no longer maps to the same exact executable+version (the runtime
    was replaced, upgraded in place, or removed), the step refuses with
    RUNTIME_CHANGED. Old checkpoints stay on disk, verified under the OLD
    runtime — evidence is never re-labelled for the new environment."""
    runtime = config.get("runtime") or {}
    requested = str(runtime.get("requested") or "").strip()
    if not requested:
        return
    from project_runtime import resolve_requested_node

    resolution = resolve_requested_node(requested)
    if not resolution.found:
        raise InvalidInputError(
            f"RUNTIME_CHANGED: requested Node {requested!r} is no longer installed; "
            + resolution.unavailable_reason
        )
    same_version = resolution.effective_version == str(runtime.get("effectiveVersion") or "")
    same_node = os.path.normcase(str(resolution.node_path)) == os.path.normcase(str(runtime.get("nodePath") or ""))
    if not (same_version and same_node):
        raise InvalidInputError(
            f"RUNTIME_CHANGED: requested Node {requested!r} resolved to "
            f"{resolution.effective_version} ({resolution.node_path}) at begin, but now to "
            f"{resolution.effective_version} ({resolution.node_path}); restart with a new "
            "begin — the stored checkpoints stay proven under the previous runtime"
        )


def _manager_runtime_identity(project_dir: Path) -> Tuple[str, str]:
    """(manager, manager_version) for the project topology root; best-effort —
    an unresolvable manager yields ('', '') rather than failing begin."""
    try:
        manager, executable, _install = _manager_and_install(project_dir)
    except Exception:  # noqa: BLE001 - manager discovery must never block begin
        return "", ""
    if not executable:
        return manager or "", ""
    try:
        completed = _run(
            [executable, "--version"],
            project_dir,
            timeout_seconds=15,
            env={},
            base_env=os.environ,
            progress_label="iterative migration manager version probe",
        )
    except Exception:  # noqa: BLE001 - version probe is best-effort evidence
        return manager or "", ""
    if completed.returncode != 0:
        return manager or "", ""
    first = ((completed.stdout or "").strip().splitlines() or [""])[0].strip()
    return manager or "", first[:64]


def _run_install(project_dir: Path, *, timeout_seconds: int, progress_label: str, runtime_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess[str]:
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
    base_env = os.environ
    if runtime_env:
        base_env = {**base_env, **runtime_env}
    return _run(
        [executable, *install_args],
        project_dir,
        timeout_seconds=timeout_seconds,
        env=env,
        base_env=base_env,
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
        unbounded_preparation=True,
        project_check_timeout_seconds=(
            config.project_check_timeout_seconds if config.project_check_timeout_seconds is not None else 0
        ),
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

# P5: cheap LOCAL readiness checks run BEFORE the expensive registry discovery
# and source snapshot. They share the SAME verification code as the
# authoritative capture-time check (source_snapshot.incomplete_submodules), so
# the preflight and capture can never diverge; the capture-time check is kept.
def project_readiness_preflight(project_dir: Path) -> None:
    submodule_issues = incomplete_submodules(project_dir)
    if submodule_issues:
        state, path = submodule_issues[0]
        verb = "не инициализирован" if state == "-" else "в конфликтном состоянии"
        # Git reports paths relative to its root; the project may be nested.
        command = f'git -C "{project_dir}" submodule update --init --recursive -- ":(top){path}"'
        raise ProjectUnreadyError(
            "SOURCE_SUBMODULE_INCOMPLETE",
            f"Git submodule {path} {verb}. Подготовьте submodule и повторите проверку.",
            command=command,
        )


def cmd_begin(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    owner = args.owner or f"pid-{os.getpid()}"
    if (run_dir / RUN_FILENAME).exists():
        raise InvalidInputError(f"RUN_ALREADY_EXISTS: {run_dir}")
    if getattr(args, "adopt_repair_source", False):
        # P1 (v0.2.163 #1): the READY run adopts the terminal repair's source —
        # this is a NORMAL migration, not the repair track; combining the two is
        # a caller error, never a silent fallback.
        if getattr(args, "repair_only", False) or getattr(args, "check_only", False):
            raise InvalidInputError(
                "ADOPT_CONFLICT: --adopt-repair-source cannot be combined with --repair-only/--check-only"
            )
        handoff_path = run_dir / REPAIR_HANDOFF_FILENAME
        if not handoff_path.exists():
            raise InvalidInputError(
                f"REPAIR_HANDOFF_MISSING: no {REPAIR_HANDOFF_FILENAME} under {run_dir} — "
                "запустите archive-repair-run после завершённого ремонта"
            )
        handoff = _read_json(handoff_path)
        if str(handoff.get("terminal") or "") != "REPAIR_VERIFIED":
            raise InvalidInputError(
                "REPAIR_HANDOFF_NOT_VERIFIED: "
                "только подтверждённый ремонт (REPAIR_VERIFIED) может стать исходником новой миграции"
            )
        if handoff.get("adopted"):
            raise InvalidInputError(
                "REPAIR_HANDOFF_ALREADY_ADOPTED: исправленные исходники уже перенесены в миграцию"
            )
        if handoff.get("superseded"):
            raise InvalidInputError(
                "REPAIR_HANDOFF_SUPERSEDED: исправленные исходники устарели — "
                "проект был переснят заново; начните миграцию обычным begin"
            )
        if not handoff.get("source") or not handoff["source"].get("container") or not handoff["source"].get("key"):
            raise InvalidInputError("REPAIR_HANDOFF_SOURCE_INVALID: handoff не содержит снимок исправленных исходников")
        adopt = handoff
    else:
        adopt = None
    if getattr(args, "repair_only", False):
        return _cmd_begin_repair(run_dir, args, owner)
    if getattr(args, "check_only", False):
        return _cmd_begin_check_only(run_dir, args, owner)

    config = build_run_config(run_dir, args)

    lock = _RunLock(run_dir, owner)
    lock.acquire()
    try:
        return _begin_locked(run_dir, config, args, adopt=adopt)
    finally:
        lock.release()


def _cmd_begin_repair(run_dir: Path, args: argparse.Namespace, owner: str) -> int:
    # P2 (#1): the repair launch is INDEPENDENT of the update roadmap. It fixes
    # the CURRENT state: `discover=False` keeps targets empty and skips registry
    # discovery, so missing roadmap targets can never block a red project from
    # reaching the isolated repair. `_begin_locked` then captures C0 and runs the
    # current-state control:
    #   - a RED verdict opens the durable BOOTSTRAP_REPAIR run + bootstrap repair
    #     request (isolated checkout -> agent -> authoritative re-verification);
    #   - a GREEN verdict opens a READY run that the coordinator finishes as
    #     REPAIR_VERIFIED (control passed, no migration targets in this run).
    config = build_run_config(run_dir, args, discover=False)
    config = {**config, "repairOnly": True}
    lock = _RunLock(run_dir, owner)
    lock.acquire()
    try:
        return _begin_locked(run_dir, config, args)
    finally:
        lock.release()


# P1#4: a REAL standalone "Проверить проект". Runs the cheap readiness
# preflight (shared with capture) AND the control verification of the CURRENT
# dependencies — the same shared machinery begin's C0 uses — then records a
# durable, non-run verdict (project-check.json). No registry discovery, no
# run.json, no source snapshot, no checkpoint: the project is verified, NOT
# migrated. A local blocker (`SOURCE_SUBMODULE_INCOMPLETE`, ...) or a failing
# current-state control (`PROJECT_CONTROL_FAILED`) exits with the machine
# envelope so the panel offers "Повторить проверку" after the user fixes it.
def _cmd_begin_check_only(run_dir: Path, args: argparse.Namespace, owner: str) -> int:
    # The preflight (inside build_run_config) runs BEFORE any lock / mkdir, so a
    # blocked check leaves nothing behind (no run dir, no lock file).
    config = build_run_config(run_dir, args, discover=False)
    lock = _RunLock(run_dir, owner)
    lock.acquire()
    try:
        return _begin_check_only_locked(run_dir, args, config)
    finally:
        lock.release()


def _begin_check_only_locked(
    run_dir: Path, args: argparse.Namespace, config: Optional[Mapping[str, Any]] = None,
) -> int:
    if config is None:
        config = build_run_config(run_dir, args, discover=False)
    project_dir = Path(config["projectDir"])
    project_name = str(config["projectName"])
    initial_assignment = direct_dependency_assignment(project_dir)
    manifest_hash, lock_hash, _lock_path = manifest_and_lock_hashes(project_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    verify_config = verify_config_from(config["verifyConfig"], run_dir)
    verify_config = dataclasses.replace(
        verify_config, verification_purpose="baseline-control"
    )
    _emit_status(
        {
            "event": "begin.check",
            "runId": "",
            "project": project_name,
            "managedDependencies": len(initial_assignment),
        }
    )
    result = verify_assignment(
        project_dir,
        initial_assignment,
        config=verify_config,
        run_project_checks=True,
        progress_label="iterative migration project check",
        progress=lambda message: _emit_status({
            "event": "begin.check-progress", "runId": "", "project": project_name,
            "message": message,
        }),
        runtime_env=_runtime_env(config),
    )
    observed = dict(result.observed_resolved_versions or {})
    result_kind = str(getattr(result, "kind", "") or "")
    result_summary = str(getattr(result, "summary", "") or "")
    result_command = str(getattr(result, "command", "") or "")
    result_output = str(getattr(result, "output", "") or "")[-8192:]
    failing_commands = [
        {"command": failure.command, "exitCode": failure.exit_code}
        for failure in result.project_failures
    ]
    # P1#4: this is a PRE-RUN gate, not a C0 checkpoint. Three honest verdicts:
    #   passed       – the control verification genuinely PASSED → the project is
    #                  ready ("Проект проверен" is only written here).
    #   failed       – project-level failures (red current state) →
    #                  PROJECT_CONTROL_FAILED; the panel offers the repair-agent
    #                  path, never a false "готово".
    #   inconclusive – infrastructure / unknown / budget / preparation /
    #                  dependency: the check could NOT establish a definitive
    #                  verdict. It is kept INCOMPLETE (ok:false), NEVER written
    #                  as "Проект проверен", and the panel offers a retry — an
    #                  infra/unknown outcome must not look like a verified green
    #                  project just because no *project* command failed.
    if result.ok:
        check_ok = True
        control_status = "passed"
    elif result_kind == "project" or result.project_failures or is_structural_project_failure(result):
        check_ok = False
        control_status = "failed"
    else:
        check_ok = False
        control_status = "inconclusive"
    project_check: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "projectDir": str(project_dir),
        "projectName": project_name,
        "checkedAt": _now_iso(),
        "ok": check_ok,
        "managedDependencies": len(initial_assignment),
        "manifestHash": manifest_hash,
        "control": {
            "status": control_status,
            "kind": result_kind,
            "summary": result_summary,
            "command": result_command,
            "output": result_output,
            "failingCommands": failing_commands,
            "resolvedStateKey": result.resolved_state_key or "",
            "observedResolvedHash": result.observed_resolved_hash or observed_resolved_hash(observed),
        },
    }
    _write_json_atomic(run_dir / PROJECT_CHECK_FILENAME, project_check)
    _emit_status(
        {
            "event": "begin.check-done",
            "runId": "",
            "ok": check_ok,
            "managedDependencies": len(initial_assignment),
        }
    )
    if not check_ok:
        if result_output:
            _emit_status({"event": "begin.check-output", "runId": "", "project": project_name,
                          "message": result_output})
        failing = "; ".join(
            f"{failure.command} (exit {failure.exit_code})"
            for failure in result.project_failures[:3]
        ) or "проверки проекта не проходят"
        if control_status == "failed":
            raise ProjectUnreadyError(
                "PROJECT_CONTROL_FAILED",
                f"Контроль текущих зависимостей не пройден: {failing}. ",
                command="",
            )
        if "SOURCE_CAPTURE_UNSTABLE" in result_summary:
            raise ProjectUnreadyError(
                "SOURCE_CAPTURE_UNSTABLE",
                "Файлы проекта меняются во время проверки. Остановите сборку или watch/dev-сервер "
                "и повторите проверку. Проверенный результат сохранён; обновление не запускалось.",
                command="",
            )
        raise ProjectUnreadyError(
            "PROJECT_CHECK_INCONCLUSIVE",
            f"Проверка не дала определённого результата ({result.kind}): {result.summary or 'результат неизвестен'} — обновление не запущено. Повторите проверку.",
            command=result_command,
        )
    return 0


def build_run_config(run_dir: Path, args: argparse.Namespace, discover: bool = True) -> Dict[str, Any]:
    project_dir = Path(args.project_dir).expanduser().resolve()
    if not (project_dir / "package.json").exists():
        raise InvalidInputError(f"PROJECT_PACKAGE_JSON_MISSING: {project_dir}")
    # P5: fail fast on local readiness blockers BEFORE registry discovery, the
    # snapshot and the control verification (the real repro spent ~30s on
    # discovery only to fail on an uninitialized submodule).
    project_readiness_preflight(project_dir)
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

    # D3: an explicitly requested project/CI Node runtime is resolved to ONE
    # concrete installed executable+exact-version at begin. Empty means "not
    # set by the user" — no CI-compatibility claim, no automatic latest; a set
    # but unavailable version is ENVIRONMENT_UNAVAILABLE, never a PATH fallback.
    requested_node = (args.requested_node or "").strip()
    runtime: Dict[str, Any] = {}
    if requested_node:
        from project_runtime import resolve_requested_node

        resolution = resolve_requested_node(requested_node)
        if not resolution.found:
            raise InvalidInputError(
                f"ENVIRONMENT_UNAVAILABLE: requested Node {requested_node!r} is not installed; "
                + resolution.unavailable_reason
            )
        runtime = {
            "requested": requested_node,
            "effectiveVersion": resolution.effective_version,
            "nodePath": resolution.node_path,
            "npmPath": resolution.npm_path,
            "source": resolution.source,
            "contractHash": resolution.contract_hash(),
        }
        # D3.3: the full runtime contract also pins the package manager and the
        # executing platform. The durable contractHash covers every field of the
        # block, so a manager swap or a different OS invalidates the identity.
        runtime["packageManager"], runtime["packageManagerVersion"] = _manager_runtime_identity(project_dir)
        runtime["platform"] = sys.platform
        runtime["arch"] = platform.machine()
        runtime["hash"] = hashlib.sha256(
            _stable_json({k: v for k, v in runtime.items() if k != "hash"}).encode("utf-8")
        ).hexdigest()
        runtime["contractHash"] = runtime["hash"]

    # #2/P1.2: bounded target discovery when the caller brought NO roadmap
    # targets (no dashboard-state and no explicit targets file): derive them
    # from the direct dependency set's registry dist-tags.latest (yellow lag
    # policy), without the full generator/solver and without inventing a
    # version. When the run pins a concrete Node, the choice is engine-aware:
    # a latest known to exclude that Node triggers a bounded search for the
    # highest COMPATIBLE version instead of a silent deferral. A saved roadmap
    # still wins when provided.
    target_discovery: List[Dict[str, Any]] = []
    if not targets and discover:
        # D3.1/postfix: the discovery probes must run UNDER the same selected
        # runtime that install/verify will use (the resolved node's directory
        # prepended to PATH), never the ambient PATH — otherwise npm metadata
        # could come from a different toolchain/registry context than the one
        # the migration actually executes with.
        discovery_env: Optional[Dict[str, str]] = None
        if runtime.get("nodePath"):
            from project_runtime import runtime_env_for_path

            discovery_env = runtime_env_for_path(str(runtime["nodePath"]))
        # Desktop bring their own budget because a bounded registry discovery is
        # still the user's explicit choice (L2: no silent 97-package sequential
        # probe in a blocking IPC). ``getattr`` keeps legacy callers (tests, the
        # CLI without the new flags) on the single-threaded, unlimited default.
        discovery_parallelism = int(getattr(args, "discover_parallelism", None) or 1)
        discovery_timeout_s = max(0, int(getattr(args, "discover_timeout_s", None) or 0))
        discovery_max_packages = max(0, int(getattr(args, "discover_max_packages", None) or 0))

        def _discovery_progress(processed: int, total: int) -> None:
            _emit_status(
                {
                    "event": "begin.discovery-progress",
                    "processed": processed,
                    "total": total,
                }
            )

        targets, target_discovery = _discover_targets(
            project_dir,
            direct_dependency_assignment(project_dir),
            discovery_env,
            runtime.get("effectiveVersion"),
            parallelism=discovery_parallelism,
            wall_budget_s=discovery_timeout_s,
            max_packages=discovery_max_packages,
            progress=_discovery_progress,
        )

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
            args.run_budget_minutes if args.run_budget_minutes is not None else 0,
            0, 0, 24 * 60,
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
    dashboard_state = (args.dashboard_state or "").strip()
    dashboard_state_path = Path(dashboard_state).expanduser().resolve() if dashboard_state else None
    if dashboard_state_path is not None and not dashboard_state_path.exists():
        raise InvalidInputError(f"DASHBOARD_STATE_MISSING: {dashboard_state_path}")

    audit_policy = {
        "lagMonths": _clamp_int(
            args.lag_months if args.lag_months is not None else 12,
            12, 1, 60,
        ),
        "minLagOkPct": _clamp_int(
            args.min_lag_ok_pct if args.min_lag_ok_pct is not None else 80,
            80, 0, 100,
        ),
        "maxKnownHigh": _clamp_int(
            args.max_known_high if args.max_known_high is not None else 1,
            1, 0, 100,
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
        "auditPolicy": audit_policy,
        "dashboardState": str(dashboard_state_path) if dashboard_state_path is not None else "",
        "requestedNode": requested_node,
        "runtime": runtime,
        "targetDiscovery": target_discovery,
        "createdAt": _now_iso(),
        "toolBuildId": args.tool_build_id or "",
    }


def _begin_locked(
    run_dir: Path,
    config: Mapping[str, Any],
    args: argparse.Namespace,
    adopt: Optional[Mapping[str, Any]] = None,
) -> int:
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(run_dir / CONFIG_FILENAME, dict(config))
    run_id = (args.run_id or "").strip() or _new_id("iter")
    project_dir = Path(config["projectDir"])
    project_name = str(config["projectName"])
    if adopt is None:
        # P1 (v0.2.163 #1): for an ADOPTED run these project.path reads and the
        # "capture" signal are skipped — the source is the verified repaired
        # snapshot, never a fresh capture of the (still red) checkout.
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
    else:
        initial_assignment: Dict[str, str] = {}
        manifest_hash = lock_hash = lock_path = ""

    discovery = config.get("targetDiscovery") or []
    roadmap_targets = dict(config.get("targets") or {})
    discovered = [
        entry for entry in discovery
        if entry.get("status") in ("discovered", "discovered-compatible")
    ]
    unavailable = [entry for entry in discovery if entry.get("status") == "registry-unavailable"]
    incompatible_no_alt = [
        entry for entry in discovery
        if entry.get("status") in ("latest-incompatible-no-alternative", "no-newer-compatible")
    ]
    budget_skipped = [entry for entry in discovery if entry.get("status") == "discovery-budget-skipped"]
    if discovered or unavailable or incompatible_no_alt or budget_skipped:
        _emit_status(
            {
                "event": "begin.discovery",
                "runId": run_id,
                "source": "registry dist-tags.latest (engine-aware)",
                "discoveredTargets": sorted(str(entry.get("package") or "") for entry in discovered),
                "registryUnavailable": sorted(str(entry.get("package") or "") for entry in unavailable),
                "noCompatibleAlternative": sorted(str(entry.get("package") or "") for entry in incompatible_no_alt),
                "budgetSkipped": sorted(str(entry.get("package") or "") for entry in budget_skipped),
            }
        )

    # P-review: when the ONLY discovery outcomes are "registry data unavailable"
    # or "budget exhausted", no update was actually established — the run must
    # NOT be created as a fake success ("nothing to do"). No run, no state files
    # to delete manually: the user simply retries the search or configures the
    # scope. "genuinely up to date" (discovered==0, unavailable==0, budget==0)
    # still proceeds (that IS an updates-not-needed outcome).
    if not roadmap_targets and not discovered and (unavailable or budget_skipped):
        raise ProjectUnreadyError(
            "DISCOVERY_UNSETTLED",
            "Не удалось установить доступные обновления: данные registry недоступны или бюджет поиска исчерпан. "
            "Результат не засчитывается как завершённая миграция. Повторите поиск или настройте состав обновления.",
            command="",
        )

    if adopt is not None:
        # P1 (v0.2.163 #1): continue the migration from the VERIFIED REPAIRED
        # bytes, not from the still-red developer checkout.
        return _begin_adopted_locked(run_dir, config, args, run_id, adopt)

    # A fresh (non-adopt) begin after an archived repair re-captures C0 from the
    # developer checkout. The preserved repaired snapshot (sources/C0) lives on
    # only as a copy under the repair archive, so clearing the live leftover is
    # safe — it is the user's explicit choice to re-capture, never silent loss.
    # The handoff is marked SUPERSEDED so a later begin never adopts stale bytes.
    if adopt is None and (run_dir / REPAIR_HANDOFF_FILENAME).exists():
        stale = run_dir / SOURCE_DIR / "C0"
        if stale.exists():
            _force_remove_tree(stale)
        try:
            handoff = _read_json(run_dir / REPAIR_HANDOFF_FILENAME, required=False)
        except InvalidInputError:
            handoff = None
        if handoff and not handoff.get("adopted") and not handoff.get("superseded"):
            handoff["superseded"] = True
            handoff["supersededBy"] = "recapture"
            handoff["supersededAt"] = _now_iso()
            _write_json_atomic(run_dir / REPAIR_HANDOFF_FILENAME, handoff)

    snapshot_container = run_dir / SOURCE_DIR / "C0"
    snapshot = capture_durable_source_snapshot(
        project_dir, snapshot_container, timeout_seconds=0
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
        runtime_env=_runtime_env(config),
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
        "runtimeContractHash": _runtime_contract_hash(config),
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


def _begin_adopted_locked(
    run_dir: Path,
    config: Mapping[str, Any],
    args: argparse.Namespace,
    run_id: str,
    handoff: Mapping[str, Any],
) -> int:
    """P1 (v0.2.163 #1): a REAL migration carrying FORWARD the verified repaired
    source. The repaired C0 snapshot (sealed by verify-bootstrap, confirmed by
    finish's audit PASS → REPAIR_VERIFIED) becomes the new run's C0 source
    identity; the migration therefore continues from the FIXED bytes, never from
    a fresh capture of the still-red developer checkout.

    Control is repeated in the current environment; old repair proof and audit
    stay in the archive and never grant authority to the new runtime or policy.
    Before adoption the snapshot is RE-OPENED and key-validated (integrity), so a moved/deleted/corrupted
    container is a hard error, never a silent fallback to the red checkout.
    """
    project_name = str(config["projectName"])
    source_refs = handoff.get("source") or {}
    container = Path(str(source_refs.get("container") or ""))
    key = str(source_refs.get("key") or "")
    try:
        source = open_source_snapshot(container, expected_key=key, timeout_seconds=0)
    except SourceCaptureError as exc:
        raise InvalidInputError(
            f"REPAIR_SOURCE_INTEGRITY_FAILED: исправленный снимок недоступен/испорчен: {exc}"
        ) from exc

    full_assignment = dict(source_refs.get("fullAssignment") or {})
    manifest_hash = str(source_refs.get("manifestHash") or "")
    lock_hash = str(source_refs.get("lockfileHash") or "")
    project_relative = Path(str(source_refs.get("projectRelative") or "."))
    # Snapshot integrity is source evidence, not proof for a new runtime,
    # command set or policy. Always control the repaired bytes afresh.
    control_root = run_dir / f"adoption-control-{_new_id('trial')}"
    try:
        _materialize_trial_tree(source, control_root, 1800)
        control_project = control_root / project_relative
        control_project.resolve().relative_to(control_root.resolve())
        verify_config = dataclasses.replace(
            verify_config_from(config["verifyConfig"], run_dir), verification_purpose="baseline-control"
        )
        result = verify_assignment(
            control_project, full_assignment, config=verify_config,
            run_project_checks=True, runtime_env=_runtime_env(config),
            progress_label="adopt repaired source: fresh current-environment control",
        )
        if not result.ok:
            raise ProjectUnreadyError(
                "REPAIR_ADOPTION_CONTROL_FAILED" if result.hard_failure else "REPAIR_ADOPTION_INCONCLUSIVE",
                f"Исправленные исходники не подтверждены в текущем окружении: {result.kind}: {result.summary}",
                command="",
            )
    finally:
        _force_remove_tree(control_root)
    status = "VERIFIED"
    checkpoint_id = "C0"

    checkpoint: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "checkpointId": checkpoint_id,
        "parentCheckpointId": None,
        "seq": 0,
        "status": status,
        "projectName": project_name,
        "sourceSnapshotKey": source.key,
        "sourceSnapshotContainer": str(source.container),
        "sourceHead": source.git_head or str(source_refs.get("sourceHead") or ""),
        "projectRelative": str(project_relative),
        "manifestHash": manifest_hash,
        "lockfileHash": lock_hash,
        "resolvedStateKey": result.resolved_state_key or "",
        "observedResolvedHash": result.observed_resolved_hash or observed_resolved_hash(result.observed_resolved_versions or {}),
        "fullAssignment": full_assignment,
        "acceptedDelta": {"added": {}, "changed": {}, "removed": {}},
        "cohortId": None,
        "verification": {
            "status": "passed",
            "kind": result.kind,
            "commands": list(verify_config.commands),
            "failingCommands": [],
        },
        "proofRefs": {
            "resolvedStateKey": result.resolved_state_key or "",
            "preparationProofKey": result.preparation_proof_key or "",
        },
        "runtimeContractHash": _runtime_contract_hash(config),
        "audit": {
            "status": "UNKNOWN",
            "evidenceRef": "",
        },
        "createdAt": _now_iso(),
        "updatedAt": _now_iso(),
        "toolBuildId": config.get("toolBuildId") or "",
        "adoptedFromRepairRunId": str(handoff.get("repairRunId") or ""),
        "adoptedRepairArchiveDir": str(handoff.get("evidence", {}).get("archiveDir") or ""),
    }
    save_checkpoint(run_dir, checkpoint)

    run: Dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "workspaceId": str(config.get("workspaceId") or ""),
        "projectId": str(config.get("projectId") or ""),
        "generation": 1,
        "targetPolicyHash": str(config["policyHash"]),
        "initialSnapshotKey": source.key,
        "activeCheckpointId": "C0",
        "activeCandidateId": None,
        "phase": "READY",
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
        "adoptedFromRepairRunId": str(handoff.get("repairRunId") or ""),
        "adoptedRepairArchiveDir": str(handoff.get("evidence", {}).get("archiveDir") or ""),
        "adoptedAt": _now_iso(),
    }
    save_run(run_dir, run)

    # Mark the handoff adopted ONLY after the run is durable: a crash in between
    # leaves RUN_ALREADY_EXISTS instead of silently re-capturing the red source.
    handoff_marked = dict(handoff)
    handoff_marked["adopted"] = True
    handoff_marked["adoptedAt"] = _now_iso()
    _write_json_atomic(run_dir / REPAIR_HANDOFF_FILENAME, handoff_marked)

    _emit_status(
        {
            "event": "begin.adopted",
            "runId": run_id,
            "phase": "READY",
            "repairedRunId": str(handoff.get("repairRunId") or ""),
            "sourceSnapshotKey": source.key,
            "managedDependencies": len(full_assignment),
        }
    )
    _emit_status(
        {
            "event": "begin.done",
            "runId": run_id,
            "checkpointId": checkpoint_id,
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
    if minutes <= 0:
        return ""
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


# ---------------------------------------------------------------------------
# archive-repair-run: transactional archive + the durable repair handoff
# ---------------------------------------------------------------------------

# P1 (v0.2.163 #2): a FINISHED repair run must never block a real migration,
# and freeing its slot must never be half-done. Every artifact except the
# repaired C0 source snapshot (the fixed bytes the next run adopts) and the
# lightweight journal moves into repair-archive/<runId>-<timestamp>/. On ANY
# failure the already-moved entries are renamed back so the run stays exactly
# as it was — an interrupted archive is recoverable, never corrupted.
def _archive_metadata_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(path.name + f".restore-{os.getpid()}")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def _recover_repair_archive(run_dir: Path) -> None:
    """Called under the run lock. A planned move is recoverable even if killed
    between rename and recording completion; publications roll back together."""
    journal_path = run_dir / REPAIR_ARCHIVE_TRANSACTION
    if not journal_path.exists():
        return
    journal = _read_json(journal_path)
    archive_relative = Path(str(journal["archiveRelative"]))
    if (not archive_relative.parts or archive_relative.is_absolute()
            or ".." in archive_relative.parts or archive_relative.parts[0] != REPAIR_ARCHIVE_DIR):
        raise InvalidInputError("REPAIR_ARCHIVE_JOURNAL_INVALID")
    archive_root = run_dir / archive_relative
    if journal.get("phase") == "committed":
        journal_path.unlink()
        return
    for name in reversed(journal["entries"]):
        if Path(name).name != name or name in (".", ".."):
            raise InvalidInputError("REPAIR_ARCHIVE_JOURNAL_INVALID")
        source, target = run_dir / name, archive_root / name
        if target.exists() or target.is_symlink():
            if source.exists() or source.is_symlink():
                raise InvalidInputError(f"REPAIR_ARCHIVE_RECOVERY_CONFLICT: {name}")
            os.rename(target, source)
    for name in (REPAIR_HANDOFF_FILENAME, PROJECT_CHECK_FILENAME):
        previous = journal["metadata"][name]
        path = run_dir / name
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            _archive_metadata_bytes(path, bytes.fromhex(previous))
    journal_path.unlink()


def _archive_repair_run_locked(
    run_dir: Path, run: Mapping[str, Any]
) -> Dict[str, Any]:
    _recover_repair_archive(run_dir)
    config = load_config(run_dir)
    c0 = load_checkpoint(run_dir, "C0")
    archive_root = run_dir / REPAIR_ARCHIVE_DIR / _new_id("archive")
    archive_root.mkdir(parents=True, exist_ok=False)
    skip = {REPAIR_ARCHIVE_DIR, SOURCE_DIR, REPAIR_HANDOFF_FILENAME,
            PROJECT_CHECK_FILENAME, REPAIR_ARCHIVE_TRANSACTION, LOCK_FILENAME,
            "attempt.json", "attempt.log"}
    entries = [entry.name for entry in sorted(run_dir.iterdir(), key=lambda p: p.name)
               if entry.name not in skip and (entry.is_dir() or entry.is_file())]
    journal = {
        "phase": "moving",
        "archiveRelative": str(archive_root.relative_to(run_dir)),
        "entries": entries,
        "metadata": {name: (run_dir / name).read_bytes().hex() if (run_dir / name).exists() else None
                     for name in (REPAIR_HANDOFF_FILENAME, PROJECT_CHECK_FILENAME)},
    }
    journal_path = run_dir / REPAIR_ARCHIVE_TRANSACTION
    _write_json_atomic(journal_path, journal)
    try:
        for name in entries:
            os.rename(run_dir / name, archive_root / name)
        live_snapshot = run_dir / SOURCE_DIR / "C0"
        if live_snapshot.is_dir():
            shutil.copytree(live_snapshot, archive_root / SOURCE_DIR / "C0", symlinks=True)
        _verify_repair_archive(run_dir, archive_root, c0)
        handoff = _build_repair_handoff(run, config, c0, archive_root, run_dir)
        _write_json_atomic(run_dir / REPAIR_HANDOFF_FILENAME, handoff)
        _write_project_check_ready(run_dir, config, handoff)
        _write_json_atomic(journal_path, {**journal, "phase": "committed"})
    except Exception as exc:
        try:
            _recover_repair_archive(run_dir)
        except Exception as recovery_error:
            raise InvalidInputError(f"REPAIR_ARCHIVE_RECOVERY_REQUIRED: {recovery_error}") from exc
        raise InvalidInputError(f"REPAIR_ARCHIVE_FAILED_ROLLED_BACK: {type(exc).__name__}: {exc}") from exc
    # Once committed, interruption during cleanup must never undo publications.
    journal_path.unlink(missing_ok=True)
    return {"archived": True, "runId": str(run.get("runId") or ""),
            "archiveDir": str(archive_root), "handoffPath": str(run_dir / REPAIR_HANDOFF_FILENAME)}


def _verify_repair_archive(run_dir: Path, archive_root: Path, c0: Mapping[str, Any]) -> None:
    """Cheap integrity gate AFTER the move, BEFORE any handoff/verdict write:
    the slot is proven free (no run.json in the run dir), the move landed
    (run.json + config under the archive) and the repaired source snapshot
    stayed reachable under the key the C0 checkpoint expects."""
    if (run_dir / RUN_FILENAME).exists():
        raise InvalidInputError("REPAIR_ARCHIVE_RUN_STILL_PRESENT")
    if not (archive_root / RUN_FILENAME).exists():
        raise InvalidInputError("REPAIR_ARCHIVE_RUN_MISSING")
    if not (archive_root / CONFIG_FILENAME).exists():
        raise InvalidInputError("REPAIR_ARCHIVE_CONFIG_MISSING")
    manifest_path = run_dir / SOURCE_DIR / "C0" / "manifest.json"
    if not manifest_path.exists():
        raise InvalidInputError("REPAIR_ARCHIVE_SOURCE_SNAPSHOT_MISSING")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidInputError(f"REPAIR_ARCHIVE_SOURCE_MANIFEST_INVALID: {exc}") from exc
    observed_key = str(manifest.get("sourceSnapshotKey") or "")
    expected_key = str(c0.get("sourceSnapshotKey") or "")
    if observed_key != expected_key:
        raise InvalidInputError(
            "REPAIR_ARCHIVE_SOURCE_KEY_MISMATCH: "
            f"expected={expected_key}; observed={observed_key}"
        )


def _build_repair_handoff(
    run: Mapping[str, Any],
    config: Mapping[str, Any],
    c0: Mapping[str, Any],
    archive_root: Path,
    run_dir: Path,
) -> Dict[str, Any]:
    """The single durable record the NEXT migration reads to continue from the
    verified repaired bytes. `source` points at the fixed C0 snapshot that stays
    at run_dir/sources/C0; `evidence` keeps WORKING references into the archive
    so repair proof remains reachable after the slot is freed."""
    terminal_outcome = run.get("terminalOutcome") or {}
    verification = c0.get("verification") or {}
    audit = c0.get("audit") or {}
    source_head = str(c0.get("sourceHead") or "")
    return {
        "schemaVersion": 1,
        "repairRunId": str(run.get("runId") or ""),
        "projectName": str(config.get("projectName") or ""),
        "projectDir": str(config.get("projectDir") or ""),
        "workspaceId": str(config.get("workspaceId") or ""),
        "terminal": "REPAIR_VERIFIED",
        "repairedAt": str(terminal_outcome.get("finishedAt") or _now_iso()),
        "source": {
            "container": str(run_dir / SOURCE_DIR / "C0"),
            "key": str(c0.get("sourceSnapshotKey") or ""),
            "projectRelative": str(c0.get("projectRelative") or "."),
            "sourceHead": source_head,
            "manifestHash": str(c0.get("manifestHash") or ""),
            "lockfileHash": str(c0.get("lockfileHash") or ""),
            "resolvedStateKey": str(c0.get("resolvedStateKey") or ""),
            "observedResolvedHash": str(c0.get("observedResolvedHash") or ""),
            "fullAssignment": dict(c0.get("fullAssignment") or {}),
        },
        "verifiedC0": {
            "status": str(c0.get("status") or ""),
            "verificationStatus": str(verification.get("status") or ""),
            "kind": str(verification.get("kind") or ""),
            "commands": list(verification.get("commands") or []),
            "failingCommands": list(verification.get("failingCommands") or []),
        },
        "audit": {
            "status": str(audit.get("status") or ""),
            "evidenceRef": str(audit.get("evidenceRef") or ""),
        },
        "evidence": {
            "archiveDir": str(archive_root),
            "sourceSnapshotCopy": str(archive_root / SOURCE_DIR / "C0"),
            "repairRequestsPath": str(archive_root / REPAIR_REQUESTS_FILENAME),
            "reportsDir": str(archive_root / REPORTS_DIR),
        },
        "adopted": False,
    }


def _write_project_check_ready(run_dir: Path, config: Mapping[str, Any], handoff: Mapping[str, Any]) -> None:
    """The durable 'project ready' verdict the panel shows after a finished
    repair — with WORKING references to the repair evidence, so the handoff and
    the archived artifacts never point at a path that disappeared."""
    _write_json_atomic(
        run_dir / PROJECT_CHECK_FILENAME,
        {
            "schemaVersion": 1,
            "projectDir": str(config.get("projectDir") or ""),
            "projectName": str(config.get("projectName") or ""),
            "checkedAt": _now_iso(),
            "ok": True,
            "managedDependencies": 0,
            "control": {
                "status": "passed",
                "kind": "repair",
                "summary": "Исходное состояние проекта исправлено и подтверждено (ремонт).",
            },
            "repair": {
                "handoffPath": str(run_dir / REPAIR_HANDOFF_FILENAME),
                "archiveDir": str(handoff.get("evidence", {}).get("archiveDir") or ""),
                "repairRunId": str(handoff.get("repairRunId") or ""),
            },
        },
    )


def cmd_archive_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}")
    lock.acquire()
    try:
        if getattr(args, 'recover_only', False):
            recover_restart_archive(run_dir, _write_json_atomic)
            result = {'archived': False}
        else:
            result = archive_for_restart(run_dir, LOCK_FILENAME, _write_json_atomic)
        print(f"ITERATIVE_ARCHIVE_RESULT_V1 {json.dumps(result)}")
        return 0
    except RestartArchiveError as exc:
        raise InvalidInputError(str(exc)) from exc
    finally:
        lock.release()


def cmd_archive_repair_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / RUN_FILENAME).exists():
        _emit_archive_result({"archived": False, "reason": "NO_RUN"})
        return 0
    run = load_run(run_dir)
    terminal_outcome = run.get("terminalOutcome") or {}
    repair_done = run.get("phase") == "TERMINAL" and (
        terminal_outcome.get("outcome") == "REPAIR_VERIFIED"
        or run.get("terminal") == "REPAIR_VERIFIED"
    )
    if not repair_done:
        _emit_archive_result(
            {"archived": False, "reason": "NOT_REPAIR_VERIFIED", "phase": str(run.get("phase") or "")}
        )
        return 0
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}")
    lock.acquire()
    try:
        result = _archive_repair_run_locked(run_dir, run)
        _emit_archive_result(result)
        return 0
    finally:
        lock.release()


def _emit_archive_result(result: Mapping[str, Any]) -> None:
    print(
        f"{ARCHIVE_RESULT_EVENT} "
        f"{json.dumps(dict(result), ensure_ascii=False, separators=(',', ':'))}"
    )
    sys_stdout_flush()


def cmd_bootstrap_materialize(args: argparse.Namespace) -> int:
    """Materialize the version-neutral C0 repair workspace into an isolated
    trial. The bootstrap repair agent works HERE, never on the developer
    checkout; verify-bootstrap then re-runs the control on the repaired bytes
    and captures a NEW C0 source snapshot."""
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}", stale_seconds=300)
    lock.acquire()
    try:
        return _bootstrap_materialize_locked(run_dir, run, config, args)
    finally:
        lock.release()


def _bootstrap_materialize_locked(
    run_dir: Path, run: Mapping[str, Any], config: Mapping[str, Any], args: argparse.Namespace
) -> int:
    if run.get("phase") != "BOOTSTRAP_REPAIR":
        raise InvalidInputError(f"PHASE_NOT_BOOTSTRAP: {run.get('phase')}")
    if not run_budget_ok(config, run):
        raise BudgetExceededError("RUN_DEADLINE_EXCEEDED")
    requests = read_repair_requests(run_dir)
    if not any(isinstance(req, dict) and req.get("bootstrap") for req in requests):
        raise InvalidInputError("BOOTSTRAP_REQUEST_MISSING")
    checkpoint = load_checkpoint(run_dir, "C0")
    if checkpoint.get("status") == "VERIFIED":
        raise InvalidInputError("C0_ALREADY_VERIFIED")

    _emit_status(
        {
            "event": "bootstrap-materialize.start",
            "runId": run["runId"],
            "checkpointId": "C0",
        }
    )
    snapshot = open_source_snapshot(
        Path(str(checkpoint["sourceSnapshotContainer"])),
        expected_key=str(checkpoint["sourceSnapshotKey"]),
        timeout_seconds=0,
    )
    workspace_root = bootstrap_workspace_root(run_dir)
    if workspace_root.exists():
        shutil.rmtree(workspace_root, ignore_errors=True)
    _materialize_trial_tree(snapshot, workspace_root, args.timeout_seconds)
    project_relative = Path(str(checkpoint.get("projectRelative") or "."))
    project_path = workspace_root / project_relative
    install_result = _run_install(
        project_path,
        timeout_seconds=args.timeout_seconds,
        progress_label="iterative migration bootstrap materialization",
        runtime_env=_runtime_env(config),
    )
    run = dict(run)
    run["bootstrapRefs"] = {
        "workspaceRoot": str(workspace_root),
        "projectRelative": str(project_relative),
        "installExitCode": install_result.returncode,
    }
    run["updatedAt"] = _now_iso()
    save_run(run_dir, run)
    _emit_status(
        {
            "event": "bootstrap-materialize.done",
            "runId": run["runId"],
            "checkpointId": "C0",
            "installExitCode": install_result.returncode,
            "outputTail": (install_result.stdout or install_result.stderr or "")[-2000:]
            if install_result.returncode != 0
            else "",
        }
    )
    return 0


def _force_remove_tree(path: Path) -> None:
    """Remove a private tree that may be write-protected.

    A sealed snapshot container is read-only, and `shutil.rmtree(ignore_errors=
    True)` would silently fail to remove it on Windows — exactly the failure
    `source_snapshot._force_rmtree` exists for, mirrored here via the imported
    `apply_tree_write_protection`.
    """
    try:
        apply_tree_write_protection(path, readonly=False)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _replace_c0_source_snapshot(
    run_dir: Path, project_path: Path, timeout_seconds: int = 0
) -> SourceSnapshot:
    """P1 (v0.2.163 #1): atomically replace the C0 source snapshot container
    with a fresh capture of `project_path` (the repaired bootstrap trial).

    The durable snapshots are immutable and `persist_source_snapshot` refuses an
    existing destination, so the old red C0 cannot be captured over. The new
    snapshot is staged + validated FIRST, then the old container is moved aside
    and the staged one swapped into `sources/C0` — on any failure the old
    container is restored, so checkpoint C0 never dangles.
    """
    c0_container = run_dir / SOURCE_DIR / "C0"
    stamp = f"{os.getpid()}-{int(time.time() * 1000)}"
    stage = run_dir / SOURCE_DIR / f"C0.stage-{stamp}"
    old = run_dir / SOURCE_DIR / f"C0.old-{stamp}"
    try:
        staged = capture_durable_source_snapshot(
            project_path, stage, timeout_seconds=timeout_seconds
        )
        if c0_container.is_dir():
            os.rename(str(c0_container), str(old))
        os.replace(str(stage), str(c0_container))
    except Exception:
        if not c0_container.exists() and old.is_dir():
            try:
                os.rename(str(old), str(c0_container))
            except OSError:
                pass
        if stage.exists():
            _force_remove_tree(stage)
        raise
    if old.is_dir():
        _force_remove_tree(old)
    # Re-open the FINAL location so the returned snapshot carries the durable
    # container path (the staged path is gone) and re-validates the key there.
    return open_source_snapshot(
        c0_container, expected_key=staged.key, timeout_seconds=timeout_seconds
    )


def cmd_verify_bootstrap(args: argparse.Namespace) -> int:
    """Re-run the C0 control verification on the REPAIRED bootstrap trial
    (version-neutral). On success the trial's repaired bytes become the NEW C0
    source snapshot: the source/config identity of checkpoint C0 is replaced by
    a fresh sealed snapshot so every later materialization uses repaired bytes."""
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
        refs = run.get("bootstrapRefs") or {}
        if not refs.get("workspaceRoot"):
            raise InvalidInputError(
                "BOOTSTRAP_TRIAL_MISSING: запустите bootstrap-materialize перед verify-bootstrap"
            )
        workspace_root = Path(str(refs["workspaceRoot"]))
        project_path = workspace_root / str(refs.get("projectRelative") or ".")
        if not (project_path / "package.json").exists():
            raise InvalidInputError(f"BOOTSTRAP_TRIAL_PROJECT_MISSING: {project_path}")
        checkpoint = load_checkpoint(run_dir, "C0")
        verify_config = verify_config_from(config["verifyConfig"], run_dir)
        verify_config = dataclasses.replace(
            verify_config, verification_purpose="baseline-control"
        )
        result = verify_assignment(
            project_path,
            checkpoint["fullAssignment"],
            config=verify_config,
            run_project_checks=True,
            progress_label="iterative migration bootstrap re-verification",
            runtime_env=_runtime_env(config),
        )
        if result.ok:
            # The repaired trial becomes the authoritative C0 source identity:
            # capture a NEW durable snapshot at the SAME checkpoint slot (C0) so
            # the source key/container point at the repaired bytes. The old
            # (red) C0 container is swapped out TRANSACTIONALLY — a failed
            # capture must never leave checkpoint C0 pointing at a removed
            # container (the verify-bootstrap success path was previously
            # unreachable: persist_source_snapshot refused the existing target).
            new_snapshot = _replace_c0_source_snapshot(
                run_dir, project_path, timeout_seconds=0
            )
            project_relative = resolve_project_relative(project_path, new_snapshot)
            manifest_hash, lock_hash, _lock = manifest_and_lock_hashes(project_path)
            checkpoint = dict(checkpoint)
            checkpoint["status"] = "VERIFIED"
            checkpoint["sourceSnapshotKey"] = new_snapshot.key
            checkpoint["sourceSnapshotContainer"] = str(new_snapshot.container)
            checkpoint["sourceHead"] = new_snapshot.git_head or ""
            checkpoint["projectRelative"] = str(project_relative)
            checkpoint["manifestHash"] = manifest_hash
            checkpoint["lockfileHash"] = lock_hash or ""
            checkpoint["resolvedStateKey"] = (
                result.resolved_state_key or checkpoint.get("resolvedStateKey", "")
            )
            checkpoint["observedResolvedHash"] = (
                result.observed_resolved_hash
                or observed_resolved_hash(result.observed_resolved_versions or {})
            )
            checkpoint["updatedAt"] = _now_iso()
            checkpoint["verification"] = {
                "status": "passed",
                "kind": result.kind,
                "commands": list(verify_config.commands),
                "failingCommands": [],
            }
            save_checkpoint(run_dir, checkpoint)
            run = dict(run)
            run["phase"] = "READY"
            run.pop("bootstrapRefs", None)
            run["updatedAt"] = _now_iso()
            save_run(run_dir, run)
            write_repair_requests(run_dir, [])
            _emit_status(
                {
                    "event": "verify-bootstrap.pass",
                    "runId": run["runId"],
                    "checkpointId": "C0",
                    "phase": "READY",
                    "newSourceSnapshotKey": new_snapshot.key,
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
    _assert_runtime_unchanged(config)
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

    # #6: runtime-aware candidate selection BEFORE the expensive materialize.
    # The run's chosen Node (runtime contract effectiveVersion) is the engine
    # the install/verification will execute under. A direct target whose
    # registry engines.node excludes it is deferred here — never materialized,
    # never converted into a false verification outcome. Abstention (no
    # metadata, missing manager, probe failure) is compatibility, never a
    # constraint. Transitive incompatibility stays install evidence.
    engine_deferrals: List[Dict[str, Any]] = []
    if actionable:
        node_version = _normalize_effective_node_version(
            (config.get("runtime") or {}).get("effectiveVersion") or ""
        )
        if node_version:
            engine_requested = {name: targets[name] for name in actionable}
            effective_targets, engine_deferrals = _engine_deferred_targets(
                Path(config.get("projectDir") or ""),
                incumbent,
                engine_requested,
                node_version,
                _runtime_env(config),
            )
            if engine_deferrals:
                ledger = dict(load_ledger(run_dir))
                _record_engine_deferrals(ledger, checkpoint_id, engine_deferrals)
                save_ledger(run_dir, ledger)
            desired = {
                name: effective_targets.get(name, version)
                for name, version in incumbent.items()
            }
            actionable = [
                name
                for name, version in sorted(desired.items())
                if name in effective_targets and effective_targets[name] != incumbent.get(name)
            ]
            targets = effective_targets

    # A verified npm ETARGET defers only the named target in this bounded run
    # and registry context. It is scheduling evidence, never a solver nogood;
    # source repairs/new checkpoints do not make a missing version appear.
    availability_deferred = unavailable_targets(load_ledger(run_dir), run, config)
    actionable = [name for name in actionable if name not in availability_deferred]
    desired = {name: incumbent[name] if name in availability_deferred else version
               for name, version in desired.items()}

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
                "engineDeferred": len(engine_deferrals),
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
    # Deferrals suppress repetition only for the exact assignment on the
    # current verified base. They remain separate from incompatibility blocks.
    blocked_fingerprints.update(
        str(entry["assignmentFingerprint"])
        for entry in ledger.get("deferrals", [])
        if entry.get("baseCheckpointId") == checkpoint_id
        and entry.get("assignmentFingerprint")
    )
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
    # D4.5/D4.6: separate, non-fatal diagnostics over the project manifest —
    # resolution-pin warnings and malformed Git dependency specs are surfaced
    # alongside the candidate, never converted into install constraints.
    dependency_diagnostics = _dependency_diagnostics(config.get("projectDir") or "")
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
        "dependencyDiagnostics": dependency_diagnostics,
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
            "dependencyDiagnostics": dependency_diagnostics,
            "engineDeferred": len(engine_deferrals),
        }
    )
    return 0


def plan_visible_groups(atomic_groups: Sequence[Sequence[str]]) -> Sequence[Sequence[str]]:
    return atomic_groups


def _is_git_dependency(spec: str) -> bool:
    lowered = spec.lower()
    return (
        "git+" in lowered
        or lowered.startswith("git://")
        or "git@" in lowered
        or lowered.startswith(("github:", "gitlab:", "bitbucket:"))
        or ".git" in lowered
    )


def _git_dependency_issue(name: str, spec: str) -> Dict[str, Any]:
    """Deterministic, local-only checks for a malformed Git dependency spec
    (D4.6). Returns {} when the form is plausibly parseable — this is a
    diagnostic, never an installer."""
    lowered = spec.lower()
    if any(ch in spec for ch in (" ", "\t", "\n", "\\", '"', "'")):
        return {"code": "GIT_URL_WHITESPACE", "detail": "spec contains whitespace or quote characters"}
    if "git@" in lowered and ":" not in spec:
        return {"code": "GIT_SSH_WITHOUT_COLON", "detail": "ssh form 'git@<host><path>' must separate host and path with ':'"}
    if "git+" in lowered and not any(
        lowered.startswith(prefix) for prefix in ("git+ssh://", "git+https://", "git+http://", "git+file://")
    ):
        return {"code": "GIT_PLUS_NO_SCHEME", "detail": "'git+' must be followed by ssh://, https://, http:// or file://"}
    for prefix in ("git+ssh://", "git+https://", "git+http://", "git://"):
        if lowered.startswith(prefix):
            rest = spec[len(prefix):].split("#", 1)[0]
            if not rest or rest.split("/", 1)[0] in {"", ":", "."}:
                return {"code": "GIT_URL_NO_HOST", "detail": f"{prefix} carries no host"}
    return {}


def _dependency_diagnostics(project_dir: Any) -> Dict[str, Any]:
    """D4.5/D4.6: NON-FATAL structured diagnostics over the project manifest.

    ``resolutionWarnings``: pins in `resolutions`/`overrides` that are not a
    version-looking spec or that pin a package with no direct consumer; they
    are surfaced separately and never become false install constraints.
    ``gitUrlWarnings``: deterministically malformed Git dependency specs.
    A missing/unreadable manifest yields ``manifestReadError``, never an
    exception, so planning cannot be blocked by diagnostics.
    """
    result: Dict[str, Any] = {
        "resolutionWarnings": [],
        "gitUrlWarnings": [],
        "manifestReadError": "",
    }
    manifest_path = Path(str(project_dir)) / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail planning
        result["manifestReadError"] = f"{exc}"
        return result

    pins: Dict[str, Any] = {}
    for field in ("resolutions", "overrides"):
        raw = manifest.get(field) or {}
        if isinstance(raw, dict):
            pins.update(raw)
    consumers: Dict[str, Any] = {}
    for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        raw = manifest.get(field) or {}
        if isinstance(raw, dict):
            consumers.update(raw)
    consumer_names = set(consumers)

    for pin_key, pin_spec in sorted(pins.items()):
        pin_key_str = str(pin_key)
        if pin_key_str.startswith("@"):
            package_part = pin_key_str.split("@", 2)[1] if pin_key_str.count("@") >= 2 else pin_key_str
        else:
            package_part = pin_key_str.rsplit("@", 1)[0] if pin_key_str.count("@") == 1 else pin_key_str
        if not any(ch.isdigit() for ch in str(pin_spec)):
            result["resolutionWarnings"].append({
                "pin": pin_key_str,
                "spec": str(pin_spec),
                "code": "PIN_SPEC_NOT_VERSION",
                "detail": "pin value does not look like a version spec",
            })
        if not any(
            name == package_part
            or (name.startswith(package_part + "@"))
            for name in consumer_names
        ):
            result["resolutionWarnings"].append({
                "pin": pin_key_str,
                "spec": str(pin_spec),
                "code": "PIN_WITHOUT_DIRECT_CONSUMER",
                "detail": "pin has no direct consumer in the manifest; it cannot be the source of a resolved version",
            })

    for name, spec in sorted(consumers.items()):
        spec_str = str(spec)
        if not _is_git_dependency(spec_str):
            continue
        issue = _git_dependency_issue(name, spec_str)
        if issue:
            result["gitUrlWarnings"].append({"package": name, "spec": spec_str, **issue})
    return result


# The wall-clock deadline shared by ALL registry probe threads of one
# `_discover_targets` phase. A module-level "active phase" value is deliberate:
# it is the mechanism that passes the discovery budget INTO every request, so
# an in-flight probe bounds its own subprocess to the time remaining until the
# deadline instead of running a full fixed timeout PAST it. 0.0 means "no
# bounded discovery phase active" (the probe keeps its default timeout). It is
# set for the duration of the probe phase only and restored in ``finally``.
_ACTIVE_DISCOVERY_DEADLINE: float = 0.0


def _probe_budget_timeout(default_seconds: int = 60) -> Optional[int]:
    """Subprocess timeout for ONE registry probe under the active discovery
    deadline.

    Returns ``None`` when the budget is already exhausted — the probe is
    SKIPPED without spawning anything (abstention, never a constraint). When a
    deadline is active the returned timeout is the smaller of ``default_seconds``
    and the wall time remaining until the deadline, clamped to at least 1s, so
    a parallel probe that starts near the deadline is killed AT the deadline
    instead of continuing a full default past it.
    """
    deadline = _ACTIVE_DISCOVERY_DEADLINE
    if deadline <= 0.0:
        return default_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return int(max(1, min(default_seconds, remaining)))


def _normalize_effective_node_version(value: Any) -> str:
    """x.y.z from an effective Node version string (v-prefix/build tolerated)."""
    text = str(value or "").strip().lstrip("vV").split("+", 1)[0]
    parts = text.split(".")
    if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
        return ".".join(parts[:3])
    return text


def _npm_engines_node(
    project_dir: Path,
    name: str,
    version: str,
    runtime_env: Optional[Dict[str, str]],
) -> str:
    """Best-effort registry ``engines.node`` of ``name@version``.

    Deliberately evidence-light and safe: a missing manager, a failed probe or
    an unparsable answer returns '' — an abstention that is treated as
    COMPATIBLE, never as a false incompatibility constraint. Only an actual
    engines.node range from the registry may defer a target.
    """
    manager = resolve_executable("npm")
    if not manager:
        return ""
    probe_timeout = _probe_budget_timeout()
    if probe_timeout is None:
        return ""
    try:
        completed = _run(
            [manager, "view", f"{name}@{version}", "engines.node", "--json"],
            project_dir,
            timeout_seconds=probe_timeout,
            env=runtime_env or {},
            base_env=os.environ,
            progress_label=f"engine metadata probe {name}@{version}",
        )
    except Exception:  # noqa: BLE001 - a probe failure is abstention, never a constraint
        return ""
    if completed.returncode != 0:
        return ""
    text = (completed.stdout or "").strip()
    if not text or text in ("{}", "null", "undefined"):
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    if isinstance(parsed, dict):
        parsed = parsed.get("node")
    return str(parsed or "").strip()[:256]


def _npm_latest_version(
    project_dir: Path,
    name: str,
    runtime_env: Optional[Dict[str, str]],
) -> str:
    """Exact registry ``dist-tags.latest`` of a package ('' = no evidence)."""
    manager = resolve_executable("npm")
    if not manager:
        return ""
    probe_timeout = _probe_budget_timeout()
    if probe_timeout is None:
        return ""
    try:
        completed = _run(
            [manager, "view", name, "dist-tags.latest", "--json"],
            project_dir,
            timeout_seconds=probe_timeout,
            env=runtime_env or {},
            base_env=os.environ,
            progress_label=f"target discovery {name}",
        )
    except Exception:  # noqa: BLE001 - a probe failure is abstention, never a constraint
        return ""
    if completed.returncode != 0:
        return ""
    text = (completed.stdout or "").strip()
    if not text or text in ("{}", "null", "undefined"):
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    if isinstance(parsed, dict):
        parsed = parsed.get("latest")
    return str(parsed or "").strip()[:128]


def _npm_versions(
    project_dir: Path,
    name: str,
    runtime_env: Optional[Dict[str, str]],
    top: int = 12,
) -> List[str]:
    """Registry version list of a package, newest-first, bounded ([] = none).

    Best-effort, evidence-light and safe: any probe failure returns an empty
    list — abstention, never a constraint. The bound keeps engine-affinity
    discovery cheap even for packages with thousands of published versions.
    """
    manager = resolve_executable("npm")
    if not manager:
        return []
    probe_timeout = _probe_budget_timeout()
    if probe_timeout is None:
        return []
    try:
        completed = _run(
            [manager, "view", name, "versions", "--json"],
            project_dir,
            timeout_seconds=probe_timeout,
            env=runtime_env or {},
            base_env=os.environ,
            progress_label=f"version discovery {name}",
        )
    except Exception:  # noqa: BLE001 - a probe failure is abstention, never a constraint
        return []
    if completed.returncode != 0:
        return []
    text = (completed.stdout or "").strip()
    if not text or text in ("{}", "null", "undefined"):
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    if not isinstance(parsed, list):
        parsed = [parsed] if isinstance(parsed, str) else []
    versions = [str(item).strip()[:128] for item in parsed if isinstance(item, str) and item.strip()]
    return sorted(versions, key=_version_sort_key, reverse=True)[:top]


def _version_sort_key(value: str) -> Tuple[int, ...]:
    """Best-effort descending version key (x.y.z, prerelease/beta tolerated)."""
    key: List[int] = []
    for part in str(value).lstrip("vV").split("."):
        try:
            key.append(int(part.split("-")[0]))
        except ValueError:
            key.append(0)
    return tuple(key)


def _exact_version(value: str) -> Optional["Version"]:
    """Strict npm-semver parse of an exact published version ('' on failure)."""
    if Version is None:
        return None
    text = str(value or "").strip().lstrip("vV")
    if not text:
        return None
    try:
        return Version(text)
    except ValueError:
        return None


def _declared_floor(spec: str) -> Optional["Version"]:
    """Lowest version the declared spec admits, or None when undecidable.

    The "never downgrade" guard of target discovery: a candidate may be
    proposed ONLY when it is provably strictly newer than this floor, so a
    migration can never silently downgrade a package below its declared range.
    Conservative by construction: an undecidable floor (upper-bound-only or
    negative spec) yields None, which refuses any alternative — keeping the
    current version instead of risking a downgrade. ``||`` takes the smallest
    clause floor; bare ``*``/``x`` admits every version, so its floor is 0.0.0.
    """
    if Version is None:
        return None
    text = str(spec or "").strip()
    if not text:
        return None
    if "||" in text:
        floors = [f for f in (_declared_floor(part) for part in text.split("||")) if f is not None]
        return min(floors) if floors else None
    if " - " in text:
        return _declared_floor(text.split(" - ", 1)[0].strip())
    candidate = text
    for op in ("^", "~", ">=", "<=", ">", "<", "=", "!"):
        if candidate.startswith(op):
            if op in ("<", "<=", "!"):
                return None  # upper-bound/negative-only ranges have no floor
            candidate = candidate[len(op):].strip()
            break
    if candidate in ("", "*", "x", "X"):
        if str(spec).strip() in ("*", "x", "X"):
            return Version("0.0.0")
        return None
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", candidate)
    if match:
        major, minor, patch = match.groups()
        return Version(".".join([major, minor or "0", patch or "0"]))
    return _exact_version(candidate)


def _strictly_newer(candidate: str, declared_spec: str) -> bool:
    """True ONLY when ``candidate`` is provably newer than the declared floor.

    An undecidable floor or an unparsable candidate refuses the proposal
    (returns False) — a discovery that cannot prove an upgrade is an upgrade
    must keep the current version rather than downgrade or invent anything.
    """
    floor = _declared_floor(declared_spec)
    if floor is None:
        return False
    parsed = _exact_version(candidate)
    if parsed is None:
        return False
    return parsed > floor


def _installed_version(project_dir: Path, package_name: str, declared_spec: Optional[str] = None) -> Optional[str]:
    """The exact version of a direct dependency RESOLVED by the canonical
    lockfile — the real installed version the never-downgrade guard compares a
    candidate against.

    Background: the declared spec in package.json is a RANGE (``^8.0.0``), not
    an installed version; the project is actually installed at whatever the
    lockfile resolved (say 8.5.0). Comparing a candidate with the range floor
    would accept 8.1.0 as an "upgrade" and silently downgrade the installed
    package. The lockfile is the reliable source of what is installed. For
    yarn.lock the DECLARED spec of the direct dependency is needed to select
    the direct resolution among several selectors of the same package name
    (see ``_yarn_lock_installed_version``). ``None`` means the installed
    version cannot be proven reliably (no canonical lockfile, unreadable file,
    ambiguous resolution) — the caller MUST abstain for the package instead of
    choosing against a guess.
    """
    lock = find_lockfile(project_dir)
    if lock is None or not lock.is_file():
        return None
    name = str(package_name)
    try:
        if lock.name in ("package-lock.json", "npm-shrinkwrap.json"):
            return _npm_lock_installed_version(lock, name)
        if lock.name == "yarn.lock":
            return _yarn_lock_installed_version(lock, name, declared_spec)
        if lock.name == "pnpm-lock.yaml":
            return _pnpm_lock_installed_version(lock, name)
    except Exception:  # noqa: BLE001 - a lockfile read failure is abstention
        return None
    return None


def _yarn_direct_selectors(package_name: str, declared_spec: Optional[str]) -> List[str]:
    """The yarn.lock block-key selector(s) that identify the DIRECT dependency.

    Yarn v1 keys a direct dependency by its verbatim declared spec
    (``sample@^8.0.0``), possibly routed through an explicit ``npm:``
    protocol. Empty when the declared spec is unknown — the caller then falls
    back to a unique resolution across every selector of the package.
    """
    if not declared_spec:
        return []
    name = str(package_name)
    spec = str(declared_spec).strip()
    raw = spec[len("npm:"):].strip() if spec.startswith("npm:") else spec
    return [f"{name}@{raw}", f"{name}@npm:{raw}"]


def _npm_lock_installed_version(lock_path: Path, package_name: str) -> Optional[str]:
    """package-lock.json / npm-shrinkwrap.json exact direct resolution."""
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    packages = data.get("packages")
    if isinstance(packages, dict):
        entry = packages.get(f"node_modules/{package_name}")
        if isinstance(entry, dict):
            version = entry.get("version")
            if isinstance(version, str) and version:
                return version
    dependencies = data.get("dependencies")
    if isinstance(dependencies, dict):
        entry = dependencies.get(package_name)
        if isinstance(entry, dict):
            version = entry.get("version")
            if isinstance(version, str) and version:
                return version
    return None


def _split_yarn_selectors(value: str) -> List[str]:
    """Split a yarn.lock block key (one or more selectors separated by commas,
    each optionall quoted) into individual selectors."""
    parts: List[str] = []
    buf: List[str] = []
    quote = ""
    for ch in str(value or ""):
        if ch in ('"', "'"):
            if not quote:
                quote = ch
            elif quote == ch:
                quote = ""
            else:
                buf.append(ch)
            continue
        if ch == "," and not quote:
            if "".join(buf).strip():
                parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def _yarn_selector_for(selector: str, package_name: str) -> bool:
    """True when a yarn selector names this package (``name@range``,
    ``@scope/name@range``, with or without an explicit ``npm:`` protocol)."""
    text = str(selector).strip().strip('"')
    return text == package_name or text.startswith(f"{package_name}@")


def _yarn_lock_installed_version(lock_path: Path, package_name: str, declared_spec: Optional[str] = None) -> Optional[str]:
    """yarn.lock (v1) exact direct resolution by selector-key matching.

    A block key joins one or more selectors; a DIRECT dependency resolves to
    one version. When the DIRECT spec is known (``declared_spec`` from
    package.json) the matching selector is ``<name>@<declared>`` — yarn v1 keys
    the direct dependency by its verbatim declared spec — and only blocks with
    that selector are counted, so a TRANSITIVE resolution of the same package
    name (e.g. ``sample@^7.0.0`` under a direct ``sample@^8.0.0``) can never
    shadow or merge with the direct one and silently unmount the project's
    installed version. If NO block carries the declared selector the DIRECT
    resolution is not proven and None is returned (abstention — a transitive
    version is never mistaken for the installed direct one). Several DIFFERENT
    resolutions for the same selector are ambiguous and yield None (abstention,
    never a guess). Without a declared spec (pure helper use) a single
    resolution across all selectors of the package is accepted.
    """
    try:
        lines = lock_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    name = str(package_name)
    selectors = _yarn_direct_selectors(name, declared_spec)
    blocks: List[Tuple[List[str], str]] = []
    keys: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not line.startswith(" "):
            # A block header: a line ending with ':' carries the selectors;
            # other top-level lines (e.g. "# yarn lockfile v1") start a keyless
            # region that can not record a version.
            keys = _split_yarn_selectors(stripped[:-1]) if stripped.endswith(":") else []
            continue
        match = re.match(r'\s*version\s+"([^"]+)"', line)
        if match and keys and any(_yarn_selector_for(key, name) for key in keys):
            blocks.append((list(keys), match.group(1)))
    if selectors:
        # Review re-check P1: a TRANSITIVE resolution of the same package name
        # (e.g. `sample@^7.0.0` -> 7.2.0) must never be mixed into the DIRECT
        # dependency's installed version (`sample@^8.0.0` -> 8.5.0): that
        # ambiguity made the whole project abstain and lose every target. Pick
        # only the block(s) whose selector names the DIRECT declared spec.
        direct_versions: set = set()
        for block_keys, version in blocks:
            if any(key in selectors for key in block_keys):
                direct_versions.add(version)
        if direct_versions:
            return direct_versions.pop() if len(direct_versions) == 1 else None
        # Review P1 (latest round): the DIRECT resolution is NOT proven when no
        # block carries the declared selector (e.g. the lockfile only has the
        # transitive `sample@^7.0.0` -> 7.2.0). Taking the transitive version as
        # the INSTALLED direct one would let discovery plan an "upgrade" on top
        # of a version the project never directly resolved — abstain instead.
        return None
    # No declared spec (pure helper call outside discovery): accept a single
    # resolution across every selector of the package, abstain on ambiguity.
    versions: set = set()
    for _block_keys, version in blocks:
        versions.add(version)
    return versions.pop() if len(versions) == 1 else None


def _pnpm_importer_direct_version(lines: Sequence[str], package_name: str) -> Optional[str]:
    """The resolved version of a DIRECT dependency from the pnpm importer.

    pnpm v9 (lockfileFormat 9.0) records the exact resolved version of every
    direct dependency in the root importer (see the pnpm lockfile spec)::

        importers:
          .:
            dependencies:
              sample:
                specifier: ^8.0.0
                version: 8.5.0

    The importer record IS the direct resolution (unique, no ambiguity with
    transitive copies), so it wins over the packages snapshot. A version with a
    peer-dependency suffix (``8.5.0(react@18.0.0)``) is reduced to the bare
    version. Workspace/link entries (no version, or a ``link:`` value) yield
    None — abstention, never a guess.
    """
    in_importers = False
    in_root = False
    in_deps = False
    pending: Optional[str] = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_importers = stripped == "importers:"
            in_root = False
            in_deps = False
            pending = None
            continue
        if not in_importers:
            continue
        if indent == 2:
            # Importer key: '.' is the root project itself; other keys are
            # workspace members and are NOT this project's direct dependencies.
            in_root = stripped.startswith(".") and not stripped.startswith("..")
            in_deps = False
            pending = None
            continue
        if not in_root:
            continue
        if indent == 4 and stripped in ("dependencies:", "devDependencies:", "optionalDependencies:"):
            in_deps = True
            pending = None
            continue
        if not in_deps:
            continue
        if indent == 6 and stripped.endswith(":"):
            pending = stripped[:-1]
            continue
        if indent == 8 and pending == str(package_name) and stripped.startswith("version:"):
            raw = stripped[len("version:"):].strip().strip('"').strip("'")
            match = re.match(r"^[^()]+", raw)
            return match.group(0).strip() if match and match.group(0).strip() else None
    return None


def _pnpm_lock_installed_version(lock_path: Path, package_name: str) -> Optional[str]:
    """pnpm-lock.yaml exact DIRECT resolution (best-effort textual parse).

    pnpm v9 (lockfileFormat 9.0) records the resolved version of every direct
    dependency in the importer section (``importers: ... .: dependencies:
    <name>: version: 8.5.0``) and keys the packages snapshot WITHOUT a leading
    slash (``sample@8.5.0``); v6-style lockfiles key it WITH one
    (``/sample@8.5.0``). The importer record is authoritative (the direct
    resolution), so it is read first; the packages snapshot is the fallback for
    older files and accepts a single resolution of the package name — any
    ambiguity yields None, never a guess.
    """
    try:
        lines = lock_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    importer_version = _pnpm_importer_direct_version(lines, package_name)
    if importer_version is not None:
        return importer_version
    pattern = re.compile(rf"^\s*/?({re.escape(str(package_name))})@([^:()]+)")
    versions: set = set()
    for line in lines:
        match = pattern.match(line.rstrip())
        if match:
            versions.add(match.group(2))
    return versions.pop() if len(versions) == 1 else None


def _newer_than_installed(candidate: str, installed: str) -> bool:
    """True ONLY when ``candidate`` is provably strictly newer than the EXACT
    installed version. An unparsable candidate or baseline refuses the
    proposal (False) — a discovery that cannot prove an upgrade is an upgrade
    keeps the current version rather than downgrading or inventing anything."""
    parsed = _exact_version(candidate)
    base = _exact_version(installed)
    return parsed is not None and base is not None and parsed > base


def _discover_one_target(
    project_dir: Path,
    name: str,
    declared: str,
    runtime_env: Optional[Dict[str, str]],
    node_version: Optional[str],
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Probe ONE direct dependency for a bounded target (see ``_discover_targets``).

    Returns ``(target_version_or_None, evidence_row)``. A target is produced
    only when the registry proves a STRICTLY NEWER version that is compatible
    with the run's chosen Node (or when no Node is pinned and the choice is
    engine-unchecked). Abstractions are evidence rows, never targets.
    """
    installed = _installed_version(project_dir, name, declared)
    if installed is None:
        return None, {
            "package": name, "declared": declared,
            "status": "installed-version-unknown",
            "reason": (
                f"the exact installed version of {name} cannot be read "
                "from the canonical lockfile; the never-downgrade guard "
                "abstains instead of choosing a target against an "
                "unverifiable baseline"
            ),
        }
    latest = _npm_latest_version(project_dir, name, runtime_env)
    if not latest:
        return None, {
            "package": name, "declared": declared, "installed": installed,
            "latest": "", "status": "registry-unavailable",
            "reason": (
                f"the registry metadata for {name} is unavailable; the upgrade "
                "cannot be assessed, so no lag is claimed"
            ),
        }
    if latest == installed:
        return None, {
            "package": name, "declared": declared, "installed": installed,
            "latest": latest, "status": "up-to-date",
        }
    # Review re-check P1: NEVER accept the registry `latest` until it is
    # provably STRICTLY newer than the INSTALLED version. This guard runs
    # BEFORE every direct acceptance of `latest` (both the unpinned-Node and
    # the pinned-Node/engine-compatible shortcuts): a dist-tag that lags the
    # installed version (installed 8.5.0, latest 8.1.0) would otherwise be
    # proposed as a target and roll the package back. When `latest` is not an
    # upgrade nothing is proposed — the bounded search below (which applies
    # the same guard per candidate) is the only remaining way a strictly
    # newer verified version can be chosen — and the unfulfilled intent is
    # recorded as no-newer-compatible instead of a false completion.
    if not _newer_than_installed(latest, installed):
        return None, {
            "package": name, "declared": declared, "installed": installed,
            "latest": latest, "status": "no-newer-compatible",
            "rejected": [
                {"version": latest, "nodeSpec": "", "status": "not-newer-than-current"}
            ],
            "reason": (
                f"registry latest {latest} is not strictly newer than the "
                f"INSTALLED {installed}; accepting it would roll the package "
                "back, so the current version is kept"
            ),
        }
    if not node_version:
        return latest, {
            "package": name, "declared": declared, "installed": installed,
            "latest": latest,
            "status": "discovered", "engineUnchecked": True,
        }
    from project_runtime import node_range_satisfied

    latest_spec = _npm_engines_node(project_dir, name, latest, runtime_env)
    if not latest_spec or node_range_satisfied(latest_spec, node_version):
        return latest, {
            "package": name, "declared": declared, "installed": installed,
            "latest": latest, "nodeVersion": node_version, "latestNodeSpec": latest_spec,
            "status": "discovered",
            **({"enginesUnknown": True} if not latest_spec else {}),
        }
    chosen: Optional[str] = None
    chosen_spec = ""
    rejected: List[Dict[str, Any]] = []
    for candidate in _npm_versions(project_dir, name, runtime_env, top=12):
        if candidate == latest:
            continue
        if candidate == installed or not _newer_than_installed(candidate, installed):
            # Postfix-review P1: never downgrade. A candidate at or below
            # the INSTALLED version (lockfile-exact) cannot be an upgrade,
            # whatever its engines — the declared range floor alone would
            # accept a lower-than-installed version (e.g. 8.1.0 under
            # ^8.0.0 with 8.5.0 installed) and roll the package back.
            rejected.append({"version": candidate, "nodeSpec": "", "status": "not-newer-than-current"})
            continue
        candidate_spec = _npm_engines_node(project_dir, name, candidate, runtime_env)
        if not candidate_spec:
            rejected.append({"version": candidate, "nodeSpec": "", "status": "engines-unknown"})
            continue
        if node_range_satisfied(candidate_spec, node_version):
            chosen = candidate
            chosen_spec = candidate_spec
            break
        rejected.append({"version": candidate, "nodeSpec": candidate_spec, "status": "engines-incompatible"})
    if chosen:
        return chosen, {
            "package": name, "declared": declared, "installed": installed,
            "latest": latest, "latestNodeSpec": latest_spec, "nodeVersion": node_version,
            "target": chosen, "targetNodeSpec": chosen_spec,
            "rejected": rejected, "status": "discovered-compatible",
        }
    # No strictly-newer verified-compatible alternative in the bounded
    # window: KEEP the installed version (no downgrade, no false plan entry)
    # and record the unfulfilled upgrade intent with an honest reason. The
    # deferred/blocked package stays visible in the discovery evidence, in
    # the begin.discovery event and in the task remainder (via the task
    # builder's no-newer-compatible intents); it is never an invented
    # completion and never silently dropped to "0 of 0" in the task.
    return None, {
        "package": name, "declared": declared, "installed": installed,
        "latest": latest, "latestNodeSpec": latest_spec, "nodeVersion": node_version,
        "rejected": rejected, "status": "no-newer-compatible",
        "reason": (
            f"latest {latest} requires node {latest_spec}, but the run's "
            f"chosen Node is {node_version}; no verified-compatible version "
            f"strictly newer than the INSTALLED {installed} exists within the "
            "bounded window — keeping the current version"
        ),
    }


def _discover_targets(
    project_dir: Path,
    current: Mapping[str, str],
    runtime_env: Optional[Dict[str, str]],
    node_version: Optional[str] = None,
    parallelism: int = 1,
    wall_budget_s: int = 0,
    max_packages: int = 0,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """#2/P1.2 bounded target discovery for begin when no roadmap targets exist.

    Yellow lag policy applied WITHOUT the legacy generator or solver: for every
    direct managed dependency, the registry ``dist-tags.latest`` is the target,
    unless the run pins a concrete Node and the registry KNOWS latest excludes
    it. In that case a bounded search over the newest published versions picks
    the highest VERIFIED-compatible version that is STRICTLY NEWER than the
    INSTALLED version (lockfile-exact, see ``_installed_version``) and that
    version becomes the target — the user gets an upgrade that actually
    installs under the chosen Node instead of a stale deferral. The declared
    spec alone is never the baseline: a range floor (``^8.0.0``) would accept
    8.1.0 as an "upgrade" while 8.5.0 is installed, silently downgrading the
    package. A version is never invented, and a package whose latest cannot be
    established is skipped as evidence. Downgrades are refused: a candidate may
    only be proposed when npm-semver proves it newer than the installed
    version, and the SAME guard runs BEFORE any direct acceptance of the
    registry ``latest`` — a dist-tag that lags the installed version (installed
    8.5.0, latest 8.1.0) is recorded as ``no-newer-compatible``, never proposed
    as a target. Discovery abstains entirely (``installed-version-unknown``)
    when the installed version cannot be read from the canonical lockfile.
    When no strictly-newer verified-compatible version exists in the bounded
    window, the CURRENT version is kept (the package is NOT added to the
    targets) and an honest ``no-newer-compatible`` evidence row records the
    unfulfilled upgrade intent with the reason — never a downgrade and never a
    false completion; the task builder surfaces that intent in the remainder.
    Unknown engines are an abstention, never a false incompatibility claim;
    without a pinned Node the choice is engine-unchecked.

    Discovery is BOUNDED so a Desktop click never blocks on many minutes of
    sequential registry probes: `parallelism` probes run concurrently,
    `wall_budget_s` caps the phase wall-clock time (>=1 enforces a deadline),
    and `max_packages` caps how many direct dependencies are probed at all.
    Packages cut off by the cap or the deadline are recorded as honest
    ``discovery-budget-skipped`` evidence — their upgrade is NOT assessed and
    NOT claimed, and the task builder keeps them in the remainder; a missing
    probe never becomes a proven lag, an invented version or zero findings.
    `progress(processed, total)` is called after each completed probe.
    """
    global _ACTIVE_DISCOVERY_DEADLINE
    targets: Dict[str, str] = {}
    evidence: List[Dict[str, Any]] = []
    names = sorted(current.items())
    if max_packages > 0 and len(names) > max_packages:
        for name, declared in names[max_packages:]:
            evidence.append(
                {
                    "package": name, "declared": declared,
                    "status": "discovery-budget-skipped",
                    "reason": _budget_skip_reason(name, f"discovery budget cap ({max_packages}) reached"),
                }
            )
        names = names[:max_packages]
    total = len(names)

    def hard_deadline() -> Optional[float]:
        if wall_budget_s <= 0:
            return None
        import time as _time

        return _time.monotonic() + wall_budget_s

    deadline = hard_deadline()
    processed = 0

    def record(target_version: Optional[str], name: str, row: Dict[str, Any]) -> None:
        nonlocal processed
        if target_version is not None:
            targets[name] = target_version
        evidence.append(row)
        processed += 1
        if progress is not None:
            progress(processed, total)

    # The wall budget is passed INTO every registry probe for the whole phase:
    # each request bounds its own subprocess to the time remaining until the
    # deadline (see `_probe_budget_timeout`), so probes that started before the
    # deadline are killed AT it instead of overrunning it by a full default — a
    # 1s budget finishes in ~1s, never in 1s + one full npm view. The deadline
    # is restored in `finally` so later phases (e.g. engine prechecks) keep
    # their own fixed timeouts.
    previous_discovery_deadline = _ACTIVE_DISCOVERY_DEADLINE
    if deadline is not None:
        _ACTIVE_DISCOVERY_DEADLINE = deadline
    try:
        if parallelism <= 1:
            for name, declared in names:
                if deadline is not None and _after_deadline(deadline):
                    evidence.append(
                        {
                            "package": name, "declared": declared,
                            "status": "discovery-budget-skipped",
                            "reason": _budget_skip_reason(name, "wall-clock budget exhausted"),
                        }
                    )
                    processed += 1
                    if progress is not None:
                        progress(processed, total)
                    continue
                target, row = _discover_one_target(project_dir, name, declared, runtime_env, node_version)
                record(target, name, row)
            _order_discovery(targets, evidence)
            return targets, evidence

        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        lock = threading.Lock()

        def probe(item: Tuple[str, str]) -> Tuple[Optional[str], str, Dict[str, Any]]:
            name, declared = item
            if deadline is not None and _after_deadline(deadline):
                return None, name, {
                    "package": name, "declared": declared,
                    "status": "discovery-budget-skipped",
                    "reason": _budget_skip_reason(name, "wall-clock budget exhausted"),
                }
            try:
                target, row = _discover_one_target(project_dir, name, declared, runtime_env, node_version)
                return target, name, row
            except Exception:  # noqa: BLE001 - a probe failure is an abstention, never a constraint
                return None, name, {
                    "package": name, "declared": declared,
                    "status": "discovery-budget-skipped",
                    "reason": _budget_skip_reason(name, "probe failed unexpectedly"),
                }

        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = {pool.submit(probe, item): item for item in names}
            for future in as_completed(futures):
                target, name, row = future.result()
                if deadline is not None and _after_deadline(deadline):
                    # Review re-check P1.2: a result that only ARRIVED after the
                    # deadline is not evidence the run stayed within its budget,
                    # so it is never accepted as a target or a finished probe —
                    # it becomes an honest budget-skipped row instead.
                    _, declared = futures[future]
                    target = None
                    row = {
                        "package": name, "declared": declared,
                        "status": "discovery-budget-skipped",
                        "reason": _budget_skip_reason(name, "wall-clock budget exhausted before the probe finished"),
                    }
                with lock:
                    record(target, name, row)
        _order_discovery(targets, evidence)
        return targets, evidence
    finally:
        _ACTIVE_DISCOVERY_DEADLINE = previous_discovery_deadline


def _budget_skip_reason(name: str, why: str) -> str:
    return (
        f"{why}; {name} was not probed, so its upgrade is not assessed "
        "and no lag is claimed"
    )


def _after_deadline(deadline: float) -> bool:
    import time as _time

    return _time.monotonic() >= deadline


def _order_discovery(targets: Dict[str, str], evidence: List[Dict[str, Any]]) -> None:
    """Deterministic discovery output: stable ordering by package name so a
    bounded/parallel discovery produces the SAME evidence rows as a sequential
    one (reproducibility, never run-order)."""
    ordered = {name: targets[name] for name in sorted(targets)}
    targets.clear()
    targets.update(ordered)
    evidence.sort(key=lambda e: (str(e.get("package") or ""), str(e.get("status") or "")))


def _engine_deferred_targets(
    project_dir: Path,
    incumbent: Mapping[str, str],
    requested: Mapping[str, str],
    node_version: str,
    runtime_env: Optional[Dict[str, str]],
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """#6 runtime-aware candidate selection BEFORE the expensive materialization.

    For every requested target whose registry ``engines.node`` excludes the
    run's chosen Node, the target is excluded from this plan's assignment and
    returned as a deferral entry — an incompatible install is never
    materialized and never becomes a false verification result. Transitive
    incompatibility (introduced by a dependency, not declared on the direct
    target) is out of scope here and remains a matter for install evidence.
    Returns ``(effective_targets, deferrals)``.
    """
    from project_runtime import node_range_satisfied

    effective: Dict[str, str] = {}
    deferrals: List[Dict[str, Any]] = []
    for name, version in sorted(requested.items()):
        spec = _npm_engines_node(project_dir, name, version, runtime_env)
        if not spec or node_range_satisfied(spec, node_version):
            effective[name] = version
            continue
        deferrals.append(
            {
                "package": name,
                "requestedVersion": version,
                "nodeVersion": node_version,
                "nodeSpec": spec,
                "kind": "ENGINES_INCOMPATIBLE",
                "reason": (
                    f"{name}@{version} requires node {spec}, but the run's chosen "
                    f"Node is {node_version}; target deferred before materialization"
                ),
            }
        )
    return effective, deferrals


def _record_engine_deferrals(
    ledger: Dict[str, Any], checkpoint_id: str, deferrals: Sequence[Mapping[str, Any]]
) -> None:
    """Persist engine-incompatible deferrals as deterministic ledger input.

    A deferred target stays in the policy remainder/denominator (the config
    targets are untouched); this is visibility evidence, never acceptance.
    """
    now = _now_iso()
    entries = list(ledger.get("deferrals", []))
    for entry in deferrals:
        entries.append(
            {
                "baseCheckpointId": checkpoint_id,
                "candidateId": "",
                "cohortId": None,
                "package": str(entry.get("package") or ""),
                "requestedVersion": str(entry.get("requestedVersion") or ""),
                "reason": str(entry.get("reason") or "engines.node incompatible with chosen Node"),
                "kind": str(entry.get("kind") or "ENGINES_INCOMPATIBLE"),
                "createdAt": now,
            }
        )
    ledger["deferrals"] = entries


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
    _assert_runtime_unchanged(config)
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
        timeout_seconds=0,
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
        runtime_env=_runtime_env(config),
    )
    if install_result.returncode != 0:
        kind = _classify_materialization_failure(install_result.stdout or "")
        ledger = dict(load_ledger(run_dir))
        if record_unavailable_targets(ledger, run, config, candidate,
                                      (install_result.stdout or "") + "\n" + (install_result.stderr or "")):
            save_ledger(run_dir, ledger)
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

    trial_snapshot = capture_source_snapshot(project_path, timeout_seconds=0)
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
        timeout_seconds=0,
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
    _assert_runtime_unchanged(config)
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
    runtime_env = _runtime_env(config)
    base_env: Dict[str, str] = os.environ
    if runtime_env:
        base_env = {**base_env, **runtime_env}
    for command in commands:
        env: Dict[str, str] = {
            "CI": "1",
            "npm_config_ignore_scripts": "true",
        }
        result = _run(
            _command_argv(command, base_env),
            project_path,
            timeout_seconds=_clamp_int(args.timeout_seconds, 1200, 120, 4 * 3600),
            env=env,
            base_env=base_env,
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


def _command_argv(command: str, environment: Optional[Mapping[str, str]] = None) -> List[str]:
    return project_check_command_argv(command, environment)


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
    _assert_runtime_unchanged(config)
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
        runtime_env=_runtime_env(config),
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
        project_path, run_dir / SOURCE_DIR / checkpoint_id, timeout_seconds=0
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
        "runtimeContractHash": _runtime_contract_hash(config),
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
    """Defer this exact attempt; a different verified base may permit a retry."""
    # This is scheduling evidence, never a learned incompatibility. Without
    # the exact fingerprint, INCONCLUSIVE starves later independent cohorts by
    # proposing the same assignment forever on the same verified checkpoint.
    ledger["deferrals"] = list(ledger.get("deferrals", [])) + [
        {
            "baseCheckpointId": candidate["baseCheckpointId"],
            "candidateId": candidate["candidateId"],
            "cohortId": candidate.get("cohortId"),
            "assignmentFingerprint": assignment_fingerprint(candidate["fullAssignment"]),
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
            # D2.4: the UI needs the requested-vs-effective runtime contract and
            # the CI evidence source to render, not to decide. The durable
            # runtime block lives in run-config.json; this is its view.
            "requestedNode": config.get("requestedNode") or "",
            "runtime": config.get("runtime") or {},
        },
    }
    status_path = run_dir / "status.json"
    _write_json_atomic(status_path, payload)
    _emit_status({"event": "status.ready", "runId": run["runId"], "statusPath": str(status_path)})
    print(_stable_json(payload))
    return 0


def _policy_satisfied(config: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> bool:
    targets = {str(k): str(v) for k, v in (config.get("targets") or {}).items()}
    if not targets:
        return False
    incumbent = checkpoint.get("fullAssignment") or {}
    return all(
        name in incumbent and str(targets[name]) == str(incumbent[name])
        for name in targets
    )


def _terminal_outcome(satisfied: bool, audit_status: str, accepted_count: int) -> str:
    # R8: durable complete/partial/blocked. A satisfied policy is COMPLETE only
    # on a PASS audit; any missing/UNKNOWN evidence never yields COMPLETE.
    if satisfied and audit_status == "PASS":
        return "COMPLETE"
    if audit_status not in ("PASS", "FAIL"):
        return "BLOCKED_BASELINE"
    if accepted_count > 0:
        return "PARTIAL_VERIFIED"
    return "NO_VERIFIED_UPGRADE"


def cmd_finish(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    run = load_run(run_dir)
    config = load_config(run_dir)
    lock = _RunLock(run_dir, args.owner or f"pid-{os.getpid()}", stale_seconds=600)
    lock.acquire()
    try:
        return _finish_locked(run_dir, run, config)
    finally:
        lock.release()


def _finish_locked(run_dir: Path, run: Mapping[str, Any], config: Mapping[str, Any]) -> int:
    checkpoints = _all_checkpoints(run_dir)
    reports_dir = run_dir / REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    existing_outcome = run.get("terminalOutcome")
    # P2 (#1): a repair-only run carries NO migration targets. Its terminal is not
    # a partial migration when the current-state control passes — the repair goal
    # (project passes its own checks) is what got satisfied.
    repair_only = bool(config.get("repairOnly")) and not (config.get("targets") or {})

    # R8: finish is an IDEMPOTENT terminal transition. The durable outcome is
    # computed once from the audit evidence + acceptance chain; every later call
    # only re-writes the reports from the same durable state.
    if not existing_outcome:
        active_id = str(run["activeCheckpointId"])
        checkpoint = load_checkpoint(run_dir, active_id)
        audit = checkpoint.get("audit") or {}
        audit_status = str(audit.get("status") or "")
        if audit_status not in ("PASS", "FAIL"):
            # The independent audit of the EXACT accepted checkpoint is a
            # precondition of finalizing; UNKNOWN evidence never yields
            # COMPLETE, so finish runs the audit when the run was finished
            # before it had evidence.
            audit_args = argparse.Namespace(
                registry="",
                lag_months=None,
                min_lag_ok_pct=None,
                max_known_high=None,
                yarn_audit_engine="auto",
            )
            _audit_locked(run_dir, run, config, audit_args)
            checkpoint = load_checkpoint(run_dir, active_id)
            audit = checkpoint.get("audit") or {}
            audit_status = str(audit.get("status") or "")
        satisfied = _policy_satisfied(config, checkpoint)
        accepted_count = sum(1 for c in checkpoints if c.get("status") == "VERIFIED")
        outcome = _terminal_outcome(satisfied, audit_status, accepted_count)
        # P2 (#1): a repair-only run whose CURRENT-state control passes the audit
        # is a repair SUCCESS — never a partial migration ("0 verified UPGRADES"
        # must not read as "часть обновлений применена"). The audit gates it
        # exactly like a real migration: UNKNOWN evidence stays BLOCKED_BASELINE.
        if repair_only and audit_status == "PASS" and str(checkpoint.get("status")) == "VERIFIED":
            outcome = "REPAIR_VERIFIED"
            satisfied = True
        run = dict(run)
        run["terminal"] = outcome
        run["terminalOutcome"] = {
            "outcome": outcome,
            "satisfied": satisfied,
            "auditStatus": audit_status,
            "acceptedCheckpoints": accepted_count,
            "finishedAt": _now_iso(),
            "activeCheckpointId": active_id,
        }
        run["phase"] = "TERMINAL"
        run["updatedAt"] = _now_iso()
        save_run(run_dir, run)
    else:
        outcome = str(existing_outcome.get("outcome") or run.get("terminal") or "")

    # finish may have just persisted fresh audit evidence. Render those bytes,
    # not the pre-audit checkpoint list loaded at entry.
    checkpoints = _all_checkpoints(run_dir)
    report = _build_migration_report(run_dir, run, config, checkpoints, existing_outcome or run.get("terminalOutcome"))
    guide = _build_developer_upgrade_guide(run_dir, config, checkpoints)
    (reports_dir / "MIGRATION_REPORT.md").write_text(report, encoding="utf-8")
    (reports_dir / "DEVELOPER_UPGRADE_GUIDE.md").write_text(guide, encoding="utf-8")
    _emit_status(
        {
            "event": "finish.done",
            "runId": run["runId"],
            "terminal": outcome,
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
    checkpoints = [load_checkpoint(run_dir, cid) for cid in checkpoint_ids]
    chain, tail = _ordered_checkpoint_chain(checkpoints)
    return chain + tail


def _ordered_checkpoint_chain(
    checkpoints: Sequence[Mapping[str, Any]],
) -> tuple:
    """Topological order by parent links: C0 -> C1 -> ... -> deepest accepted
    checkpoint. A plain filename sort would mis-rank C10 before C9; the ACTIVE
    checkpoint is resolved by identity (run.activeCheckpointId) with the chain
    tail as fallback, never by string comparison. Unconnected checkpoints are
    appended deterministically by id."""
    by_id = {str(c.get("checkpointId")): c for c in checkpoints}
    chain: List[Mapping[str, Any]] = []
    seen = set()
    current = next(
        (c for c in by_id.values() if not str(c.get("parentCheckpointId") or "").strip()),
        None,
    )
    while current is not None:
        cid = str(current.get("checkpointId"))
        if cid in seen:
            break
        seen.add(cid)
        chain.append(current)
        current = next(
            (
                c
                for c in by_id.values()
                if str(c.get("checkpointId")) not in seen
                and str(c.get("parentCheckpointId") or "") == cid
            ),
            None,
        )
    tail = [c for c in by_id.values() if str(c.get("checkpointId")) not in seen]
    tail.sort(key=lambda c: str(c.get("checkpointId")))
    return chain, tail


def _resolve_active_checkpoint(
    run: Mapping[str, Any], checkpoints: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any]:
    """R10: the ACTIVE checkpoint is governed by identity (run.activeCheckpointId),
    with the deepest accepted chain member as explicit fallback — never the
    string-max checkpoint id (C10 > C9 would silently describe the wrong state)."""
    active_id = str(run.get("activeCheckpointId") or "")
    if active_id:
        for checkpoint in checkpoints:
            if str(checkpoint.get("checkpointId")) == active_id:
                return checkpoint
    chain, _tail = _ordered_checkpoint_chain(checkpoints)
    return chain[-1] if chain else (checkpoints[-1] if checkpoints else {})


def _build_migration_report(
    run_dir: Path,
    run: Mapping[str, Any],
    config: Mapping[str, Any],
    checkpoints: Sequence[Mapping[str, Any]],
    terminal_outcome: Optional[Mapping[str, Any]] = None,
) -> str:
    active = _resolve_active_checkpoint(run, checkpoints)
    targets = {str(k): str(v) for k, v in (config.get("targets") or {}).items()}
    assignment = {str(k): str(v) for k, v in (active.get("fullAssignment") or {}).items()}
    satisfied = all(
        name in assignment and str(targets[name]) == assignment[name] for name in targets
    )
    remaining = sum(1 for name in targets if assignment.get(name) != str(targets[name]))
    active_audit = active.get("audit") or {}
    ledger: Dict[str, Any] = {}
    ledger_path = run_dir / "ledger.json"
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            if not isinstance(ledger, dict):
                ledger = {}
        except (ValueError, OSError):
            ledger = {}
    feedback = [entry for entry in (ledger.get("feedback") or []) if isinstance(entry, dict)]

    lines: List[str] = []
    lines.append("# MIGRATION_REPORT")
    lines.append("")
    lines.append(f"- Run: `{run.get('runId')}`")
    lines.append(f"- Project: `{config.get('projectName')}`")
    lines.append(f"- Target level: `{config.get('targetLevel')}`")
    lines.append(f"- Terminal: `{run.get('terminal') or 'in-progress'}`")
    lines.append(f"- Active checkpoint: `{active.get('checkpointId')}`")
    lines.append("")
    if terminal_outcome:
        lines.append("## Outcome")
        lines.append("")
        lines.append(f"- Verdict: `{terminal_outcome.get('outcome')}`")
        lines.append(f"- Satisfied policy targets: `{terminal_outcome.get('satisfied')}`")
        lines.append(f"- Independent audit of the accepted checkpoint: `{terminal_outcome.get('auditStatus')}`")
        lines.append(f"- Accepted checkpoints: `{terminal_outcome.get('acceptedCheckpoints')}`")
        lines.append(f"- Finished at: `{terminal_outcome.get('finishedAt')}`")
        lines.append("")
    lines.append("## Итог миграции (остаток и покрытие)")
    lines.append("")
    lines.append(
        f"- Целей по политике: `{len(targets)}`; достигнуто на активном checkpoint "
        f"`{active.get('checkpointId')}`: `{len(targets) - remaining}`; остаток: `{remaining}`; "
        f"policy satisfied: `{satisfied}`."
    )
    lines.append(
        f"- Независимый аудит активного checkpoint: статус `{active_audit.get('status') or 'UNKNOWN'}`, "
        f"lag-ok `{active_audit.get('lagOkPct')}%` (`{active_audit.get('lagOk')}` из "
        f"`{active_audit.get('lagTotal')}`), отстающих `{active_audit.get('lagLagging')}`, "
        f"неизвестных `{active_audit.get('lagUnknown')}`, пакетов с уязвимостями "
        f"`{active_audit.get('vulnerabilityPackages')}`; evidence: `{active_audit.get('evidenceRef') or '-'}`."
    )
    lines.append("")
    lines.append("## Checkpoints (по цепочке parent)")
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
    lines.append("## Accepted dependency state")
    lines.append("")
    lines.append("```json")
    lines.append(_stable_json(assignment))
    lines.append("```")
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    for checkpoint in checkpoints:
        audit = checkpoint.get("audit") or {}
        lines.append(
            f"- {checkpoint.get('checkpointId')}: sourceSnapshotKey=`{checkpoint.get('sourceSnapshotKey')}`, "
            f"manifestHash=`{checkpoint.get('manifestHash')}`, lockfileHash=`{checkpoint.get('lockfileHash') or '-'}`, "
            f"resolvedStateKey=`{checkpoint.get('resolvedStateKey') or '-'}`"
        )
        if audit.get("status"):
            lines.append(
                f"  - audit=`{audit.get('status')}`, evidence=`{audit.get('evidenceRef') or '-'}`, "
                f"lagOkPct=`{audit.get('lagOkPct')}`, lagOk=`{audit.get('lagOk')}`/"
                f"{audit.get('lagLagging')}`lagging/`{audit.get('lagUnknown')}`unknown "
                f"(of `{audit.get('lagTotal')}`), vulnerabilityPackages=`{audit.get('vulnerabilityPackages')}`"
            )
    lines.append("")
    if feedback:
        lines.append("## Объяснения агента и release-факты (по принятой цепочке)")
        lines.append("")
        for entry in feedback:
            base = entry.get("baseCheckpointId") or "-"
            kind = entry.get("kind") or "-"
            candidate = entry.get("candidateId") or "-"
            reason = str(entry.get("reason") or "").strip()
            changed = entry.get("changedFiles") or []
            lines.append(f"- checkpoint `{base}` / candidate `{candidate}` / kind `{kind}`")
            if reason:
                lines.append(f"  - reason: {reason}")
            if changed:
                lines.append(f"  - изменённые файлы: {', '.join('`' + str(f) + '`' for f in changed)}")
        lines.append("")
    return "\n".join(lines)


def _build_developer_upgrade_guide(
    run_dir: Path, config: Mapping[str, Any], checkpoints: Sequence[Mapping[str, Any]]
) -> str:
    upgraded: Dict[str, Dict[str, str]] = {}
    adaptations: Dict[str, List[str]] = {}
    changed_files: Dict[str, List[str]] = {}
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
    ledger: Dict[str, Any] = {}
    ledger_path = run_dir / "ledger.json"
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            if not isinstance(ledger, dict):
                ledger = {}
        except (ValueError, OSError):
            ledger = {}
    # R10: aggregate the ACTUAL agent explanations/adaptations recorded per
    # candidate — never the "will describe later" placeholder.
    for entry in (ledger.get("feedback") or []):
        if not isinstance(entry, dict):
            continue
        reason = str(entry.get("reason") or "")
        files = [str(f) for f in (entry.get("changedFiles") or []) if isinstance(f, str)]
        for name in upgraded:
            if name in reason or any(name in f for f in files):
                adaptations.setdefault(name, [])
                if reason and reason not in adaptations[name]:
                    adaptations[name].append(reason)
        changed_files.setdefault(str(entry.get("baseCheckpointId") or "-"), []).extend(files)
    deferrals = [
        entry for entry in (ledger.get("deferrals") or [])
        if isinstance(entry, dict) and str(entry.get("reason") or "").strip()
    ]

    lines: List[str] = []
    lines.append("# DEVELOPER_UPGRADE_GUIDE")
    lines.append("")
    if not upgraded:
        lines.append(
            "Проверенных обновлений зависимостей на этой цепочке нет: полный assignment "
            "активного checkpoint совпадает с исходным (ни один пакет не принят к изменению). "
            "Это НЕ означает завершения миграции: см. MIGRATION_REPORT (остаток целей)."
        )
        lines.append("")
        lines.append("- Проверено: принятых изменений `fullAssignment` нет; verified checkpoint только фиксирует состояние.")
        lines.append("- Ограничения и отложенные пакеты:")
        if deferrals:
            for entry in deferrals:
                lines.append(
                    f"  - `{entry.get('package') or entry.get('candidateId') or '-'}`: {entry.get('reason')}"
                )
        else:
            lines.append("  - явных отложенных пакетов в ledger нет.")
        lines.append("- Рекомендация: продолжить план-next/новый cohort по остатку целей; при следующем принятом "
                     "улучшении guide пополняется фактическими возможностями и breaking changes.")
    else:
        lines.append("## Accepted upgrades")
        lines.append("")
        lines.append("| Package | From | To |")
        lines.append("| --- | --- | --- |")
        for name, versions in sorted(upgraded.items()):
            lines.append(f"| `{name}` | `{versions['from']}` | `{versions['to']}` |")
        lines.append("")
        lines.append("## Возможности и адаптации (фактические, по ответам агента)")
        lines.append("")
        if adaptations:
            for name, notes in sorted(adaptations.items()):
                lines.append(f"- `{name}`:")
                for note in notes:
                    lines.append(f"  - {note}")
        else:
            lines.append(
                "Записанных агентских объяснений по этим пакетам в ledger нет; адаптация "
                "проверялась только через verify-exact на отремонтированных bytes."
            )
        lines.append("")
        lines.append("## Ограничения и отложенные пакеты")
        lines.append("")
        if deferrals:
            for entry in deferrals:
                lines.append(
                    f"- `{entry.get('package') or entry.get('candidateId') or '-'}`: {entry.get('reason')}"
                )
        else:
            lines.append("- Явных отложенных пакетов нет; остаток целей см. в MIGRATION_REPORT.")
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
    _assert_runtime_unchanged(config)
    checkpoint = load_checkpoint(run_dir, str(run["activeCheckpointId"]))
    snapshot = open_source_snapshot(
        Path(str(checkpoint["sourceSnapshotContainer"])),
        expected_key=str(checkpoint["sourceSnapshotKey"]),
        timeout_seconds=0,
    )
    project_path = Path(snapshot.root) / str(checkpoint.get("projectRelative") or ".")
    try:
        from manual_dependency_audit import build_report, markdown
    except Exception as exc:
        raise IterativeMigrationError("AUDIT_UNAVAILABLE", f"{exc}") from exc
    audit_workspace = run_dir / AUDIT_DIR / f"{checkpoint['checkpointId']}"
    audit_workspace.mkdir(parents=True, exist_ok=True)
    audit_policy = config.get("auditPolicy") or {}
    dashboard_state_raw = str(config.get("dashboardState") or "").strip()
    dashboard_state = (
        Path(dashboard_state_raw).expanduser().resolve()
        if dashboard_state_raw and Path(dashboard_state_raw).exists()
        else None
    )
    # R8: the independent audit runs over the EXACT accepted checkpoint bytes
    # with the ACTUAL policy captured at begin (lag window, lag-ok threshold,
    # known-High limit) plus the user's package lag policy from dashboard state
    # — never dashboard_state=None.
    report = build_report(
        project_path,
        str(config.get("projectName") or ""),
        registry=args.registry or "",
        lag_months=int(
            (args.lag_months if args.lag_months is not None
             else audit_policy.get("lagMonths", 12))
        ),
        dashboard_state=dashboard_state,
        audit_workspace=audit_workspace,
        target_level=str(config.get("targetLevel") or "yellow"),
        min_lag_ok_pct=int(
            (args.min_lag_ok_pct if args.min_lag_ok_pct is not None
             else audit_policy.get("minLagOkPct", 80))
        ),
        max_known_high=int((args.max_known_high if args.max_known_high is not None
                            else audit_policy.get("maxKnownHigh", 1))),
        yarn_audit_engine=args.yarn_audit_engine or "auto",
        runtime_env=_runtime_env(config),
    )
    # R8: persist the FULL report as JSON/Markdown evidence (not just a status +
    # directory link), then record the coverage/vuln metrics in the checkpoint.
    evidence_report = audit_workspace / "audit-report.json"
    evidence_markdown = audit_workspace / "audit-report.md"
    evidence_report.write_text(
        _stable_json(report), encoding="utf-8"
    )
    try:
        evidence_markdown.write_text(markdown(report), encoding="utf-8")
    except Exception as exc:  # the markdown renderer must never hide a finished audit
        evidence_markdown.write_text(
            f"# Audit report (markdown render failed: {exc})\n\n" + _stable_json(report),
            encoding="utf-8",
        )
    evidence_ref = str(audit_workspace)
    audit_status = _audit_status_from_report(report)
    audit_metrics = report.get("audit") or {}
    checkpoint = dict(checkpoint)
    checkpoint["audit"] = {
        "status": audit_status,
        "evidenceRef": evidence_ref,
        "auditComplete": bool(report.get("auditComplete")),
        "lagOkPct": audit_metrics.get("lagOkPct"),
        "lagOk": audit_metrics.get("lagOk"),
        "lagLagging": audit_metrics.get("lagLagging"),
        "lagUnknown": audit_metrics.get("lagUnknown"),
        "lagTotal": audit_metrics.get("lagTotal"),
        "vulnerabilityPackages": len(audit_metrics.get("packages") or {}),
        "policy": report.get("policy") or {},
        "generatedAt": report.get("generatedAt") or "",
    }
    save_checkpoint(run_dir, checkpoint)
    _emit_status(
        {
            "event": "audit.done",
            "runId": run["runId"],
            "checkpointId": checkpoint["checkpointId"],
            "status": audit_status,
            "evidenceRef": evidence_ref,
            "evidenceJson": str(evidence_report),
            "evidenceMarkdown": str(evidence_markdown),
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

    R9: with --legacy-dashboard-state the LEGACY saved Baseline result is
    imported by the SAME builder (versioned artifactSource=legacy-baseline)
    into the shared task contract without any rerun or invented proof.
    """
    run_dir = Path(args.run_dir).resolve()
    legacy_dashboard = (args.legacy_dashboard_state or "").strip()
    if legacy_dashboard:
        if not (args.project_name or "").strip():
            raise InvalidInputError("LEGACY_PROJECT_NAME_REQUIRED: --project-name must name the legacy project")
        from iterative_task import TaskExportError, export_legacy_task_artifact, verify_task_input_errors

        run_dir.mkdir(parents=True, exist_ok=True)
        languages = ["ru", "en"] if args.language == "both" else [args.language]
        try:
            summary = export_legacy_task_artifact(
                Path(legacy_dashboard).expanduser().resolve(),
                (args.project_name or "").strip(),
                run_dir,
                languages=languages,
            )
        except TaskExportError as exc:
            raise InvalidInputError(f"{exc.code}: {exc.summary}") from exc
        _emit_status(summary)
        return 0
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
    begin.add_argument(
        "--check-only",
        action="store_true",
        help="standalone 'Проверить проект': preflight + контроль текущих зависимостей, без run.json/discovery/снапшота",
    )
    begin.add_argument(
        "--repair-only",
        action="store_true",
        help="repair текущего состояния: контроль текущих версий БЕЗ целей и discovery (обход roadmap); красный C0 → BOOTSTRAP_REPAIR, зелёный → READY/REPAIR_VERIFIED",
    )
    begin.add_argument(
        "--adopt-repair-source",
        action="store_true",
        help="начать миграцию С исправленных исходников завершённого ремонта (REPAIR_VERIFIED): C0 наследует проверенный снимок repair-handoff.json вместо повторного снятия с красного project-dir",
    )
    begin.add_argument("--targets-file", default="", help="JSON {package: exact version} policy targets")
    begin.add_argument("--verify-config", default="", help="BaselineVerifyConfig JSON file (commands, projectChecks, ...)")
    begin.add_argument("--tool-build-id", default="")
    begin.add_argument("--run-budget-minutes", type=int, default=None)
    begin.add_argument("--max-repair-attempts", type=int, default=None)
    begin.add_argument("--max-infra-retries", type=int, default=None)
    begin.add_argument("--phase-timeout-seconds", type=int, default=None)
    begin.add_argument(
        "--dashboard-state",
        default="",
        help="Workspace dashboard-state.json carrying the user package lag policy (used by the independent audit)",
    )
    begin.add_argument("--lag-months", type=int, default=None)
    begin.add_argument("--min-lag-ok-pct", type=int, default=None)
    begin.add_argument("--max-known-high", type=int, default=None)
    begin.add_argument(
        "--requested-node",
        default="",
        help="Явный Node.js для проекта/CI (точная версия или major). Пусто = не задано пользователем; "
        "недоступная версия даёт ENVIRONMENT_UNAVAILABLE, а не fallback на PATH",
    )
    begin.add_argument(
        "--discover-parallelism",
        type=int,
        default=1,
        help="Сколько registry-проб discovery выполнять параллельно (Desktop передаёт ограниченный бюджет; 1 = строго последовательно, как раньше)",
    )
    begin.add_argument(
        "--discover-timeout-s",
        type=int,
        default=0,
        help="Wall-clock бюджет фазы discovery в секундах; 0 = без жёсткого дедлайна",
    )
    begin.add_argument(
        "--discover-max-packages",
        type=int,
        default=0,
        help="Сколько прямых зависимостей максимум пробовать в discovery; 0 = без капа",
    )

    verify_bootstrap = sub.add_parser("verify-bootstrap", help="Повторная контрольная проверка C0 после bootstrap repair")
    verify_bootstrap.add_argument("--timeout-seconds", type=int, default=1800)

    archive_run = sub.add_parser("archive-run", help="Сохранить прежний прогон целиком в истории для нового запуска")
    archive_run.add_argument("--recover-only", action="store_true")

    archive_repair = sub.add_parser(
        "archive-repair-run",
        help="Транзакционная архивация TERMINAL REPAIR_VERIFIED прогона: фиксирует repair-handoff.json + project-check.json, исправленный снимок sources/C0 остаётся для следующей миграции",
    )

    bootstrap_materialize = sub.add_parser(
        "bootstrap-materialize",
        help="Материализация version-neutral trial для repair исходников C0",
    )
    bootstrap_materialize.add_argument("--timeout-seconds", type=int, default=1800)

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

    export_task = sub.add_parser("export-task", help="Сформировать ТЗ (task artifact) из durable state или legacy Baseline без Baseline")
    export_task.add_argument("--language", choices=("ru", "en", "both"), default="both")
    export_task.add_argument(
        "--check-only", action="store_true",
        help="Only diagnose input sufficiency; never produces an artifact",
    )
    export_task.add_argument(
        "--legacy-dashboard-state",
        default="",
        help="Путь к сохранённому dashboard-state.json старого Baseline; import в общий task-контракт тем же builder",
    )
    export_task.add_argument(
        "--project-name",
        default="",
        help="Имя проекта внутри dashboard-state.json (обязательно при --legacy-dashboard-state)",
    )

    return parser


COMMANDS = {
    "begin": cmd_begin,
    "verify-bootstrap": cmd_verify_bootstrap,
    "bootstrap-materialize": cmd_bootstrap_materialize,
    "archive-repair-run": cmd_archive_repair_run,
    "archive-run": cmd_archive_run,
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
        recovery_dir = Path(args.run_dir).resolve()
        if (recovery_dir / RESTART_TRANSACTION).exists():
            recovery_lock = _RunLock(recovery_dir, args.owner or f"pid-{os.getpid()}")
            recovery_lock.acquire()
            try:
                recover_restart_archive(recovery_dir, _write_json_atomic)
            except RestartArchiveError as exc:
                raise InvalidInputError(str(exc)) from exc
            finally:
                recovery_lock.release()
        if (recovery_dir / REPAIR_ARCHIVE_TRANSACTION).exists():
            recovery_lock = _RunLock(recovery_dir, args.owner or f"pid-{os.getpid()}")
            recovery_lock.acquire()
            try:
                _recover_repair_archive(recovery_dir)
            finally:
                recovery_lock.release()
        return int(COMMANDS[args.command](args) or 0)
    except ProjectUnreadyError as exc:
        print(f"ITERATIVE_MIGRATION_FAILURE_V1 {json.dumps({'code': exc.code, 'summary': str(exc), 'command': exc.command, 'fixable': True})}")
        return 2
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
