#!/usr/bin/env python3
"""Block V-A durable PreparedArtifact index.

PreparedArtifact is a cache of an exact lifecycle-prepared filesystem
precondition.  It is never solver/project-proof authority by itself.  The
existing preparationProofKey remains the authority identity; this module only
maps that exact key to an immutable on-disk tree across process restarts.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from artifact_integrity import (
    ArtifactIntegrityError,
    build_artifact_tree_integrity,
)
from block_vex_storage import verification_root
from prepared_workspace_fastpath import (
    _DirectoryWatcher,
    drain_watcher,
    tree_object_identity,
    try_acquire_snapshot_cleanup_lease,
)
from reparse_materialization import ReparseLink
from substrate_identity import tool_build_id
from verification_observability import emit_observability_event

ARTIFACT_INDEX_SCHEMA = 2
ARTIFACT_AUTHORITY = "PRECONDITION_CACHE"

# BLOCK_OMEGA_VERIFICATION_SUBSTRATE_V2
_LOCK = threading.RLock()
_CONFIGURED_ROOT: Optional[Path] = None
_INTEGRITY_VALIDATION_LOCK = threading.RLock()
_VALIDATED_CONTENT: set[tuple[str, str, str]] = set()
# slot -> (watcher, sealed directory-object identity at arm time)
_VALIDATION_WATCHERS: dict[
    tuple[str, str, str], tuple[_DirectoryWatcher, Optional[tuple[int, int]]]
] = {}
# Only exact sentinels created by this process may be ignored by its watcher.
_VALIDATION_DRAIN_TOKENS: dict[tuple[str, str, str], set[str]] = {}


def _retire_validation_watchers(*, key: Optional[str] = None) -> None:
    with _INTEGRITY_VALIDATION_LOCK:
        slots = [
            slot for slot in _VALIDATION_WATCHERS
            if key is None or slot[1] == key
        ]
        watchers = [_VALIDATION_WATCHERS.pop(slot)[0] for slot in slots]
        for slot in slots:
            _VALIDATED_CONTENT.discard(slot)
            _VALIDATION_DRAIN_TOKENS.pop(slot, None)
    for watcher in watchers:
        watcher.stop()


def _retire_validation_slot(slot: tuple[str, str, str]) -> None:
    with _INTEGRITY_VALIDATION_LOCK:
        entry = _VALIDATION_WATCHERS.pop(slot, None)
        _VALIDATED_CONTENT.discard(slot)
        _VALIDATION_DRAIN_TOKENS.pop(slot, None)
    if entry is not None:
        entry[0].stop()


def _watcher_is_clean(watcher: _DirectoryWatcher) -> bool:
    thread = watcher._thread
    return bool(
        thread is not None
        and thread.is_alive()
        and not watcher.events
        and not watcher.errors
    )


def _dependency_link_topology_issue(
    workspace: Path,
    dependency_roots: Sequence[str],
    progress: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Return why dependency bytes cannot use the watcher-only memo.

    ReadDirectoryChangesW observes names below ``workspace``. Creating or
    writing an NTFS hardlink through a name outside that tree need not produce
    an in-tree notification, but it does raise the sealed file object's link
    count. A metadata-only topology pass closes that blind spot without
    rehashing package payloads on every hot lookup.
    """
    pending = [
        workspace.joinpath(*Path(relative).parts)
        for relative in dependency_roots
    ]
    reparse_attribute = int(
        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0
    )
    scanned = 0
    heartbeat_at = time.monotonic() + 15
    while pending:
        if progress and time.monotonic() >= heartbeat_at:
            progress(f"prepared artifact continuity: checked {scanned} entries")
            heartbeat_at = time.monotonic() + 15
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError:
            return "link-topology-unavailable"
        for item in entries:
            scanned += 1
            path = Path(item.path)
            try:
                # CPython may leave DirEntry.stat().st_nlink as zero on
                # Windows even though os.stat() has the real NTFS count.
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                return "link-topology-unavailable"
            if (
                reparse_attribute
                and int(getattr(metadata, "st_file_attributes", 0) or 0)
                & reparse_attribute
            ):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif (
                stat.S_ISREG(metadata.st_mode)
                and int(getattr(metadata, "st_nlink", 1) or 1) != 1
            ):
                return "external-hardlink-observed"
    return None


