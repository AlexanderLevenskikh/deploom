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
from typing import Callable, Optional, Sequence

ProgressCallback = Callable[[str], None]

from io_governor import io_slot
from reparse_materialization import (
    ReparseLink,
    ReparseMaterializationError,
    inventory_reparse_plan,
    is_windows_junction,
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
    reparse_plan: Optional[Sequence[ReparseLink]] = None,
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
    if reparse_plan is None:
        try:
            canonical_reparse_plan = inventory_reparse_plan(root)
        except ReparseMaterializationError as exc:
            raise ArtifactIntegrityError(
                f"PREPARED_ARTIFACT_REPARSE_INVALID: {exc}"
            ) from exc
    else:
        canonical_reparse_plan = tuple(reparse_plan)
    reparse_by_path = {
        item.link_relative: item
        for item in canonical_reparse_plan
    }
    if len(reparse_by_path) != len(canonical_reparse_plan):
        raise ArtifactIntegrityError("PREPARED_ARTIFACT_REPARSE_PLAN_DUPLICATE")

    entries: list[dict[str, object]] = []
    files: list[tuple[Path, str]] = []
    directories: list[tuple[Path, tuple[int, int, int]]] = []
    seen_reparse: set[str] = set()
    scanned_entries = 0
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
        for item in scanned:
            scanned_entries += 1
            heartbeat("scanning", scanned_entries)
            path = Path(item.path)
            relative = relative_directory / item.name
            relative_text = relative.as_posix()
            planned = reparse_by_path.get(relative_text)
            try:
                metadata = item.stat(follow_symlinks=False)
            except OSError as exc:
                raise ArtifactIntegrityError(
                    f"PREPARED_ARTIFACT_ENTRY_UNREADABLE: {path}: {exc}"
                ) from exc
            mode = metadata.st_mode
            reparse_attribute = int(
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0
            )
            is_reparse_entry = bool(
                stat.S_ISLNK(mode)
                or (
                    reparse_attribute
                    and int(getattr(metadata, "st_file_attributes", 0) or 0)
                    & reparse_attribute
                )
            )

            if planned is not None:
                planned_is_reparse = bool(
                    is_reparse_entry
                    or path.is_symlink()
                    or (os.name == "nt" and is_windows_junction(path))
                )
                if not planned_is_reparse:
                    raise ArtifactIntegrityError(
                        "PREPARED_ARTIFACT_REPARSE_DRIFT: "
                        f"{relative_text} is no longer a reparse entry"
                    )
                try:
                    actual_target = path.resolve(strict=True)
                    expected_target = root.joinpath(
                        *Path(planned.target_relative).parts
                    ).resolve(strict=True)
                    actual_target.relative_to(root)
                    expected_target.relative_to(root)
                    matches = actual_target.samefile(expected_target)
                except (OSError, ValueError, RuntimeError):
                    matches = False
                if not matches:
                    raise ArtifactIntegrityError(
                        "PREPARED_ARTIFACT_REPARSE_DRIFT: "
                        f"{relative_text} target changed"
                    )
                actual_kind = (
                    "junction"
                    if is_windows_junction(path)
                    else (
                        "symlink-directory"
                        if actual_target.is_dir()
                        else "symlink-file"
                    )
                )
                if actual_kind != planned.link_kind:
                    raise ArtifactIntegrityError(
                        "PREPARED_ARTIFACT_REPARSE_DRIFT: "
                        f"{relative_text} kind={actual_kind}, expected={planned.link_kind}"
                    )
                seen_reparse.add(relative_text)
                entries.append({
                    "path": relative_text,
                    "kind": planned.link_kind,
                    "target": planned.target_relative,
                    "authority": planned.authority,
                    "package": planned.package_name,
                })
                continue

            # A new link/junction appearing after the canonical plan was built
            # must never be silently traversed. Reusing the plan is an
            # optimization only; this scan still detects topology drift.
            if (
                is_reparse_entry
                or path.is_symlink()
                or (
                    os.name == "nt"
                    and stat.S_ISDIR(mode)
                    and is_windows_junction(path)
                )
            ):
                raise ArtifactIntegrityError(
                    "PREPARED_ARTIFACT_REPARSE_DRIFT: "
                    f"unplanned reparse entry {relative_text}"
                )
            if stat.S_ISDIR(mode):
                entries.append({"path": relative_text, "kind": "directory"})
                pending.append((path, relative))
            elif stat.S_ISREG(mode):
                raw_links = int(getattr(metadata, "st_nlink", 0) or 0)
                if raw_links < 1:
                    try:
                        raw_links = int(
                            getattr(path.stat(follow_symlinks=False), "st_nlink", 1)
                            or 1
                        )
                    except OSError as exc:
                        raise ArtifactIntegrityError(
                            f"PREPARED_ARTIFACT_ENTRY_UNREADABLE: {path}: {exc}"
                        ) from exc
                links = max(1, raw_links)
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

    missing_reparse = sorted(set(reparse_by_path) - seen_reparse)
    if missing_reparse:
        raise ArtifactIntegrityError(
            "PREPARED_ARTIFACT_REPARSE_DRIFT: planned entries disappeared: "
            + ", ".join(missing_reparse[:8])
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
    hashed_files = 0
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
                hashed_files += 1
                heartbeat("hashing", hashed_files, len(files))
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
        reparse_count=len(canonical_reparse_plan),
        tool_build_id=build_id,
    )
