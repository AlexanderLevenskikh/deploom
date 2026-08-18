#!/usr/bin/env python3
"""Pre-agent Baseline assignment verifier.

The dependency solver proves metadata constraints.  This verifier proves that a
chosen direct-version assignment can actually be materialized by the project's
package manager before any migration branch or LLM worker is created.

Hard failures (dependency resolution) are suitable for learned nogood
constraints. Infrastructure failures are *never* learned: a missing yarn shim,
network outage or temp-directory problem must not change the dependency plan.
Project checks use an adaptive policy by default: ordinary source-migration
failures remain Executor work, while deterministic package/loadability failures
are fed back into planning before any Executor branch is created.
"""
from __future__ import annotations

import atexit
import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


from verification_proof import (
    VerificationProofIdentity,
    VerificationProofStore,
    bind_resolved_state_identity,
    build_verification_proof_identity,
    emit_verification_event,
    fixed_resolver_input_fingerprint,
    remote_fixed_resolver_input_fingerprint,
    SourceIdentityUnavailable,
    is_fixed_manifest_spec,
)
from resolved_dependency_state import (
    ResolvedDependencyState,
    ResolvedDependencyStateError,
    assert_resolved_dependency_state,
    capture_resolved_dependency_state,
    load_resolved_dependency_state,
    resolved_state_metadata,
    restore_resolved_dependency_state,
)
from prepared_workspace_fastpath import (
    acquire_snapshot_copy_lease,
    build_dependency_integrity_manifest,
    try_acquire_snapshot_cleanup_lease,
    cleanup_guarded_clone,
    guarded_clone_is_active,
    stop_guarded_clone,
    try_materialize_guarded_clone,
)
# BLOCK_U_VERIFICATION_SUBSTRATE_V1
from verification_process_supervisor import run_supervised
from verification_workspace_backend import (
    materialize_private_tree,
    workspace_backend_summary,
)

@dataclasses.dataclass(frozen=True)
class BaselineVerifyConfig:
    enabled: bool = True
    parallelism: int = 4
    max_iterations: int = 8
    max_delta_checks: int = 24
    timeout_seconds: int = 600
    attempt_timeout_seconds: int = 3600
    localization_timeout_seconds: int = 7200
    progress_interval_seconds: int = 15
    snapshot_copy_timeout_seconds: int = 1800
    project_checks: str = "adaptive"  # off | diagnostic | adaptive | strict
    commands: Tuple[str, ...] = ()
    registry: str = ""
    telemetry_path: str = ""
    proof_cache_dir: str = ""
    reuse_resolver_proof_key: str = ""

    @staticmethod
    def from_mapping(value: Optional[Mapping[str, object]], *, fallback_commands: Sequence[str] = ()) -> "BaselineVerifyConfig":
        raw = dict(value or {})
        mode = str(raw.get("projectChecks", raw.get("project_checks", "adaptive"))).strip().lower()
        if mode not in {"off", "diagnostic", "adaptive", "strict"}:
            raise ValueError("CONSTRAINT_VERIFY_CONFIG_INVALID: projectChecks must be off, diagnostic, adaptive, or strict")
        commands_raw = raw.get("commands")
        if commands_raw is None:
            commands = tuple(str(item).strip() for item in fallback_commands if str(item).strip())
        elif isinstance(commands_raw, list) and all(isinstance(item, str) and item.strip() for item in commands_raw):
            commands = tuple(item.strip() for item in commands_raw)
        else:
            raise ValueError("CONSTRAINT_VERIFY_CONFIG_INVALID: commands must be an array of non-empty strings")
        return BaselineVerifyConfig(
            enabled=_as_bool(raw.get("enabled"), True),
            parallelism=max(1, min(_as_int(raw.get("parallelism"), 4), 16)),
            max_iterations=max(1, min(_as_int(raw.get("maxIterations", raw.get("max_iterations")), 8), 32)),
            max_delta_checks=max(1, min(_as_int(raw.get("maxDeltaChecks", raw.get("max_delta_checks")), 24), 128)),
            timeout_seconds=max(30, min(_as_int(raw.get("timeoutSeconds", raw.get("timeout_seconds")), 600), 3600)),
            attempt_timeout_seconds=max(60, min(_as_int(raw.get("attemptTimeoutSeconds", raw.get("attempt_timeout_seconds")), 3600), 14400)),
            localization_timeout_seconds=max(300, min(_as_int(raw.get("localizationTimeoutSeconds", raw.get("localization_timeout_seconds")), 7200), 21600)),
            progress_interval_seconds=max(5, min(_as_int(raw.get("progressIntervalSeconds", raw.get("progress_interval_seconds")), 15), 60)),
            snapshot_copy_timeout_seconds=max(
                60,
                min(
                    _as_int(
                        raw.get(
                            "snapshotCopyTimeoutSeconds",
                            raw.get("snapshot_copy_timeout_seconds"),
                        ),
                        1800,
                    ),
                    7200,
                ),
            ),
            project_checks=mode,
            commands=commands,
        )


