"""Opt-in runtime metrics for the DepLoom Python CLI critical path.

Purpose: give the performance benchmark (benchmarks/) a cheap, machine-readable
decomposition of where each CLI step spends time WITHOUT changing normal
execution. Everything is gated behind the DEPLOOM_METRICS environment
variable; when it is not set the module is a no-op with almost zero cost
(reads one env var, and main() adds one helper call).

What is measured when enabled:

* process wall time (start handed in from the entry point);
* number and total wall of subprocess launches (git / npm / node / python /
  other), counted by wrapping subprocess.Popen (run/check_output/... all go
  through Popen, so counting Popen covers every child);
* number of os.walk directory scans;
* number of os.stat calls (cheap approximation of filesystem touches);
* the final point is emitted on an ITERATIVE_MIGRATION_METRICS_V1 envelope so
  the benchmark harness can parse it without parsing human logs.

None of this is durable state, none of it has verification/audit authority,
and none of it is required for correctness.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

_ENV = "DEPLOOM_METRICS"
_METRICS_EVENT = "ITERATIVE_MIGRATION_METRICS_V1"

_enabled: Optional[bool] = None

_process_started: float = 0.0

# (name, count) counters
_proc_counts: "dict[str, int]" = {}
_proc_wall: "dict[str, float]" = {}
_git_commands: "dict[str, int]" = {}
_argv_samples: "list[str]" = []
_walk_count: int = 0
_stat_count: int = 0


def enabled() -> bool:
    global _enabled
    if _enabled is None:
        value = os.environ.get(_ENV, "")
        _enabled = value.strip() not in ("", "0", "false", "no")
    return _enabled


def mark_start() -> None:
    global _process_started
    if enabled():
        _process_started = time.perf_counter()


def _git_subcommand(parts: "list[str]") -> str:
    """Tolerantly recover the git subcommand, skipping -c/-C option pairs."""
    index = 1
    while index < len(parts):
        item = parts[index]
        if item in ("-c", "-C", "--git-dir", "--work-tree", "--exec-path", "--namespace"):
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        return item.lower()
    return "?"


def _record_proc(argv: Any) -> None:
    try:
        parts = [str(item) for item in argv]
    except Exception:
        parts = []
    key = parts[0].lower() if parts else "unknown"
    if "git" in key:
        key = "git"
        detail = _git_subcommand(parts)
        _git_commands[detail] = _git_commands.get(detail, 0) + 1
    elif "npm" in key:
        key = "npm"
    elif "node" in key:
        key = "node"
    elif "python" in key:
        key = "python"
    elif "robocopy" in key:
        key = "robocopy"
    else:
        base = os.path.basename(key)
        key = base or "unknown"
    _proc_counts[key] = _proc_counts.get(key, 0) + 1
    if len(_argv_samples) < 40:
        text = " ".join(parts)
        _argv_samples.append(f"[{key}] {text[:220]}")


def wrap_popen() -> None:
    """Wrap subprocess.Popen so every child (run/check_output/Popen) is counted."""
    if not enabled():
        return
    import subprocess

    original = subprocess.Popen

    class _CountingPopen(original):  # type: ignore[misc]
        def __init__(self, args, *a: Any, **kw: Any) -> None:
            _record_proc(args)
            started = time.perf_counter()
            try:
                super().__init__(args, *a, **kw)
            finally:
                _proc_wall["_popen_setup"] = _proc_wall.get("_popen_setup", 0.0) + (
                    time.perf_counter() - started
                )

    subprocess.Popen = _CountingPopen  # type: ignore[assignment]


def wrap_walk() -> None:
    if not enabled():
        return
    import os as _os

    original = _os.walk

    def _walk(top, *a: Any, **kw: Any):
        global _walk_count
        _walk_count += 1
        yield from original(top, *a, **kw)

    _os.walk = _walk  # type: ignore[assignment]


def wrap_stat() -> None:
    if not enabled():
        return
    import os as _os

    for name in ("stat", "lstat"):
        original = getattr(_os, name)

        def make_wrapper(orig):
            def _wrapped(path, *a: Any, **kw: Any):
                global _stat_count
                _stat_count += 1
                return orig(path, *a, **kw)

            return _wrapped

        setattr(_os, name, make_wrapper(original))


def snapshot() -> "dict[str, Any]":
    total = 0
    for name, count in _proc_counts.items():
        total += count
    return {
        "processWallSeconds": round(time.perf_counter() - _process_started, 6)
        if _process_started
        else 0.0,
        "subprocessLaunches": total,
        "subprocessByKind": dict(sorted(_proc_counts.items())),
        "gitByCommand": dict(sorted(_git_commands.items())),
        "argvSamples": list(_argv_samples),
        "subprocessSetupWallSeconds": round(_proc_wall.get("_popen_setup", 0.0), 6),
        "directoryWalks": _walk_count,
        "statCalls": _stat_count,
    }


def emit_final() -> None:
    if not enabled():
        return
    data = snapshot()
    print(f"{_METRICS_EVENT} {json.dumps(data, separators=(',', ':'))}")
    try:
        sys.stdout.flush()
    except Exception:
        pass
