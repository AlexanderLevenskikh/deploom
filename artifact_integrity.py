#!/usr/bin/env python3
"""Strong whole-tree integrity seal for durable PreparedArtifacts."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import hashlib
import json
import os
import stat
from pathlib import Path
import time
from typing import Callable, Optional

ProgressCallback = Callable[[str], None]

from io_governor import io_slot
from reparse_materialization import (
    ReparseMaterializationError,
    inventory_reparse_plan,
)
from substrate_identity import tool_build_id

ARTIFACT_INTEGRITY_SCHEMA = "prepared-artifact-content-v1"


class ArtifactIntegrityError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ArtifactTreeIntegrity:
    key: str
    file_count: int
    directory_count: int
    byte_count: int
    reparse_count: int
    tool_build_id: str


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_file(path: Path) -> tuple[str, int, bool]:
    try:
        before = path.stat(follow_symlinks=False)
        digest = hashlib.sha256()
        with io_slot("hash", label="prepared-artifact-integrity"):
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ArtifactIntegrityError(
            f"PREPARED_ARTIFACT_FILE_UNREADABLE: {path}: {exc}"
        ) from exc
    stable_fields = (
        before.st_size == after.st_size,
        before.st_mtime_ns == after.st_mtime_ns,
        getattr(before, "st_ino", 0) == getattr(after, "st_ino", 0),
        int(getattr(before, "st_nlink", 1) or 1) == 1,
        int(getattr(after, "st_nlink", 1) or 1) == 1,
    )
    if not all(stable_fields):
        raise ArtifactIntegrityError(
            f"PREPARED_ARTIFACT_CONTENT_UNSTABLE: {path}"
        )
    return (
        digest.hexdigest(),
        int(after.st_size),
        bool(after.st_mode & 0o111),
    )


def _directory_stamp(path: Path) -> tuple[int, int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ArtifactIntegrityError(
            f"PREPARED_ARTIFACT_DIRECTORY_UNREADABLE: {path}: {exc}"
        ) from exc
    return (
        int(value.st_mtime_ns),
        int(value.st_size),
        int(getattr(value, "st_ino", 0)),
    )


def build_artifact_tree_integrity(
    root: Path,
    *,
    max_workers: Optional[int] = None,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "prepared artifact integrity",
    progress_interval_seconds: int = 15,
) -> ArtifactTreeIntegrity:
    """Whole-tree content seal.

    This is an O(files) pass over a durable artifact that can hold hundreds of
    thousands of entries, so it MUST report progress: a silent multi-minute
    stage is indistinguishable from a hang to anything watching it.
    """
    started = time.monotonic()
    next_progress = started + max(1, int(progress_interval_seconds))

    def heartbeat(subphase: str, done: int, total: Optional[int] = None) -> None:
        nonlocal next_progress
        if progress is None:
            return
        now = time.monotonic()
        if now < next_progress:
            return
        next_progress = now + max(1, int(progress_interval_seconds))
        scope = f"{done}/{total}" if total else str(done)
        progress(
            f"{progress_label}: {subphase} {scope}; "
            f"elapsed={int(now - started)}s"
        )
    root = root.resolve()
    if not root.is_dir():
        raise ArtifactIntegrityError(
            f"PREPARED_ARTIFACT_TREE_MISSING: {root}"
        )
    try:
        reparse_plan = inventory_reparse_plan(root)
    except ReparseMaterializationError as exc:
        raise ArtifactIntegrityError(
            f"PREPARED_ARTIFACT_REPARSE_INVALID: {exc}"
        ) from exc
    reparse_by_path = {
        item.link_relative: item
        for item in reparse_plan
    }

    entries: list[dict[str, object]] = []
    files: list[tuple[Path, str]] = []
    directories: list[tuple[Path, tuple[int, int, int]]] = []
    pending = [(root, Path("."))]
    while pending:
        directory, relative_directory = pending.pop()
        directories.append((directory, _directory_stamp(directory)))
        try:
            scanned = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ArtifactIntegrityError(
                f"PREPARED_ARTIFACT_DIRECTORY_UNREADABLE: {directory}: {exc}"
            ) from exc
        heartbeat("scanning", len(files))
        for item in scanned:
            path = Path(item.path)
            relative = relative_directory / item.name
            relative_text = relative.as_posix()
            planned = reparse_by_path.get(relative_text)
            if planned is not None:
                entries.append({
                    "path": relative_text,
                    "kind": planned.link_kind,
                    "target": planned.target_relative,
                    "authority": planned.authority,
                    "package": planned.package_name,
                })
                continue
            try:
                mode = item.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise ArtifactIntegrityError(
                    f"PREPARED_ARTIFACT_ENTRY_UNREADABLE: {path}: {exc}"
                ) from exc
            if stat.S_ISDIR(mode):
                entries.append({"path": relative_text, "kind": "directory"})
                pending.append((path, relative))
            elif stat.S_ISREG(mode):
                links = int(getattr(item.stat(follow_symlinks=False), "st_nlink", 1) or 1)
                if links > 1:
                    raise ArtifactIntegrityError(
                        "PREPARED_ARTIFACT_HARDLINK_UNSUPPORTED: "
                        f"{relative_text}; nlink={links}"
                    )
                files.append((path, relative_text))
            else:
                raise ArtifactIntegrityError(
                    "PREPARED_ARTIFACT_SPECIAL_FILE_UNSUPPORTED: "
                    f"{relative_text}; mode={oct(mode)}"
                )

    workers = max_workers
    if workers is None:
        raw = str(os.environ.get("DEPLOOM_ARTIFACT_HASH_WORKERS") or "").strip()
        try:
            workers = int(raw) if raw else min(8, max(2, os.cpu_count() or 2))
        except ValueError:
            workers = min(8, max(2, os.cpu_count() or 2))
    workers = max(1, min(32, int(workers)))
    byte_count = 0
    # Bound in-flight futures. A durable artifact may contain hundreds of
    # thousands of files; allocating one Future per path defeats the I/O
    # governor by creating avoidable RAM and scheduler pressure.
    queue_limit = max(workers, workers * 3)
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="deploom-artifact-hash",
    ) as executor:
        pending: dict[concurrent.futures.Future[tuple[str, int, bool]], tuple[Path, str]] = {}
        iterator = iter(files)

        def submit_until_full() -> None:
            while len(pending) < queue_limit:
                try:
                    path, relative = next(iterator)
                except StopIteration:
                    break
                pending[executor.submit(_hash_file, path)] = (path, relative)

        submit_until_full()
        while pending:
            done, _ = concurrent.futures.wait(
                tuple(pending), return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                _path, relative = pending.pop(future)
                digest, size, executable = future.result()
                byte_count += size
                heartbeat("hashing", len(entries), len(files))
                entries.append({
                    "path": relative,
                    "kind": "file",
                    "size": size,
                    "sha256": digest,
                    "executable": executable,
                })
            submit_until_full()

    heartbeat("verifying directory stamps", len(directories), len(directories))
    for directory, stamp in directories:
        if _directory_stamp(directory) != stamp:
            raise ArtifactIntegrityError(
                f"PREPARED_ARTIFACT_CONTENT_UNSTABLE: directory changed: {directory}"
            )

    entries.sort(key=lambda item: str(item["path"]))
    build_id = tool_build_id()
    key = _canonical_hash({
        "schema": ARTIFACT_INTEGRITY_SCHEMA,
        "toolBuildId": build_id,
        "entries": entries,
    })
    return ArtifactTreeIntegrity(
        key=key,
        file_count=len(files),
        directory_count=len(directories),
        byte_count=byte_count,
        reparse_count=len(reparse_plan),
        tool_build_id=build_id,
    )