# BLOCK_V_PREPARED_ARTIFACT_ASYNC_GC_V2
# PreparedArtifact is a performance cache. Heavy physical deletion must never
# block the authoritative verifier critical path.
_MAINTENANCE_THREADS_LOCK = threading.Lock()
_MAINTENANCE_THREADS: dict[str, threading.Thread] = {}
_DEFAULT_MAX_ARTIFACTS = 8
_DEFAULT_GC_INITIAL_DELAY_SECONDS = 30.0
_DEFAULT_GC_PAUSE_SECONDS = 5.0


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

    # Maintenance is asynchronous. The old synchronous rmtree here could
    # spend many minutes deleting an old node_modules tree immediately after
    # "resolver started", producing no verifier heartbeat and triggering the
    # Desktop HARD_STALL watchdog.
    schedule_prepared_artifact_maintenance(
        root=root,
        max_count=_DEFAULT_MAX_ARTIFACTS,
        initial_delay_seconds=_DEFAULT_GC_INITIAL_DELAY_SECONDS,
    )
    return root

def configured_prepared_artifact_root() -> Optional[Path]:
    with _LOCK:
        return _CONFIGURED_ROOT



def _windows_background_io_mode(begin: bool) -> bool:
    # Best-effort low-priority mode for cache deletion on Windows.
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriority.restype = ctypes.c_int
        # THREAD_MODE_BACKGROUND_BEGIN / THREAD_MODE_BACKGROUND_END
        priority = 0x00010000 if begin else 0x00020000
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), priority))
    except Exception:
        return False


def _maintenance_root_key(root: Path) -> str:
    try:
        value = str(root.resolve())
    except OSError:
        value = str(root.absolute())
    return os.path.normcase(value)


def _retired_trash_root(root: Path) -> Path:
    target = root / "trash"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _retire_workspace_tree(key: str, workspace: Path) -> Optional[Path]:
    # Atomically detach before slow deletion. If deletion is interrupted, the
    # tree remains discoverable under trash/ and can be retried later.
    root = configured_prepared_artifact_root()
    if root is None:
        return None

    try:
        trees = (root / "trees").resolve()
        container = workspace.parent.resolve()
        container.relative_to(trees)
    except (OSError, ValueError):
        return None

    if container == trees:
        return None

    target = _retired_trash_root(root) / (
        f"{container.name}.{_valid_key(key)[:12]}.{os.getpid()}.{time.time_ns()}"
    )
    try:
        os.replace(container, target)
    except OSError:
        return None
    return target


