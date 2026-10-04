"""Shared, process-local memoization of read-only Git identity probes.

Every DepLoom Git probe here is read-only (`rev-parse --show-toplevel` with
GIT_OPTIONAL_LOCKS semantics preserved by the callers' original environment).
Within one process the probe outcome for a resolved directory cannot change:
no DepLoom code creates, moves or commits a repository at the probed paths
after they have been probed. Probing each resolved path is therefore done at
most once per process and the raw outcome is cached, cutting repeated
subprocess spawns that dominated CLI wall time.

Callers keep their own error semantics on top of the raw probe outcome
(return None, fall back to the project dir, raise an identity error, ...).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple

# (stdout, returncode, stderr, marker_exists_at_probe_time)
Probe = Tuple[str, int, str, bool]

_cache: dict[str, Probe] = {}


def _git_marker_exists(project_dir: Path) -> bool:
    current = Path(project_dir).resolve()
    while True:
        if (current / ".git").exists():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _probe(project_dir: Path) -> Probe:
    project_dir = Path(project_dir).expanduser().resolve()
    key = os.path.normcase(str(project_dir))
    cached = _cache.get(key)
    if cached is not None:
        return cached
    marker_exists = _git_marker_exists(project_dir)
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--show-toplevel"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        out, rc, err = result.stdout, result.returncode, result.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        out, rc, err = "", 127, str(exc)
    probe: Probe = (out, rc, err, marker_exists)
    _cache[key] = probe
    return probe


def git_toplevel(project_dir: Path) -> Optional[Path]:
    """Resolved top-level Git root for a project dir, or None if not a repo.

    Mirrors the success predicate every DepLoom probe used:
    returncode == 0 and a non-empty stdout line.
    """
    out, rc, _, _ = _probe(project_dir)
    if rc == 0 and out.strip():
        return Path(out.strip()).resolve()
    return None


def git_toplevel_raw(project_dir: Path) -> Probe:
    """Raw cached probe for callers that need the returncode/stderr/marker."""
    return _probe(project_dir)
