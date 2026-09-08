#!/usr/bin/env python3
"""Ψ.5.7 proof-neutral Yarn Classic resolver-seed continuation.

A ResolverSeed is an exact same-run filesystem cache produced immediately after
an authoritative Yarn Classic resolver install with lifecycle scripts disabled.
It is *not* ResolverProof, PreparationProof, ProjectProof, or Solver authority.

On a later exact ResolverProof HIT, DepLoom may clone the seed into a fresh
private workspace, delete Yarn Classic's `.yarn-integrity` marker, and then run
the ordinary scripts-enabled `yarn install --frozen-lockfile`.  Removing the
marker deliberately prevents Yarn's integrity bailout and causes Yarn Classic
to force package install scripts during the normal install pipeline.  The
existing verifier still checks the exact ResolvedState and observed direct tree
before any PreparationProof can be published.

The seed store is same-process only.  A watcher is armed immediately after seed
publication and must remain clean while a consumer copies the seed.  If watcher
continuity is unavailable or uncertain, the optimization is skipped and the
ordinary fresh lifecycle path remains authoritative.
"""
from __future__ import annotations

import atexit
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from prepared_workspace_fastpath import WorkspaceChangeGuard
from reparse_materialization import inventory_reparse_plan
from substrate_identity import tool_build_id
from verification_workspace_backend import materialize_private_tree
from verification_observability import emit_observability_event


AUTHORITY = "PERFORMANCE_ONLY"
STRATEGY = "yarn1-private-resolver-seed-integrity-reset-v1"
YARN_INTEGRITY_RELATIVE = Path("node_modules") / ".yarn-integrity"
_DEFAULT_MAX_SEEDS = 2
CERTIFIED_YARN_CLASSIC_VERSIONS = frozenset({"1.22.22"})
_RUNTIME_VERSION_LOCK = threading.Lock()
_RUNTIME_VERSION_CACHE: dict[str, str] = {}


class ResolverSeedError(RuntimeError):
    pass