def _as_int(value: object, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclasses.dataclass(frozen=True)
class BaselineProjectFailure:
    command: str
    exit_code: int
    output: str = ""


@dataclasses.dataclass
class BaselineVerifyResult:
    ok: bool
    kind: str  # passed | dependency | preparation | project | infrastructure | unknown
    summary: str
    command: str = ""
    output: str = ""
    workspace: str = ""
    project_failures: Tuple[BaselineProjectFailure, ...] = ()
    observed_resolved_versions: Mapping[str, str] = dataclasses.field(default_factory=dict)
    observed_resolved_hash: str = ""
    resolved_state_key: str = ""
    resolved_lockfile_hash: str = ""

    @property
    def hard_failure(self) -> bool:
        return self.kind in {"dependency", "preparation", "project"}


class AssignmentMaterializationError(RuntimeError):
    """Raised when the solver assignment cannot be represented by package.json."""


class ObservedResolutionError(RuntimeError):
    """Raised when the installed direct tree differs from the exact assignment."""


INFRA_PATTERNS = re.compile(
    r"(?:ENOENT|not recognized as an internal or external command|command not found|"
    r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|ESOCKETTIMEDOUT|EAI_AGAIN|ENETUNREACH|"
    r"getaddrinfo\s+ENOTFOUND|socket hang up|"
    r"There appears to be trouble with (?:your network connection|the npm registry)|"
    r"(?:ResponseError:\s*)?Request failed [\"'](?:401 Unauthorized|403 Forbidden|"
    r"408 Request Timeout|429 Too Many Requests|5\d\d [^\"']+)[\"']|"
    r"SELF_SIGNED_CERT|unable to get local issuer|certificate has expired|"
    r"ENOSPC|EPERM|EACCES|HTTP\s+(?:401|403|408|429|500|502|503|504)\b)",
    re.IGNORECASE,
)

DEPENDENCY_PATTERNS = re.compile(
    r"(?:ERESOLVE|unable to resolve dependency tree|could not resolve dependency|"
    r"No matching version found|Couldn't find any versions|YN0082|"
    r"conflicting peer dependency)",
    re.IGNORECASE,
)


STRUCTURAL_PROJECT_FAILURE_PATTERNS = re.compile(
    r"(?:ERR_REQUIRE_ESM|Must use import to load ES Module|Cannot use import statement outside a module|"
    r"Unexpected token ['\"]?export|No \"exports\" main defined)",
    re.IGNORECASE,
)

TS_MODULE_RESOLUTION_FAILURE = re.compile(
    r"TS2307[^\n\r]*Cannot find module ['\"]([^'\"]+)['\"](?:(?!TS\d{4}).){0,1800}?"
    r"(?:could not be resolved under your current ['\"]?moduleResolution['\"]? setting|"
    r"Consider updating to ['\"]?node16['\"]?, ['\"]?nodenext['\"]?, or ['\"]?bundler['\"]?)",
    re.IGNORECASE | re.DOTALL,
)

TYPE_IDENTITY_FAILURE_PATTERNS = re.compile(
    r"(?:TS2322|TS2345|TS2416|TS2430|TS2719|TS2769|Two different types with this name exist)",
    re.IGNORECASE,
)
IMPORT_TYPE_PATH = re.compile(r"import\([\"']([^\"']*node_modules[\\/][^\"']+)[\"']\)", re.IGNORECASE)

# Narrow evidence-backed runtime/toolchain incompatibility. This is intentionally
# not a generic "x.y is not a function" classifier: only the already observed
# Sass compiler API transition is promoted to solver evidence, and only after
# candidate-vs-control differential comparison plus fresh reproduction.
SASS_COMPILER_API_FAILURE = re.compile(
    r"(?:TypeError:\s*)?sass\.initAsyncCompiler\s+is\s+not\s+a\s+function",
    re.IGNORECASE,
)

EPHEMERAL_VERIFICATION_CACHE_PATHS = (
    "node_modules/.vite",
    "node_modules/.vitest",
    "node_modules/.cache",
    "src/node_modules/.vite",
    "src/node_modules/.vitest",
    "src/node_modules/.cache",
)

ProgressCallback = Callable[[str], None]


def _package_from_node_modules_path(value: str) -> Tuple[str, str]:
    normalized = value.replace("\\", "/")
    markers = normalized.split("/node_modules/")
    if len(markers) < 2:
        return "", ""
    tail = markers[-1]
    parts = [part for part in tail.split("/") if part]
    if not parts:
        return "", ""
    package = "/".join(parts[:2]) if parts[0].startswith("@") and len(parts) >= 2 else parts[0]
    package_root = normalized[: normalized.rfind("/node_modules/")] + "/node_modules/" + package
    return package, package_root.lower()


def duplicate_type_universe_packages(text: str) -> Tuple[str, ...]:
    """Return packages referenced through two distinct node_modules roots."""
    roots: Dict[str, set[str]] = {}
    for raw_path in IMPORT_TYPE_PATH.findall(text):
        package, root = _package_from_node_modules_path(raw_path)
        if package and root:
            roots.setdefault(package, set()).add(root)
    return tuple(sorted(package for package, values in roots.items() if len(values) > 1))


def _project_failure_records(result: BaselineVerifyResult) -> Tuple[BaselineProjectFailure, ...]:
    if result.project_failures:
        return result.project_failures
    if result.kind == "project" and not result.ok:
        return (BaselineProjectFailure(result.command or result.summary, 1, f"{result.summary}\n{result.output}"),)
    return ()


def _esm_loader_package(text: str) -> str:
    # Prefer the package actually mentioned in a node_modules path.  This keeps
    # the structural signature stable across temp-workspace roots while still
    # distinguishing unrelated loader failures.
    for raw in re.findall(r"[^\s'\"]*node_modules[\\/][^\s'\"]+", text, re.IGNORECASE):
        package, _root = _package_from_node_modules_path(raw)
        if package:
            return package
    return ""


def structural_project_failure_signatures(result: BaselineVerifyResult) -> Tuple[str, ...]:
    """Return stable structural diagnostics suitable for differential comparison.

    A project command may already be red before migration for an unrelated API
    problem.  Baseline must therefore compare *structural signatures*, not the
    command's aggregate exit code.  Only narrow package/runtime identities are
    emitted here; ordinary source API errors intentionally produce no signature.
    """
    if result.ok or result.kind != "project":
        return ()
    signatures: set[str] = set()
    for failure in _project_failure_records(result):
        text = failure.output or ""
        for match in TS_MODULE_RESOLUTION_FAILURE.finditer(text):
            signatures.add(f"ts-module-resolution:{match.group(1).lower()}")
        if STRUCTURAL_PROJECT_FAILURE_PATTERNS.search(text):
            package = _esm_loader_package(text)
            signatures.add(f"esm-cjs:{package.lower()}" if package else "esm-cjs")
        if TYPE_IDENTITY_FAILURE_PATTERNS.search(text):
            for package in duplicate_type_universe_packages(text):
                signatures.add(f"duplicate-type-universe:{package.lower()}")
        if SASS_COMPILER_API_FAILURE.search(text):
            signatures.add("toolchain-runtime-api:sass.initasynccompiler")
    # Backwards-compatible fallback for callers constructing a synthetic result
    # without project_failures (the helper above turns it into one record).
    return tuple(sorted(signatures))


def is_structural_project_failure(result: BaselineVerifyResult) -> bool:
    """True for failures that version/source planning should prevent."""
    return bool(structural_project_failure_signatures(result))


def discover_baseline_project_checks(project_dir: Path) -> Tuple[str, ...]:
    """Discover deterministic project checks useful before branch creation.

    Adaptive classification means build/test failures are diagnostic unless a
    narrow structural signature is newly introduced by the candidate assignment.
    Running build/test here therefore catches runtime/toolchain incompatibilities
    (for example Vitest/Vite/Sass) without turning ordinary source migration
    failures into solver authority. Explicit commands still take precedence.
    """
    try:
        manifest = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ()
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict):
        return ()
    manager = detect_package_manager(project_dir)
    # Keep checks deterministic and ordered. Build runs before tests because
    # several real Vite/Vitest projects write caches during tests that can make a
    # later build fail for environmental rather than dependency reasons.
    preferred = (
        "lint:types", "typecheck",
        "flow:check", "lint:flow", "typecheck:flow", "flow",
        "lint:styles", "lint:scripts",
        "build", "test:unit", "test",
    )
    names = [name for name in preferred if isinstance(scripts.get(name), str) and scripts.get(name, "").strip()]
    # Do not run two aliases for the same Flow checker in one Baseline. Prefer
    # the most explicit script name above; this avoids starting/checking the
    # same Flow server repeatedly in legacy projects.
    flow_names = [name for name in names if name in {"flow:check", "lint:flow", "typecheck:flow", "flow"}]
    if len(flow_names) > 1:
        keep = flow_names[0]
        names = [name for name in names if name not in set(flow_names) or name == keep]
    if "test:unit" in names and "test" in names:
        names = [name for name in names if name != "test"]
    commands: List[str] = []
    for name in names:
        if manager == "yarn":
            commands.append(f"yarn {name}")
        else:
            commands.append(f"{manager} run {name}")
    return tuple(commands)


def _candidate_search_dirs() -> List[Path]:
    env = os.environ
    values: List[Path] = []
    for key, suffix in (
        ("NVM_SYMLINK", ""),
        ("NVM_HOME", ""),
        ("VOLTA_HOME", "bin"),
        ("FNM_MULTISHELL_PATH", ""),
        ("APPDATA", "npm"),
    ):
        raw = env.get(key)
        if raw:
            values.append(Path(raw) / suffix if suffix else Path(raw))
    if os.name == "nt":
        if env.get("ProgramFiles"):
            values.append(Path(env["ProgramFiles"]) / "nodejs")
        if env.get("LOCALAPPDATA"):
            values.append(Path(env["LOCALAPPDATA"]) / "Programs" / "nodejs")
        if env.get("USERPROFILE"):
            values.append(Path(env["USERPROFILE"]) / "scoop" / "shims")
    return values


def resolve_executable(command: str) -> Optional[str]:
    direct = shutil.which(command)
    if direct:
        return direct
    extensions = [""] if os.name != "nt" else [".exe", ".com", ".cmd", ".bat", ""]
    for directory in _candidate_search_dirs():
        for extension in extensions:
            candidate = directory / f"{command}{extension}"
            if candidate.is_file():
                return str(candidate)
    return None


def _command_prefix(executable: str, args: Sequence[str]) -> Tuple[List[str], bool]:
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        # subprocess(list) does not reliably CreateProcess a batch shim when the
        # parent is a GUI app. cmd.exe is explicit and keeps shell=False here.
        quoted = subprocess.list2cmdline([executable, *args])
        return [comspec, "/d", "/s", "/c", quoted], False
    return [executable, *args], False


def detect_package_manager(project_dir: Path) -> str:
    package_json = project_dir / "package.json"
    if package_json.exists():
        try:
            package_manager = str(json.loads(package_json.read_text(encoding="utf-8")).get("packageManager") or "")
            if package_manager:
                return package_manager.split("@", 1)[0].strip().lower()
        except (OSError, ValueError, TypeError):
            pass
    if (project_dir / "yarn.lock").exists():
        return "yarn"
    if (project_dir / "pnpm-lock.yaml").exists():
        return "pnpm"
    return "npm"


def install_args(
    manager: str, *, ignore_scripts: bool, frozen: bool = False
) -> List[str]:
    if manager == "yarn":
        # Resolver discovery may create/extend the lockfile. Every phase after
        # ResolvedState capture is frozen to those exact bytes.
        args = ["install"]
        if frozen:
            args.append("--frozen-lockfile")
        if ignore_scripts:
            args.append("--ignore-scripts")
        return args
    if manager == "pnpm":
        args = ["install", "--frozen-lockfile" if frozen else "--no-frozen-lockfile"]
        if ignore_scripts:
            args.append("--ignore-scripts")
        return args
    if frozen:
        args = ["ci", "--no-audit", "--no-fund"]
    else:
        args = ["install", "--no-audit", "--no-fund"]
    if ignore_scripts:
        args.append("--ignore-scripts")
    return args