def reap_prepared_artifact_trash(
    *,
    root: Optional[Path] = None,
    max_removals: int = 1,
) -> int:
    # Detached trees have no published locator and no proof authority.
    target_root = root or configured_prepared_artifact_root()
    if target_root is None:
        return 0

    trash = target_root / "trash"
    if not trash.is_dir():
        return 0

    limit = max(1, int(max_removals))
    try:
        candidates = sorted(
            [path for path in trash.iterdir() if path.is_dir()],
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return 0

    removed = 0
    for path in candidates:
        try:
            shutil.rmtree(path)
        except OSError:
            # Keep partial trash for a future maintenance pass.
            continue
        removed += 1
        if removed >= limit:
            break
    return removed


def _prepared_artifact_maintenance_worker(
    root: Path,
    *,
    max_count: int,
    initial_delay_seconds: float,
) -> None:
    root_key = _maintenance_root_key(root)
    maintenance_lease = None
    background_mode = False

    try:
        if initial_delay_seconds > 0:
            time.sleep(initial_delay_seconds)
        if not root.is_dir():
            return

        # Only one heavy cleaner per verification store across Windows
        # processes. Independent project Baselines may still verify in parallel.
        if os.name == "nt":
            maintenance_lease = try_acquire_snapshot_cleanup_lease(root)
            if maintenance_lease is None:
                return
            background_mode = _windows_background_io_mode(True)

        while True:
            configured = configured_prepared_artifact_root()
            if configured is None or _maintenance_root_key(configured) != root_key:
                return

            # Bound one pass to one old trash tree and one over-limit artifact.
            reaped = reap_prepared_artifact_trash(root=root, max_removals=1)
            pruned = prune_prepared_artifact_store(
                max_count=max_count,
                max_removals=1,
            )

            if reaped == 0 and pruned == 0:
                return

            time.sleep(_DEFAULT_GC_PAUSE_SECONDS)
    finally:
        if background_mode:
            _windows_background_io_mode(False)
        if maintenance_lease is not None:
            maintenance_lease.close()

        with _MAINTENANCE_THREADS_LOCK:
            current = _MAINTENANCE_THREADS.get(root_key)
            if current is threading.current_thread():
                _MAINTENANCE_THREADS.pop(root_key, None)


def schedule_prepared_artifact_maintenance(
    *,
    root: Optional[Path] = None,
    max_count: int = _DEFAULT_MAX_ARTIFACTS,
    initial_delay_seconds: float = _DEFAULT_GC_INITIAL_DELAY_SECONDS,
) -> bool:
    # No synthetic verifier heartbeat is emitted here. Background cache work
    # must never hide a genuine authoritative verifier stall.
    target = root or configured_prepared_artifact_root()
    if target is None:
        return False

    root_key = _maintenance_root_key(target)

    with _MAINTENANCE_THREADS_LOCK:
        existing = _MAINTENANCE_THREADS.get(root_key)
        if existing is not None and existing.is_alive():
            return False

        worker = threading.Thread(
            target=_prepared_artifact_maintenance_worker,
            kwargs={
                "root": target,
                "max_count": max(1, int(max_count)),
                "initial_delay_seconds": max(0.0, float(initial_delay_seconds)),
            },
            name="deploom-prepared-artifact-gc",
            daemon=True,
        )
        _MAINTENANCE_THREADS[root_key] = worker
        worker.start()
        return True


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


def _normalize_dependency_roots(values) -> list[str]:
    result: list[str] = []
    for raw in values or ():
        value = str(raw or "").strip().replace("\\", "/").strip("/")
        if not value:
            continue
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("PREPARED_ARTIFACT_DEPENDENCY_ROOT_INVALID")
        if path.name != "node_modules":
            raise ValueError("PREPARED_ARTIFACT_DEPENDENCY_ROOT_INVALID")
        result.append(path.as_posix())
    return sorted(set(result))


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
    dependency_roots: Sequence[str] = (),
    reparse_plan: Optional[Sequence[ReparseLink]] = None,
    progress: Optional[Callable[[str], None]] = None,
    progress_interval_seconds: int = 15,
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
    try:
        integrity = build_artifact_tree_integrity(
            workspace_root,
            progress=progress,
            progress_label="snapshot-publish: durable-record integrity seal",
            progress_interval_seconds=progress_interval_seconds,
            reparse_plan=reparse_plan,
        )
    except ArtifactIntegrityError as exc:
        emit_observability_event(
            "prepared-artifact.integrity",
            outcome="publish-rejected",
            artifactKey=str(key),
            errorType=type(exc).__name__, error=str(exc),
        )
        if progress:
            progress(f"snapshot-publish: durable record rejected: {exc}")
        return False
    payload = {
        "schemaVersion": ARTIFACT_INDEX_SCHEMA,
        "authority": ARTIFACT_AUTHORITY,
        "key": _valid_key(key),
        "toolBuildId": integrity.tool_build_id,
        "artifactContentKey": integrity.key,
        "artifactFileCount": integrity.file_count,
        "artifactDirectoryCount": integrity.directory_count,
        "artifactByteCount": integrity.byte_count,
        "artifactReparseCount": integrity.reparse_count,
        "sourceProjectIdentity": _source_identity(source_project),
        "workspaceRoot": str(workspace_root),
        "projectRelative": project_relative.as_posix(),
        "storageMode": str(storage_mode),
        "observedResolvedVersions": dict(sorted(
            (str(name), str(version))
            for name, version in observed_resolved_versions.items()
        )),
        "observedResolvedHash": observed_hash,
        "dependencyRoots": _normalize_dependency_roots(dependency_roots),
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
        slot = (
            os.path.normcase(str(workspace_root)),
            _valid_key(key),
            integrity.key,
        )
        with _INTEGRITY_VALIDATION_LOCK:
            _VALIDATED_CONTENT.add(slot)
        emit_observability_event(
            "prepared-artifact.integrity",
            outcome="published",
            artifactKey=_valid_key(key),
            artifactContentKey=integrity.key,
            fileCount=integrity.file_count,
            byteCount=integrity.byte_count,
            toolBuildId=integrity.tool_build_id,
        )
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
    *,
    progress: Optional[Callable[[str], None]] = None,
) -> Optional[dict[str, object]]:
    path = _record_path(key)
    if path is None or not path.is_file():
        return None
    try:
        record_text = path.read_text(encoding="utf-8")
        payload = json.loads(record_text)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("schemaVersion") != ARTIFACT_INDEX_SCHEMA:
        return None
    if payload.get("authority") != ARTIFACT_AUTHORITY or payload.get("key") != _valid_key(key):
        return None
    current_build_id = tool_build_id()
    content_key = str(payload.get("artifactContentKey") or "").lower()
    if payload.get("toolBuildId") != current_build_id:
        invalidate_prepared_artifact_record(key, remove_tree=False)
        return None
    if len(content_key) != 64 or any(ch not in "0123456789abcdef" for ch in content_key):
        invalidate_prepared_artifact_record(key, remove_tree=False)
        return None
    if payload.get("sourceProjectIdentity") != _source_identity(source_project):
        return None
    workspace_raw = payload.get("workspaceRoot")
    relative_raw = payload.get("projectRelative")
    observed_hash = str(payload.get("observedResolvedHash") or "").lower()
    versions = payload.get("observedResolvedVersions")
    try:
        dependency_roots = _normalize_dependency_roots(payload.get("dependencyRoots") or ())
    except ValueError:
        return None
    if not isinstance(workspace_raw, str) or not isinstance(relative_raw, str):
        return None
    if not isinstance(versions, dict) or not all(isinstance(name, str) and isinstance(version, str) for name, version in versions.items()):
        return None
    if len(observed_hash) != 64 or any(ch not in "0123456789abcdef" for ch in observed_hash):
        return None
    relative = Path(relative_raw)
    if relative.is_absolute() or ".." in relative.parts:
        invalidate_prepared_artifact_record(key, remove_tree=False)
        return None
    workspace = Path(workspace_raw).resolve()
    if not workspace.is_dir() or not is_durable_prepared_path(workspace):
        invalidate_prepared_artifact_record(key)
        return None
    validation_slot = (os.path.normcase(str(workspace)), _valid_key(key), content_key)
    with _INTEGRITY_VALIDATION_LOCK:
        watcher_entry = _VALIDATION_WATCHERS.get(validation_slot)
        memory_hit = validation_slot in _VALIDATED_CONTENT
    watcher = watcher_entry[0] if watcher_entry is not None else None
    sealed_identity = watcher_entry[1] if watcher_entry is not None else None

    validation_mode = "full-hash"
    candidate_watcher: Optional[_DirectoryWatcher] = None
    if watcher is not None:
        # A memo HIT must mean the SAME sealed bytes, the SAME directory object
        # and proven notification delivery -- not merely "the watcher has not
        # spoken". Anything short of that falls back to authoritative hashing;
        # watcher uncertainty is never a HIT and never a reason to destroy a
        # durable record.
        current_identity = tree_object_identity(workspace)
        delivered, drain_token = drain_watcher(watcher, workspace)
        with _INTEGRITY_VALIDATION_LOCK:
            owned_tokens = _VALIDATION_DRAIN_TOKENS.setdefault(validation_slot, set())
            owned_tokens.add(drain_token)
            owned_tokens = frozenset(owned_tokens)
        pending_events = [
            item for item in list(watcher.events) if item[1] not in owned_tokens
        ]
        thread = watcher._thread
        fallback_reason = ""
        if sealed_identity is None or current_identity is None:
            fallback_reason = "object-identity-unavailable"
        elif current_identity != sealed_identity:
            fallback_reason = "directory-identity-changed"
        elif watcher.errors:
            fallback_reason = "watcher-failure"
        elif thread is None or not thread.is_alive():
            fallback_reason = "watcher-thread-dead"
        elif not delivered:
            fallback_reason = "watcher-drain-unconfirmed"
        elif pending_events:
            fallback_reason = "watcher-event-observed"
        elif len(owned_tokens) > 256:
            fallback_reason = "drain-history-limit"
        elif any(os.path.lexists(workspace / token) for token in owned_tokens):
            # A sentinel must have been removed. Recreating an old sentinel name
            # cannot smuggle a new persistent file into the sealed tree.
            fallback_reason = "drain-sentinel-still-present"
        else:
            fallback_reason = _dependency_link_topology_issue(
                workspace, dependency_roots, progress=progress
            ) or ""

        if fallback_reason:
            emit_observability_event(
                "prepared-artifact.integrity", outcome="watcher-fallback",
                reason=fallback_reason,
                artifactKey=_valid_key(key),
                watcherErrors=list(watcher.errors),
                watcherEvents=len(pending_events),
            )
            _retire_validation_slot(validation_slot)
            watcher = None
            if os.name == "nt":
                candidate_watcher = _DirectoryWatcher(workspace)
                if not candidate_watcher.start():
                    candidate_watcher.stop()
                    candidate_watcher = None
        else:
            validation_mode = "watcher-memo"
    elif os.name == "nt":
        candidate_watcher = _DirectoryWatcher(workspace)
        if not candidate_watcher.start():
            candidate_watcher.stop()
            candidate_watcher = None

    if validation_mode == "full-hash":
        try:
            if progress:
                progress("prepared artifact continuity: full integrity validation started")
            observed_integrity = build_artifact_tree_integrity(
                workspace, progress=progress,
                progress_label="prepared artifact continuity",
            )
        except ArtifactIntegrityError as exc:
            if candidate_watcher is not None:
                candidate_watcher.stop()
            emit_observability_event(
                "prepared-artifact.integrity", outcome="validation-error",
                artifactKey=_valid_key(key), errorType=type(exc).__name__, error=str(exc),
            )
            invalidate_prepared_artifact_record(key, remove_tree=False)
            return None
        if (
            observed_integrity.key != content_key
            or observed_integrity.tool_build_id != current_build_id
        ):
            if candidate_watcher is not None:
                candidate_watcher.stop()
            emit_observability_event(
                "prepared-artifact.integrity", outcome="mismatch",
                artifactKey=_valid_key(key), expectedContentKey=content_key,
                observedContentKey=observed_integrity.key, toolBuildId=current_build_id,
            )
            invalidate_prepared_artifact_record(key, remove_tree=False)
            return None
        if candidate_watcher is not None and not _watcher_is_clean(candidate_watcher):
            candidate_watcher.stop()
            emit_observability_event(
                "prepared-artifact.integrity", outcome="watcher-invalidated-during-hash",
                artifactKey=_valid_key(key),
            )
            invalidate_prepared_artifact_record(key, remove_tree=False)
            return None

    try:
        durable_record_unchanged = path.is_file() and path.read_text(encoding="utf-8") == record_text
    except OSError:
        durable_record_unchanged = False
    if not durable_record_unchanged:
        if candidate_watcher is not None:
            candidate_watcher.stop()
        emit_observability_event(
            "prepared-artifact.cache", memoryHit=memory_hit,
            durableRecordPresent=path.is_file(), generationMatch=False,
            contentKeyMatch=True, invalidationReason="durable-record-changed",
        )
        return None
    with _INTEGRITY_VALIDATION_LOCK:
        _VALIDATED_CONTENT.add(validation_slot)
        if candidate_watcher is not None:
            _VALIDATION_WATCHERS[validation_slot] = (
                candidate_watcher,
                tree_object_identity(workspace),
            )
    emit_observability_event(
        "prepared-artifact.cache", memoryHit=memory_hit,
        durableRecordPresent=True, generationMatch=True,
        contentKeyMatch=True, invalidationReason="",
        validationMode=validation_mode,
    )
    project = workspace / relative
    if not project.is_dir():
        invalidate_prepared_artifact_record(key)
        return None
    return {
        "key": _valid_key(key), "workspaceRoot": workspace,
        "projectRelative": relative,
        "storageMode": str(payload.get("storageMode") or "durable-prepared-artifact"),
        "toolBuildId": current_build_id, "artifactContentKey": content_key,
        "observedResolvedVersions": dict(sorted(versions.items())),
        "observedResolvedHash": observed_hash,
        "dependencyRoots": tuple(dependency_roots),
        "publishedAt": str(payload.get("publishedAt") or ""),
    }

def invalidate_prepared_artifact_record(key: str, *, remove_tree: bool = False) -> bool:
    normalized_key = _valid_key(key)
    _retire_validation_watchers(key=normalized_key)
    with _INTEGRITY_VALIDATION_LOCK:
        _VALIDATED_CONTENT.difference_update([
            item for item in _VALIDATED_CONTENT if item[1] == normalized_key
        ])
    path = _record_path(normalized_key)
    if path is None:
        return False

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

    lease = None
    retired_tree: Optional[Path] = None

    if workspace is not None and workspace.exists():
        if os.name == "nt":
            lease = try_acquire_snapshot_cleanup_lease(workspace)
            if lease is None:
                # A live verifier still owns this artifact. Preserve both the
                # locator and tree and retry on a later maintenance pass.
                return False

        retired_tree = _retire_workspace_tree(key, workspace)
        if retired_tree is None:
            if lease is not None:
                lease.close()
            # Do not invalidate a usable cache locator unless tree retirement
            # became crash-safe.
            return False

    try:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False

        if retired_tree is not None:
            try:
                shutil.rmtree(retired_tree)
            except OSError:
                # Tree is already detached under trash/. A later background
                # pass can resume deletion without affecting proof identity.
                pass

        return not path.exists()
    finally:
        if lease is not None:
            lease.close()


def _clear_artifact_integrity_validation_cache_for_tests() -> None:
    _retire_validation_watchers()
    with _INTEGRITY_VALIDATION_LOCK:
        _VALIDATED_CONTENT.clear()


def prune_prepared_artifact_store(
    max_count: int = 8,
    *,
    max_removals: Optional[int] = None,
) -> int:
    # Best-effort LRU-ish pruning. configure_prepared_artifact_store never calls
    # this synchronously; automatic maintenance always uses max_removals=1.
    root = configured_prepared_artifact_root()
    if root is None:
        return 0

    limit = max(1, int(max_count))
    removal_limit = None if max_removals is None else max(1, int(max_removals))
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
        if invalidate_prepared_artifact_record(record.stem, remove_tree=True):
            removed += 1
            if removal_limit is not None and removed >= removal_limit:
                break

    return removed


def verification_trial_parent(proof_cache_dir: str | Path | None = None) -> Optional[Path]:
    """Common parent for fresh trials when an optimized root was selected."""
    accelerated = verification_root()
    if accelerated is not None:
        root = accelerated / "trials"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None