def yarn1_runtime_version(executable: str) -> str:
    """Return the exact Yarn Classic runtime version used by this process."""
    command = str(executable or "").strip()
    if not command:
        return ""
    cache_key = os.path.normcase(str(Path(command).expanduser().absolute()))
    with _RUNTIME_VERSION_LOCK:
        cached = _RUNTIME_VERSION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    path = Path(command)
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        quoted = subprocess.list2cmdline([command, "--version"])
        argv = [comspec, "/d", "/s", "/c", quoted]
    else:
        argv = [command, "--version"]
    try:
        result = subprocess.run(
            argv,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        version = (result.stdout or "").strip().splitlines()
        value = version[0].strip() if result.returncode == 0 and version else ""
    except (OSError, subprocess.SubprocessError):
        value = ""
    with _RUNTIME_VERSION_LOCK:
        _RUNTIME_VERSION_CACHE[cache_key] = value
    return value


def yarn1_resolver_seed_runtime_supported(executable: str) -> tuple[bool, str]:
    """Fail closed outside the platform/runtime physically certified by Ψ.5.7."""
    version = yarn1_runtime_version(executable)
    # Same-run seed continuity currently relies on WorkspaceChangeGuard, whose
    # sound watcher implementation is Windows-only. Other platforms keep the
    # ordinary frozen lifecycle until an equivalent continuity substrate exists.
    return (
        os.name == "nt" and version in CERTIFIED_YARN_CLASSIC_VERSIONS,
        version,
    )


def _canonical_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def resolver_seed_identity(
    *,
    resolver_input_key: str,
    resolved_state_key: str,
    observed_resolved_hash: str,
    manager_family: str,
) -> str:
    """Identity for an exact pre-lifecycle filesystem seed.

    ResolverInputKey already binds source snapshot, assignment, package-manager
    executable/version, registry/config, environment, fixed inputs and resolver
    overrides.  ResolvedStateKey additionally binds the exact lockfile bytes.
    """
    values = {
        "schema": "resolver-seed-v1",
        "toolBuildId": tool_build_id(),
        "resolverInputKey": str(resolver_input_key or ""),
        "resolvedStateKey": str(resolved_state_key or ""),
        "observedResolvedHash": str(observed_resolved_hash or ""),
        "managerFamily": str(manager_family or ""),
    }
    if not all(values[name] for name in (
        "resolverInputKey",
        "resolvedStateKey",
        "observedResolvedHash",
        "managerFamily",
    )):
        raise ResolverSeedError("RESOLVER_SEED_IDENTITY_INCOMPLETE")
    return _canonical_hash(values)


def yarn1_integrity_path(package_manager_root: Path) -> Path:
    return Path(package_manager_root) / YARN_INTEGRITY_RELATIVE


def _yarn1_integrity_payload(package_manager_root: Path) -> dict[str, object]:
    path = yarn1_integrity_path(package_manager_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResolverSeedError(
            f"YARN1_RESOLVER_SEED_INTEGRITY_MISSING: {path}"
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise ResolverSeedError(
            f"YARN1_RESOLVER_SEED_INTEGRITY_UNREADABLE: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ResolverSeedError(
            f"YARN1_RESOLVER_SEED_INTEGRITY_INVALID: {path}: root must be an object"
        )
    return value


def yarn1_integrity_flags(package_manager_root: Path) -> tuple[str, ...]:
    payload = _yarn1_integrity_payload(package_manager_root)
    raw = payload.get("flags")
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ResolverSeedError(
            "YARN1_RESOLVER_SEED_INTEGRITY_INVALID: flags must be a string array"
        )
    return tuple(sorted(str(item) for item in raw))


def validate_yarn1_resolver_seed(package_manager_root: Path) -> tuple[str, ...]:
    """Require evidence that this tree came from an ignore-scripts Yarn install."""
    flags = yarn1_integrity_flags(package_manager_root)
    if "ignoreScripts" not in flags:
        raise ResolverSeedError(
            "YARN1_RESOLVER_SEED_NOT_IGNORE_SCRIPTS: .yarn-integrity does not "
            "contain the ignoreScripts flag"
        )
    return flags


def prepare_yarn1_resolver_seed_for_lifecycle(package_manager_root: Path) -> Path:
    """Invalidate Yarn's integrity bailout in a private seed clone.

    Yarn Classic's install implementation treats a missing integrity file as a
    reason to continue the install and force package install scripts.  This is
    deliberately done only in the disposable lifecycle clone; the immutable
    resolver seed is never modified.
    """
    validate_yarn1_resolver_seed(package_manager_root)
    marker = yarn1_integrity_path(package_manager_root)
    try:
        marker.unlink()
    except OSError as exc:
        raise ResolverSeedError(
            f"YARN1_RESOLVER_SEED_INTEGRITY_RESET_FAILED: {marker}: {exc}"
        ) from exc
    if marker.exists():
        raise ResolverSeedError(
            f"YARN1_RESOLVER_SEED_INTEGRITY_RESET_FAILED: {marker} still exists"
        )
    return marker


def validate_yarn1_lifecycle_completion(package_manager_root: Path) -> tuple[str, ...]:
    """Require Yarn to have recreated a scripts-enabled integrity receipt."""
    flags = yarn1_integrity_flags(package_manager_root)
    if "ignoreScripts" in flags:
        raise ResolverSeedError(
            "YARN1_LIFECYCLE_SCRIPTS_NOT_CERTIFIED: resulting .yarn-integrity "
            "still contains ignoreScripts"
        )
    return flags


def _source_project_identity(path: Path) -> str:
    return _canonical_hash({
        "path": os.path.normcase(str(Path(path).resolve())),
    })


@dataclasses.dataclass(frozen=True)
class ResolverSeedMaterialization:
    key: str
    workspace_root: Path
    project_relative: Path
    package_manager_relative: Path
    method: str
    authority: str = AUTHORITY

    @property
    def project_path(self) -> Path:
        return self.workspace_root / self.project_relative

    @property
    def package_manager_path(self) -> Path:
        return self.workspace_root / self.package_manager_relative


@dataclasses.dataclass
class _ResolverSeedRecord:
    key: str
    workspace_root: Path
    project_relative: Path
    package_manager_relative: Path
    source_project_identity: str
    resolved_state_key: str
    observed_resolved_hash: str
    producer: str
    guard: WorkspaceChangeGuard
    last_used: float


class SameRunResolverSeedStore:
    """Watcher-backed exact resolver seeds; performance only, never durable proof."""

    def __init__(self, *, max_count: int = _DEFAULT_MAX_SEEDS) -> None:
        self._max_count = max(1, int(max_count))
        self._lock = threading.RLock()
        self._root: Optional[Path] = None
        self._records: dict[str, _ResolverSeedRecord] = {}
        self._miss_reasons: dict[str, str] = {}

    def _ensure_root(self, parent: Optional[Path] = None) -> Path:
        with self._lock:
            if self._root is not None and self._root.is_dir():
                return self._root
            base = Path(parent).resolve() if parent is not None else None
            if base is not None:
                base.mkdir(parents=True, exist_ok=True)
            self._root = Path(tempfile.mkdtemp(
                prefix="dependency-flow-resolver-seed-",
                dir=str(base) if base is not None else None,
            ))
            return self._root

    @staticmethod
    def _stop_guard(record: _ResolverSeedRecord) -> tuple[bool, str]:
        try:
            continuity = record.guard.stop()
        except Exception as exc:
            return False, f"watcher-stop:{type(exc).__name__}"
        if continuity.errors:
            return False, "watcher-error"
        if continuity.changes:
            return False, "workspace-mutated"
        if not record.workspace_root.is_dir():
            return False, "path-missing"
        return True, "clean"

    @staticmethod
    def _arm_guard(workspace_root: Path) -> WorkspaceChangeGuard:
        guard = WorkspaceChangeGuard(workspace_root)
        try:
            started = bool(guard.start())
        except Exception as exc:
            try:
                guard.stop()
            except Exception:
                pass
            raise ResolverSeedError(
                f"RESOLVER_SEED_WATCHER_UNAVAILABLE: {type(exc).__name__}: {exc}"
            ) from exc
        if not started:
            try:
                guard.stop()
            except Exception:
                pass
            raise ResolverSeedError("RESOLVER_SEED_WATCHER_UNAVAILABLE")
        return guard

    def _retire_record_tree(self, record: _ResolverSeedRecord) -> None:
        """Delete only a container owned by this store.

        Cache metadata is performance-only, but cleanup must still fail closed:
        a corrupted/injected record must never turn into an arbitrary rmtree.
        """
        root = self._root
        if root is None:
            return
        try:
            store_root = root.resolve()
            workspace = record.workspace_root.resolve()
            container = workspace.parent.resolve()
            container.relative_to(store_root)
        except (OSError, ValueError):
            return
        if workspace.name != "workspace" or container == store_root:
            return
        shutil.rmtree(container, ignore_errors=True)

    def _evict_locked(self, key: str, *, reason: str = "evicted") -> None:
        seed_key = str(key)
        record = self._records.pop(seed_key, None)
        if record is None:
            return
        self._miss_reasons[seed_key] = str(reason or "evicted")
        emit_observability_event(
            "resolver-seed.evicted",
            seedKey=seed_key,
            reason=self._miss_reasons[seed_key],
            authority=AUTHORITY,
        )
        try:
            record.guard.stop()
        except Exception:
            pass
        self._retire_record_tree(record)

    def _enforce_budget_locked(self, protected_key: str = "") -> None:
        while len(self._records) > self._max_count:
            candidates = [
                record for record in self._records.values()
                if record.key != protected_key
            ]
            if not candidates:
                return
            victim = min(candidates, key=lambda item: (item.last_used, item.key))
            self._evict_locked(victim.key, reason="evicted-budget")

    def publish(
        self,
        *,
        key: str,
        source_workspace_root: Path,
        project_relative: Path,
        package_manager_relative: Path,
        source_project: Path,
        resolved_state_key: str,
        observed_resolved_hash: str,
        parent: Optional[Path] = None,
        timeout_seconds: int = 1800,
        progress: Optional[Callable[[str], None]] = None,
        progress_label: str = "resolver seed publish",
        producer: str = "",
    ) -> bool:
        """Copy a post-resolver/pre-lifecycle tree and arm continuity evidence."""
        seed_key = str(key or "").strip()
        if len(seed_key) != 64:
            raise ResolverSeedError("RESOLVER_SEED_KEY_INVALID")
        source_workspace_root = Path(source_workspace_root).resolve()
        project_relative = Path(project_relative)
        package_manager_relative = Path(package_manager_relative)
        source_project = Path(source_project).resolve()
        if project_relative.is_absolute() or package_manager_relative.is_absolute():
            raise ResolverSeedError("RESOLVER_SEED_RELATIVE_PATH_INVALID")
        source_pm = source_workspace_root / package_manager_relative
        validate_yarn1_resolver_seed(source_pm)

        # Serialize store publication/materialization/clear. A Baseline may run
        # several diagnostic verifications concurrently; allowing a reset or a
        # second publication to retire the store root during this copy would turn
        # a performance cache race into substrate uncertainty.
        with self._lock:
            root = self._ensure_root(parent)
            container = Path(tempfile.mkdtemp(prefix=f"{seed_key[:12]}-", dir=str(root)))
            target = container / "workspace"
            guard: Optional[WorkspaceChangeGuard] = None
            try:
                plan = inventory_reparse_plan(source_workspace_root)
                materialize_private_tree(
                    source_workspace_root,
                    target,
                    timeout_seconds=max(1, int(timeout_seconds)),
                    progress=progress,
                    progress_label=progress_label,
                    reparse_plan=plan,
                )
                copied_project = target / project_relative
                copied_pm = target / package_manager_relative
                if not copied_project.is_dir() or not copied_pm.is_dir():
                    raise ResolverSeedError("RESOLVER_SEED_COPY_TOPOLOGY_INVALID")
                validate_yarn1_resolver_seed(copied_pm)
                guard = self._arm_guard(target)
                # Revalidate after watcher arming so the published cache point has a
                # clean continuity interval from a known exact marker state.
                validate_yarn1_resolver_seed(copied_pm)
                record = _ResolverSeedRecord(
                    key=seed_key,
                    workspace_root=target,
                    project_relative=project_relative,
                    package_manager_relative=package_manager_relative,
                    source_project_identity=_source_project_identity(source_project),
                    resolved_state_key=str(resolved_state_key or ""),
                    observed_resolved_hash=str(observed_resolved_hash or ""),
                    producer=str(producer or ""),
                    guard=guard,
                    last_used=time.monotonic(),
                )
                self._evict_locked(seed_key, reason="replaced")
                self._records[seed_key] = record
                self._miss_reasons.pop(seed_key, None)
                self._enforce_budget_locked(seed_key)
                return True
            except Exception:
                if guard is not None:
                    try:
                        guard.stop()
                    except Exception:
                        pass
                shutil.rmtree(container, ignore_errors=True)
                raise

    def materialize(
        self,
        *,
        key: str,
        target_workspace_root: Path,
        source_project: Path,
        resolved_state_key: str,
        observed_resolved_hash: str,
        timeout_seconds: int = 1800,
        progress: Optional[Callable[[str], None]] = None,
        progress_label: str = "resolver seed materialization",
    ) -> Optional[ResolverSeedMaterialization]:
        """Clone an exact clean seed while its watcher proves continuity."""
        seed_key = str(key or "").strip()
        target_workspace_root = Path(target_workspace_root).resolve()
        source_id = _source_project_identity(Path(source_project))

        # Serialize consumers of one tiny same-run cache. The seed itself is
        # immutable and never handed to Yarn; only a fresh private clone is.
        with self._lock:
            record = self._records.get(seed_key)
            if record is None:
                return None
            if (
                record.source_project_identity != source_id
                or record.resolved_state_key != str(resolved_state_key or "")
                or record.observed_resolved_hash != str(observed_resolved_hash or "")
            ):
                self._evict_locked(seed_key, reason="identity-mismatch")
                return None
            if target_workspace_root.exists():
                raise ResolverSeedError(
                    f"RESOLVER_SEED_TARGET_EXISTS: {target_workspace_root}"
                )

            method = ""
            try:
                plan = inventory_reparse_plan(record.workspace_root)
                method = materialize_private_tree(
                    record.workspace_root,
                    target_workspace_root,
                    timeout_seconds=max(1, int(timeout_seconds)),
                    progress=progress,
                    progress_label=progress_label,
                    reparse_plan=plan,
                )
                clean, reason = self._stop_guard(record)
                if not clean:
                    shutil.rmtree(target_workspace_root, ignore_errors=True)
                    self._evict_locked(
                        seed_key, reason=f"continuity-{reason}"
                    )
                    return None
                # The clone itself must still carry the resolver-stage marker.
                validate_yarn1_resolver_seed(
                    target_workspace_root / record.package_manager_relative
                )
                record.last_used = time.monotonic()
                try:
                    record.guard = self._arm_guard(record.workspace_root)
                except ResolverSeedError:
                    # Current clone is valid because continuity was proven
                    # through the completed copy. Retire only future reuse.
                    self._records.pop(seed_key, None)
                    self._miss_reasons[seed_key] = "watcher-rearm-unavailable"
                    emit_observability_event(
                        "resolver-seed.evicted",
                        seedKey=seed_key,
                        reason="watcher-rearm-unavailable",
                        authority=AUTHORITY,
                    )
                    self._retire_record_tree(record)
                return ResolverSeedMaterialization(
                    key=seed_key,
                    workspace_root=target_workspace_root,
                    project_relative=record.project_relative,
                    package_manager_relative=record.package_manager_relative,
                    method=method,
                )
            except Exception:
                shutil.rmtree(target_workspace_root, ignore_errors=True)
                try:
                    self._evict_locked(
                        seed_key, reason="materialization-error"
                    )
                except Exception:
                    pass
                raise

    def clear(self) -> None:
        with self._lock:
            keys = tuple(self._records)
            for key in keys:
                self._evict_locked(key, reason="cache-clear")
            root = self._root
            self._root = None
            self._miss_reasons.clear()
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)

    def miss_reason(self, key: str) -> str:
        with self._lock:
            return str(
                self._miss_reasons.get(str(key or "").strip()) or "not-published"
            )

    def size(self) -> int:
        with self._lock:
            return len(self._records)


_SAME_RUN_RESOLVER_SEEDS = SameRunResolverSeedStore()


def same_run_resolver_seed_store() -> SameRunResolverSeedStore:
    return _SAME_RUN_RESOLVER_SEEDS


def reset_same_run_resolver_seed_cache() -> None:
    _SAME_RUN_RESOLVER_SEEDS.clear()


atexit.register(reset_same_run_resolver_seed_cache)
