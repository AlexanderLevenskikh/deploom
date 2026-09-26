"""Shared cross-language test helper: ensure desktop/dist-electron is built.

Multiple production-chain test modules (manual-audit verdict chain, Draft
reader cross-language tests, validation regressions) load the COMPILED
Electron modules (desktop/dist-electron/*.js). `dist-electron` is gitignored,
so on a fresh checkout (or a CI job that did not build the desktop) the
modules must be compiled once before probes/verdicts can run. This helper
makes every dependent module self-sufficient: build when missing or stale,
and SkipTest honestly when the Node/TypeScript toolchain is unavailable.

A12: the helper must distinguish an ABSENT toolchain (an honest skip: no
node/npx on PATH, or typescript is not installed) from a REAL compile error
(tsc ran and emitted "error TS..." diagnostics -- that is a failing build and
must hard-fail, never silently skip). A tsc run that reports success but
produces no output asset is also a hard failure. Release CI therefore builds
explicitly (npm ci + npx tsc) before running the Python suites, and cannot
mask a broken TypeScript consumer as a skip.
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
    """Cross-platform tsc invocation: npx.cmd on Windows, npx elsewhere.

    --no-install keeps the build deterministic: npx must use the project's
    installed typescript instead of silently fetching one from the registry
    (which would make CI and local runs diverge).
    """
    return [shutil.which("npx.cmd") or "npx", "--no-install", "tsc", "-p", "tsconfig.electron.json"]


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


def _toolchain_present() -> bool:
    # R8: "toolchain available" is decided BEFORE the compiler runs: node/npx on
    # PATH AND the project-installed typescript package (npx --no-install will
    # refuse to fetch a missing one). Once the compiler IS available, any
    # non-zero exit is a real compiler failure, never a skip.
    if not (shutil.which("npx.cmd") or shutil.which("npx") or shutil.which("node")):
        return False
    return (ROOT / "desktop" / "node_modules" / "typescript").is_dir()


def _run_tsc() -> subprocess.CompletedProcess:
    return subprocess.run(
        tsc_command(),
        cwd=str(ROOT / "desktop"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def ensure_dist_electron() -> None:
    """Compile desktop/dist-electron when missing/stale.

    Skips only when the toolchain is genuinely unavailable (no node/npx, or
    typescript not installed in desktop/node_modules). R8: once the compiler
    is present, ANY non-zero exit is a hard compile failure -- diagnostics
    land on stdout for tsc just as often as on stderr, so a pattern-scanned
    stderr alone could never be a safe skip signal. A successful build that
    produced no asset is also a hard failure.
    """
    if not _needs_build():
        return
    if not _toolchain_present():
        raise unittest.SkipTest(
            "tsc electron unavailable: no Node/npx on PATH (or typescript not installed) and dist-electron is missing"
        )
    try:
        finished = _run_tsc()
    except FileNotFoundError:
        raise unittest.SkipTest(
            "tsc electron unavailable: npx not found and dist-electron is missing"
        ) from None

    if finished.returncode == 0:
        missing = [asset for asset in ASSETS if not (DIST_DIR / asset).is_file()]
        if missing:
            raise AssertionError(
                f"tsc electron reported success but produced no output asset: {missing}"
            )
        return

    combined = f"{finished.stdout or ''}\n{finished.stderr or ''}"
    raise AssertionError(
        "tsc electron compile error (dist-electron missing and the installed "
        f"TypeScript compiler exited {finished.returncode}):\n{combined[-2000:]}"
    )
