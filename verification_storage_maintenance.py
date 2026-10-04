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
# GC locators are the claim target of a concurrent-backoff sweep; they are still
# trash-named so `retired_trials` keeps returning them until deletion succeeds.
GC_NAME = re.compile(r"^\.deploom-trial-trash-dependency-flow-baseline-verify-gc_[a-z0-9_]+-\d+-\d+$")
MIN_AGE_SECONDS = 24 * 3600

# Traversal budgets. The hot path (reap before an expensive check) may only burn
# a tiny amount of wall/time/IO; oversized trees are detached O(1) and deleted by
# a later maintenance pass with a much larger budget. A full _has_links/rmtree
# walk of a multi-thousand-file tree must never stall trial reuse.
HOT_SWEEP_BYTES = 64 * 1024          # ~64 KiB of file content counted
HOT_SWEEP_FILES = 1024               # ~1024 entries lstat'ed
HOT_SWEEP_SECONDS = 0.5              # ~0.5 s of traversal wall
MAINT_SWEEP_BYTES = 512 * 1024 * 1024
MAINT_SWEEP_FILES = 200_000
MAINT_SWEEP_SECONDS = 120.0


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


def storage_available(root: Path) -> bool:
    # Distinguishes "no garbage" (True) from "could not scan" (False): a root
    # that is missing, has a junction in its ancestry, or lacks the trials
    # namespace cannot be certified, and emptiness must not be misread as a
    # successful clean.
    return _trusted_parent(root) is not None


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


def _make_budget(*, bytes: int, files: int, seconds: float) -> dict:
    return {
        "bytes_left": max(0, int(bytes)),
        "files_left": max(0, int(files)),
        "deadline": time.monotonic() + max(0.0, float(seconds)),
    }


def _budget_exhausted(budget: dict) -> bool:
    return budget["bytes_left"] <= 0 or budget["files_left"] <= 0 or time.monotonic() >= budget["deadline"]


def _budgeted_scan(path: Path, budget: dict) -> dict:
    # Only traverse while the shared budget lasts; never follows links. Returns
    # {'status': 'ok'|'links'|'oversized', 'files': int, 'bytes': int}. A tree
    # that cannot be certified inside the budget is 'oversized' (protected).
    files = 0
    total = 0

    def fail(error):
        raise error

    try:
        for current, dirs, names in os.walk(path, followlinks=False, onerror=fail):
            for name in dirs + names:
                try:
                    info = (Path(current) / name).lstat()
                except OSError:
                    return {"status": "oversized", "files": files, "bytes": total}
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
                    return {"status": "links", "files": files, "bytes": total}
                if not stat.S_ISDIR(info.st_mode):
                    files += 1
                    total += getattr(info, "st_size", 0)
                    budget["bytes_left"] -= getattr(info, "st_size", 0)
                    budget["files_left"] -= 1
                    if _budget_exhausted(budget):
                        return {"status": "oversized", "files": files, "bytes": total}
    except OSError:
        return {"status": "oversized", "files": files, "bytes": total}
    return {"status": "ok", "files": files, "bytes": total}


def _claim_gc(path: Path) -> Path | None:
    # Claim by atomic rename so concurrent cleaners never delete the same tree.
    # The encoded retirement timestamp derives from the original mtime, so a
    # locator left behind by an interrupted/budget-deferred deletion stays
    # eligible for the next pass instead of waiting another retirement window.
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    target = path.with_name(f".deploom-trial-trash-dependency-flow-baseline-verify-gc_{os.getpid()}-{os.getpid()}-{int(mtime * 1e9)}")
    try:
        os.rename(path, target)
        return target
    except OSError:
        return None


def bounded_sweep_candidate(path: Path, budget: dict) -> str:
    # Delete one detached garbage <path> only if it fits the caller's shared
    # traversal budget. Returns 'removed' | 'links' | 'oversized' | 'failed'.
    # 'links'/'oversized' leave the tree in place at its current locator for a
    # bigger maintenance pass; only a budget-fitting, link-free tree is moved
    # (claim) and actually removed in this call.
    if _budget_exhausted(budget):
        return "oversized"
    scan = _budgeted_scan(path, budget)
    if scan["status"] != "ok":
        return scan["status"]
    if _has_links(path):
        return "links"
    if not GC_NAME.fullmatch(path.name):
        claimed = _claim_gc(path)
        if claimed is None:
            return "failed"
        path = claimed
    try:
        shutil.rmtree(path)
        return "removed"
    except OSError:
        return "failed"


def claim_dead_owner_trials(root: Path, *, max_age: float = MIN_AGE_SECONDS, limit: int = 256) -> int:
    # O(1) detach of old normal-name trials whose owner process is gone. No
    # tree walking, no link checks, no deletion; the locators become trash that
    # a later budgeted sweep (maintenance/reap) can safely remove. This is the
    # atomic-detach half of "detach fast, delete later".
    parent = _trusted_parent(root)
    if parent is None:
        return 0
    cutoff = time.time() - max_age
    claimed = 0
    try:
        entries = sorted(parent.iterdir(), key=lambda item: item.stat().st_mtime)
    except OSError:
        return 0
    for item in entries:
        if claimed >= max(0, limit):
            break
        if not item.name.startswith("dependency-flow-baseline-verify-"):
            continue
        if not _plain_directory(item) or not dead_trial_owner(item):
            continue
        try:
            if item.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        trash = item.with_name(f".deploom-trial-trash-{item.name}-{os.getpid()}-{int(item.stat().st_mtime * 1e9)}")
        if trash.exists():
            continue
        try:
            os.rename(item, trash)
            claimed += 1
        except OSError:
            continue
    return claimed


def clean_retired_trials(
    root: Path,
    *,
    limit: int = 256,
    now: float | None = None,
    budget_bytes: int = MAINT_SWEEP_BYTES,
    budget_files: int = MAINT_SWEEP_FILES,
    budget_seconds: float = MAINT_SWEEP_SECONDS,
) -> dict:
    candidates = retired_trials(root, now=now)
    removed = failed = protected = 0
    budget = _make_budget(bytes=budget_bytes, files=budget_files, seconds=budget_seconds)
    for path in candidates[:max(0, limit)]:
        if _trusted_parent(root) != path.parent or not _plain_directory(path):
            protected += 1
            continue
        outcome = bounded_sweep_candidate(path, budget)
        if outcome == "removed":
            removed += 1
        elif outcome == "failed":
            failed += 1
        else:
            protected += 1
    return {"eligible": len(candidates), "removed": removed, "failed": failed, "protected": protected}


def maintenance(action: str) -> dict:
    from block_vex_storage import verification_storage_profile
    profile = verification_storage_profile()
    if profile.root is None:
        raise RuntimeError("Verification storage is unavailable")
    root = profile.root.absolute()
    if action == "clean":
        # Maintenance is the "delete later" side of the detach-fast contract and
        # also recovers normal-name trials left by a crashed verifier.
        claim_dead_owner_trials(root)
        counts = clean_retired_trials(root)
    else:
        counts = {"eligible": len(retired_trials(root)), "removed": 0, "failed": 0, "protected": 0}
    return {"root": str(root), "filesystem": profile.filesystem, "freeBytes": shutil.disk_usage(root).free, "available": storage_available(root), **counts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "clean"])
    args = parser.parse_args()
    print(json.dumps(maintenance(args.action)))
