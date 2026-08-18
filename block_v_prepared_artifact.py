#!/usr/bin/env python3
"""Block V-A durable PreparedArtifact index.

PreparedArtifact is a cache of an exact lifecycle-prepared filesystem
precondition.  It is never solver/project-proof authority by itself.  The
existing preparationProofKey remains the authority identity; this module only
maps that exact key to an immutable on-disk tree across process restarts.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Mapping, Optional

from block_vex_storage import verification_root
from prepared_workspace_fastpath import try_acquire_snapshot_cleanup_lease

ARTIFACT_INDEX_SCHEMA = 1
ARTIFACT_AUTHORITY = "PRECONDITION_CACHE"
_LOCK = threading.RLock()
_CONFIGURED_ROOT: Optional[Path] = None


def configure_prepared_artifact_store(proof_cache_dir: str | Path | None) -> Optional[Path]:
    """Configure durable PreparedArtifact storage."""
    global _CONFIGURED_ROOT
    accelerated = verification_root()
    if accelerated is not None:
        root = accelerated / "baseline-prepared-artifacts"
    elif proof_cache_dir:
        root = Path(proof_cache_dir).expanduser().resolve().parent / "baseline-prepared-artifacts"
    else:
        return _CONFIGURED_ROOT
    root.mkdir(parents=True, exist_ok=True)
    (root / "index").mkdir(parents=True, exist_ok=True)
    (root / "trees").mkdir(parents=True, exist_ok=True)
    with _LOCK:
        _CONFIGURED_ROOT = root

    # PreparedArtifact is a performance cache. Keep it bounded by default;
    # pruning never changes proof authority and uses the same live snapshot
    # lease before removing any tree.
    prune_prepared_artifact_store(max_count=8)
    return root

def configured_prepared_artifact_root() -> Optional[Path]:
    with _LOCK:
        return _CONFIGURED_ROOT


def prepared_snapshot_storage_root() -> Optional[Path]:
    root = configured_prepared_artifact_root()
    if root is None:
        return None
    target = root / "trees"
    target.mkdir(parents=True, exist_ok=True)
    return target


def is_durable_prepared_path(path: Path) -> bool:
    root = configured_prepared_artifact_root()
    if root is None:
        return False
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _valid_key(key: str) -> str:
    normalized = str(key or "").strip().lower()
    if len(normalized) not in {32, 64} or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError("PREPARED_ARTIFACT_KEY_INVALID")
    return normalized


def _source_identity(source_project: Path) -> str:
    # The source snapshot itself is already bound by preparationProofKey.  This
    # path hash prevents one index slot from being accidentally presented to a
    # different local project checkout with the same display/package name.
    value = str(source_project.resolve()).replace("\\", "/")
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record_path(key: str) -> Optional[Path]:
    root = configured_prepared_artifact_root()
    if root is None:
        return None
    return root / "index" / f"{_valid_key(key)}.json"


def publish_prepared_artifact_record(
    *,
    key: str,
    workspace_root: Path,
    project_relative: Path,
    source_project: Path,
    storage_mode: str,
    observed_resolved_versions: Mapping[str, str],
    observed_resolved_hash: str,
) -> bool:
    """Atomically publish an immutable artifact locator.

    The tree must already be fully prepared.  The record is written last, so a
    crash can leave an unindexed tree (harmless cache garbage) but can never
    expose a half-published artifact as a HIT.
    """
    path = _record_path(key)
    if path is None:
        return False
    workspace_root = workspace_root.resolve()
    if not workspace_root.is_dir() or not is_durable_prepared_path(workspace_root):
        return False
    observed_hash = str(observed_resolved_hash or "").lower()
    if len(observed_hash) != 64 or any(ch not in "0123456789abcdef" for ch in observed_hash):
        return False
    payload = {
        "schemaVersion": ARTIFACT_INDEX_SCHEMA,
        "authority": ARTIFACT_AUTHORITY,
        "key": _valid_key(key),
        "sourceProjectIdentity": _source_identity(source_project),
        "workspaceRoot": str(workspace_root),
        "projectRelative": project_relative.as_posix(),
        "storageMode": str(storage_mode),
        "observedResolvedVersions": dict(sorted(
            (str(name), str(version))
            for name, version in observed_resolved_versions.items()
        )),
        "observedResolvedHash": observed_hash,
        "publishedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp, path)
        return True
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def load_prepared_artifact_record(
    key: str,
    source_project: Path,
) -> Optional[dict[str, object]]:
    path = _record_path(key)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schemaVersion") != ARTIFACT_INDEX_SCHEMA:
        return None
    if payload.get("authority") != ARTIFACT_AUTHORITY:
        return None
    if payload.get("key") != _valid_key(key):
        return None
    if payload.get("sourceProjectIdentity") != _source_identity(source_project):
        return None
    workspace_raw = payload.get("workspaceRoot")
    relative_raw = payload.get("projectRelative")
    observed_hash = str(payload.get("observedResolvedHash") or "").lower()
    versions = payload.get("observedResolvedVersions")
    if not isinstance(workspace_raw, str) or not isinstance(relative_raw, str):
        return None
    if not isinstance(versions, dict) or not all(
        isinstance(name, str) and isinstance(version, str)
        for name, version in versions.items()
    ):
        return None
    if len(observed_hash) != 64 or any(ch not in "0123456789abcdef" for ch in observed_hash):
        return None
    workspace = Path(workspace_raw)
    if not workspace.is_dir() or not is_durable_prepared_path(workspace):
        invalidate_prepared_artifact_record(key)
        return None
    project = workspace / Path(relative_raw)
    if not project.is_dir():
        invalidate_prepared_artifact_record(key)
        return None
    return {
        "key": _valid_key(key),
        "workspaceRoot": workspace,
        "projectRelative": Path(relative_raw),
        "storageMode": str(payload.get("storageMode") or "durable-prepared-artifact"),
        "observedResolvedVersions": dict(sorted(versions.items())),
        "observedResolvedHash": observed_hash,
        "publishedAt": str(payload.get("publishedAt") or ""),
    }


def invalidate_prepared_artifact_record(key: str, *, remove_tree: bool = False) -> None:
    path = _record_path(key)
    if path is None:
        return

    workspace: Optional[Path] = None
    if remove_tree and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("workspaceRoot"), str):
                candidate = Path(str(payload["workspaceRoot"]))
                if is_durable_prepared_path(candidate):
                    workspace = candidate
        except (OSError, ValueError, TypeError):
            workspace = None

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

    if workspace is None:
        return

    lease = None
    if os.name == "nt":
        lease = try_acquire_snapshot_cleanup_lease(workspace)
        if lease is None:
            # Locator is already invalidated. Leaving an unreachable tree is
            # safer than deleting bytes under a live cross-process reader.
            return
    try:
        shutil.rmtree(workspace.parent, ignore_errors=True)
    finally:
        if lease is not None:
            lease.close()


def prune_prepared_artifact_store(max_count: int = 8) -> int:
    """Best-effort LRU-ish pruning by index mtime; never affects proof authority."""
    root = configured_prepared_artifact_root()
    if root is None:
        return 0
    limit = max(1, int(max_count))
    index = root / "index"
    try:
        records = sorted(
            [path for path in index.glob("*.json") if path.is_file()],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for record in records[limit:]:
        key = record.stem
        invalidate_prepared_artifact_record(key, remove_tree=True)
        removed += 1
    return removed


def verification_trial_parent(proof_cache_dir: str | Path | None = None) -> Optional[Path]:
    """Common parent for fresh trials when an optimized root was selected."""
    accelerated = verification_root()
    if accelerated is not None:
        root = accelerated / "trials"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None

