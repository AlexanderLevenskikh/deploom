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

import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


from verification_proof import (
    VerificationProofIdentity,
    VerificationProofStore,
    build_verification_proof_identity,
    emit_verification_event,
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

    @property
    def hard_failure(self) -> bool:
        return self.kind in {"dependency", "preparation", "project"}


class AssignmentMaterializationError(RuntimeError):
    """Raised when the solver assignment cannot be represented by package.json."""


INFRA_PATTERNS = re.compile(
    r"(?:ENOENT|not recognized as an internal or external command|command not found|"
    r"ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|socket hang up|"
    r"SELF_SIGNED_CERT|unable to get local issuer|certificate has expired|"
    r"ENOSPC|EPERM|EACCES|HTTP\s+(?:429|500|502|503|504)\b)",
    re.IGNORECASE,
)

DEPENDENCY_PATTERNS = re.compile(
    r"(?:ERESOLVE|unable to resolve dependency tree|could not resolve dependency|"
    r"No matching version found|Couldn't find any versions|YN0060|YN0082|"
    r"peer dependency|conflicting peer dependency|resolution field .* incompatible)",
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
    "src/node_modules/.vite",
    "src/node_modules/.vitest",
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


def install_args(manager: str, *, ignore_scripts: bool) -> List[str]:
    if manager == "yarn":
        # Yarn Classic ignores YARN_ENABLE_SCRIPTS.  The explicit flag is the
        # proof boundary for resolver-only verification; Berry is rejected by
        # lockfile preflight before this verifier is reached.
        args = ["install"]
        if ignore_scripts:
            args.append("--ignore-scripts")
        return args
    if manager == "pnpm":
        args = ["install", "--no-frozen-lockfile"]
        if ignore_scripts:
            args.append("--ignore-scripts")
        return args
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
    """Run a child with heartbeat and a hard process-tree timeout.

    `subprocess.run(timeout=...)` kills only the immediate process on Windows.
    Package-manager shims commonly spawn cmd -> yarn/npm -> node descendants; a
    descendant retaining stdout can keep communicate()/worker shutdown alive
    indefinitely. This runner owns the whole tree and reports liveness while it
    waits.
    """
    merged_env = dict(base_env) if base_env is not None else os.environ.copy()
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})
    started = time.monotonic()
    popen_kwargs = dict(
        cwd=str(cwd), env=merged_env, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False,
    )
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(list(argv), **popen_kwargs)
    interval = max(1, min(int(progress_interval_seconds or 15), int(timeout_seconds)))
    while True:
        elapsed = time.monotonic() - started
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            _emit_progress(progress, f"{progress_label}: HARD_TIMEOUT after {int(elapsed)}s; terminating process tree pid={process.pid}")
            _terminate_process_tree(process)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
            raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
        try:
            stdout, _stderr = process.communicate(timeout=min(interval, remaining))
            return subprocess.CompletedProcess(list(argv), process.returncode or 0, stdout=stdout or "", stderr=None)
        except subprocess.TimeoutExpired:
            _emit_progress(
                progress,
                f"{progress_label}: running; elapsed={int(time.monotonic() - started)}s; hardTimeout={timeout_seconds}s; pid={process.pid}",
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
) -> BaselineVerifyResult:
    """Materialize one exact direct-dependency assignment in an isolated clone."""
    project_dir = project_dir.resolve()
    attempt_started = time.monotonic()
    attempt_deadline = attempt_started + config.attempt_timeout_seconds
    base_env = dict(os.environ)
    telemetry_path = Path(config.telemetry_path).resolve() if config.telemetry_path else None
    assignment_hash = assignment_fingerprint(assignment)
    proof_identity: Optional[VerificationProofIdentity] = None

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

    def phase_progress(phase: str) -> ProgressCallback:
        return lambda message: _emit_progress(progress, f"{progress_label}: {phase}: {message}")

    _emit_progress(progress, f"{progress_label}: started; attemptHardTimeout={config.attempt_timeout_seconds}s")
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

        proof_store = VerificationProofStore(
            Path(config.proof_cache_dir) if config.proof_cache_dir else None
        )

        def publish_pass(proof_type: str, key: str) -> None:
            if proof_store.publish_pass(proof_type, key, proof_identity):
                event("proof.cache.publish", proofType=proof_type, cacheKey=key)

        install = install_args(manager, ignore_scripts=True)
        argv, _ = _command_prefix(executable, install)
        install_env = {
            "CI": "1",
            "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
            "YARN_ENABLE_SCRIPTS": "false",
            "npm_config_ignore_scripts": "true",
        }
        resolver_reused = bool(
            config.reuse_resolver_proof_key
            and config.reuse_resolver_proof_key == proof_identity.resolver_input_key
        )
        if resolver_reused:
            event(
                "proof.cache.hit",
                proofType="resolver",
                cacheKey=proof_identity.resolver_input_key,
                reuseMode="skip-scripts-off-install",
            )
            _emit_progress(
                progress,
                f"{progress_label}: resolver PASS reused from exact ResolverInputKey; "
                "lifecycle materialization remains fresh",
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

        try:
            materialized_manifest = json.loads((workspace_project / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            return BaselineVerifyResult(False, "unknown", f"ASSIGNMENT_MANIFEST_UNREADABLE: {exc}")
        for expected_name, expected_version in sorted(assignment.items()):
            observed_versions = []
            for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                values = materialized_manifest.get(section)
                if isinstance(values, dict) and expected_name in values:
                    observed_versions.append(str(values[expected_name]))
            if not observed_versions or any(value != expected_version for value in observed_versions):
                return BaselineVerifyResult(
                    False,
                    "unknown",
                    f"ASSIGNMENT_MANIFEST_DRIFT: {expected_name} expected {expected_version!r}, "
                    f"observed {observed_versions!r}",
                    workspace=str(workspace_project),
                )

        publish_pass("resolver", proof_identity.resolver_input_key)

        if run_project_checks and config.project_checks != "off" and config.commands:
            # Re-enable lifecycle scripts before project verification. This is
            # still an external package-manager invocation, so classify its
            # failure exactly like the resolver-only install *before* deciding
            # it is project/migration evidence. Otherwise a transient registry
            # 502, native postinstall/toolchain failure, auth issue, etc. could
            # be mislabeled as a deterministic project incompatibility and be
            # learned as a false solver nogood in strict/adaptive refinement.
            full_install = install_args(manager, ignore_scripts=False)
            full_argv, _ = _command_prefix(executable, full_install)
            preparation_started = time.monotonic()
            event("verify.preparation.start", command=" ".join(full_argv))
            try:
                full_result = _run(
                    full_argv,
                    workspace_project,
                    timeout_seconds=phase_timeout(),
                    env={"CI": "1", "YARN_ENABLE_IMMUTABLE_INSTALLS": "false", "YARN_ENABLE_SCRIPTS": "true", "npm_config_ignore_scripts": "false"},
                    base_env=base_env,
                    progress=phase_progress("lifecycle-install"), progress_label="package-manager lifecycle install",
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
                classified = preparation_classified
                if classified in {"infrastructure", "dependency"}:
                    return BaselineVerifyResult(
                        False,
                        classified,
                        "assignment resolves without lifecycle scripts, but lifecycle install failed",
                        command=" ".join(full_argv), output=tail, workspace=str(workspace_project),
                    )
                return BaselineVerifyResult(
                    False,
                    "preparation",
                    "assignment resolves, but lifecycle/preparation failed deterministically",
                    command=" ".join(full_argv), output=tail, workspace=str(workspace_project),
                )

            publish_pass("preparation", proof_identity.preparation_proof_key)

            project_failures: List[BaselineProjectFailure] = []
            for command_index, command in enumerate(config.commands, start=1):
                removed_caches = clean_ephemeral_verification_caches(workspace_project)
                if removed_caches:
                    _emit_progress(progress, f"{progress_label}: normalized transient caches before {command}: {', '.join(removed_caches)}")
                _emit_progress(progress, f"{progress_label}: project check {command_index}/{len(config.commands)} started: {command}")
                check_started = time.monotonic()
                event(
                    "verify.project-check.start",
                    command=command,
                    check=command_index,
                    checks=len(config.commands),
                )
                shell_argv: Sequence[str]
                if os.name == "nt":
                    shell_argv = [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", command]
                else:
                    shell_argv = ["/bin/sh", "-lc", command]
                try:
                    check_result = _run(
                        shell_argv,
                        workspace_project,
                        timeout_seconds=phase_timeout(),
                        base_env=base_env,
                        progress=phase_progress(f"project-check:{command}"),
                        progress_label=command,
                        progress_interval_seconds=config.progress_interval_seconds,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    return BaselineVerifyResult(False, "infrastructure", f"project check launch failed: {exc}", command=command)
                event(
                    "verify.project-check.finish",
                    command=command,
                    check=command_index,
                    checks=len(config.commands),
                    durationMs=int((time.monotonic() - check_started) * 1000),
                    exitCode=check_result.returncode,
                    outcome="passed" if check_result.returncode == 0 else "failed",
                )
                if check_result.returncode == 0:
                    _emit_progress(progress, f"{progress_label}: project check {command_index}/{len(config.commands)} PASS: {command}")
                    continue
                tail = "\n".join((check_result.stdout or "").splitlines()[-80:])
                # Project commands are external processes too.  Never let a
                # network/disk/tooling failure masquerade as structural evidence.
                if INFRA_PATTERNS.search(tail):
                    return BaselineVerifyResult(
                        False, "infrastructure", f"project preflight infrastructure failure: {command}",
                        command=command, output=tail, workspace=str(workspace_project),
                    )
                project_failures.append(BaselineProjectFailure(command, check_result.returncode, tail))
                _emit_progress(progress, f"{progress_label}: project check {command_index}/{len(config.commands)} RED exit={check_result.returncode}: {command}")

            if project_failures:
                summary_commands = ", ".join(item.command for item in project_failures)
                output = "\n\n".join(
                    f"=== {item.command} (exit {item.exit_code}) ===\n{item.output}" for item in project_failures
                )
                first = project_failures[0]
                return BaselineVerifyResult(
                    False, "project", f"project preflight failed: {summary_commands}", command=first.command,
                    output=output[-16000:], workspace=str(workspace_project), project_failures=tuple(project_failures),
                )

        if run_project_checks and config.project_checks != "off" and config.commands:
            publish_pass("project", proof_identity.project_proof_key)

        _emit_progress(progress, f"{progress_label}: completed; elapsed={int(time.monotonic() - attempt_started)}s")
        event("verify.attempt.finish", outcome="passed")
        return BaselineVerifyResult(
            True, "passed", f"resolver preflight passed for {len(changed)} changed direct package(s)",
            command=" ".join(argv), workspace=str(workspace_project),
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def assignment_fingerprint(assignment: Mapping[str, str]) -> str:
    payload = json.dumps(dict(sorted(assignment.items())), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_verify_assignment_uncached = verify_assignment


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
    """Cache-aware proof entry point."""
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
    if wants_project_proof:
        cache_event("proof.cache.lookup", "project", identity.project_proof_key)
        if proof_store.lookup_pass("project", identity.project_proof_key) is not None:
            cache_event("proof.cache.hit", "project", identity.project_proof_key)
            _emit_progress(
                progress,
                f"{progress_label}: exact ProjectProof cache HIT; "
                "clone/install/lifecycle/project checks skipped",
            )
            return BaselineVerifyResult(
                True,
                "passed",
                "exact ProjectProof cache hit",
                command=f"proof-cache:{identity.project_proof_key[:12]}",
            )
        cache_event("proof.cache.miss", "project", identity.project_proof_key)

    cache_event("proof.cache.lookup", "resolver", identity.resolver_input_key)
    resolver_hit = proof_store.lookup_pass("resolver", identity.resolver_input_key) is not None
    if resolver_hit:
        cache_event("proof.cache.hit", "resolver", identity.resolver_input_key)
        if not wants_project_proof:
            _emit_progress(
                progress,
                f"{progress_label}: exact ResolverProof cache HIT; resolver verification skipped",
            )
            return BaselineVerifyResult(
                True,
                "passed",
                "exact ResolverProof cache hit",
                command=f"proof-cache:{identity.resolver_input_key[:12]}",
            )
    else:
        cache_event("proof.cache.miss", "resolver", identity.resolver_input_key)

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
    )