def _emit_progress(progress: Optional[ProgressCallback], message: str) -> None:
    if progress is not None:
        progress(message)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate *process* and descendants; never leave npm/node children alive."""
    if process.poll() is not None:
        return
    if os.name == "nt" and process.pid:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    elif process.pid:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _run(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout_seconds: int,
    env: Optional[Mapping[str, str]] = None,
    base_env: Optional[Mapping[str, str]] = None,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "command",
    progress_interval_seconds: int = 15,
) -> subprocess.CompletedProcess[str]:
    """Run a verifier child through the cross-platform Block U supervisor."""
    return run_supervised(
        argv,
        cwd,
        timeout_seconds=timeout_seconds,
        env=env,
        base_env=base_env,
        progress=progress,
        progress_label=progress_label,
        progress_interval_seconds=progress_interval_seconds,
    )


def clean_ephemeral_verification_caches(project_dir: Path) -> Tuple[str, ...]:
    removed: List[str] = []
    for relative in EPHEMERAL_VERIFICATION_CACHE_PATHS:
        target = project_dir.joinpath(*relative.split("/"))
        if not target.exists():
            continue
        try:
            shutil.rmtree(target, ignore_errors=False)
            removed.append(relative)
        except OSError:
            # A cache cleanup failure is handled by the following deterministic
            # command; do not silently mutate source/config to work around it.
            pass
    return tuple(removed)


@dataclasses.dataclass(frozen=True)
class PreparedWorkspaceSnapshot:
    key: str
    workspace_root: Path
    project_relative: Path
    source_project: Path
    storage_mode: str
    observed_resolved_versions: Mapping[str, str]
    observed_resolved_hash: str
    dependency_integrity: Mapping[str, str] = dataclasses.field(default_factory=dict)


_PREPARED_SNAPSHOT_LOCK = threading.Lock()
_PREPARED_SNAPSHOT_ROOT: Optional[Path] = None
_PREPARED_SNAPSHOTS: Dict[Tuple[str, str], PreparedWorkspaceSnapshot] = {}
_PREPARED_SNAPSHOT_MAX_COUNT = 2
_PREPARED_FASTPATH_DISABLED: set[Tuple[str, str]] = set()

# BLOCK_U_TIME_TO_RESULT_V1
# Process-local optimization state only. It is never proof/Solver authority.
_PREPARED_FASTPATH_COMMAND_DISABLED: set[Tuple[str, str]] = set()


def _cleanup_prepared_snapshot_root() -> None:
    global _PREPARED_SNAPSHOT_ROOT
    root = _PREPARED_SNAPSHOT_ROOT
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)
    _PREPARED_SNAPSHOT_ROOT = None
    _PREPARED_SNAPSHOTS.clear()
    _PREPARED_FASTPATH_DISABLED.clear()
    _PREPARED_FASTPATH_COMMAND_DISABLED.clear()


atexit.register(_cleanup_prepared_snapshot_root)


def _prepared_snapshot_root() -> Path:
    global _PREPARED_SNAPSHOT_ROOT
    with _PREPARED_SNAPSHOT_LOCK:
        if _PREPARED_SNAPSHOT_ROOT is None:
            _PREPARED_SNAPSHOT_ROOT = Path(
                tempfile.mkdtemp(prefix="dependency-flow-prepared-snapshots-")
            )
        return _PREPARED_SNAPSHOT_ROOT


def _run_snapshot_copy(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout_seconds: int,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "snapshot copy",
    progress_interval_seconds: int = 15,
) -> subprocess.CompletedProcess[str]:
    return run_supervised(
        argv,
        cwd,
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_label=progress_label,
        progress_interval_seconds=progress_interval_seconds,
    )


def _copy_tree_snapshot(
    source: Path,
    target: Path,
    *,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "prepared snapshot copy",
    timeout_seconds: int = 1800,
    progress_interval_seconds: int = 15,
) -> None:
    """Materialize a proof-safe private tree through the platform backend."""
    mode = materialize_private_tree(
        source,
        target,
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_label=progress_label,
        progress_interval_seconds=progress_interval_seconds,
        # Preserve the verifier-level heartbeat/timeout hook. WorkspaceBackend
        # still owns platform selection; the caller owns command supervision.
        runner=_run_snapshot_copy,
    )
    _emit_progress(
        progress,
        f"{progress_label}: materialization backend={mode}",
    )


def _prepared_snapshot_slot(key: str, source_project: Path) -> Tuple[str, str]:
    return str(key), str(source_project.resolve())


def _disable_prepared_snapshot_fastpath(key: str, source_project: Path) -> None:
    slot = _prepared_snapshot_slot(key, source_project)
    with _PREPARED_SNAPSHOT_LOCK:
        _PREPARED_FASTPATH_DISABLED.add(slot)


def _prepared_snapshot_fastpath_allowed(key: str, source_project: Path) -> bool:
    slot = _prepared_snapshot_slot(key, source_project)
    with _PREPARED_SNAPSHOT_LOCK:
        return slot not in _PREPARED_FASTPATH_DISABLED


def _prepared_command_fastpath_slot(
    source_project: Path, command: str
) -> Tuple[str, str]:
    return str(source_project.resolve()), str(command).strip()


def _disable_prepared_command_fastpath(
    source_project: Path, command: str
) -> None:
    slot = _prepared_command_fastpath_slot(source_project, command)
    with _PREPARED_SNAPSHOT_LOCK:
        _PREPARED_FASTPATH_COMMAND_DISABLED.add(slot)


def _prepared_command_fastpath_allowed(
    source_project: Path, command: str
) -> bool:
    slot = _prepared_command_fastpath_slot(source_project, command)
    with _PREPARED_SNAPSHOT_LOCK:
        return slot not in _PREPARED_FASTPATH_COMMAND_DISABLED


def _ntfs_fastpath_min_commands() -> int:
    # A new one-command targeted proof is cheaper as one proof-safe private
    # copy than hashing ~100k+ dependency files just to save that one copy.
    raw = str(os.environ.get("DEPLOOM_NTFS_FASTPATH_MIN_COMMANDS") or "").strip()
    if raw:
        try:
            return max(1, min(32, int(raw)))
        except ValueError:
            pass
    return 2


def _prepared_snapshot_fastpath_worth_sealing(
    commands: Sequence[str], source_project: Path
) -> bool:
    if os.name != "nt":
        return False
    eligible = tuple(
        command
        for command in commands
        if _prepared_command_fastpath_allowed(source_project, command)
    )
    return len(eligible) >= _ntfs_fastpath_min_commands()


def _lookup_prepared_workspace_snapshot(
    key: str, source_project: Path
) -> Optional[PreparedWorkspaceSnapshot]:
    slot = _prepared_snapshot_slot(key, source_project)
    with _PREPARED_SNAPSHOT_LOCK:
        snapshot = _PREPARED_SNAPSHOTS.pop(slot, None)
        if snapshot is None:
            return None
        if not snapshot.workspace_root.is_dir():
            return None
        # Dict insertion order becomes a tiny LRU. Snapshot eviction never
        # weakens proof; it only forces rematerialization on a future miss.
        _PREPARED_SNAPSHOTS[slot] = snapshot
        return snapshot


def _retire_prepared_workspace_snapshot(snapshot: PreparedWorkspaceSnapshot) -> bool:
    if os.name != "nt":
        shutil.rmtree(snapshot.workspace_root.parent, ignore_errors=True)
        return True
    lease = try_acquire_snapshot_cleanup_lease(snapshot.workspace_root)
    if lease is None:
        # A live reader owns the snapshot (or the lease substrate itself is
        # uncertain). Deleting it would be less safe than leaking a temp tree.
        return False
    try:
        shutil.rmtree(snapshot.workspace_root.parent, ignore_errors=True)
        return True
    finally:
        lease.close()


def _evict_prepared_workspace_snapshot(key: str, source_project: Path) -> None:
    slot = _prepared_snapshot_slot(key, source_project)
    with _PREPARED_SNAPSHOT_LOCK:
        snapshot = _PREPARED_SNAPSHOTS.pop(slot, None)
    if snapshot is not None:
        _retire_prepared_workspace_snapshot(snapshot)


def _enforce_prepared_snapshot_budget(protected_slot: Tuple[str, str]) -> None:
    victims: List[PreparedWorkspaceSnapshot] = []
    with _PREPARED_SNAPSHOT_LOCK:
        while len(_PREPARED_SNAPSHOTS) > _PREPARED_SNAPSHOT_MAX_COUNT:
            slot = next(iter(_PREPARED_SNAPSHOTS))
            if slot == protected_slot and len(_PREPARED_SNAPSHOTS) > 1:
                snapshot = _PREPARED_SNAPSHOTS.pop(slot)
                _PREPARED_SNAPSHOTS[slot] = snapshot
                slot = next(iter(_PREPARED_SNAPSHOTS))
            victim = _PREPARED_SNAPSHOTS.pop(slot, None)
            if victim is not None:
                victims.append(victim)
    for victim in victims:
        _retire_prepared_workspace_snapshot(victim)


def _publish_prepared_workspace_snapshot(
    workspace_root: Path,
    workspace_project: Path,
    *,
    key: str,
    observed_versions: Mapping[str, str],
    observed_hash: str,
    source_project: Path,
    seal_dependency_integrity: bool = True,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "prepared snapshot publish copy",
    timeout_seconds: int = 1800,
    progress_interval_seconds: int = 15,
) -> PreparedWorkspaceSnapshot:
    slot = _prepared_snapshot_slot(key, source_project)
    existing = _lookup_prepared_workspace_snapshot(key, source_project)
    if existing is not None:
        return existing

    root = _prepared_snapshot_root()
    stage = Path(tempfile.mkdtemp(prefix=f"{key[:12]}-", dir=root))
    stage_workspace = stage / "workspace"
    relative = workspace_project.resolve().relative_to(workspace_root.resolve())
    storage_mode = "copied-sealed-workspace"
    try:
        # On the normal Windows path both directories live under the same temp
        # volume. Renaming the already-prepared tree publishes the immutable
        # snapshot in O(metadata), instead of physically copying node_modules.
        moved = False
        if os.name == "nt":
            try:
                os.replace(workspace_root, stage_workspace)
                moved = True
                storage_mode = "moved-sealed-workspace"
                _emit_progress(
                    progress,
                    f"{progress_label}: zero-copy snapshot promotion complete",
                )
            except OSError:
                moved = False
        if not moved:
            _copy_tree_snapshot(
                workspace_root,
                stage_workspace,
                progress=progress,
                progress_label=progress_label,
                timeout_seconds=timeout_seconds,
                progress_interval_seconds=progress_interval_seconds,
            )

        dependency_integrity: Mapping[str, str] = {}
        if os.name == "nt" and seal_dependency_integrity:
            dependency_integrity = build_dependency_integrity_manifest(
                stage_workspace,
                progress=progress,
                progress_interval_seconds=progress_interval_seconds,
            )

        snapshot = PreparedWorkspaceSnapshot(
            key=str(key),
            workspace_root=stage_workspace,
            project_relative=relative,
            source_project=source_project.resolve(),
            storage_mode=storage_mode,
            observed_resolved_versions=dict(sorted(
                (str(name), str(version)) for name, version in observed_versions.items()
            )),
            observed_resolved_hash=str(observed_hash),
            dependency_integrity=dict(dependency_integrity),
        )
        with _PREPARED_SNAPSHOT_LOCK:
            raced = _PREPARED_SNAPSHOTS.get(slot)
            if raced is None:
                _PREPARED_SNAPSHOTS[slot] = snapshot
                published = snapshot
            else:
                published = raced
        if raced is not None:
            shutil.rmtree(stage, ignore_errors=True)
        _enforce_prepared_snapshot_budget(slot)
        return published
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _materialize_prepared_workspace_snapshot(
    snapshot: PreparedWorkspaceSnapshot,
    target: Path,
    *,
    allow_fastpath: bool = True,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "prepared snapshot clone copy",
    timeout_seconds: int = 1800,
    progress_interval_seconds: int = 15,
) -> Path:
    # Zero-config Windows/NTFS fast path: clone only source/config, then expose
    # the immutable prepared dependency bytes through junctions guarded by
    # ReadDirectoryChangesW. Any package-tree notification invalidates this
    # optimization and is retried through proof-safe private copies.
    fastpath_allowed = (
        allow_fastpath
        and _prepared_snapshot_fastpath_allowed(snapshot.key, snapshot.source_project)
    )
    if fastpath_allowed:
        fast = try_materialize_guarded_clone(
            source_project=snapshot.source_project,
            prepared_workspace_root=snapshot.workspace_root,
            project_relative=snapshot.project_relative,
            target=target,
            dependency_integrity=snapshot.dependency_integrity,
            progress=progress,
        )
        if fast is not None:
            return fast
    elif progress and allow_fastpath:
        progress(
            "NTFS fast clone disabled for this preparation identity after a prior "
            "shared-tree notification; using proof-safe full copy"
        )

    copy_lease = acquire_snapshot_copy_lease(
        snapshot.workspace_root,
        timeout_seconds=timeout_seconds,
        progress=progress,
    ) if os.name == "nt" else None
    try:
        _copy_tree_snapshot(
            snapshot.workspace_root,
            target,
            progress=progress,
            progress_label=progress_label,
            timeout_seconds=timeout_seconds,
            progress_interval_seconds=progress_interval_seconds,
        )
    finally:
        if copy_lease is not None:
            copy_lease.close()
    project = target / snapshot.project_relative
    if not project.is_dir():
        raise RuntimeError(
            f"PREPARED_SNAPSHOT_PROJECT_MISSING: {snapshot.project_relative}"
        )
    return project


def _package_manager_cache_environment(
    config: BaselineVerifyConfig,
    manager: str,
) -> Dict[str, str]:
    """Configure artifact caches without changing resolver authority.

    Yarn Classic keeps its native user cache. Pointing YARN_CACHE_FOLDER at a
    fresh DepLoom-private directory turns every first Baseline into a cold
    transitive refetch and does not strengthen the proof: the authoritative
    boundary is still the fresh isolated install plus observed direct tree.
    """
    if manager == "yarn":
        return {}
    if not config.proof_cache_dir:
        return {}
    root = (
        Path(config.proof_cache_dir).resolve().parent
        / "package-manager-artifacts"
        / str(manager).lower()
    )
    root.mkdir(parents=True, exist_ok=True)
    if manager == "npm":
        return {"npm_config_cache": str(root)}
    if manager == "pnpm":
        return {"npm_config_store_dir": str(root)}
    return {}


def _git_root_and_relative(project_dir: Path) -> Tuple[Optional[Path], Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--show-toplevel"],
            text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, Path(".")
    if result.returncode != 0 or not result.stdout.strip():
        return None, Path(".")
    root = Path(result.stdout.strip()).resolve()
    try:
        return root, project_dir.resolve().relative_to(root)
    except ValueError:
        return root, Path(".")


def _materialize_workspace(project_dir: Path, target: Path) -> Path:
    git_root, relative = _git_root_and_relative(project_dir)
    if git_root is not None:
        # A local shared clone is much cheaper than copying node_modules and
        # still gives diagnostic project checks a real .git directory.
        result = subprocess.run(
            ["git", "clone", "--quiet", "--shared", "--no-hardlinks", str(git_root), str(target)],
            text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=120, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"temporary git clone failed: {result.stdout.strip()}")
        return target / relative

    ignore = shutil.ignore_patterns(
        "node_modules", ".idea", ".vs", ".vscode", ".fleet", "dist", "build", ".cache", ".git"
    )
    shutil.copytree(project_dir, target, ignore=ignore, dirs_exist_ok=True)
    return target


def _apply_assignment(
    project_dir: Path,
    assignment: Mapping[str, str],
    *,
    remove_packages: Iterable[str] = (),
) -> List[str]:
    package_json = project_dir / "package.json"
    manifest = json.loads(package_json.read_text(encoding="utf-8"))
    changed: List[str] = []
    sections = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
    removals = {str(name) for name in remove_packages}
    for name in sorted(set(assignment) | removals):
        declarations: List[Tuple[str, str]] = []
        for section in sections:
            deps = manifest.get(section)
            if isinstance(deps, dict) and name in deps:
                declarations.append((section, str(deps[name])))
        fixed_declarations = [
            (section, spec)
            for section, spec in declarations
            if is_fixed_manifest_spec(spec)
        ]
        managed_declarations = [
            (section, spec)
            for section, spec in declarations
            if not is_fixed_manifest_spec(spec)
        ]
        if fixed_declarations:
            detail = ", ".join(f"{section}={spec}" for section, spec in declarations)
            if managed_declarations:
                raise AssignmentMaterializationError(
                    f"ASSIGNMENT_HETEROGENEOUS_SOURCE_CONFLICT: {name}: "
                    f"fixed and registry declarations cannot be materialized as one exact version; {detail}"
                )
            if name in removals:
                raise AssignmentMaterializationError(
                    f"ASSIGNMENT_REMOVES_FIXED_INPUT: {name}: fixed declaration is immutable; {detail}"
                )
            if name in assignment:
                raise AssignmentMaterializationError(
                    f"ASSIGNMENT_TARGETS_FIXED_INPUT: {name}: fixed declaration is immutable; {detail}"
                )

        package_changed = False
        package_seen = False
        for section in sections:
            deps = manifest.get(section)
            if not isinstance(deps, dict) or name not in deps:
                continue
            package_seen = True
            if name in removals:
                del deps[name]
                package_changed = True
                continue
            version = assignment[name]
            if deps[name] != version:
                deps[name] = version
                package_changed = True
        if name in assignment and name not in removals and not package_seen:
            raise AssignmentMaterializationError(
                f"ASSIGNMENT_PACKAGE_NOT_DECLARED: {name} is absent from all direct dependency sections"
            )
        if package_changed:
            changed.append(name)
    package_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed



OBSERVED_REMOVED = "<removed>"
OBSERVED_PEER_ONLY = "<peer-only>"
OBSERVED_OPTIONAL_NOT_INSTALLED = "<optional-not-installed>"
DIRECT_DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


def _installed_package_json_path(project_dir: Path, package_name: str) -> Optional[Path]:
    parts = package_name.split("/") if package_name.startswith("@") else [package_name]
    cursor = project_dir.resolve()
    while True:
        candidate = cursor.joinpath("node_modules", *parts, "package.json")
        if candidate.is_file():
            return candidate
        if cursor.parent == cursor:
            return None
        cursor = cursor.parent


def observed_resolved_assignment(
    project_dir: Path,
    assignment: Mapping[str, str],
    *,
    remove_packages: Iterable[str] = (),
) -> Dict[str, str]:
    """Observe the full direct assignment actually installed by the package manager."""
    try:
        manifest = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ObservedResolutionError(
            f"OBSERVED_RESOLVED_ASSIGNMENT_MANIFEST_INVALID: {exc}"
        ) from exc

    removals = {str(name) for name in remove_packages}
    observed: Dict[str, str] = {}
    for name, target in sorted((str(k), str(v)) for k, v in assignment.items()):
        if name in removals:
            observed[name] = OBSERVED_REMOVED
            continue

        declared_sections = [
            section
            for section in DIRECT_DEPENDENCY_SECTIONS
            if isinstance(manifest.get(section), dict)
            and name in manifest[section]
        ]
        if not declared_sections:
            raise ObservedResolutionError(
                f"OBSERVED_RESOLVED_ASSIGNMENT_UNDECLARED: {name}@{target}"
            )

        if set(declared_sections) == {"peerDependencies"}:
            observed[name] = OBSERVED_PEER_ONLY
            continue

        package_json = _installed_package_json_path(project_dir, name)
        optional_only = (
            "optionalDependencies" in declared_sections
            and set(declared_sections).issubset(
                {"optionalDependencies", "peerDependencies"}
            )
        )
        if package_json is None:
            if optional_only:
                observed[name] = OBSERVED_OPTIONAL_NOT_INSTALLED
                continue
            raise ObservedResolutionError(
                f"OBSERVED_RESOLVED_ASSIGNMENT_MISSING: {name}@{target}"
            )

        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            version = str(package.get("version") or "").strip()
        except (OSError, ValueError, TypeError) as exc:
            raise ObservedResolutionError(
                f"OBSERVED_RESOLVED_ASSIGNMENT_INVALID: {name}: {exc}"
            ) from exc
        if version != target:
            raise ObservedResolutionError(
                f"OBSERVED_RESOLVED_ASSIGNMENT_DRIFT: "
                f"{name} expected={target} observed={version or '<missing-version>'}"
            )
        observed[name] = version
    return dict(sorted(observed.items()))


def observed_resolved_hash(observed: Mapping[str, str]) -> str:
    payload = json.dumps(
        dict(sorted((str(k), str(v)) for k, v in observed.items())),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _classify_install_failure(output: str) -> str:
    if INFRA_PATTERNS.search(output):
        return "infrastructure"
    if DEPENDENCY_PATTERNS.search(output):
        return "dependency"
    # Learning a hard package-version nogood from an unclassified resolver
    # failure is unsound: auth/proxy/cache/PM-internal failures can look like a
    # deterministic non-zero exit without being dependency constraints.
    return "unknown"


def verify_assignment(
    project_dir: Path,
    assignment: Mapping[str, str],
    *,
    config: BaselineVerifyConfig,
    run_project_checks: bool = False,
    remove_packages: Iterable[str] = (),
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "assignment verification",
    proof_identity: Optional[VerificationProofIdentity] = None,
    _allow_prepared_fastpath: bool = True,
) -> BaselineVerifyResult:
    """Materialize one exact direct-dependency assignment in an isolated clone."""
    project_dir = project_dir.resolve()
    attempt_started = time.monotonic()
    attempt_deadline = attempt_started + config.attempt_timeout_seconds
    base_env = dict(os.environ)
    remove_packages = tuple(sorted({str(item) for item in remove_packages}))
    telemetry_path = Path(config.telemetry_path).resolve() if config.telemetry_path else None
    assignment_hash = assignment_fingerprint(assignment)

    def event(name: str, **fields: object) -> None:
        payload = {
            "projectPath": str(project_dir),
            "label": progress_label,
            "assignment": assignment_hash,
            "elapsedMs": int((time.monotonic() - attempt_started) * 1000),
            **fields,
        }
        if proof_identity is not None:
            payload.update(proof_identity.event_fields())
        emit_verification_event(telemetry_path, name, **payload)

    event(
        "verify.attempt.start",
        runProjectChecks=run_project_checks,
        projectChecks=config.project_checks,
        commands=list(config.commands),
    )

    def phase_timeout() -> int:
        remaining = int(attempt_deadline - time.monotonic())
        if remaining <= 0:
            raise subprocess.TimeoutExpired(progress_label, config.attempt_timeout_seconds)
        return max(1, min(config.timeout_seconds, remaining))

    def snapshot_copy_timeout() -> int:
        remaining = int(attempt_deadline - time.monotonic())
        if remaining <= 0:
            raise subprocess.TimeoutExpired(progress_label, config.attempt_timeout_seconds)
        return max(1, min(config.snapshot_copy_timeout_seconds, remaining))

    def phase_progress(phase: str) -> ProgressCallback:
        return lambda message: _emit_progress(progress, f"{progress_label}: {phase}: {message}")

    _emit_progress(progress, f"{progress_label}: started; attemptHardTimeout={config.attempt_timeout_seconds}s")
    _emit_progress(progress, f"{progress_label}: verification substrate: {workspace_backend_summary()}")
    temp_root = Path(tempfile.mkdtemp(prefix="dependency-flow-baseline-verify-"))
    try:
        workspace_root = temp_root / "repo"
        workspace_started = time.monotonic()
        event("verify.workspace.start")
        try:
            workspace_project = _materialize_workspace(project_dir, workspace_root)
            changed = _apply_assignment(workspace_project, assignment, remove_packages=remove_packages)
            event(
                "verify.workspace.finish",
                durationMs=int((time.monotonic() - workspace_started) * 1000),
                changedPackages=len(changed),
            )
        except AssignmentMaterializationError as exc:
            # A solver/planner assignment that cannot be represented by the
            # project manifest is not package-manager evidence and must never
            # pass vacuously or become a learned dependency nogood.
            return BaselineVerifyResult(False, "unknown", str(exc))
        except Exception as exc:  # filesystem/git setup is infrastructure, never a nogood
            return BaselineVerifyResult(False, "infrastructure", f"workspace preparation failed: {exc}")

        manager = detect_package_manager(workspace_project)
        executable = resolve_executable(manager)
        if not executable:
            return BaselineVerifyResult(
                False,
                "infrastructure",
                f"INFRA_PACKAGE_MANAGER_NOT_FOUND: {manager} is not available in PATH or known Windows shim locations",
                command=manager,
                workspace=str(workspace_project),
            )

        if proof_identity is None:
            proof_identity = build_verification_proof_identity(
                project_dir,
                assignment=assignment,
                remove_packages=tuple(sorted(str(item) for item in remove_packages)),
                manager=manager,
                manager_executable=executable,
                registry=config.registry,
                project_checks=config.project_checks if run_project_checks else "off",
                commands=config.commands if run_project_checks else (),
                environment=base_env,
            )
        event("proof.identity", manager=manager)

        def fixed_source_identity_gate(stage: str) -> Optional[BaselineVerifyResult]:
            try:
                # The complete identity is rebound to the original checkout.
                # Local/workspace paths are intentionally not re-identified
                # from the temporary assignment clone.
                current_source_fixed_key = fixed_resolver_input_fingerprint(
                    project_dir, manager=manager
                )
                expected_remote_key = remote_fixed_resolver_input_fingerprint(
                    project_dir, manager=manager
                )
                observed_remote_key = remote_fixed_resolver_input_fingerprint(
                    workspace_project, manager=manager
                )
            except (SourceIdentityUnavailable, OSError, ValueError) as exc:
                event(
                    "verify.fixed-source.identity-unavailable",
                    stage=stage,
                    detail=str(exc),
                )
                return BaselineVerifyResult(
                    False,
                    "unknown",
                    f"FIXED_SOURCE_IDENTITY_UNAVAILABLE_DURING_{stage.upper()}: {exc}",
                    workspace=str(workspace_project),
                )
            expected_fixed_key = proof_identity.fixed_resolver_inputs_key
            if (
                current_source_fixed_key == expected_fixed_key
                and observed_remote_key == expected_remote_key
            ):
                return None
            event(
                "verify.fixed-source.identity-drift",
                stage=stage,
                expectedFixedResolverInputsKey=expected_fixed_key,
                observedFixedResolverInputsKey=current_source_fixed_key,
                expectedRemoteFixedResolverInputsKey=expected_remote_key,
                observedRemoteFixedResolverInputsKey=observed_remote_key,
            )
            return BaselineVerifyResult(
                False,
                "unknown",
                "FIXED_SOURCE_IDENTITY_DRIFT_DURING_" + stage.upper() + ": "
                + f"expected={expected_fixed_key} observed={current_source_fixed_key} "
                + f"remoteExpected={expected_remote_key} remoteObserved={observed_remote_key}",
                workspace=str(workspace_project),
            )

        proof_store = VerificationProofStore(
            Path(config.proof_cache_dir) if config.proof_cache_dir else None
        )

        resolved_state: Optional[ResolvedDependencyState] = None

        def publish_pass(
            proof_type: str,
            key: str,
            observed_versions: Mapping[str, str],
            observed_hash: str,
            *,
            extra_metadata: Optional[Mapping[str, object]] = None,
        ) -> None:
            metadata: Dict[str, object] = {
                "observedResolvedVersions": dict(sorted(observed_versions.items())),
                "observedResolvedHash": observed_hash,
            }
            if extra_metadata:
                metadata.update(dict(extra_metadata))
            if proof_store.publish_pass(
                proof_type, key, proof_identity, metadata=metadata
            ):
                event(
                    "proof.cache.publish",
                    proofType=proof_type,
                    cacheKey=key,
                    observedResolvedHash=observed_hash,
                    resolvedStateKey=proof_identity.resolved_state_key,
                )

        install = install_args(manager, ignore_scripts=True, frozen=False)
        argv, _ = _command_prefix(executable, install)
        install_env = {
            "CI": "1",
            "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
            "YARN_ENABLE_SCRIPTS": "false",
            "npm_config_ignore_scripts": "true",
        }
        install_env.update(_package_manager_cache_environment(config, manager))
        resolver_record = (
            proof_store.lookup_pass("resolver", config.reuse_resolver_proof_key)
            if config.reuse_resolver_proof_key
            else None
        )
        resolver_reused = bool(
            resolver_record is not None
            and config.reuse_resolver_proof_key == proof_identity.resolver_input_key
        )
        observed_versions: Dict[str, str] = {}
        observed_hash = ""

        if resolver_reused and resolver_record is not None:
            resolved_state = load_resolved_dependency_state(
                resolver_record.metadata,
                proof_cache_dir=proof_store.root,
            )
            if resolved_state is None:
                resolver_reused = False
                resolver_record = None
                event(
                    "proof.cache.rejected",
                    proofType="resolver",
                    cacheKey=proof_identity.resolver_input_key,
                    reason="resolved-state-artifact-invalid-or-missing",
                )
            else:
                observed_versions = {
                    str(name): str(version)
                    for name, version in dict(
                        resolver_record.metadata.get("observedResolvedVersions") or {}
                    ).items()
                }
                observed_hash = str(
                    resolver_record.metadata.get("observedResolvedHash") or ""
                )
                proof_identity = bind_resolved_state_identity(
                    proof_identity,
                    resolved_state.key,
                    project_checks=config.project_checks if run_project_checks else "off",
                    commands=config.commands if run_project_checks else (),
                )
                try:
                    restore_resolved_dependency_state(workspace_project, resolved_state)
                except ResolvedDependencyStateError as exc:
                    return BaselineVerifyResult(
                        False, "unknown", str(exc), workspace=str(workspace_project)
                    )

        if resolver_reused:
            event(
                "proof.cache.hit",
                proofType="resolver",
                cacheKey=proof_identity.resolver_input_key,
                reuseMode="restore-exact-resolved-state-then-frozen-lifecycle",
            )
            _emit_progress(
                progress,
                f"{progress_label}: ResolverProof HIT; exact proven lockfile restored. "
                "Fresh lifecycle remains frozen and authoritative.",
            )
        else:
            resolver_started = time.monotonic()
            event("verify.resolver.start", command=" ".join(argv))
            try:
                result = _run(
                    argv,
                    workspace_project,
                    timeout_seconds=phase_timeout(),
                    env=install_env,
                    base_env=base_env,
                    progress=phase_progress("resolver-install"),
                    progress_label="package-manager resolver install",
                    progress_interval_seconds=config.progress_interval_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                return BaselineVerifyResult(False, "infrastructure", f"package-manager verification timed out: {exc}", command=" ".join(argv))
            except OSError as exc:
                return BaselineVerifyResult(False, "infrastructure", f"package-manager launch failed: {exc}", command=" ".join(argv))

            output = result.stdout or ""
            kind = "passed" if result.returncode == 0 else _classify_install_failure(output)
            event(
                "verify.resolver.finish",
                durationMs=int((time.monotonic() - resolver_started) * 1000),
                exitCode=result.returncode,
                outcome=kind,
            )
            if result.returncode != 0:
                tail = "\n".join(output.splitlines()[-80:])
                return BaselineVerifyResult(
                    False,
                    kind,
                    f"{manager} resolver preflight failed for {len(changed)} changed direct package(s)",
                    command=" ".join(argv),
                    output=tail,
                    workspace=str(workspace_project),
                )

        fixed_identity_result = fixed_source_identity_gate("resolver")
        if fixed_identity_result is not None:
            return fixed_identity_result

        try:
            materialized_manifest = json.loads((workspace_project / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return BaselineVerifyResult(False, "unknown", f"ASSIGNMENT_MANIFEST_UNREADABLE: {exc}")
        removal_names = set(remove_packages)
        for expected_name, expected_version in sorted(assignment.items()):
            declared_versions = []
            for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                values = materialized_manifest.get(section)
                if isinstance(values, dict) and expected_name in values:
                    declared_versions.append(str(values[expected_name]))
            if expected_name in removal_names:
                if declared_versions:
                    return BaselineVerifyResult(
                        False,
                        "unknown",
                        f"ASSIGNMENT_MANIFEST_DRIFT: {expected_name} was proven for removal but remains declared as {declared_versions!r}",
                        workspace=str(workspace_project),
                    )
                continue
            if not declared_versions or any(value != expected_version for value in declared_versions):
                return BaselineVerifyResult(
                    False,
                    "unknown",
                    f"ASSIGNMENT_MANIFEST_DRIFT: {expected_name} expected {expected_version!r}, "
                    f"observed {declared_versions!r}",
                    workspace=str(workspace_project),
                )

        if not resolver_reused:
            try:
                observed_versions = observed_resolved_assignment(
                    workspace_project,
                    assignment,
                    remove_packages=remove_packages,
                )
                observed_hash = observed_resolved_hash(observed_versions)
            except ObservedResolutionError as exc:
                event("verify.resolver.observed-drift", outcome="unknown", detail=str(exc))
                return BaselineVerifyResult(
                    False, "unknown", str(exc), workspace=str(workspace_project)
                )
            try:
                resolved_state = capture_resolved_dependency_state(
                    workspace_project,
                    manager=manager,
                    resolver_input_key=proof_identity.resolver_input_key,
                    observed_resolved_hash=observed_hash,
                    proof_cache_dir=proof_store.root,
                )
            except ResolvedDependencyStateError as exc:
                return BaselineVerifyResult(
                    False,
                    "unknown",
                    str(exc),
                    workspace=str(workspace_project),
                )
            proof_identity = bind_resolved_state_identity(
                proof_identity,
                resolved_state.key,
                project_checks=config.project_checks if run_project_checks else "off",
                commands=config.commands if run_project_checks else (),
            )
            publish_pass(
                "resolver",
                proof_identity.resolver_input_key,
                observed_versions,
                observed_hash,
                extra_metadata=resolved_state_metadata(resolved_state),
            )
            event(
                "verify.resolved-state.captured",
                resolvedStateKey=resolved_state.key,
                lockfilePath=resolved_state.lockfile_path,
                lockfileHash=resolved_state.lockfile_hash,
            )
        elif not observed_hash or resolved_state is None:
            return BaselineVerifyResult(
                False,
                "unknown",
                "RESOLVED_STATE_PROOF_MISSING: reused ResolverProof has no exact resolved state",
                workspace=str(workspace_project),
            )

        if run_project_checks and config.project_checks != "off" and config.commands:
            # The integrity manifest exists solely for the junction-backed clone
            # optimization. Private full-copy project clones do not consume it.
            preparation_fastpath_enabled = (
                _allow_prepared_fastpath
                and _prepared_snapshot_fastpath_worth_sealing(
                    config.commands, project_dir
                )
            )
            snapshot = _lookup_prepared_workspace_snapshot(
                proof_identity.preparation_proof_key, project_dir
            )
            if snapshot is not None and snapshot.observed_resolved_hash != observed_hash:
                event(
                    "verify.preparation.snapshot-rejected",
                    reason="observed-resolved-hash-mismatch",
                    snapshotObservedHash=snapshot.observed_resolved_hash,
                    resolverObservedHash=observed_hash,
                )
                _evict_prepared_workspace_snapshot(
                    proof_identity.preparation_proof_key, project_dir
                )
                snapshot = None

            if snapshot is not None:
                event(
                    "verify.preparation.snapshot-hit",
                    preparationProofKey=proof_identity.preparation_proof_key,
                    observedResolvedHash=observed_hash,
                )
                _emit_progress(
                    progress,
                    f"{progress_label}: lifecycle preparation snapshot HIT; fresh project-check clones will be materialized from the sealed tree",
                )
                observed_versions = dict(snapshot.observed_resolved_versions)
            else:
                full_install = install_args(manager, ignore_scripts=False, frozen=True)
                full_argv, _ = _command_prefix(executable, full_install)
                lifecycle_env = {
                    "CI": "1",
                    "YARN_ENABLE_IMMUTABLE_INSTALLS": "true",
                    "YARN_ENABLE_SCRIPTS": "true",
                    "npm_config_ignore_scripts": "false",
                }
                lifecycle_env.update(_package_manager_cache_environment(config, manager))
                preparation_started = time.monotonic()
                event("verify.preparation.start", command=" ".join(full_argv))
                try:
                    full_result = _run(
                        full_argv, workspace_project,
                        timeout_seconds=phase_timeout(), env=lifecycle_env,
                        base_env=base_env, progress=phase_progress("lifecycle-install"),
                        progress_label="package-manager lifecycle install",
                        progress_interval_seconds=config.progress_interval_seconds,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    return BaselineVerifyResult(False, "infrastructure", f"project-preflight install failed: {exc}", command=" ".join(full_argv))
                preparation_classified = "passed" if full_result.returncode == 0 else _classify_install_failure(full_result.stdout or "")
                event(
                    "verify.preparation.finish",
                    durationMs=int((time.monotonic() - preparation_started) * 1000),
                    exitCode=full_result.returncode,
                    outcome=preparation_classified,
                )
                if full_result.returncode != 0:
                    tail = "\n".join((full_result.stdout or "").splitlines()[-80:])
                    common = dict(
                        command=" ".join(full_argv),
                        output=tail,
                        workspace=str(workspace_project),
                        observed_resolved_versions=observed_versions,
                        observed_resolved_hash=observed_hash,
                        resolved_state_key=resolved_state.key if resolved_state is not None else "",
                        resolved_lockfile_hash=resolved_state.lockfile_hash if resolved_state is not None else "",
                    )
                    if preparation_classified in {"infrastructure", "dependency"}:
                        return BaselineVerifyResult(
                            False, preparation_classified,
                            "assignment resolves without lifecycle scripts, but lifecycle install failed",
                            **common,
                        )
                    return BaselineVerifyResult(
                        False, "preparation",
                        "assignment resolves, but lifecycle/preparation failed deterministically",
                        **common,
                    )

                fixed_identity_result = fixed_source_identity_gate("lifecycle")
                if fixed_identity_result is not None:
                    return fixed_identity_result

                try:
                    lifecycle_observed = observed_resolved_assignment(
                        workspace_project, assignment, remove_packages=remove_packages,
                    )
                    lifecycle_observed_hash = observed_resolved_hash(lifecycle_observed)
                except ObservedResolutionError as exc:
                    event("verify.preparation.observed-drift", outcome="unknown", detail=str(exc))
                    return BaselineVerifyResult(False, "unknown", str(exc), workspace=str(workspace_project))
                if lifecycle_observed_hash != observed_hash:
                    return BaselineVerifyResult(
                        False, "unknown",
                        "OBSERVED_RESOLVED_ASSIGNMENT_DRIFT: lifecycle install changed "
                        f"the proven direct tree resolver={observed_hash} lifecycle={lifecycle_observed_hash}",
                        workspace=str(workspace_project),
                    )
                observed_versions = lifecycle_observed
                observed_hash = lifecycle_observed_hash
                try:
                    assert resolved_state is not None
                    assert_resolved_dependency_state(workspace_project, resolved_state)
                except (AssertionError, ResolvedDependencyStateError) as exc:
                    return BaselineVerifyResult(
                        False, "unknown",
                        f"RESOLVED_STATE_PREPARATION_DRIFT: {exc}",
                        workspace=str(workspace_project),
                    )
                publish_pass(
                    "preparation",
                    proof_identity.preparation_proof_key,
                    observed_versions,
                    observed_hash,
                    extra_metadata=resolved_state_metadata(resolved_state),
                )

                normalized = clean_ephemeral_verification_caches(workspace_project)
                if normalized:
                    event("verify.preparation.snapshot-normalized", removedCaches=list(normalized))
                snapshot_started = time.monotonic()
                snapshot_timeout = snapshot_copy_timeout()
                event(
                    "verify.preparation.snapshot-publish.start",
                    preparationProofKey=proof_identity.preparation_proof_key,
                    hardTimeoutSeconds=snapshot_timeout,
                )
                _emit_progress(
                    progress,
                    f"{progress_label}: snapshot publish started; hardTimeout={snapshot_timeout}s",
                )
                try:
                    snapshot = _publish_prepared_workspace_snapshot(
                        workspace_root, workspace_project,
                        key=proof_identity.preparation_proof_key,
                        observed_versions=observed_versions,
                        observed_hash=observed_hash,
                        source_project=project_dir,
                        seal_dependency_integrity=preparation_fastpath_enabled,
                        progress=progress,
                        progress_label=f"{progress_label}: snapshot-publish",
                        timeout_seconds=snapshot_timeout,
                        progress_interval_seconds=config.progress_interval_seconds,
                    )
                except Exception as exc:
                    return BaselineVerifyResult(False, "infrastructure", f"PREPARED_SNAPSHOT_PUBLISH_FAILED: {exc}", workspace=str(workspace_project))
                snapshot_duration_ms = int((time.monotonic() - snapshot_started) * 1000)
                event(
                    "verify.preparation.snapshot-publish",
                    durationMs=snapshot_duration_ms,
                    preparationProofKey=proof_identity.preparation_proof_key,
                )
                _emit_progress(
                    progress,
                    f"{progress_label}: snapshot publish PASS; elapsed={snapshot_duration_ms // 1000}s; mode={snapshot.storage_mode}",
                )
                workspace_project = snapshot.workspace_root / snapshot.project_relative

            if snapshot is None:
                return BaselineVerifyResult(False, "infrastructure", "PREPARED_SNAPSHOT_UNAVAILABLE: project checks require a sealed preparation tree")

            project_failures: List[BaselineProjectFailure] = []
            for command_index, command in enumerate(config.commands, start=1):
                command_root = temp_root / f"project-check-{command_index:02d}"
                clone_started = time.monotonic()
                clone_timeout = snapshot_copy_timeout()
                event(
                    "verify.project-check.clone.start",
                    command=command,
                    check=command_index,
                    checks=len(config.commands),
                    preparationProofKey=proof_identity.preparation_proof_key,
                    hardTimeoutSeconds=clone_timeout,
                )
                _emit_progress(
                    progress,
                    f"{progress_label}: project clone {command_index}/{len(config.commands)} started; "
                    f"hardTimeout={clone_timeout}s",
                )
                try:
                    command_fastpath_allowed = (
                        _allow_prepared_fastpath
                        and bool(snapshot.dependency_integrity)
                        and _prepared_command_fastpath_allowed(
                            project_dir, command
                        )
                    )
                    if (
                        _allow_prepared_fastpath
                        and bool(snapshot.dependency_integrity)
                        and not command_fastpath_allowed
                    ):
                        _emit_progress(
                            progress,
                            f"{progress_label}: NTFS fast clone command quarantine "
                            f"for {command}; using proof-safe private copy",
                        )
                    command_project = _materialize_prepared_workspace_snapshot(
                        snapshot,
                        command_root,
                        allow_fastpath=command_fastpath_allowed,
                        progress=progress,
                        progress_label=f"{progress_label}: project-clone:{command_index}/{len(config.commands)}",
                        timeout_seconds=clone_timeout,
                        progress_interval_seconds=config.progress_interval_seconds,
                    )
                except Exception as exc:
                    return BaselineVerifyResult(False, "infrastructure", f"PREPARED_SNAPSHOT_CLONE_FAILED: {command}: {exc}", command=command)
                clone_isolation = (
                    "ntfs-junction-guarded"
                    if guarded_clone_is_active(command_root)
                    else "fresh-prepared-snapshot-clone"
                )
                clone_duration_ms = int((time.monotonic() - clone_started) * 1000)
                event(
                    "verify.project-check.clone.finish",
                    command=command,
                    check=command_index,
                    checks=len(config.commands),
                    durationMs=clone_duration_ms,
                    preparationProofKey=proof_identity.preparation_proof_key,
                    isolation=clone_isolation,
                )
                _emit_progress(
                    progress,
                    f"{progress_label}: project clone {command_index}/{len(config.commands)} PASS; "
                    f"elapsed={clone_duration_ms // 1000}s; isolation={clone_isolation}",
                )
                try:
                    removed_caches = clean_ephemeral_verification_caches(command_project)
                    if removed_caches:
                        _emit_progress(progress, f"{progress_label}: normalized transient caches before {command}: {', '.join(removed_caches)}")
                    _emit_progress(progress, f"{progress_label}: project check {command_index}/{len(config.commands)} started: {command}")
                    check_started = time.monotonic()
                    event(
                        "verify.project-check.start",
                        command=command,
                        check=command_index,
                        checks=len(config.commands),
                        isolation=clone_isolation,
                    )
                    if os.name == "nt":
                        shell_argv: Sequence[str] = [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", command]
                    else:
                        shell_argv = ["/bin/sh", "-lc", command]
                    try:
                        check_result = _run(
                            shell_argv,
                            command_project,
                            timeout_seconds=phase_timeout(),
                            base_env=base_env,
                            progress=phase_progress(f"project-check:{command}"),
                            progress_label=command,
                            progress_interval_seconds=config.progress_interval_seconds,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        if clone_isolation == "ntfs-junction-guarded":
                            cleanup_guarded_clone(command_root)
                            _evict_prepared_workspace_snapshot(
                                proof_identity.preparation_proof_key, project_dir
                            )
                        return BaselineVerifyResult(False, "infrastructure", f"project check launch failed: {exc}", command=command)

                    guard_result = stop_guarded_clone(command_root)
                    if (
                        guard_result.errors
                        or guard_result.mutations
                        or guard_result.notification_only
                    ):
                        # Any notification means this optimization is no longer
                        # authoritative for the preparation identity. Even an
                        # integrity-matched final state may have been transiently
                        # mutated while another consumer was reading it.
                        cleanup_guarded_clone(command_root)
                        # This is performance state only. The command is still
                        # freshly executed in an isolated private clone.
                        _disable_prepared_command_fastpath(project_dir, command)
                        _disable_prepared_snapshot_fastpath(
                            proof_identity.preparation_proof_key, project_dir
                        )
                        _evict_prepared_workspace_snapshot(
                            proof_identity.preparation_proof_key, project_dir
                        )
                        rejection_reason = (
                            "watcher-error"
                            if guard_result.errors
                            else (
                                "confirmed-content-mutation"
                                if guard_result.mutations
                                else "integrity-matched-notification"
                            )
                        )
                        event(
                            "verify.project-check.fastpath-rejected",
                            command=command,
                            check=command_index,
                            checks=len(config.commands),
                            reason=rejection_reason,
                            watcherErrors=list(guard_result.errors),
                            confirmedMutations=list(guard_result.mutations),
                            integrityMatchedNotifications=list(
                                guard_result.notification_only
                            ),
                        )
                        _emit_progress(
                            progress,
                            f"{progress_label}: NTFS fast clone rejected during {command} "
                            f"({rejection_reason}); rebuilding trusted preparation and "
                            "retrying with proof-safe full-copy project clones",
                        )
                        # The failed fast-path check is infrastructure evidence,
                        # never dependency evidence. Re-enter with the same proof
                        # identity; ResolverProof may be reused, while lifecycle
                        # preparation is rebuilt because the shared snapshot was
                        # quarantined.
                        return _retry_assignment_without_prepared_fastpath(
                            project_dir,
                            assignment,
                            config=config,
                            run_project_checks=run_project_checks,
                            remove_packages=remove_packages,
                            progress=progress,
                            progress_label=progress_label,
                            proof_identity=proof_identity,
                        )

                    # Allowed root cache writes are cache-only. Remove them
                    # from the sealed dependency base before any later check.
                    prepared_project = snapshot.workspace_root / snapshot.project_relative
                    normalized_shared = clean_ephemeral_verification_caches(prepared_project)
                    if normalized_shared:
                        event(
                            "verify.project-check.shared-cache-normalized",
                            command=command,
                            removedCaches=list(normalized_shared),
                        )

                    try:
                        assert resolved_state is not None
                        assert_resolved_dependency_state(command_project, resolved_state)
                    except (AssertionError, ResolvedDependencyStateError) as exc:
                        _evict_prepared_workspace_snapshot(
                            proof_identity.preparation_proof_key, project_dir
                        )
                        return BaselineVerifyResult(
                            False,
                            "unknown",
                            f"RESOLVED_STATE_PROJECT_DRIFT: {command}: {exc}",
                            command=command,
                            workspace=str(command_project),
                        )

                    try:
                        check_observed = observed_resolved_assignment(
                            command_project, assignment, remove_packages=remove_packages,
                        )
                        check_observed_hash = observed_resolved_hash(check_observed)
                    except ObservedResolutionError as exc:
                        return BaselineVerifyResult(False, "unknown", str(exc), command=command, workspace=str(command_project))
                    if check_observed_hash != observed_hash:
                        return BaselineVerifyResult(
                            False, "unknown",
                            "OBSERVED_RESOLVED_ASSIGNMENT_DRIFT: project check "
                            f"{command} mutated the proven direct dependency tree",
                            command=command, workspace=str(command_project),
                        )

                    event(
                        "verify.project-check.finish",
                        command=command,
                        check=command_index,
                        checks=len(config.commands),
                        durationMs=int((time.monotonic() - check_started) * 1000),
                        exitCode=check_result.returncode,
                        outcome="passed" if check_result.returncode == 0 else "failed",
                        isolation=clone_isolation,
                    )
                    if check_result.returncode == 0:
                        _emit_progress(progress, f"{progress_label}: project check {command_index}/{len(config.commands)} PASS: {command}")
                        continue
                    tail = "\n".join((check_result.stdout or "").splitlines()[-80:])
                    if INFRA_PATTERNS.search(tail):
                        return BaselineVerifyResult(False, "infrastructure", f"project preflight infrastructure failure: {command}", command=command, output=tail, workspace=str(command_project))
                    project_failures.append(BaselineProjectFailure(command, check_result.returncode, tail))
                    _emit_progress(progress, f"{progress_label}: project check {command_index}/{len(config.commands)} RED exit={check_result.returncode}: {command}")
                finally:
                    cleanup_guarded_clone(command_root)
                    shutil.rmtree(command_root, ignore_errors=True)

            if project_failures:
                summary_commands = ", ".join(item.command for item in project_failures)
                output = "\n\n".join(
                    f"=== {item.command} (exit {item.exit_code}) ===\n{item.output}"
                    for item in project_failures
                )
                first = project_failures[0]
                return BaselineVerifyResult(
                    False, "project", f"project preflight failed: {summary_commands}",
                    command=first.command, output=output[-16000:], workspace=str(workspace_project),
                    project_failures=tuple(project_failures),
                    observed_resolved_versions=observed_versions,
                    observed_resolved_hash=observed_hash,
                    resolved_state_key=resolved_state.key if resolved_state is not None else "",
                    resolved_lockfile_hash=resolved_state.lockfile_hash if resolved_state is not None else "",
                )

            assert resolved_state is not None
            publish_pass(
                "project", proof_identity.project_proof_key, observed_versions, observed_hash,
                extra_metadata=resolved_state_metadata(resolved_state),
            )

        _emit_progress(progress, f"{progress_label}: completed; elapsed={int(time.monotonic() - attempt_started)}s")
        event("verify.attempt.finish", outcome="passed")
        return BaselineVerifyResult(
            True, "passed", f"resolver preflight passed for {len(changed)} changed direct package(s)",
            command=" ".join(argv), workspace=str(workspace_project),
            observed_resolved_versions=observed_versions,
            observed_resolved_hash=observed_hash,
            resolved_state_key=resolved_state.key if resolved_state is not None else "",
            resolved_lockfile_hash=resolved_state.lockfile_hash if resolved_state is not None else "",
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def assignment_fingerprint(assignment: Mapping[str, str]) -> str:
    payload = json.dumps(dict(sorted(assignment.items())), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_verify_assignment_uncached = verify_assignment


def _retry_assignment_without_prepared_fastpath(
    project_dir: Path,
    assignment: Mapping[str, str],
    *,
    config: BaselineVerifyConfig,
    run_project_checks: bool,
    remove_packages: Iterable[str],
    progress: Optional[ProgressCallback],
    progress_label: str,
    proof_identity: VerificationProofIdentity,
) -> BaselineVerifyResult:
    # Project checks run only after ResolverProof + ResolvedState have been
    # captured. Preserve that exact proof identity and restore the exact
    # ResolvedState on retry instead of needlessly resolving from the network
    # again. The quarantined prepared snapshot is still rebuilt, and every
    # project clone is forced onto the private full-copy path.
    retry_config = dataclasses.replace(
        config,
        reuse_resolver_proof_key=proof_identity.resolver_input_key,
    )
    return _verify_assignment_uncached(
        project_dir,
        assignment,
        config=retry_config,
        run_project_checks=run_project_checks,
        remove_packages=remove_packages,
        progress=progress,
        progress_label=progress_label,
        proof_identity=proof_identity,
        _allow_prepared_fastpath=False,
    )


def verify_assignment(
    project_dir: Path,
    assignment: Mapping[str, str],
    *,
    config: BaselineVerifyConfig,
    run_project_checks: bool = False,
    remove_packages: Iterable[str] = (),
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "assignment verification",
) -> BaselineVerifyResult:
    """Cache-aware proof entry point with ResolvedState-bound project proofs."""
    project_dir = project_dir.resolve()
    proof_store = VerificationProofStore(
        Path(config.proof_cache_dir) if config.proof_cache_dir else None
    )
    if proof_store.root is None:
        return _verify_assignment_uncached(
            project_dir,
            assignment,
            config=config,
            run_project_checks=run_project_checks,
            remove_packages=remove_packages,
            progress=progress,
            progress_label=progress_label,
        )

    manager = detect_package_manager(project_dir)
    executable = resolve_executable(manager)
    if not executable:
        return _verify_assignment_uncached(
            project_dir,
            assignment,
            config=config,
            run_project_checks=run_project_checks,
            remove_packages=remove_packages,
            progress=progress,
            progress_label=progress_label,
        )

    environment = dict(os.environ)
    identity = build_verification_proof_identity(
        project_dir,
        assignment=assignment,
        remove_packages=tuple(sorted(str(item) for item in remove_packages)),
        manager=manager,
        manager_executable=executable,
        registry=config.registry,
        project_checks=config.project_checks if run_project_checks else "off",
        commands=config.commands if run_project_checks else (),
        environment=environment,
    )
    telemetry_path = Path(config.telemetry_path).resolve() if config.telemetry_path else None

    def cache_event(name: str, proof_type: str, key: str, **extra: object) -> None:
        emit_verification_event(
            telemetry_path,
            name,
            projectPath=str(project_dir),
            label=progress_label,
            proofType=proof_type,
            cacheKey=key,
            **identity.event_fields(),
            **extra,
        )

    wants_project_proof = bool(
        run_project_checks and config.project_checks != "off" and config.commands
    )

    cache_event("proof.cache.lookup", "resolver", identity.resolver_input_key)
    resolver_record = proof_store.lookup_pass("resolver", identity.resolver_input_key)
    resolved_state = (
        load_resolved_dependency_state(
            resolver_record.metadata,
            proof_cache_dir=proof_store.root,
        )
        if resolver_record is not None
        else None
    )
    resolver_hit = resolver_record is not None and resolved_state is not None
    if resolver_record is not None and resolved_state is None:
        cache_event(
            "proof.cache.rejected",
            "resolver",
            identity.resolver_input_key,
            reason="resolved-state-artifact-invalid-or-missing",
        )
    elif resolver_hit and resolved_state is not None:
        identity = bind_resolved_state_identity(
            identity,
            resolved_state.key,
            project_checks=config.project_checks if run_project_checks else "off",
            commands=config.commands if run_project_checks else (),
        )
        cache_event(
            "proof.cache.hit",
            "resolver",
            identity.resolver_input_key,
        )
    else:
        cache_event("proof.cache.miss", "resolver", identity.resolver_input_key)

    if wants_project_proof and resolver_hit:
        cache_event("proof.cache.lookup", "project", identity.project_proof_key)
        project_record = proof_store.lookup_pass("project", identity.project_proof_key)
        if project_record is not None:
            cache_event("proof.cache.hit", "project", identity.project_proof_key)
            _emit_progress(
                progress,
                f"{progress_label}: exact ProjectProof cache HIT for ResolvedState "
                f"{identity.resolved_state_key[:12]}; install/lifecycle/checks skipped",
            )
            assert resolved_state is not None
            return BaselineVerifyResult(
                True,
                "passed",
                "exact ProjectProof cache hit",
                command=f"proof-cache:{identity.project_proof_key[:12]}",
                observed_resolved_versions={
                    str(name): str(version)
                    for name, version in dict(
                        project_record.metadata.get("observedResolvedVersions") or {}
                    ).items()
                },
                observed_resolved_hash=str(
                    project_record.metadata.get("observedResolvedHash") or ""
                ),
                resolved_state_key=resolved_state.key,
                resolved_lockfile_hash=resolved_state.lockfile_hash,
            )
        cache_event("proof.cache.miss", "project", identity.project_proof_key)

    if resolver_hit and not wants_project_proof:
        _emit_progress(
            progress,
            f"{progress_label}: exact ResolverProof + ResolvedState cache HIT; resolver verification skipped",
        )
        assert resolver_record is not None and resolved_state is not None
        return BaselineVerifyResult(
            True,
            "passed",
            "exact ResolverProof cache hit",
            command=f"proof-cache:{identity.resolver_input_key[:12]}",
            observed_resolved_versions={
                str(name): str(version)
                for name, version in dict(
                    resolver_record.metadata.get("observedResolvedVersions") or {}
                ).items()
            },
            observed_resolved_hash=str(
                resolver_record.metadata.get("observedResolvedHash") or ""
            ),
            resolved_state_key=resolved_state.key,
            resolved_lockfile_hash=resolved_state.lockfile_hash,
        )

    effective_config = (
        dataclasses.replace(
            config,
            reuse_resolver_proof_key=identity.resolver_input_key,
        )
        if resolver_hit and wants_project_proof
        else config
    )
    return _verify_assignment_uncached(
        project_dir,
        assignment,
        config=effective_config,
        run_project_checks=run_project_checks,
        remove_packages=remove_packages,
        progress=progress,
        progress_label=progress_label,
        proof_identity=identity,
    )
