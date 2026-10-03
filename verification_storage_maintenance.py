"""Conservative maintenance of detached verification garbage, never run state."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

import ctypes

def owner_alive(pid):
    # Unknown liveness is protected; PID reuse can only retain garbage.
    if os.name != 'nt':
        try: os.kill(pid, 0); return True
        except ProcessLookupError: return False
        except OSError: return True
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle: return ctypes.get_last_error() != 87
    try:
        code = ctypes.c_ulong()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally: kernel.CloseHandle(handle)


def register_trial_owner(path):
    try: (path / '.deploom-trial-owner.json').write_text(json.dumps({'pid':os.getpid()}), encoding='utf-8')
    except OSError: pass  # Missing ownership evidence protects the tree.


def retire_trial(path):
    # Called only by the process that created this private tree, after use.
    if not _plain_directory(path): return
    target = path.with_name(f'.deploom-trial-trash-{path.name}-{os.getpid()}-{time.time_ns()}')
    try: os.rename(path, target)
    except OSError: pass


def dead_trial_owner(path):
    try:
        raw = json.loads((path / '.deploom-trial-owner.json').read_text(encoding='utf-8'))
        pid = raw.get('pid')
        return isinstance(pid,int) and not isinstance(pid,bool) and 0 < pid < 2**31 and not owner_alive(pid)
    except (OSError,ValueError,TypeError): return False


TRASH_NAME = re.compile(r"^\.deploom-trial-trash-dependency-flow-baseline-verify-[a-z0-9_]+-\d+-\d+$")
MIN_AGE_SECONDS = 24 * 3600


def _plain_directory(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _trusted_parent(root: Path) -> Path | None:
    # Reject junctions in every ancestor as well as the selected namespace.
    for path in [root, *root.parents]:
        if not _plain_directory(path):
            return None
    parent = root / "trials"
    return parent if _plain_directory(parent) else None


def retired_trials(root: Path, *, now: float | None = None) -> list[Path]:
    parent = _trusted_parent(root)
    if parent is None:
        return []
    cutoff = (time.time() if now is None else now) - MIN_AGE_SECONDS
    result = []
    for path in parent.iterdir():
        try:
            # Retirement timestamp and directory write time must both be old.
            if TRASH_NAME.fullmatch(path.name) and _plain_directory(path) and path.stat().st_mtime <= cutoff and int(path.name.rsplit("-", 1)[1]) / 1e9 <= cutoff:
                result.append(path)
        except OSError:
            continue
    return sorted(result, key=lambda path: path.name)


def _has_links(path: Path) -> bool:
    # Do not remove trees containing junctions/symlinks, even though modern
    # rmtree handles them. They can represent a still-guarded shared snapshot.
    def failed(error):
        raise error
    for current, dirs, files in os.walk(path, followlinks=False, onerror=failed):
        for name in dirs + files:
            info = (Path(current) / name).lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                return True
    return False


def clean_retired_trials(root: Path, *, limit: int = 256, now: float | None = None) -> dict:
    candidates = retired_trials(root, now=now)
    removed = failed = protected = 0
    for path in candidates[:max(0, limit)]:
        try:
            if _trusted_parent(root) != path.parent or not _plain_directory(path) or _has_links(path):
                protected += 1
                continue
            # Claim by atomic rename. Concurrent cleaners never delete the same
            # locator. Failed/partial removal remains recognizable next time.
            claimed = path.with_name(f".deploom-trial-trash-dependency-flow-baseline-verify-gc_{os.getpid()}-{os.getpid()}-{time.time_ns()}")
            os.rename(path, claimed)
            shutil.rmtree(claimed)
            removed += 1
        except OSError:
            failed += 1
    return {"eligible": len(candidates), "removed": removed, "failed": failed, "protected": protected}


def maintenance(action: str) -> dict:
    from block_vex_storage import verification_storage_profile
    profile = verification_storage_profile()
    if profile.root is None:
        raise RuntimeError("Verification storage is unavailable")
    root = profile.root.absolute()
    counts = clean_retired_trials(root) if action == "clean" else {"eligible": len(retired_trials(root)), "removed": 0, "failed": 0, "protected": 0}
    return {"root": str(root), "filesystem": profile.filesystem, "freeBytes": shutil.disk_usage(root).free, **counts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "clean"])
    args = parser.parse_args()
    print(json.dumps(maintenance(args.action)))
