#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional

from verification_process_supervisor import run_supervised

ProgressCallback = Callable[[str], None]


def _copy_workers() -> int:
    raw = str(os.environ.get("DEPLOOM_WORKSPACE_COPY_WORKERS") or "").strip()
    if raw:
        try:
            return max(1, min(32, int(raw)))
        except ValueError:
            pass
    return 8


def _windows_filesystem(path: Path) -> str:
    if os.name != "nt":
        return ""
    try:
        root = Path(path.resolve().anchor or str(path.resolve()))
        volume = ctypes.create_unicode_buffer(261)
        fs_name = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32()
        max_component = ctypes.c_uint32()
        flags = ctypes.c_uint32()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
            str(root), volume, len(volume), ctypes.byref(serial),
            ctypes.byref(max_component), ctypes.byref(flags), fs_name, len(fs_name),
        )
        return fs_name.value.lower() if ok else ""
    except Exception:
        return ""


def workspace_backend_summary(root: Optional[Path] = None) -> str:
    if os.name == "nt":
        probe = root or Path(os.environ.get("DEPLOOM_VERIFICATION_ROOT") or Path.cwd())
        fs = _windows_filesystem(probe)
        if fs == "refs":
            # Windows 11 24H2+/Server 2025 can accelerate supported copy APIs on
            # ReFS. We deliberately keep robocopy/private-copy semantics here;
            # the label does not claim that a particular copy was block-cloned.
            return "windows-refs-private-copy (native block-clone capable; proof-safe copy fallback)"
        return "windows-private-copy (ReFS/Dev Drive accelerator capable; NTFS-safe fallback)"
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
    """Create a writable private tree without weakening verification semantics.

    Native clone/reflink paths are performance accelerators only. Any unsupported
    operation falls back to the same deep/private-copy semantics used by Block U.
    """
    source = source.resolve()
    target = target.resolve()
    if target.exists():
        raise RuntimeError(f"WORKSPACE_TARGET_EXISTS: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        robocopy = shutil.which("robocopy")
        if robocopy:
            result = runner(
                [
                    robocopy, str(source), str(target),
                    "/E", "/COPY:DAT", "/DCOPY:DAT", "/R:1", "/W:1",
                    "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/SL",
                    f"/MT:{_copy_workers()}",
                ],
                source,
                timeout_seconds=max(1, int(timeout_seconds)),
                progress=progress,
                progress_label=progress_label,
                progress_interval_seconds=progress_interval_seconds,
            )
            if result.returncode < 8:
                return (
                    "windows-refs-private"
                    if _windows_filesystem(target) == "refs"
                    else "windows-robocopy-private"
                )
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
