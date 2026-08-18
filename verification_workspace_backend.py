#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

from verification_process_supervisor import run_supervised
from block_vex_storage import windows_filesystem

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


def materialize_private_tree(
    source: Path,
    target: Path,
    *,
    timeout_seconds: int,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "workspace materialization",
    progress_interval_seconds: int = 15,
    runner=run_supervised,
) -> str:
    source = source.resolve()
    target = target.resolve()
    if target.exists():
        raise RuntimeError(f"WORKSPACE_TARGET_EXISTS: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

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
            result = runner(
                [
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
                    f"/MT:{_copy_workers(refs_same_volume=refs_same_volume)}",
                ],
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

    if sys.platform.startswith("linux"):
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

    if sys.platform == "darwin":
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

    shutil.copytree(source, target, symlinks=True)
    return "portable-deep-copy"

# BLOCK_VG_ZERO_CONFIG_TRANSACTIONAL_UI_V1
