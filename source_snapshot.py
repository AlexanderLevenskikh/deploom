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
from pathlib import Path
from typing import Callable, Iterable, Optional

from verification_workspace_backend import materialize_private_tree

# BLOCK_X_SOURCE_TRUTH_V1
SOURCE_SNAPSHOT_SCHEMA = "source-snapshot-v1-captured-content"
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
_LOCK = threading.RLock()


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


def _validate_git_layout(capture_root: Path) -> None:
    marker = capture_root / ".git"
    if not marker.is_file():
        return
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raise SourceCaptureError(f"SOURCE_GIT_MARKER_UNREADABLE: {marker}: {exc}") from exc
    if not text.lower().startswith("gitdir:"):
        raise SourceCaptureError(f"SOURCE_GIT_MARKER_INVALID: {marker}")
    target = Path(text.split(":", 1)[1].strip())
    if not target.is_absolute():
        target = marker.parent / target
    if not _within(target, capture_root):
        # A linked worktree's .git file points at the parent repository's
        # common object database. Copying only the worktree would create a
        # source snapshot that looks self-contained but is not.
        raise SourceCaptureError(
            "SOURCE_GIT_LINKED_WORKTREE_UNSUPPORTED: root .git points outside "
            f"captured tree: {target.resolve(strict=False)}"
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


def _hash_regular_file(path: Path) -> tuple[str, os.stat_result]:
    try:
        before = path.stat(follow_symlinks=False)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SourceCaptureError(f"SOURCE_FILE_UNREADABLE: {path}: {exc}") from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", 0) != getattr(after, "st_ino", 0)
    ):
        raise SourceCaptureError(f"SOURCE_CAPTURE_UNSTABLE: file changed while hashing: {path}")
    return digest.hexdigest(), after


def build_source_tree_manifest(
    root: Path,
    *,
    policy: SourceInputPolicy = SourceInputPolicy(),
    timeout_seconds: int = 1800,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "source manifest",
    progress_interval_seconds: int = 15,
) -> SourceTreeManifest:
    root = root.resolve()
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    next_progress = time.monotonic() + max(1, int(progress_interval_seconds))
    entries: list[dict[str, object]] = []
    files = 0
    directories = 0
    byte_count = 0

    def tick() -> None:
        nonlocal next_progress
        now = time.monotonic()
        if now > deadline:
            raise SourceCaptureError(
                f"SOURCE_MATERIALIZATION_TIMEOUT: {progress_label} exceeded {timeout_seconds}s"
            )
        if progress is not None and now >= next_progress:
            progress(
                f"{progress_label}: files={files}, dirs={directories}, bytes={byte_count}"
            )
            next_progress = now + max(1, int(progress_interval_seconds))

    def walk(directory: Path, relative_dir: Path) -> None:
        nonlocal files, directories, byte_count
        tick()
        try:
            scanned = sorted(os.scandir(directory), key=lambda entry: entry.name)
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
                # Portable copy backends do not promise to preserve source
                # hardlink aliasing. Outside .git that aliasing can be a
                # semantic build input, so X.1 refuses rather than silently
                # proving a tree with different mutation semantics.
                if int(getattr(st, "st_nlink", 1) or 1) > 1 and not (
                    relative.parts and relative.parts[0] == ".git"
                ):
                    raise SourceCaptureError(
                        f"SOURCE_HARDLINK_UNSUPPORTED: {relative.as_posix()} nlink={st.st_nlink}"
                    )
                digest, stable = _hash_regular_file(path)
                files += 1
                byte_count += int(stable.st_size)
                entries.append({
                    "path": relative.as_posix(),
                    "kind": "file",
                    "size": int(stable.st_size),
                    "sha256": digest,
                    # Executability can change build behaviour on POSIX.
                    "executable": bool(stable.st_mode & 0o111),
                })
                continue

            raise SourceCaptureError(
                f"SOURCE_SPECIAL_FILE_UNSUPPORTED: {relative.as_posix()} mode={oct(st.st_mode)}"
            )

    walk(root, Path("."))
    entries.sort(key=lambda entry: str(entry["path"]))
    key = _canonical_hash({
        "schema": SOURCE_SNAPSHOT_SCHEMA,
        "policyKey": policy.key,
        "entries": entries,
    }, length=64)
    if progress is not None:
        progress(f"{progress_label}: ready; files={files}, dirs={directories}, bytes={byte_count}")
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
    _local_dependency_preflight(project_path, capture_root)

    pre = build_source_tree_manifest(
        capture_root,
        policy=policy,
        timeout_seconds=_remaining(deadline),
        progress=progress,
        progress_label="source capture pre-manifest",
        progress_interval_seconds=progress_interval_seconds,
    )

    container = Path(tempfile.mkdtemp(prefix="dependency-flow-source-snapshot-"))
    _ALL_CONTAINERS.add(container)
    snapshot_root = container / "tree"
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
        captured = build_source_tree_manifest(
            snapshot_root,
            policy=policy,
            timeout_seconds=_remaining(deadline),
            progress=progress,
            progress_label="source capture sealed-manifest",
            progress_interval_seconds=progress_interval_seconds,
        )
        post = build_source_tree_manifest(
            capture_root,
            policy=policy,
            timeout_seconds=_remaining(deadline),
            progress=progress,
            progress_label="source capture post-manifest",
            progress_interval_seconds=progress_interval_seconds,
        )
        if pre.key != post.key or captured.key != post.key:
            raise SourceCaptureError(
                "SOURCE_CAPTURE_UNSTABLE: pre/captured/post content manifests differ"
            )

        key = _canonical_hash({
            "schema": SOURCE_SNAPSHOT_SCHEMA,
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
        shutil.rmtree(container, ignore_errors=True)
        _ALL_CONTAINERS.discard(container)
        raise


def capture_source_snapshot(
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
            shutil.rmtree(existing.container, ignore_errors=True)
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


def proof_subject_project_dir(project_dir: Path) -> Path:
    snapshot = active_source_snapshot(project_dir)
    return snapshot.project_path if snapshot is not None else project_dir.expanduser().resolve()


def source_snapshot_fingerprint(project_dir: Path) -> str:
    snapshot = active_source_snapshot(project_dir)
    if snapshot is not None:
        return snapshot.key
    root, relative, _head = _subject_layout(project_dir)
    manifest = build_source_tree_manifest(root)
    return _canonical_hash({
        "schema": SOURCE_SNAPSHOT_SCHEMA,
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
    method = materialize_private_tree(
        snapshot.root,
        target,
        timeout_seconds=timeout_seconds,
        progress=progress,
        progress_label=progress_label,
        progress_interval_seconds=progress_interval_seconds,
    )
    project = (target / snapshot.project_relative).resolve()
    if not project.is_dir():
        raise SourceCaptureError(
            f"SOURCE_MATERIALIZED_PROJECT_MISSING: {snapshot.project_relative.as_posix()}"
        )
    return project, snapshot, method


def clear_source_snapshot_epochs() -> None:
    with _LOCK:
        snapshots = list(_ACTIVE.values())
        _ACTIVE.clear()
    for snapshot in snapshots:
        shutil.rmtree(snapshot.container, ignore_errors=True)
        _ALL_CONTAINERS.discard(snapshot.container)


def _cleanup_all() -> None:
    with _LOCK:
        _ACTIVE.clear()
        containers = list(_ALL_CONTAINERS)
        _ALL_CONTAINERS.clear()
    for container in containers:
        shutil.rmtree(container, ignore_errors=True)


atexit.register(_cleanup_all)
