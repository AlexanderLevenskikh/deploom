"""Shared cross-language test helper: ensure desktop/dist-electron is built.

Multiple production-chain test modules (manual-audit verdict chain, Draft
reader cross-language tests, validation regressions) load the COMPILED
Electron modules (desktop/dist-electron/*.js). `dist-electron` is gitignored,
so on a fresh checkout (or a CI job that did not build the desktop) the
modules must be compiled once before probes/verdicts can run. This helper
makes every dependent module self-sufficient: build when missing or stale,
and SkipTest honestly when the Node/TypeScript toolchain is unavailable
(instead of hard-failing the whole suite in an environment without Node).
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ELECTRON_DIR = ROOT / "desktop" / "electron"
DIST_DIR = ROOT / "desktop" / "dist-electron"

# The compiled assets the cross-language tests import. tsc -p
# tsconfig.electron.json emits all of them; check each so a partial build
# (only some modules outdated) is detected and rebuilt.
ASSETS = (
    "acceptance-policy.js",
    "target-closure.js",
    "draft-artifact-reader.js",
)


def tsc_command() -> list[str]:
    """Cross-platform tsc invocation: npx.cmd on Windows, npx elsewhere."""
    return [shutil.which("npx.cmd") or "npx", "tsc", "-p", "tsconfig.electron.json"]


def _needs_build() -> bool:
    if not all((DIST_DIR / asset).is_file() for asset in ASSETS):
        return True
    try:
        newest_source = max(
            path.stat().st_mtime_ns for path in ELECTRON_DIR.glob("*.ts")
        )
        for asset in ASSETS:
            dst = DIST_DIR / asset
            if dst.is_file() and dst.stat().st_mtime_ns < newest_source:
                return True
    except OSError:
        return True
    return False


def ensure_dist_electron() -> None:
    """Compile desktop/dist-electron when missing/stale; SkipTest if unavailable."""
    if not _needs_build():
        return
    finished = subprocess.run(
        tsc_command(),
        cwd=str(ROOT / "desktop"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    if finished.returncode != 0:
        raise unittest.SkipTest(
            f"tsc electron unavailable (dist-electron missing and build failed): "
            f"{finished.stderr[-500:]}"
        )
    missing = [asset for asset in ASSETS if not (DIST_DIR / asset).is_file()]
    if missing:
        raise unittest.SkipTest(f"tsc electron produced no output asset: {missing}")
