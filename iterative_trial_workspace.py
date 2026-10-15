"""Retire only the private trial workspace, including read-only Git objects."""
from __future__ import annotations
import os
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
    if reparse(parent) or reparse(target) or target.resolve().parent != parent.resolve() or parent.resolve().parent != root:
        raise TrialWorkspaceCleanupError(f'TRIAL_WORKSPACE_CLEANUP_BLOCKED: reparse/outside-owned-root: {target}')
    def readonly_retry(function, path, error):
        info = os.lstat(path)
        # Never change permissions through a link to source or a sealed snapshot.
        if not isinstance(error, PermissionError) or not stat.S_ISREG(info.st_mode) or info.st_mode & stat.S_IWRITE or info.st_nlink > 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise error
        os.chmod(path, info.st_mode | stat.S_IWRITE)
        function(path)
    try:
        shutil.rmtree(target, onexc=readonly_retry)
    except OSError as error:
        raise TrialWorkspaceCleanupError(f'TRIAL_WORKSPACE_CLEANUP_BLOCKED: {target}: {error}') from error
    if os.path.lexists(target):
        raise TrialWorkspaceCleanupError(f'TRIAL_WORKSPACE_CLEANUP_BLOCKED: target remains: {target}')
