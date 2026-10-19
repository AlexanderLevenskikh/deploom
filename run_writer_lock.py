"""Crash-released run writer guard; metadata is diagnostic, never the lock."""
import json
import os
import time
import uuid
from pathlib import Path


def _unlock(handle):
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def acquire(path, owner, stale, *, timeout=15):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Outside the run directory: archive-run must never move the lock inode.
    guard = path.parent.with_name('.' + path.parent.name + '.writer-lock')
    deadline = time.monotonic() + timeout
    while True:
        handle = guard.open('a+b')
        if guard.stat().st_size == 0:
            handle.write(b'0'); handle.flush()
        try:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
        else:
            # Respect a still-running legacy writer that predates this guard.
            try:
                metadata = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                metadata = {}
            guarded = isinstance(metadata, dict) and metadata.get('protocol') == 'os-lock-v1'
            if path.exists() and not guarded and not stale():
                _unlock(handle)
            else:
                token = uuid.uuid4().hex
                temporary = path.with_name(path.name + '.' + token + '.tmp')
                try:
                    temporary.write_text(json.dumps({'protocol': 'os-lock-v1', 'owner': owner, 'pid': os.getpid(),
                        'token': token, 'startedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}), encoding='utf-8')
                    os.replace(temporary, path)
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    _unlock(handle)
                    raise
                return handle, token
        if time.monotonic() >= deadline:
            raise TimeoutError('RUN_LOCK_BUSY: ' + str(path))
        time.sleep(.05)


def release(path, lease):
    if lease is None:
        return
    handle, token = lease
    try:
        try:
            metadata = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            if metadata.get('token') == token:
                Path(path).unlink()
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    finally:
        _unlock(handle)
