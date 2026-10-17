"""Crash-released machine-wide I/O slots; scheduling never grants proof authority."""
from __future__ import annotations

import contextlib
import ctypes
import functools
import hashlib
import os
import sys
import threading
import time

_LOCAL = threading.local()


def _try_lock(kind, slot):
    identity = hashlib.sha256(os.path.expanduser("~").encode()).hexdigest()[:20]
    if sys.platform == "win32":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateMutexW(None, False, f"Local\\DepLoomIoV1-{identity}-{kind}-{slot}")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        result = kernel.WaitForSingleObject(handle, 0)
        if result in (0, 0x80):  # WAIT_ABANDONED transfers ownership after a crash.
            def release():
                try:
                    if not kernel.ReleaseMutex(handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel.CloseHandle(handle)
            return release
        kernel.CloseHandle(handle)
        if result != 0x102:
            raise OSError(f"IO_SLOT_WAIT_FAILED: {result}")
        return None
    import fcntl
    directory = os.path.join(os.path.expanduser("~"), ".cache", f"deploom-io-v1-{identity}")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor = os.open(os.path.join(directory, f"{kind}-{slot}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    def release():
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    return release


@contextlib.contextmanager
def shared_io_slot(kind, *, progress=None, label="", timeout_seconds=0):
    from io_governor import _configured_slots
    from verification_observability import emit_observability_event, new_observability_id
    if kind not in {"copy", "hash", "pm"}:
        raise ValueError(f"IO_GOVERNOR_KIND_INVALID: {kind}")
    held = getattr(_LOCAL, "held", None)
    if held is None:
        held = _LOCAL.held = set()
    if kind in held:
        yield 0
        return
    operation = new_observability_id("shared-io")
    started = time.monotonic()
    next_progress = started + 15
    release = None
    emit_observability_event("filesystem.shared-io.start", operationId=operation, ioKind=kind, label=label)
    try:
        while release is None:
            for slot in range(_configured_slots(kind)):
                release = _try_lock(kind, slot)
                if release is not None:
                    break
            if release is not None:
                break
            elapsed = time.monotonic() - started
            if timeout_seconds and elapsed >= timeout_seconds:
                raise TimeoutError(f"IO_SLOT_WAIT_TIMEOUT: {kind}: {label}")
            if progress and time.monotonic() >= next_progress:
                progress(f"{label}: waiting for shared {kind} I/O slot; elapsed={int(elapsed)}s")
                next_progress = time.monotonic() + 15
            time.sleep(0.05)
        wait_ms = int((time.monotonic() - started) * 1000)
        emit_observability_event("filesystem.shared-io.acquired", operationId=operation, ioKind=kind,
                                 label=label, ioGovernorWaitMs=wait_ms, ioGovernorSlots=_configured_slots(kind))
        held.add(kind)
        try:
            yield wait_ms
        finally:
            held.remove(kind)
    finally:
        if release is not None:
            release()
        emit_observability_event("filesystem.shared-io.finish", operationId=operation, ioKind=kind,
                                 label=label, durationMs=int((time.monotonic() - started) * 1000))


def coordinated_io(kind, *, error_type=None):
    """Coordinate whole tree passes, rather than one kernel call per file."""
    def decorate(function):
        @functools.wraps(function)
        def run(*args, **kwargs):
            with contextlib.ExitStack() as admission:
                try:
                    waited_ms = admission.enter_context(shared_io_slot(
                        kind, progress=kwargs.get("progress"),
                        label=kwargs.get("progress_label", function.__name__),
                        timeout_seconds=kwargs.get("timeout_seconds", 0)))
                except OSError as exc:
                    if error_type is not None:
                        raise error_type(str(exc)) from exc
                    raise
                if kwargs.get("timeout_seconds", 0):
                    kwargs["timeout_seconds"] = max(1, kwargs["timeout_seconds"] - waited_ms / 1000)
                return function(*args, **kwargs)
        return run
    return decorate
