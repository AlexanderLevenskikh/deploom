#!/usr/bin/env python3
"""Block X SourceSnapshot: bind proof identity to the bytes Verifier consumes.

Git is provenance, not source authority. The authoritative subject is a
sealed content tree captured from the live filesystem under one explicit
policy. `gitignore` is deliberately ignored by this policy: ignored files
such as .env or generated source are included unless an explicit DepLoom
exclusion says otherwise.
"""
from __future__ import annotations

import atexit
import concurrent.futures
import dataclasses
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable, Optional

from verification_workspace_backend import materialize_private_tree
from prepared_workspace_fastpath import (
    _DirectoryWatcher,
    apply_tree_write_protection,
)
from io_governor import io_slot
from reparse_materialization import ReparseMaterializationError
from verification_observability import (
    configured_observability_path,
    emit_observability_event,
    new_observability_id,
    process_resource_snapshot,
    suppress_observability,
)
from project_topology import (
    ProjectTopologyError,
    semantic_manifest_paths,
)
from substrate_identity import tool_build_id
# BLOCK_Z_PROJECT_TOPOLOGY_V1

# BLOCK_Y_FULL_OBSERVABILITY_V1

# BLOCK_X_SOURCE_TRUTH_V1
SOURCE_SNAPSHOT_SCHEMA = "source-snapshot-v2-tool-build-content"
SOURCE_INPUT_POLICY_SCHEMA = "source-input-policy-v1-explicit"
SOURCE_CAPTURE_RETRIES = 3

# Deliberately small and explicit. In particular: dist/, build/, .next/,
# generated source and every gitignored file are INCLUDED in X.1 because
# excluding an input is unsound unless DepLoom has an explicit semantic
# policy for it. node_modules is separate resolved dependency state.
DEFAULT_EXCLUDED_DIR_NAMES = (
    "node_modules",
    ".dependency-roadmap",
    ".idea",
    ".vs",
    ".vscode",
    ".fleet",
)
DEFAULT_EXCLUDED_FILE_NAMES = (
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
)

ProgressCallback = Callable[[str], None]


class SourceCaptureError(RuntimeError):
    """Source capture uncertainty is infrastructure, never incompatibility."""


@dataclasses.dataclass(frozen=True)
class SourceInputPolicy:
    excluded_dir_names: tuple[str, ...] = DEFAULT_EXCLUDED_DIR_NAMES
    excluded_file_names: tuple[str, ...] = DEFAULT_EXCLUDED_FILE_NAMES

    @property
    def key(self) -> str:
        return _canonical_hash({
            "schema": SOURCE_INPUT_POLICY_SCHEMA,
            "excludedDirNames": list(self.excluded_dir_names),
            "excludedFileNames": list(self.excluded_file_names),
        }, length=64)


@dataclasses.dataclass(frozen=True)
class SourceTreeManifest:
    key: str
    entries: tuple[dict[str, object], ...]
    file_count: int
    directory_count: int
    byte_count: int


@dataclasses.dataclass(frozen=True)
class SourceSnapshot:
    original_project_path: Path
    capture_root: Path
    project_relative: Path
    container: Path
    root: Path
    project_path: Path
    key: str
    manifest_key: str
    policy_key: str
    file_count: int
    directory_count: int
    byte_count: int
    materialization_method: str
    git_head: str
    git_root: str
    created_at: float


_ACTIVE: dict[str, SourceSnapshot] = {}
_ALL_CONTAINERS: set[Path] = set()
# (snapshot, watcher, sealed directory-object identity at arm time)
_ACTIVE_WATCHERS: dict[
    str, tuple[SourceSnapshot, _DirectoryWatcher, Optional[object]]
] = {}
_LOCK = threading.RLock()


