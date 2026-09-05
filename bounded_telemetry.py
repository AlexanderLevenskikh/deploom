"""Bounded, best-effort telemetry storage. Never stores proof authority."""
from __future__ import annotations

import os
from pathlib import Path
import time

MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_ARCHIVES = 2
MAX_AGE_SECONDS = 14 * 24 * 60 * 60


def append_bounded_telemetry(path: Path, line: str, *, max_bytes: int = MAX_LOG_BYTES,
                             max_archives: int = MAX_ARCHIVES,
                             max_age_seconds: float = MAX_AGE_SECONDS) -> None:
    """Serialize rotation across processes; skip telemetry if another writer is busy.

    OS locks are released on process exit. Only exact numbered telemetry siblings
    are eligible for deletion, never checkpoints, proofs or arbitrary directory files.
    """
    data = line.encode("utf-8")
    if max_bytes < 1 or max_archives < 0 or len(data) > max_bytes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".write-lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        try:
            cutoff = time.time() - max(0, max_age_seconds)
            # A constant number of metadata checks, independent of workspace size.
            for index in range(1, max_archives + 1):
                archive = path.with_name(f"{path.name}.{index}")
                if archive.exists() and (archive.stat().st_mtime < cutoff or archive.stat().st_size > max_bytes):
                    archive.unlink()
            if path.exists():
                current = path.stat()
                if current.st_mtime < cutoff or current.st_size > max_bytes:
                    # Oversized legacy logs must not survive as oversized archives.
                    path.unlink()
                elif current.st_size + len(data) > max_bytes:
                    if max_archives:
                        path.with_name(f"{path.name}.{max_archives}").unlink(missing_ok=True)
                        for index in range(max_archives - 1, 0, -1):
                            old = path.with_name(f"{path.name}.{index}")
                            if old.exists():
                                os.replace(old, path.with_name(f"{path.name}.{index + 1}"))
                        os.replace(path, path.with_name(f"{path.name}.1"))
                    else:
                        path.unlink()
            with path.open("ab") as sink:
                sink.write(data)
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
