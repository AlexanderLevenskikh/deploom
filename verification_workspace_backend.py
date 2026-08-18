#!/usr/bin/env python3
from __future__ import annotations

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


def workspace_backend_summary() -> str:
    if os.name == "nt":
        return "windows-private-copy (ReFS accelerator pluggable; NTFS-safe fallback)"
    if sys.platform == "darwin":
        return "macos-private-copy (APFS clone accelerator pluggable)"
    if sys.platform.startswith("linux"):
        return "linux-reflink-auto"
    return "portable-private-copy"


# BLOCK_U4_FULL_SUITE_COMPAT_V1
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

    shutil.copytree(source, target, symlinks=True)
    return "portable-deep-copy"
