"""Best-effort placement of temporary sealed SourceSnapshots beside verification data.

This is a storage optimization, never proof authority. No candidate directory is
created before checking that it lies outside the entire live source subject.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping, Optional


SNAPSHOT_SUBDIR = "source-snapshots"


def _contained(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def candidate_snapshot_parents(*, environment: Optional[Mapping[str, str]] = None) -> tuple[Path, ...]:
    """Return *uncreated* candidates, in verification preference order.

    The selected storage root is derived from the same config as VEx, but
    without its write-probe side effects. Explicit roots and auto-ReFS follow
    its selection semantics. A system-TEMP legacy fallback is always available.
    """
    from block_vex_storage import _candidate_refs_roots, _default_verification_root

    env = os.environ if environment is None else environment
    raw = str(env.get("DEPLOOM_VERIFICATION_ROOT") or "").strip()
    auto = (raw.lower() in {"auto", "refs", "devdrive"} or
            str(env.get("DEPLOOM_VEX_AUTO_REFS") or "").strip().lower() in {"1", "true", "yes", "on"})
    roots: list[Path] = []
    if raw and raw.lower() not in {"auto", "refs", "devdrive"}:
        roots.append(Path(raw).expanduser().absolute())
    elif auto:
        roots.extend(volume / "DepLoom" / "verification" for _free, volume in _candidate_refs_roots())
    roots.append(_default_verification_root(env))
    # The legacy root is a fallback, and remains discoverable by the reaper.
    roots.append(Path(tempfile.gettempdir()))
    result: list[Path] = []
    seen: set[str] = set()
    for base in roots:
        # The traditional TEMP root already holds the container directly.
        candidate = base if base == Path(tempfile.gettempdir()) else base / SNAPSHOT_SUBDIR
        key = os.path.normcase(str(candidate.resolve()))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)


def new_snapshot_container(capture_root: Path, *, prefix: str) -> Path:
    """Create a private temporary container outside the live source tree.

    On unsupported/unwritable verification volumes, fall back to the normal
    OS temporary location. This selection changes neither source identity nor
    SourceSnapshot integrity validation.
    """
    live_root = Path(capture_root).resolve()
    for parent in candidate_snapshot_parents():
        if _contained(parent, live_root):
            continue  # Never create even a probe file under a source subject.
        try:
            parent.mkdir(parents=True, exist_ok=True)
            container = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
        except OSError:
            continue
        if _contained(container, live_root):
            # Defence in depth against unusual aliasing / reparse races.
            container.rmdir()
            continue
        return container
    raise OSError("SOURCE_SNAPSHOT_TEMP_UNAVAILABLE: no writable storage outside the source root")
