#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from io_governor import io_slot
from reparse_materialization import (
    ReparseLink,
    inventory_reparse_plan,
    recreate_reparse_plan,
)

from verification_process_supervisor import run_supervised
from block_vex_storage import windows_filesystem
from verification_observability import (
    emit_observability_event,
    new_observability_id,
    process_resource_snapshot,
)

# BLOCK_Y_FULL_OBSERVABILITY_V1

# BLOCK_VEX_VERIFICATION_SUBSTRATE_V1

ProgressCallback = Callable[[str], None]


def _windows_volume_root(path: Path) -> str:
    if os.name != "nt":
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        ok = ctypes.windll.kernel32.GetVolumePathNameW(  # type: ignore[attr-defined]
            str(path.expanduser().absolute()), buffer, len(buffer)
        )
        return buffer.value.lower() if ok and buffer.value else ""
    except Exception:
        return str(path.expanduser().absolute().anchor).lower()


def _same_windows_volume(source: Path, target: Path) -> bool:
    if os.name != "nt":
        return False
    source_root = _windows_volume_root(source)
    target_root = _windows_volume_root(target.parent if not target.exists() else target)
    return bool(source_root and source_root == target_root)


def _copy_workers(*, refs_same_volume: bool = False) -> int:
    raw = str(
        os.environ.get(
            "DEPLOOM_VEX_REFS_COPY_WORKERS"
            if refs_same_volume
            else "DEPLOOM_WORKSPACE_COPY_WORKERS"
        )
        or ""
    ).strip()
    if raw:
        try:
            return max(1, min(128, int(raw)))
        except ValueError:
            pass

    # node_modules copies are dominated by per-file metadata/filter latency.
    # Scale with the machine instead of pinning every user to the old 8/24
    # defaults; robocopy supports /MT up to 128.
    logical = max(1, int(os.cpu_count() or 4))
    adaptive = max(16, min(128, logical * 4))
    return max(32, adaptive) if refs_same_volume else adaptive


def workspace_backend_summary(root: Optional[Path] = None) -> str:
    if os.name == "nt":
        probe = root or Path(os.environ.get("DEPLOOM_VERIFICATION_ROOT") or Path.cwd())
        fs = windows_filesystem(probe)
        if fs == "refs":
            return (
                "windows-refs-same-volume-optimized "
                "(native block-clone eligible on supported Windows copy paths; "
                "proof-safe private-copy fallback)"
            )
        return (
            "windows-private-copy "
            "(zero-config private verification; optional optimized roots are auto-detected/configurable)"
        )
    if sys.platform == "darwin":
        return "macos-apfs-clone-first (private-copy fallback)"
    if sys.platform.startswith("linux"):
        return "linux-reflink-first (private-copy fallback)"
    return "portable-private-copy"


def _materialize_private_tree_impl(
    source: Path,
    target: Path,
    *,
    timeout_seconds: int,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "workspace materialization",
    progress_interval_seconds: int = 15,
    runner=run_supervised,
    exclude_dir_names: tuple[str, ...] = (),
    exclude_file_names: tuple[str, ...] = (),
    reparse_plan: Optional[tuple[ReparseLink, ...]] = None,
) -> str:
    # BLOCK_X_SOURCE_TRUTH_V1
    source = source.resolve()
    target = target.resolve()
    if target.exists():
        raise RuntimeError(f"WORKSPACE_TARGET_EXISTS: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    canonical_plan = (
        tuple(reparse_plan)
        if reparse_plan is not None
        else inventory_reparse_plan(
            source,
            excluded_dir_names=exclude_dir_names,
            excluded_file_names=exclude_file_names,
        )
    )

    if os.name == "nt":
        source_fs = windows_filesystem(source)
        target_fs = windows_filesystem(target.parent)
        refs_same_volume = (
            source_fs == "refs"
            and target_fs == "refs"
            and _same_windows_volume(source, target)
        )
        robocopy = shutil.which("robocopy")
        if robocopy:
            robocopy_args = [
                robocopy,
                str(source),
                str(target),
                "/E",
                "/COPY:DAT",
                "/DCOPY:DAT",
                "/R:3",
                "/W:1",
                "/NFL",
                "/NDL",
                "/NJH",
                "/NJS",
                "/NP",
                "/SL",
                "/XJ",
                f"/MT:{_copy_workers(refs_same_volume=refs_same_volume)}",
            ]
            if exclude_dir_names:
                robocopy_args.extend(["/XD", *sorted(set(exclude_dir_names))])
            if exclude_file_names:
                robocopy_args.extend(["/XF", *sorted(set(exclude_file_names))])
            result = runner(
                robocopy_args,
                source,
                timeout_seconds=max(1, int(timeout_seconds)),
                progress=progress,
                progress_label=(
                    f"{progress_label}: refs-same-volume"
                    if refs_same_volume
                    else progress_label
                ),
                progress_interval_seconds=progress_interval_seconds,
            )
            if result.returncode < 8:
                recreate_reparse_plan(source, target, canonical_plan)
                if refs_same_volume:
                    return "windows-refs-same-volume-native-copy-eligible"
                if target_fs == "refs":
                    return "windows-refs-cross-volume-private"
                return "windows-robocopy-private"
            shutil.rmtree(target, ignore_errors=True)
            raise RuntimeError(
                "WORKSPACE_ROBOCOPY_FAILED: "
                f"exit={result.returncode}: {(result.stdout or '')[-1200:]}"
            )

    # Native whole-tree clones cannot express the explicit Block X
    # source-input exclusion policy. Use them only for complete trees; filtered
    # source capture falls back to the proof-safe portable copy below.
    if sys.platform.startswith("linux") and not exclude_dir_names and not exclude_file_names:
        cp = shutil.which("cp")
        if cp:
            target.mkdir(parents=True, exist_ok=False)
            result = runner(
                [cp, "-a", "--reflink=always", f"{source}/.", str(target)],
                source,
                timeout_seconds=max(1, int(timeout_seconds)),
                progress=progress,
                progress_label=f"{progress_label}: reflink",
                progress_interval_seconds=progress_interval_seconds,
            )
            if result.returncode == 0:
                return "linux-reflink"
            shutil.rmtree(target, ignore_errors=True)

    if sys.platform == "darwin" and not exclude_dir_names and not exclude_file_names:
        cp = shutil.which("cp")
        if cp:
            target.mkdir(parents=True, exist_ok=False)
            result = runner(
                [cp, "-cR", f"{source}/.", str(target)],
                source,
                timeout_seconds=max(1, int(timeout_seconds)),
                progress=progress,
                progress_label=f"{progress_label}: clonefile",
                progress_interval_seconds=progress_interval_seconds,
            )
            if result.returncode == 0:
                return "macos-clonefile"
            shutil.rmtree(target, ignore_errors=True)

    ignored = tuple(sorted(set(exclude_dir_names) | set(exclude_file_names)))
    ignore = shutil.ignore_patterns(*ignored) if ignored else None
    shutil.copytree(source, target, symlinks=True, ignore=ignore)
    recreate_reparse_plan(source, target, canonical_plan)
    return "portable-filtered-deep-copy" if ignored else "portable-deep-copy"

