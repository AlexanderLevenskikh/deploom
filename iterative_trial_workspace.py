"""Retire only the private trial workspace, including read-only Git objects."""
from __future__ import annotations
import os
import errno
import time
from pathlib import Path
import shutil
import stat


class TrialWorkspaceCleanupError(RuntimeError):
    pass


def remove_trial_workspace(run_dir: Path) -> None:
    root = Path(run_dir).resolve()
    parent = root / 'trial'
    target = parent / 'workspace'
    if not os.path.lexists(target):
        return
    def reparse(path: Path) -> bool:
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    def require_owned():
        if reparse(parent) or reparse(target) or target.resolve().parent != parent.resolve() or parent.resolve().parent != root:
            raise TrialWorkspaceCleanupError(f'TRIAL_WORKSPACE_CLEANUP_BLOCKED: reparse/outside-owned-root: {target}')
    require_owned()
    def readonly_retry(function, path, error):
        info = os.lstat(path)
        # Never change permissions through a link to source or a sealed snapshot.
        if not isinstance(error, PermissionError) or not stat.S_ISREG(info.st_mode) or info.st_mode & stat.S_IWRITE or info.st_nlink > 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise error
        os.chmod(path, info.st_mode | stat.S_IWRITE)
        function(path)
    for attempt in range(6):
        if not os.path.lexists(target):
            return
        require_owned()  # A retry never follows a replaced workspace/root link.
        try:
            shutil.rmtree(target, onexc=readonly_retry)
            break
        except OSError as error:
            transient = (getattr(error, 'winerror', None) in {32, 33, 145}
                         or error.errno in {errno.ENOTEMPTY, errno.EBUSY})
            if not transient or attempt == 5:
                raise TrialWorkspaceCleanupError(f'TRIAL_WORKSPACE_CLEANUP_BLOCKED: {target}: {error}') from error
            # AV/indexers or a finishing cache writer can recreate entries after
            # rmtree's enumeration. Re-enumerate the owned tree, with a bounded
            # backoff; never treat a still-present/locked trial as removed.
            from verification_observability import emit_observability_event
            emit_observability_event('filesystem.trial-cleanup.retry', attempt=attempt + 1,
                                     winError=getattr(error, 'winerror', None), errno=error.errno)
            time.sleep(.05 * (2 ** attempt))
    if os.path.lexists(target):
        raise TrialWorkspaceCleanupError(f'TRIAL_WORKSPACE_CLEANUP_BLOCKED: target remains: {target}')