def _tree_object_identity(path: Path) -> Optional[tuple[int, int]]:
    """Filesystem object identity of a directory, not just its pathname.

    A watched directory can be renamed away and a different directory put back
    at the same textual path. The watcher holds a handle to the ORIGINAL object
    and reports nothing, while `is_dir()` on the path still succeeds. Only the
    (device, file-id) pair distinguishes the two objects.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    if not stat.st_ino:
        return None
    return (stat.st_dev, stat.st_ino)


def _snapshot_object_identity(
    snapshot: SourceSnapshot,
) -> Optional[tuple[Optional[tuple[int, int]], Optional[tuple[int, int]]]]:
    root = _tree_object_identity(snapshot.root)
    project = _tree_object_identity(snapshot.project_path)
    if root is None or project is None:
        return None
    return (root, project)


_DRAIN_MARKER_DIR = ".dependency-roadmap"


def _content_relevant_events(
    events: "list[tuple[int, str]]",
) -> "list[tuple[int, str]]":
    """Drop notifications for paths the sealed manifest does not measure.

    An excluded path cannot change the content key by definition, so a write
    there is not evidence of content mutation.
    """
    relevant: list[tuple[int, str]] = []
    for action, relative in events:
        parts = [part for part in relative.replace("\\", "/").split("/") if part]
        if any(part in DEFAULT_EXCLUDED_DIR_NAMES for part in parts):
            continue
        if parts and parts[-1] in DEFAULT_EXCLUDED_FILE_NAMES:
            continue
        relevant.append((action, relative))
    return relevant


def _drain_watcher(
    watcher: _DirectoryWatcher,
    root: Path,
    *,
    timeout_seconds: float = 2.0,
) -> bool:
    """Prove every change that completed before this boundary was delivered.

    ReadDirectoryChangesW reports changes for one tree in order. Writing a
    sentinel inside the watched subtree and then waiting for that sentinel's
    OWN notification therefore proves that every earlier change has already
    been observed. Without this barrier a mutation that completed moments
    before consumption could still be in flight, and the memo would report a
    clean tree that is in fact already stale.

    The sentinel lives under an excluded directory, so it can never alter the
    sealed content key. Returns False when delivery could not be confirmed --
    the caller must then fall back to authoritative validation rather than
    treating silence as proof.
    """
    marker_directory = root / _DRAIN_MARKER_DIR
    token = f".drain-{uuid.uuid4().hex}"
    marker = marker_directory / token
    try:
        marker_directory.mkdir(parents=True, exist_ok=True)
        marker.write_text("drain", encoding="utf-8")
    except OSError:
        return False
    deadline = time.monotonic() + max(0.05, float(timeout_seconds))
    delivered = False
    try:
        while time.monotonic() < deadline:
            if any(token in relative for _action, relative in list(watcher.events)):
                delivered = True
                break
            if watcher.errors:
                break
            time.sleep(0.005)
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
    return delivered


def _watcher_is_clean(watcher: _DirectoryWatcher) -> bool:
    thread = watcher._thread
    return bool(
        thread is not None
        and thread.is_alive()
        and not watcher.events
        and not watcher.errors
    )


def _retire_snapshot_watcher(snapshot: SourceSnapshot) -> None:
    key = _active_key(snapshot.original_project_path)
    with _LOCK:
        entry = _ACTIVE_WATCHERS.get(key)
        watcher = None
        if entry is not None and entry[0] is snapshot:
            _ACTIVE_WATCHERS.pop(key, None)
            watcher = entry[1]
    if watcher is not None:
        watcher.stop()


def _apply_tree_write_protection(root: Path, *, readonly: bool) -> int:
    """Seal or release a private source tree.

    Delegates to the shared reparse-safe implementation: a naive walk would
    follow junctions out of this tree and unprotect whatever they point at.
    """
    return apply_tree_write_protection(root, readonly=readonly)


def _force_rmtree(path: Path) -> None:
    """Remove a private tree that may be write-protected.

    A sealed tree is read-only, and `shutil.rmtree(ignore_errors=True)` would
    silently fail to remove it on Windows -- turning every sealed container
    into a permanent orphan.
    """
    try:
        _apply_tree_write_protection(path, readonly=False)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def _canonical_hash(value: object, *, length: int = 64) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def _active_key(project_dir: Path) -> str:
    return os.path.normcase(str(project_dir.expanduser().resolve()))


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _run_git(project_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(project_dir), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(["git", *args], 127, stdout="", stderr=str(exc))


def _subject_layout(project_dir: Path, *, require_git: bool = False) -> tuple[Path, Path, str]:
    project_dir = project_dir.expanduser().resolve()
    result = _run_git(project_dir, ["rev-parse", "--show-toplevel"])
    if result.returncode == 0 and result.stdout.strip():
        root = Path(result.stdout.strip()).resolve()
        try:
            relative = project_dir.relative_to(root)
        except ValueError as exc:
            raise SourceCaptureError(
                f"SOURCE_PROJECT_OUTSIDE_GIT_ROOT: project={project_dir}, root={root}"
            ) from exc
        head_result = _run_git(project_dir, ["rev-parse", "HEAD"])
        if head_result.returncode != 0 or not head_result.stdout.strip():
            detail = (head_result.stderr or head_result.stdout or "empty HEAD").strip()
            raise SourceCaptureError(f"SOURCE_GIT_HEAD_UNAVAILABLE: {detail}")
        return root, relative, head_result.stdout.strip()
    if require_git:
        detail = (result.stderr or result.stdout or "not a Git repository").strip()
        raise SourceCaptureError(f"SOURCE_GIT_REQUIRED: {detail}")
    return project_dir, Path("."), ""


def _iter_source_files_without_links(
    root: Path,
    *,
    policy: SourceInputPolicy = SourceInputPolicy(),
) -> Iterable[Path]:
    """Walk policy coverage without following symlinks or reparse directories."""
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError as exc:
            raise SourceCaptureError(
                f"SOURCE_DIRECTORY_UNREADABLE: {directory}: {exc}"
            ) from exc
        for item in entries:
            path = Path(item.path)
            try:
                relative = path.relative_to(root)
            except ValueError as exc:
                raise SourceCaptureError(
                    f"SOURCE_ENTRY_OUTSIDE_ROOT: {path}"
                ) from exc
            if _excluded(relative, policy):
                continue
            try:
                if item.is_symlink():
                    continue
                if item.is_dir(follow_symlinks=False):
                    is_junction = getattr(path, "is_junction", None)
                    if callable(is_junction) and is_junction():
                        continue
                    pending.append(path)
                elif item.is_file(follow_symlinks=False):
                    yield path
            except OSError as exc:
                raise SourceCaptureError(
                    f"SOURCE_ENTRY_UNREADABLE: {path}: {exc}"
                ) from exc


def _read_git_indirection(path: Path, *, prefix: str = "") -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeError) as exc:
        raise SourceCaptureError(
            f"SOURCE_GIT_INDIRECTION_UNREADABLE: {path}: {exc}"
        ) from exc
    if prefix and not text.lower().startswith(prefix.lower()):
        raise SourceCaptureError(f"SOURCE_GIT_MARKER_INVALID: {path}")
    return text.split(":", 1)[1].strip() if prefix else text


def _require_sealed_relative_git_target(
    *,
    capture_root: Path,
    marker: Path,
    raw_target: str,
    relative_base: Path,
    kind: str,
) -> None:
    target = Path(raw_target).expanduser()
    if target.is_absolute():
        raise SourceCaptureError(
            f"SOURCE_GIT_ABSOLUTE_INDIRECTION_UNSUPPORTED: {kind}: "
            f"{marker} -> {raw_target}"
        )
    resolved = (relative_base / target).resolve(strict=False)
    if not _within(resolved, capture_root):
        raise SourceCaptureError(
            f"SOURCE_GIT_EXTERNAL_INDIRECTION_UNSUPPORTED: {kind}: "
            f"{marker} -> {raw_target}"
        )


def _validate_git_layout(
    capture_root: Path,
    *,
    policy: SourceInputPolicy = SourceInputPolicy(),
) -> None:
    """Reject every Git metadata edge that would escape a sealed copy.

    Root and nested repositories are covered. Absolute indirections are
    rejected even when they currently resolve inside the live checkout because
    the absolute text would still point back to that mutable checkout after
    materialization.
    """
    capture_root = capture_root.resolve()
    for path in _iter_source_files_without_links(capture_root, policy=policy):
        if path.name == ".git":
            target = _read_git_indirection(path, prefix="gitdir:")
            _require_sealed_relative_git_target(
                capture_root=capture_root,
                marker=path,
                raw_target=target,
                relative_base=path.parent,
                kind="gitdir",
            )
            continue

        if (
            path.name == "commondir"
            and ".git" in path.relative_to(capture_root).parts
        ):
            target = _read_git_indirection(path)
            _require_sealed_relative_git_target(
                capture_root=capture_root,
                marker=path,
                raw_target=target,
                relative_base=path.parent,
                kind="commondir",
            )
            continue

        if (
            path.name == "alternates"
            and path.parent.name == "info"
            and path.parent.parent.name == "objects"
        ):
            try:
                lines = path.read_text(
                    encoding="utf-8", errors="strict"
                ).splitlines()
            except (OSError, UnicodeError) as exc:
                raise SourceCaptureError(
                    f"SOURCE_GIT_ALTERNATES_UNREADABLE: {path}: {exc}"
                ) from exc
            object_database = path.parent.parent
            for line in lines:
                target = line.strip()
                if not target or target.startswith("#"):
                    continue
                _require_sealed_relative_git_target(
                    capture_root=capture_root,
                    marker=path,
                    raw_target=target,
                    relative_base=object_database,
                    kind="objects/info/alternates",
                )


def _submodule_preflight(capture_root: Path) -> None:
    if not (capture_root / ".gitmodules").is_file():
        return
    result = _run_git(capture_root, ["submodule", "status", "--recursive"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise SourceCaptureError(f"SOURCE_SUBMODULE_STATUS_FAILED: {detail}")
    for line in result.stdout.splitlines():
        if not line:
            continue
        state = line[0]
        if state == "-":
            raise SourceCaptureError(
                f"SOURCE_SUBMODULE_INCOMPLETE: uninitialized submodule: {line[1:].strip()}"
            )
        if state == "U":
            raise SourceCaptureError(
                f"SOURCE_SUBMODULE_INCOMPLETE: conflicted submodule: {line[1:].strip()}"
            )
        # '+' (checked-out commit differs from superproject gitlink) is
        # allowed: Source Truth is the actual checked-out bytes, which the
        # canonical manifest hashes below.


def _local_dependency_preflight(project_path: Path, capture_root: Path) -> None:
    manifest_path = project_path / "package.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SourceCaptureError(f"SOURCE_PACKAGE_JSON_UNREADABLE: {manifest_path}: {exc}") from exc
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        values = manifest.get(section)
        if not isinstance(values, dict):
            continue
        for name, raw in values.items():
            spec = str(raw or "").strip()
            lowered = spec.lower()
            if not lowered.startswith(("file:", "link:", "portal:")):
                continue
            payload = spec.split(":", 1)[1].strip()
            if not payload:
                continue
            target = Path(payload).expanduser()
            if not target.is_absolute():
                target = project_path / target
            target = target.resolve(strict=False)
            if not _within(target, capture_root):
                raise SourceCaptureError(
                    "SOURCE_EXTERNAL_LOCAL_DEPENDENCY_UNSUPPORTED: "
                    f"{name}={spec} resolves outside captured root: {target}"
                )


def _excluded(relative: Path, policy: SourceInputPolicy) -> bool:
    if any(part in policy.excluded_dir_names for part in relative.parts):
        return True
    return relative.name in policy.excluded_file_names


def _hash_regular_file(
    path: Path,
    *,
    suppress_worker_observability: bool = False,
) -> tuple[str, os.stat_result]:
    try:
        before = path.stat(follow_symlinks=False)
        digest = hashlib.sha256()
        def hash_stream() -> None:
            with io_slot("hash", label="source-manifest"):
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
        if suppress_worker_observability:
            # ContextVars do not automatically propagate into ThreadPoolExecutor
            # workers. Re-establish suppression in the worker so an in-source
            # telemetry sink cannot mutate the Source Truth while it is hashed.
            with suppress_observability():
                hash_stream()
        else:
            hash_stream()
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SourceCaptureError(f"SOURCE_FILE_UNREADABLE: {path}: {exc}") from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_mode != after.st_mode
        or int(getattr(before, "st_nlink", 1) or 1) != int(getattr(after, "st_nlink", 1) or 1)
        or getattr(before, "st_ino", 0) != getattr(after, "st_ino", 0)
    ):
        raise SourceCaptureError(f"SOURCE_CAPTURE_UNSTABLE: file changed while hashing: {path}")
    return digest.hexdigest(), after


def _source_hash_workers() -> int:
    raw = str(os.environ.get("DEPLOOM_SOURCE_HASH_WORKERS") or "").strip()
    if raw:
        try:
            return max(1, min(32, int(raw)))
        except ValueError:
            pass
    logical = max(1, int(os.cpu_count() or 4))
    return max(2, min(8, logical // 2 or 1))


def _directory_stability_stamp(path: Path) -> tuple[int, int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SourceCaptureError(f"SOURCE_DIRECTORY_UNREADABLE: {path}: {exc}") from exc
    return (
        int(value.st_mtime_ns),
        int(value.st_size),
        int(getattr(value, "st_ino", 0)),
    )


def _build_source_tree_manifest_impl(
    root: Path,
    *,
    policy: SourceInputPolicy = SourceInputPolicy(),
    timeout_seconds: int = 1800,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "source manifest",
    progress_interval_seconds: int = 15,
    suppress_worker_observability: bool = False,
    require_exclusive_links: bool = False,
) -> SourceTreeManifest:
    """Build a deterministic manifest with bounded parallel file hashing.

    Only a small multiple of worker futures may exist at once; million-file
    repositories therefore do not allocate a future per file. Directory stamps
    are rechecked after hashing so add/remove/rename races fail closed.
    """
    root = root.resolve()
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    next_progress = time.monotonic() + max(1, int(progress_interval_seconds))
    entries: list[dict[str, object]] = []
    files = 0
    directories = 0
    byte_count = 0
    workers = _source_hash_workers()
    max_pending = max(workers, workers * 3)
    directory_stamps: list[tuple[Path, tuple[int, int, int]]] = []
    pending: dict[concurrent.futures.Future[tuple[str, os.stat_result]], tuple[Path, str]] = {}

    def tick() -> None:
        nonlocal next_progress
        now = time.monotonic()
        if now > deadline:
            raise SourceCaptureError(
                f"SOURCE_MATERIALIZATION_TIMEOUT: {progress_label} exceeded {timeout_seconds}s"
            )
        if progress is not None and now >= next_progress:
            progress(
                f"{progress_label}: files={files}, dirs={directories}, bytes={byte_count}, workers={workers}"
            )
            next_progress = now + max(1, int(progress_interval_seconds))

    def accept_future(future: concurrent.futures.Future[tuple[str, os.stat_result]]) -> None:
        nonlocal files, byte_count
        path, relative_text = pending.pop(future)
        digest, stable = future.result()
        if require_exclusive_links and int(getattr(stable, "st_nlink", 1) or 1) > 1:
            # A sealed tree is a fresh private copy, so every file must have
            # exactly one directory entry. A second link is an alias through
            # which bytes can be rewritten WITHOUT any watcher notification
            # inside the watched subtree.
            raise SourceCaptureError(
                "SOURCE_SNAPSHOT_LINK_TOPOLOGY_VIOLATION: "
                f"{relative_text}: sealed file has {int(stable.st_nlink)} links; "
                "an external alias can mutate sealed bytes unobserved"
            )
        files += 1
        byte_count += int(stable.st_size)
        entries.append({
            "path": relative_text,
            "kind": "file",
            "size": int(stable.st_size),
            "sha256": digest,
            "executable": bool(stable.st_mode & 0o111),
        })
        tick()

    def drain_one() -> None:
        if not pending:
            return
        done, _ = concurrent.futures.wait(
            tuple(pending), return_when=concurrent.futures.FIRST_COMPLETED
        )
        for future in done:
            accept_future(future)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="deploom-source-hash",
    ) as executor:
        def walk(directory: Path, relative_dir: Path) -> None:
            nonlocal directories
            tick()
            directory_stamps.append((directory, _directory_stability_stamp(directory)))
            try:
                with os.scandir(directory) as iterator:
                    scanned = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                raise SourceCaptureError(f"SOURCE_DIRECTORY_UNREADABLE: {directory}: {exc}") from exc
            for item in scanned:
                relative = relative_dir / item.name
                if _excluded(relative, policy):
                    continue
                path = Path(item.path)
                tick()
                try:
                    st = item.stat(follow_symlinks=False)
                except OSError as exc:
                    raise SourceCaptureError(f"SOURCE_ENTRY_UNREADABLE: {path}: {exc}") from exc

                is_junction = getattr(path, "is_junction", None)
                if callable(is_junction):
                    try:
                        if is_junction():
                            raise SourceCaptureError(
                                f"SOURCE_UNSUPPORTED_REPARSE_POINT: junction: {path}"
                            )
                    except OSError as exc:
                        raise SourceCaptureError(
                            f"SOURCE_ENTRY_UNREADABLE: junction probe {path}: {exc}"
                        ) from exc

                if stat.S_ISLNK(st.st_mode):
                    try:
                        target_text = os.readlink(path)
                    except OSError as exc:
                        raise SourceCaptureError(f"SOURCE_SYMLINK_UNREADABLE: {path}: {exc}") from exc
                    target = Path(target_text)
                    if not target.is_absolute():
                        target = path.parent / target
                    if not _within(target, root):
                        raise SourceCaptureError(
                            f"SOURCE_SYMLINK_ESCAPE: {relative.as_posix()} -> {target_text}"
                        )
                    try:
                        target_relative = target.resolve(strict=False).relative_to(root)
                    except ValueError:
                        target_relative = Path(".")
                    if _excluded(target_relative, policy):
                        raise SourceCaptureError(
                            f"SOURCE_SYMLINK_TARGET_EXCLUDED: {relative.as_posix()} -> {target_text}"
                        )
                    entries.append({
                        "path": relative.as_posix(),
                        "kind": "symlink",
                        "target": target_text.replace("\\", "/"),
                    })
                    continue

                if stat.S_ISDIR(st.st_mode):
                    directories += 1
                    entries.append({"path": relative.as_posix(), "kind": "directory"})
                    walk(path, relative)
                    continue

                if stat.S_ISREG(st.st_mode):
                    if int(getattr(st, "st_nlink", 1) or 1) > 1 and not (
                        relative.parts and relative.parts[0] == ".git"
                    ):
                        raise SourceCaptureError(
                            f"SOURCE_HARDLINK_UNSUPPORTED: {relative.as_posix()} nlink={st.st_nlink}"
                        )
                    future = executor.submit(
                    _hash_regular_file,
                    path,
                    suppress_worker_observability=suppress_worker_observability,
                )
                    pending[future] = (path, relative.as_posix())
                    if len(pending) >= max_pending:
                        drain_one()
                    continue

                raise SourceCaptureError(
                    f"SOURCE_SPECIAL_FILE_UNSUPPORTED: {relative.as_posix()} mode={oct(st.st_mode)}"
                )

        walk(root, Path("."))
        while pending:
            drain_one()

    for directory, stamp in directory_stamps:
        if _directory_stability_stamp(directory) != stamp:
            raise SourceCaptureError(
                f"SOURCE_CAPTURE_UNSTABLE: directory changed while hashing: {directory}"
            )

    entries.sort(key=lambda entry: str(entry["path"]))
    key = _canonical_hash({
        "schema": SOURCE_SNAPSHOT_SCHEMA,
        "toolBuildId": tool_build_id(),
        "policyKey": policy.key,
        "entries": entries,
    }, length=64)
    if progress is not None:
        progress(
            f"{progress_label}: ready; files={files}, dirs={directories}, bytes={byte_count}, workers={workers}"
        )
    return SourceTreeManifest(
        key=key,
        entries=tuple(entries),
        file_count=files,
        directory_count=directories,
        byte_count=byte_count,
    )


def _remaining(deadline: float) -> int:
    value = int(deadline - time.monotonic())
    if value <= 0:
        raise SourceCaptureError("SOURCE_MATERIALIZATION_TIMEOUT: source capture deadline exhausted")
    return max(1, value)


def _capture_once(
    project_dir: Path,
    *,
    policy: SourceInputPolicy,
    timeout_seconds: int,
    progress: Optional[ProgressCallback],
    progress_interval_seconds: int,
) -> SourceSnapshot:
    started = time.monotonic()
    deadline = started + max(1, int(timeout_seconds))
    capture_root, project_relative, git_head = _subject_layout(project_dir)
    project_path = (capture_root / project_relative).resolve()
    _validate_git_layout(capture_root)
    _submodule_preflight(capture_root)
    try:
        topology_manifests = semantic_manifest_paths(project_path)
    except ProjectTopologyError:
        # Topology authority will reject unsupported/malformed layouts before
        # verification. Source capture still checks the selected manifest here
        # rather than weakening the existing standalone SourceSnapshot API.
        topology_manifests = ((project_path / "package.json").resolve(),)
    for manifest_path in topology_manifests:
        if manifest_path.is_file():
            _local_dependency_preflight(manifest_path.parent, capture_root)

    _reap_once()
    container = Path(
        tempfile.mkdtemp(prefix=SOURCE_SNAPSHOT_CONTAINER_PREFIX)
    )
    if _within(container, capture_root):
        _force_rmtree(container)
        raise SourceCaptureError(
            "SOURCE_SNAPSHOT_TEMP_INSIDE_SUBJECT: "
            f"container={container}; captureRoot={capture_root}"
        )
    _ALL_CONTAINERS.add(container)
    snapshot_root = container / "tree"
    try:
        try:
            method = materialize_private_tree(
                capture_root,
                snapshot_root,
                timeout_seconds=_remaining(deadline),
                progress=progress,
                progress_label="source capture materialization",
                progress_interval_seconds=progress_interval_seconds,
                exclude_dir_names=policy.excluded_dir_names,
                exclude_file_names=policy.excluded_file_names,
            )
        except ReparseMaterializationError as exc:
            detail = str(exc)
            if os.name != "nt" and "REPARSE_EXTERNAL_TARGET_UNSUPPORTED" in detail:
                raise SourceCaptureError(f"SOURCE_SYMLINK_ESCAPE: {detail}") from exc
            raise SourceCaptureError(f"SOURCE_REPARSE_LAYOUT_UNSUPPORTED: {detail}") from exc
        # Seal BEFORE hashing so the sealed manifest measures bytes that are
        # already write-protected against every alias to the same file object.
        _apply_tree_write_protection(snapshot_root, readonly=True)
        captured = build_source_tree_manifest(
            snapshot_root,
            policy=policy,
            timeout_seconds=_remaining(deadline),
            progress=progress,
            progress_label="source capture sealed-manifest",
            progress_interval_seconds=progress_interval_seconds,
            pass_role="sealed-snapshot",
        )
        post = build_source_tree_manifest(
            capture_root,
            policy=policy,
            timeout_seconds=_remaining(deadline),
            progress=progress,
            progress_label="source capture live-final-manifest",
            progress_interval_seconds=progress_interval_seconds,
            pass_role="live-final",
        )
        if captured.key != post.key:
            raise SourceCaptureError(
                "SOURCE_CAPTURE_UNSTABLE: sealed/live-final content manifests differ"
            )

        key = _canonical_hash({
            "schema": SOURCE_SNAPSHOT_SCHEMA,
            "toolBuildId": tool_build_id(),
            "policyKey": policy.key,
            "manifestKey": captured.key,
            "projectRelative": project_relative.as_posix() or ".",
        }, length=32)
        manifest_path = container / "manifest.json"
        manifest_path.write_text(
            json.dumps({
                "schemaVersion": 1,
                "type": "deploom-source-snapshot",
                "sourceSnapshotKey": key,
                "toolBuildId": tool_build_id(),
                "manifestKey": captured.key,
                "policyKey": policy.key,
                "projectRelative": project_relative.as_posix() or ".",
                "gitHead": git_head,
                "fileCount": captured.file_count,
                "directoryCount": captured.directory_count,
                "byteCount": captured.byte_count,
                "materializationMethod": method,
                # Hashes + paths only. Never persist source contents here.
                "entries": list(captured.entries),
            }, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return SourceSnapshot(
            original_project_path=project_dir.resolve(),
            capture_root=capture_root,
            project_relative=project_relative,
            container=container,
            root=snapshot_root,
            project_path=(snapshot_root / project_relative).resolve(),
            key=key,
            manifest_key=captured.key,
            policy_key=policy.key,
            file_count=captured.file_count,
            directory_count=captured.directory_count,
            byte_count=captured.byte_count,
            materialization_method=method,
            git_head=git_head,
            git_root=str(capture_root) if git_head else "",
            created_at=time.time(),
        )
    except Exception:
        _force_rmtree(container)
        _ALL_CONTAINERS.discard(container)
        raise


def _capture_source_snapshot_impl(
    project_dir: Path,
    *,
    policy: SourceInputPolicy = SourceInputPolicy(),
    timeout_seconds: int = 1800,
    progress: Optional[ProgressCallback] = None,
    progress_interval_seconds: int = 15,
) -> SourceSnapshot:
    last: Optional[BaseException] = None
    for attempt in range(1, SOURCE_CAPTURE_RETRIES + 1):
        try:
            if progress is not None:
                progress(f"SourceSnapshot capture attempt {attempt}/{SOURCE_CAPTURE_RETRIES}")
            return _capture_once(
                project_dir,
                policy=policy,
                timeout_seconds=timeout_seconds,
                progress=progress,
                progress_interval_seconds=progress_interval_seconds,
            )
        except SourceCaptureError as exc:
            last = exc
            if "SOURCE_CAPTURE_UNSTABLE" not in str(exc) or attempt >= SOURCE_CAPTURE_RETRIES:
                raise
            if progress is not None:
                progress(f"SourceSnapshot capture unstable; retrying: {exc}")
    raise SourceCaptureError(f"SOURCE_CAPTURE_UNSTABLE: {last}")


def activate_source_snapshot_epoch(
    project_dir: Path,
    *,
    timeout_seconds: int = 1800,
    progress: Optional[ProgressCallback] = None,
    progress_interval_seconds: int = 15,
    replace: bool = False,
) -> SourceSnapshot:
    key = _active_key(project_dir)
    with _LOCK:
        existing = _ACTIVE.get(key)
        if existing is not None and not replace:
            return existing
        if existing is not None:
            _ACTIVE.pop(key, None)
            watcher_entry = _ACTIVE_WATCHERS.pop(key, None)
            if watcher_entry is not None:
                watcher_entry[1].stop()
            _force_rmtree(existing.container)
            _ALL_CONTAINERS.discard(existing.container)
        snapshot = capture_source_snapshot(
            project_dir,
            timeout_seconds=timeout_seconds,
            progress=progress,
            progress_interval_seconds=progress_interval_seconds,
        )
        _ACTIVE[key] = snapshot
        return snapshot


def active_source_snapshot(project_dir: Path) -> Optional[SourceSnapshot]:
    with _LOCK:
        return _ACTIVE.get(_active_key(project_dir))


def validate_source_snapshot(
    snapshot: SourceSnapshot,
    *,
    timeout_seconds: int = 1800,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "active SourceSnapshot validation",
) -> SourceSnapshot:
    """Prove snapshot bytes once, then keep that proof live with a watcher."""
    started = time.monotonic()
    active_key = _active_key(snapshot.original_project_path)
    with _LOCK:
        watcher_entry = _ACTIVE_WATCHERS.get(active_key)
        usable_entry = (
            watcher_entry is not None and watcher_entry[0] is snapshot
        )
        watcher = watcher_entry[1] if usable_entry else None
        sealed_identity = watcher_entry[2] if usable_entry else None
        is_active = _ACTIVE.get(active_key) is snapshot
    # Drain BEFORE reading events: a mutation that completed just before this
    # boundary may still be in flight, and silence would otherwise be misread
    # as proof of an unchanged tree.
    drain_confirmed = True
    if watcher is not None:
        drain_confirmed = _drain_watcher(watcher, snapshot.root)
    watcher_events = (
        _content_relevant_events(list(watcher.events)) if watcher is not None else []
    )
    watcher_errors = list(watcher.errors) if watcher is not None else []

    fallback_reason = ""
    if watcher is not None:
        # A memo hit is only sound while the watched path still resolves to the
        # SAME directory object that was sealed. Path identity is not object
        # identity: a directory swap leaves `is_dir()` true and the watcher --
        # which holds a handle to the original object -- permanently silent.
        current_identity = _snapshot_object_identity(snapshot)
        identity_stable = (
            sealed_identity is not None
            and current_identity is not None
            and current_identity == sealed_identity
        )
        if not snapshot.root.is_dir() or not snapshot.project_path.is_dir():
            fallback_reason = "root-missing"
        elif not identity_stable:
            fallback_reason = "directory-identity-changed"
        elif watcher.errors:
            fallback_reason = "watcher-failure"
        elif not (watcher._thread is not None and watcher._thread.is_alive()):
            fallback_reason = "watcher-thread-dead"
        elif not drain_confirmed:
            # Delivery could not be proven, so watcher silence proves nothing.
            fallback_reason = "watcher-drain-unconfirmed"
        elif watcher_events:
            # Could be a real mutation or an attribute-only touch. The watcher
            # cannot tell us which, so it does not get to decide the verdict.
            fallback_reason = "watcher-event-observed"

        if not fallback_reason:
            emit_observability_event(
                "source.snapshot.validation",
                snapshotKey=snapshot.key, outcome="watcher-memo-hit",
                generation=str(snapshot.created_at),
                validationMs=max(0, int((time.monotonic() - started) * 1000)),
            )
            return snapshot

        # Watcher uncertainty is NEVER a content mismatch and NEVER a pass.
        # Retire the memo and let authoritative full validation decide. The
        # snapshot stays registered so a benign event can revalidate cleanly.
        _retire_snapshot_watcher(snapshot)
        watcher = None
        emit_observability_event(
            "source.snapshot.validation",
            snapshotKey=snapshot.key, outcome="watcher-fallback",
            reason=fallback_reason,
            generation=str(snapshot.created_at),
            watcherEvents=len(watcher_events),
            watcherErrors=list(watcher_errors),
        )

    candidate_watcher: Optional[_DirectoryWatcher] = None
    if os.name == "nt" and is_active and snapshot.root.is_dir():
        candidate_watcher = _DirectoryWatcher(snapshot.root)
        if not candidate_watcher.start():
            candidate_watcher.stop()
            candidate_watcher = None

    try:
        observed = build_source_tree_manifest(
            snapshot.root,
            policy=SourceInputPolicy(),
            timeout_seconds=timeout_seconds,
            progress=progress,
            progress_label=progress_label,
            pass_role="active-validation",
        )
    except SourceCaptureError:
        if candidate_watcher is not None:
            candidate_watcher.stop()
        with _LOCK:
            if _ACTIVE.get(active_key) is snapshot:
                _ACTIVE.pop(active_key, None)
        emit_observability_event(
            "source.snapshot.validation",
            snapshotKey=snapshot.key, outcome="error",
            generation=str(snapshot.created_at),
            validationMs=max(0, int((time.monotonic() - started) * 1000)),
        )
        raise
    if observed.key != snapshot.manifest_key:
        if candidate_watcher is not None:
            candidate_watcher.stop()
        with _LOCK:
            if _ACTIVE.get(active_key) is snapshot:
                _ACTIVE.pop(active_key, None)
        emit_observability_event(
            "source.snapshot.validation",
            snapshotKey=snapshot.key, outcome="content-mismatch",
            generation=str(snapshot.created_at),
            validationMs=max(0, int((time.monotonic() - started) * 1000)),
            files=observed.file_count, bytes=observed.byte_count,
        )
        raise SourceCaptureError(
            "SOURCE_SNAPSHOT_CONTENT_MISMATCH: "
            f"expected={snapshot.manifest_key}; observed={observed.key}"
        )
    if candidate_watcher is not None and not _watcher_is_clean(candidate_watcher):
        candidate_watcher.stop()
        with _LOCK:
            if _ACTIVE.get(active_key) is snapshot:
                _ACTIVE.pop(active_key, None)
        raise SourceCaptureError(
            "SOURCE_SNAPSHOT_CONTENT_MISMATCH: snapshot changed during validation"
        )
    if candidate_watcher is not None:
        with _LOCK:
            if _ACTIVE.get(active_key) is snapshot:
                _ACTIVE_WATCHERS[active_key] = (
                    snapshot,
                    candidate_watcher,
                    _snapshot_object_identity(snapshot),
                )
                candidate_watcher = None
        if candidate_watcher is not None:
            candidate_watcher.stop()
    emit_observability_event(
        "source.snapshot.validation",
        snapshotKey=snapshot.key, outcome="passed",
        generation=str(snapshot.created_at),
        validationMs=max(0, int((time.monotonic() - started) * 1000)),
        files=observed.file_count, bytes=observed.byte_count,
    )
    return snapshot


def proof_subject_project_dir(project_dir: Path) -> Path:
    snapshot = active_source_snapshot(project_dir)
    return validate_source_snapshot(snapshot).project_path if snapshot is not None else project_dir.expanduser().resolve()


def source_snapshot_fingerprint(project_dir: Path) -> str:
    snapshot = active_source_snapshot(project_dir)
    if snapshot is not None:
        return validate_source_snapshot(snapshot).key
    root, relative, _head = _subject_layout(project_dir)
    manifest = build_source_tree_manifest(root)
    return _canonical_hash({
        "schema": SOURCE_SNAPSHOT_SCHEMA,
        "toolBuildId": tool_build_id(),
        "policyKey": SourceInputPolicy().key,
        "manifestKey": manifest.key,
        "projectRelative": relative.as_posix() or ".",
    }, length=32)


def source_snapshot_provenance_head(project_dir: Path, *, require_git: bool = False) -> str:
    snapshot = active_source_snapshot(project_dir)
    if snapshot is not None:
        if require_git and not snapshot.git_head:
            raise SourceCaptureError("SOURCE_GIT_REQUIRED: active source snapshot is not a Git repository")
        return snapshot.git_head
    _root, _relative, head = _subject_layout(project_dir, require_git=require_git)
    return head


def materialize_source_for_verification(
    project_dir: Path,
    target: Path,
    *,
    timeout_seconds: int,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "source snapshot materialization",
    progress_interval_seconds: int = 15,
) -> tuple[Path, SourceSnapshot, str]:
    snapshot = active_source_snapshot(project_dir)
    if snapshot is None:
        snapshot = capture_source_snapshot(
            project_dir,
            timeout_seconds=timeout_seconds,
            progress=progress,
            progress_interval_seconds=progress_interval_seconds,
        )
    validate_source_snapshot(
        snapshot,
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_label=f"{progress_label} pre-consumption validation",
    )
    method = materialize_private_tree(
        snapshot.root,
        target,
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_label=progress_label,
        progress_interval_seconds=progress_interval_seconds,
    )
    observed = build_source_tree_manifest(
        target,
        policy=SourceInputPolicy(),
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_label=f"{progress_label} post-copy validation",
        pass_role="materialized-validation",
    )
    if observed.key != snapshot.manifest_key:
        _force_rmtree(target)
        raise SourceCaptureError(
            "SOURCE_MATERIALIZED_CONTENT_MISMATCH: "
            f"expected={snapshot.manifest_key}; observed={observed.key}"
        )
    # The sealed lower is immutable, but this materialized copy is a working
    # tree: the package manager must be able to write into it.
    _apply_tree_write_protection(target, readonly=False)
    project = (target / snapshot.project_relative).resolve()
    if not project.is_dir():
        raise SourceCaptureError(
            f"SOURCE_MATERIALIZED_PROJECT_MISSING: {snapshot.project_relative.as_posix()}"
        )
    return project, snapshot, method


def clear_source_snapshot_epochs() -> None:
    with _LOCK:
        snapshots = list(_ACTIVE.values())
        watchers = [entry[1] for entry in _ACTIVE_WATCHERS.values()]
        _ACTIVE.clear()
        _ACTIVE_WATCHERS.clear()
    for watcher in watchers:
        watcher.stop()
    for snapshot in snapshots:
        _force_rmtree(snapshot.container)
        _ALL_CONTAINERS.discard(snapshot.container)


SOURCE_SNAPSHOT_CONTAINER_PREFIX = "dependency-flow-source-snapshot-"
_REAP_MAX_AGE_SECONDS = 6 * 3600
_REAP_MAX_CONTAINERS = 8
_REAP_DONE = threading.Event()


def reap_orphaned_source_snapshots(
    *,
    max_age_seconds: int = _REAP_MAX_AGE_SECONDS,
    max_containers: int = _REAP_MAX_CONTAINERS,
) -> dict[str, int]:
    """Remove source snapshot containers abandoned by a crashed/killed run.

    `_cleanup_all` only runs at a clean exit, so a crash, a kill or a watchdog
    stop leaves the whole sealed tree behind forever. This reaper is bounded on
    purpose: it does a little work per process rather than an unbounded sweep.

    Liveness is decided by the rename itself. Windows refuses to rename a
    directory that another process still has open, so a successful atomic
    rename into a trash name is the proof that nothing is using the container.
    A container this process owns is never considered. Failing to reap is
    telemetry, never corruption -- the tree is simply left for a later run.
    """
    reaped = skipped = failed = 0
    base = Path(tempfile.gettempdir())
    now = time.time()
    with _LOCK:
        owned = {os.path.normcase(str(item)) for item in _ALL_CONTAINERS}
    try:
        candidates = sorted(base.glob(SOURCE_SNAPSHOT_CONTAINER_PREFIX + "*"))
    except OSError:
        candidates = []
    for container in candidates:
        if reaped >= max_containers:
            break
        if os.path.normcase(str(container)) in owned or not container.is_dir():
            skipped += 1
            continue
        try:
            age = now - container.stat().st_mtime
        except OSError:
            skipped += 1
            continue
        if age < max_age_seconds:
            skipped += 1
            continue
        trash = container.with_name(f"{container.name}.reap-{os.getpid()}")
        try:
            os.rename(container, trash)
        except OSError:
            # Still open somewhere: treat as live and leave it alone.
            skipped += 1
            continue
        try:
            # Sealed trees are write-protected, so a plain rmtree would fail
            # and silently turn every orphan into a permanent one.
            _force_rmtree(trash)
            reaped += 1
        except Exception:
            failed += 1
    if reaped or failed:
        emit_observability_event(
            "source.snapshot.reap",
            reaped=reaped,
            skipped=skipped,
            failed=failed,
            maxAgeSeconds=int(max_age_seconds),
        )
    return {"reaped": reaped, "skipped": skipped, "failed": failed}


def _reap_once() -> None:
    """Bounded, best-effort, at most once per process."""
    if _REAP_DONE.is_set():
        return
    _REAP_DONE.set()
    try:
        reap_orphaned_source_snapshots()
    except Exception:
        pass


def _cleanup_all() -> None:
    with _LOCK:
        _ACTIVE.clear()
        watchers = [entry[1] for entry in _ACTIVE_WATCHERS.values()]
        _ACTIVE_WATCHERS.clear()
        containers = list(_ALL_CONTAINERS)
        _ALL_CONTAINERS.clear()
    for watcher in watchers:
        watcher.stop()
    for container in containers:
        _force_rmtree(container)


atexit.register(_cleanup_all)

def _observability_sink_is_source_safe(
    root: Path,
    policy: SourceInputPolicy,
) -> bool:
    sink = configured_observability_path()
    if sink is None:
        return True
    root = root.resolve()
    sink = sink.resolve()
    try:
        relative = sink.relative_to(root)
    except ValueError:
        return True
    return _excluded(relative, policy)


_EXCLUSIVE_LINK_PASS_ROLES = frozenset({
    "sealed-snapshot",
    "active-validation",
    "materialized-validation",
})


def build_source_tree_manifest(
    root: Path,
    *,
    policy: SourceInputPolicy = SourceInputPolicy(),
    timeout_seconds: int = 1800,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "source manifest",
    progress_interval_seconds: int = 15,
    pass_role: str = "",
) -> SourceTreeManifest:
    exclusive_links = str(pass_role) in _EXCLUSIVE_LINK_PASS_ROLES
    if not _observability_sink_is_source_safe(root, policy):
        with suppress_observability():
            return _build_source_tree_manifest_impl(
                root,
                policy=policy,
                timeout_seconds=timeout_seconds,
                progress=progress,
                progress_label=progress_label,
                progress_interval_seconds=progress_interval_seconds,
                suppress_worker_observability=True,
                require_exclusive_links=exclusive_links,
            )

    operation_id = new_observability_id("source-manifest")
    started = time.monotonic()
    before = process_resource_snapshot()
    emit_observability_event(
        "source.manifest.start",
        operationId=operation_id,
        label=progress_label,
        passRole=str(pass_role or progress_label),
        workers=_source_hash_workers(),
        timeoutSeconds=int(timeout_seconds),
        policyKey=policy.key,
    )
    try:
        manifest = _build_source_tree_manifest_impl(
            root,
            policy=policy,
            timeout_seconds=timeout_seconds,
            progress=progress,
            progress_label=progress_label,
            progress_interval_seconds=progress_interval_seconds,
            require_exclusive_links=exclusive_links,
        )
    except BaseException as exc:
        after = process_resource_snapshot()
        emit_observability_event(
            "source.manifest.finish",
            operationId=operation_id,
            label=progress_label,
            passRole=str(pass_role or progress_label),
            workers=_source_hash_workers(),
            outcome="exception",
            durationMs=max(0, int((time.monotonic() - started) * 1000)),
            cpuMs=max(0, after.cpu_ms - before.cpu_ms),
            rssBytes=after.rss_bytes,
            peakRssBytes=after.peak_rss_bytes,
            errorType=type(exc).__name__,
        )
        raise
    after = process_resource_snapshot()
    emit_observability_event(
        "source.manifest.finish",
        operationId=operation_id,
        label=progress_label,
        passRole=str(pass_role or progress_label),
        workers=_source_hash_workers(),
        outcome="passed",
        manifestKey=manifest.key,
        fileCount=manifest.file_count,
        directoryCount=manifest.directory_count,
        byteCount=manifest.byte_count,
        durationMs=max(0, int((time.monotonic() - started) * 1000)),
        cpuMs=max(0, after.cpu_ms - before.cpu_ms),
        rssBytes=after.rss_bytes,
        peakRssBytes=after.peak_rss_bytes,
    )
    return manifest


def capture_source_snapshot(
    project_dir: Path,
    *,
    policy: SourceInputPolicy = SourceInputPolicy(),
    timeout_seconds: int = 1800,
    progress: Optional[ProgressCallback] = None,
    progress_interval_seconds: int = 15,
) -> SourceSnapshot:
    capture_root, _project_relative, _git_head = _subject_layout(project_dir)
    if not _observability_sink_is_source_safe(capture_root, policy):
        with suppress_observability():
            return _capture_source_snapshot_impl(
                project_dir,
                policy=policy,
                timeout_seconds=timeout_seconds,
                progress=progress,
                progress_interval_seconds=progress_interval_seconds,
            )

    operation_id = new_observability_id("source-capture")
    started = time.monotonic()
    before = process_resource_snapshot()
    emit_observability_event(
        "source.capture.start",
        operationId=operation_id,
        timeoutSeconds=int(timeout_seconds),
        policyKey=policy.key,
    )
    try:
        snapshot = _capture_source_snapshot_impl(
            project_dir,
            policy=policy,
            timeout_seconds=timeout_seconds,
            progress=progress,
            progress_interval_seconds=progress_interval_seconds,
        )
    except BaseException as exc:
        after = process_resource_snapshot()
        emit_observability_event(
            "source.capture.finish",
            operationId=operation_id,
            outcome="exception",
            durationMs=max(0, int((time.monotonic() - started) * 1000)),
            cpuMs=max(0, after.cpu_ms - before.cpu_ms),
            rssBytes=after.rss_bytes,
            peakRssBytes=after.peak_rss_bytes,
            errorType=type(exc).__name__,
        )
        raise
    after = process_resource_snapshot()
    emit_observability_event(
        "source.capture.finish",
        operationId=operation_id,
        outcome="passed",
        sourceSnapshotKey=snapshot.key,
        manifestKey=snapshot.manifest_key,
        policyKey=snapshot.policy_key,
        fileCount=snapshot.file_count,
        directoryCount=snapshot.directory_count,
        byteCount=snapshot.byte_count,
        materializationMethod=snapshot.materialization_method,
        gitHead=bool(snapshot.git_head),
        durationMs=max(0, int((time.monotonic() - started) * 1000)),
        cpuMs=max(0, after.cpu_ms - before.cpu_ms),
        rssBytes=after.rss_bytes,
        peakRssBytes=after.peak_rss_bytes,
    )
    return snapshot
def open_source_snapshot(
    container: Path,
    *,
    expected_key: str = "",
    timeout_seconds: int = 1800,
) -> SourceSnapshot:
    """Open and strongly validate a durable SourceSnapshot container."""
    container = container.expanduser().resolve()
    manifest_path = container / "manifest.json"
    root = container / "tree"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SourceCaptureError(
            f"SOURCE_SNAPSHOT_MANIFEST_INVALID: {manifest_path}: {exc}"
        ) from exc
    if (
        not isinstance(raw, dict)
        or int(raw.get("schemaVersion", 0) or 0) != 1
        or raw.get("type") != "deploom-source-snapshot"
    ):
        raise SourceCaptureError("SOURCE_SNAPSHOT_MANIFEST_SCHEMA_INVALID")
    if not root.is_dir():
        raise SourceCaptureError(f"SOURCE_SNAPSHOT_TREE_MISSING: {root}")

    key = str(raw.get("sourceSnapshotKey") or "")
    manifest_key = str(raw.get("manifestKey") or "")
    policy_key = str(raw.get("policyKey") or "")
    current_build_id = tool_build_id()
    if raw.get("toolBuildId") != current_build_id:
        raise SourceCaptureError(
            "SOURCE_SNAPSHOT_TOOL_BUILD_MISMATCH: "
            f"expected={current_build_id}; observed={raw.get('toolBuildId')}"
        )
    if expected_key and key != expected_key:
        raise SourceCaptureError(
            "SOURCE_SNAPSHOT_KEY_MISMATCH: "
            f"expected={expected_key}; observed={key}"
        )
    policy = SourceInputPolicy()
    if policy_key != policy.key:
        raise SourceCaptureError(
            f"SOURCE_SNAPSHOT_POLICY_UNSUPPORTED: {policy_key}"
        )

    relative_text = str(raw.get("projectRelative") or ".")
    relative = Path(relative_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise SourceCaptureError(
            f"SOURCE_SNAPSHOT_PROJECT_RELATIVE_INVALID: {relative_text}"
        )
    project_path = (root / relative).resolve()
    if not _within(project_path, root) or not project_path.is_dir():
        raise SourceCaptureError(
            f"SOURCE_SNAPSHOT_PROJECT_MISSING: {relative_text}"
        )

    _validate_git_layout(root, policy=policy)
    observed = build_source_tree_manifest(
        root,
        policy=policy,
        timeout_seconds=timeout_seconds,
        progress_label="durable source snapshot validation",
    )
    if observed.key != manifest_key:
        raise SourceCaptureError(
            "SOURCE_SNAPSHOT_CONTENT_MISMATCH: "
            f"expected={manifest_key}; observed={observed.key}"
        )
    recomputed_key = _canonical_hash({
        "schema": SOURCE_SNAPSHOT_SCHEMA,
        "toolBuildId": current_build_id,
        "policyKey": policy_key,
        "manifestKey": observed.key,
        "projectRelative": relative.as_posix() or ".",
    }, length=32)
    if recomputed_key != key:
        raise SourceCaptureError(
            "SOURCE_SNAPSHOT_IDENTITY_MISMATCH: "
            f"expected={key}; observed={recomputed_key}"
        )

    return SourceSnapshot(
        original_project_path=project_path,
        capture_root=root,
        project_relative=relative,
        container=container,
        root=root,
        project_path=project_path,
        key=key,
        manifest_key=observed.key,
        policy_key=policy_key,
        file_count=observed.file_count,
        directory_count=observed.directory_count,
        byte_count=observed.byte_count,
        materialization_method=str(raw.get("materializationMethod") or "durable"),
        git_head=str(raw.get("gitHead") or ""),
        git_root=str(root) if raw.get("gitHead") else "",
        created_at=float(raw.get("createdAt") or manifest_path.stat().st_mtime),
    )


def persist_source_snapshot(
    snapshot: SourceSnapshot,
    destination: Path,
    *,
    timeout_seconds: int = 1800,
) -> SourceSnapshot:
    """Atomically publish an active snapshot as durable evidence."""
    validate_source_snapshot(snapshot, timeout_seconds=timeout_seconds)
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise SourceCaptureError(
            f"SOURCE_SNAPSHOT_DESTINATION_EXISTS: {destination}"
        )
    stage = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.tmp-",
        dir=str(destination.parent),
    ))
    try:
        shutil.copytree(
            snapshot.container,
            stage,
            dirs_exist_ok=True,
            symlinks=True,
        )
        open_source_snapshot(
            stage,
            expected_key=snapshot.key,
            timeout_seconds=timeout_seconds,
        )
        os.replace(stage, destination)
        return open_source_snapshot(
            destination,
            expected_key=snapshot.key,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def capture_durable_source_snapshot(
    project_dir: Path,
    destination: Path,
    *,
    timeout_seconds: int = 1800,
) -> SourceSnapshot:
    snapshot = capture_source_snapshot(
        project_dir,
        timeout_seconds=timeout_seconds,
    )
    return persist_source_snapshot(
        snapshot,
        destination,
        timeout_seconds=timeout_seconds,
    )


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="DepLoom sealed SourceSnapshot")
    parser.add_argument("--capture-durable", metavar="PROJECT")
    parser.add_argument("--destination")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.capture_durable:
        parser.error("--capture-durable is required")
    if not args.destination:
        parser.error("--destination is required")
    snapshot = capture_durable_source_snapshot(
        Path(args.capture_durable),
        Path(args.destination),
        timeout_seconds=args.timeout_seconds,
    )
    result = {
        "sourceSnapshotLocator": str(snapshot.container),
        "sourceSnapshotKey": snapshot.key,
        "toolBuildId": tool_build_id(),
        "projectRelative": snapshot.project_relative.as_posix() or ".",
        "manifestKey": snapshot.manifest_key,
        "fileCount": snapshot.file_count,
        "byteCount": snapshot.byte_count,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
