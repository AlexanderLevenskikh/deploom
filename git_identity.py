"""Shared read-only Git identity probes.

Live repositories and Git environment can change between calls, including
between requests in a persistent worker. Never reuse a path-only identity or
an infrastructure failure. Immutable SourceSnapshot identities own reuse.

Callers keep their own error semantics on top of the raw probe outcome
(return None, fall back to the project dir, raise an identity error, ...).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Tuple

# (stdout, returncode, stderr, marker_exists_at_probe_time)
Probe = Tuple[str, int, str, bool]

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
    """Fresh probe for callers that need the returncode/stderr/marker."""
    return _probe(project_dir)
