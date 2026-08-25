from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Mapping, Optional
from project_topology import ProjectTopologyError, resolve_project_topology

# BLOCK_Z_PROJECT_TOPOLOGY_V1

RESOLVED_STATE_SCHEMA = "resolved-state-v1"


class ResolvedDependencyStateError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ResolvedDependencyState:
    key: str
    resolver_input_key: str
    manager: str
    lockfile_path: str
    lockfile_hash: str
    observed_resolved_hash: str
    artifact_relative_path: str
    lockfile_bytes: bytes = dataclasses.field(repr=False, compare=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _git_root_or_project(project_dir: Path) -> Path:
    project_dir = project_dir.resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--show-toplevel"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return project_dir
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return project_dir


def _lockfile_names(manager: str) -> tuple[str, ...]:
    manager = str(manager).lower()
    if manager == "yarn":
        return ("yarn.lock",)
    if manager == "pnpm":
        return ("pnpm-lock.yaml",)
    if manager == "npm":
        return ("npm-shrinkwrap.json", "package-lock.json")
    raise ResolvedDependencyStateError(f"RESOLVED_STATE_MANAGER_UNSUPPORTED: {manager}")


def _find_lockfile(project_dir: Path, manager: str) -> tuple[Path, Path]:
    project_dir = project_dir.resolve()
    try:
        topology = resolve_project_topology(
            project_dir,
            allow_discovery=False,
            require_supported=True,
        )
    except ProjectTopologyError as exc:
        raise ResolvedDependencyStateError(
            f"RESOLVED_STATE_PROJECT_TOPOLOGY_UNAVAILABLE: {exc}"
        ) from exc
    if topology.profile.manager != str(manager).lower():
        raise ResolvedDependencyStateError(
            "RESOLVED_STATE_MANAGER_TOPOLOGY_MISMATCH: "
            f"requested={manager}; topology={topology.profile.manager}"
        )
    identity_root = topology.git_root or topology.source_root
    return identity_root.resolve(), topology.lockfile.resolve()


def _state_key(
    *,
    resolver_input_key: str,
    manager: str,
    lockfile_path: str,
    lockfile_hash: str,
    observed_resolved_hash: str,
) -> str:
    return _canonical_hash({
        "schema": RESOLVED_STATE_SCHEMA,
        "resolverInputKey": str(resolver_input_key),
        "manager": str(manager),
        "lockfilePath": str(lockfile_path),
        "lockfileHash": str(lockfile_hash),
        "observedResolvedHash": str(observed_resolved_hash),
    })


def capture_resolved_dependency_state(
    project_dir: Path,
    *,
    manager: str,
    resolver_input_key: str,
    observed_resolved_hash: str,
    proof_cache_dir: Optional[Path],
) -> ResolvedDependencyState:
    git_root, lockfile = _find_lockfile(project_dir, manager)
    try:
        lockfile_bytes = lockfile.read_bytes()
    except OSError as exc:
        raise ResolvedDependencyStateError(
            f"RESOLVED_STATE_LOCKFILE_UNREADABLE: {lockfile}: {exc}"
        ) from exc
    lockfile_hash = _sha256_bytes(lockfile_bytes)
    try:
        lockfile_path = lockfile.relative_to(git_root).as_posix()
    except ValueError as exc:
        raise ResolvedDependencyStateError(
            f"RESOLVED_STATE_LOCKFILE_OUTSIDE_GIT_ROOT: {lockfile}"
        ) from exc

    key = _state_key(
        resolver_input_key=resolver_input_key,
        manager=manager,
        lockfile_path=lockfile_path,
        lockfile_hash=lockfile_hash,
        observed_resolved_hash=observed_resolved_hash,
    )
    artifact_relative_path = ""
    if proof_cache_dir is not None:
        root = proof_cache_dir.resolve()
        artifact = root / "resolved-state" / key / "lockfile.bin"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if artifact.exists():
            existing = artifact.read_bytes()
            if _sha256_bytes(existing) != lockfile_hash:
                raise ResolvedDependencyStateError(
                    f"RESOLVED_STATE_ARTIFACT_COLLISION: {artifact}"
                )
        else:
            temporary = artifact.with_name(
                f"{artifact.name}.tmp-{os.getpid()}-{threading_get_ident()}"
            )
            temporary.write_bytes(lockfile_bytes)
            os.replace(temporary, artifact)
        artifact_relative_path = artifact.relative_to(root).as_posix()

    return ResolvedDependencyState(
        key=key,
        resolver_input_key=str(resolver_input_key),
        manager=str(manager),
        lockfile_path=lockfile_path,
        lockfile_hash=lockfile_hash,
        observed_resolved_hash=str(observed_resolved_hash),
        artifact_relative_path=artifact_relative_path,
        lockfile_bytes=lockfile_bytes,
    )


def threading_get_ident() -> int:
    # Kept local to avoid importing threading on the hot path unless persistence is used.
    import threading
    return threading.get_ident()


def resolved_state_metadata(state: ResolvedDependencyState) -> dict[str, str]:
    return {
        "resolvedStateKey": state.key,
        "resolvedStateResolverInputKey": state.resolver_input_key,
        "resolvedPackageManager": state.manager,
        "resolvedLockfilePath": state.lockfile_path,
        "resolvedLockfileHash": state.lockfile_hash,
        "resolvedStateArtifact": state.artifact_relative_path,
        "resolvedStateObservedHash": state.observed_resolved_hash,
    }


def load_resolved_dependency_state(
    metadata: Mapping[str, object],
    *,
    proof_cache_dir: Optional[Path],
) -> Optional[ResolvedDependencyState]:
    try:
        key = str(metadata.get("resolvedStateKey") or "")
        resolver_input_key = str(metadata.get("resolvedStateResolverInputKey") or "")
        manager = str(metadata.get("resolvedPackageManager") or "")
        lockfile_path = str(metadata.get("resolvedLockfilePath") or "")
        lockfile_hash = str(metadata.get("resolvedLockfileHash") or "")
        artifact_relative_path = str(metadata.get("resolvedStateArtifact") or "")
        observed_hash = str(metadata.get("resolvedStateObservedHash") or "")
        if not (
            len(key) == 64
            and len(lockfile_hash) == 64
            and len(observed_hash) == 64
            and resolver_input_key
            and manager
            and lockfile_path
            and artifact_relative_path
            and proof_cache_dir is not None
        ):
            return None

        root = proof_cache_dir.resolve()
        artifact = (root / Path(artifact_relative_path)).resolve()
        try:
            artifact.relative_to(root)
        except ValueError:
            return None
        if not artifact.is_file():
            return None
        lockfile_bytes = artifact.read_bytes()
        if _sha256_bytes(lockfile_bytes) != lockfile_hash:
            return None
        expected = _state_key(
            resolver_input_key=resolver_input_key,
            manager=manager,
            lockfile_path=lockfile_path,
            lockfile_hash=lockfile_hash,
            observed_resolved_hash=observed_hash,
        )
        if expected != key:
            return None
        return ResolvedDependencyState(
            key=key,
            resolver_input_key=resolver_input_key,
            manager=manager,
            lockfile_path=lockfile_path,
            lockfile_hash=lockfile_hash,
            observed_resolved_hash=observed_hash,
            artifact_relative_path=artifact_relative_path,
            lockfile_bytes=lockfile_bytes,
        )
    except (OSError, TypeError, ValueError):
        return None


def resolved_lockfile_path(project_dir: Path, state: ResolvedDependencyState) -> Path:
    git_root = _git_root_or_project(project_dir)
    target = (git_root / Path(state.lockfile_path)).resolve()
    try:
        target.relative_to(git_root)
    except ValueError as exc:
        raise ResolvedDependencyStateError(
            f"RESOLVED_STATE_LOCKFILE_PATH_ESCAPE: {state.lockfile_path}"
        ) from exc
    return target


def restore_resolved_dependency_state(
    project_dir: Path, state: ResolvedDependencyState
) -> Path:
    target = resolved_lockfile_path(project_dir, state)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.deploom-resolved-{os.getpid()}.tmp")
    temporary.write_bytes(state.lockfile_bytes)
    os.replace(temporary, target)
    assert_resolved_dependency_state(project_dir, state)
    return target


def assert_resolved_dependency_state(
    project_dir: Path, state: ResolvedDependencyState
) -> None:
    target = resolved_lockfile_path(project_dir, state)
    if not target.is_file():
        raise ResolvedDependencyStateError(
            f"RESOLVED_STATE_LOCKFILE_DISAPPEARED: {state.lockfile_path}"
        )
    current_hash = _sha256_bytes(target.read_bytes())
    if current_hash != state.lockfile_hash:
        raise ResolvedDependencyStateError(
            "RESOLVED_STATE_LOCKFILE_DRIFT: "
            f"expected={state.lockfile_hash} current={current_hash} path={state.lockfile_path}"
        )