# BLOCK_VG_ZERO_CONFIG_TRANSACTIONAL_UI_V1

def materialize_private_tree(
    source: Path,
    target: Path,
    *,
    timeout_seconds: int,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "workspace materialization",
    progress_interval_seconds: int = 15,
    runner=run_supervised,
    exclude_dir_names: tuple[str, ...] = (),
    exclude_file_names: tuple[str, ...] = (),
    reparse_plan: Optional[tuple[ReparseLink, ...]] = None,
) -> str:
    """Instrumented private materialization without following reparse points."""
    operation_id = new_observability_id("materialize")
    started = time.monotonic()
    before = process_resource_snapshot()
    source_path = source.resolve()
    target_path = target.resolve()
    source_fs = windows_filesystem(source_path) if os.name == "nt" else sys.platform
    target_fs = (
        windows_filesystem(target_path.parent)
        if os.name == "nt"
        else sys.platform
    )
    refs_same_volume = (
        os.name == "nt"
        and source_fs == "refs"
        and target_fs == "refs"
        and _same_windows_volume(source_path, target_path)
    )
    workers = _copy_workers(refs_same_volume=bool(refs_same_volume))

    emit_observability_event(
        "filesystem.materialize.start",
        operationId=operation_id,
        label=progress_label,
        platform=sys.platform,
        sourceFilesystem=source_fs,
        targetFilesystem=target_fs,
        configuredWorkers=workers,
        excludeDirCount=len(exclude_dir_names),
        excludeFileCount=len(exclude_file_names),
        timeoutSeconds=int(timeout_seconds),
    )
    try:
        method = _materialize_private_tree_impl(
            source,
            target,
            timeout_seconds=timeout_seconds,
            progress=progress,
            progress_label=progress_label,
            progress_interval_seconds=progress_interval_seconds,
            runner=runner,
            exclude_dir_names=exclude_dir_names,
            exclude_file_names=exclude_file_names,
            reparse_plan=reparse_plan,
        )
    except BaseException as exc:
        after = process_resource_snapshot()
        emit_observability_event(
            "filesystem.materialize.finish",
            operationId=operation_id,
            label=progress_label,
            outcome="exception",
            method="",
            platform=sys.platform,
            sourceFilesystem=source_fs,
            targetFilesystem=target_fs,
            configuredWorkers=workers,
            durationMs=max(0, int((time.monotonic() - started) * 1000)),
            cpuMs=max(0, after.cpu_ms - before.cpu_ms),
            rssBytes=after.rss_bytes,
            peakRssBytes=after.peak_rss_bytes,
            errorType=type(exc).__name__,
        )
        raise

    after = process_resource_snapshot()
    emit_observability_event(
        "filesystem.materialize.finish",
        operationId=operation_id,
        label=progress_label,
        outcome="passed",
        method=method,
        platform=sys.platform,
        sourceFilesystem=source_fs,
        targetFilesystem=target_fs,
        configuredWorkers=workers,
        durationMs=max(0, int((time.monotonic() - started) * 1000)),
        cpuMs=max(0, after.cpu_ms - before.cpu_ms),
        rssBytes=after.rss_bytes,
        peakRssBytes=after.peak_rss_bytes,
    )
    return method

