#!/usr/bin/env python3
"""Cross-platform process-tree supervision with bounded output capture.

Block Sigma closes a subtle lifetime gap: a successful parent process is not
proof that its descendants are gone.  Verification callers may release shared
filesystem leases only after the owned process tree is quiescent.
"""
from __future__ import annotations

import ctypes
import dataclasses
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from verification_observability import emit_observability_event

ProgressCallback = Callable[[str], None]
OutputObserver = Callable[[str], None]


class ProcessSupervisionError(OSError):
    """Tree ownership/quiescence could not be proven."""


@dataclasses.dataclass(frozen=True)
class SupervisionMetadata:
    quality: str
    descendants_terminated: int = 0
    descendants_remaining: int = 0
    quiescence_ms: int = 0
    captured_bytes: int = 0
    dropped_bytes: int = 0
    output_truncated: bool = False
    attach_before_execution: bool = False


class _BoundedOutput:
    """Bound stdout memory while retaining useful beginning and end context."""

    def __init__(self, max_bytes: int, observer: Optional[OutputObserver]) -> None:
        self.max_bytes = max(16 * 1024, int(max_bytes))
        self.head_limit = self.max_bytes // 3
        self.tail_limit = self.max_bytes - self.head_limit
        self.observer = observer
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self._lock = threading.Lock()

    def feed(self, text: str) -> None:
        if not text:
            return
        if self.observer is not None:
            try:
                self.observer(text)
            except Exception:
                # Observability/classification callbacks must never own process
                # lifetime. Callers can independently fail closed on their data.
                pass
        data = text.encode("utf-8", errors="replace")
        with self._lock:
            self._total += len(data)
            if len(self._head) < self.head_limit:
                take = min(self.head_limit - len(self._head), len(data))
                self._head.extend(data[:take])
                data = data[take:]
            if data:
                self._tail.extend(data)
                if len(self._tail) > self.tail_limit:
                    del self._tail[: len(self._tail) - self.tail_limit]

    def render(self) -> tuple[str, int, int, bool]:
        with self._lock:
            head = bytes(self._head)
            tail = bytes(self._tail)
            total = int(self._total)
        kept = len(head) + len(tail)
        dropped = max(0, total - kept)
        if dropped:
            marker = (
                f"\n... [DepLoom output truncated; droppedBytes={dropped}] ...\n"
            ).encode("utf-8")
            payload = head + marker + tail
        else:
            payload = head + tail
        return payload.decode("utf-8", errors="replace"), total, dropped, bool(dropped)


class _JOBOBJECT_IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _JOBOBJECT_IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobProcessMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _WindowsJob:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectBasicAccountingInformation = 1
    JobObjectExtendedLimitInformation = 9

    def __init__(self) -> None:
        self.handle = 0
        self.attached = False
        self.error = ""
        if os.name != "nt":
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            self.error = f"CreateJobObjectW:{ctypes.get_last_error()}"
            return
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle,
            self.JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            self.error = f"SetInformationJobObject:{ctypes.get_last_error()}"
            kernel32.CloseHandle(handle)
            return
        self.handle = int(ctypes.cast(handle, ctypes.c_void_p).value or 0)

    def attach(self, process: subprocess.Popen[str]) -> bool:
        if os.name != "nt" or not self.handle or process.pid <= 0:
            return False
        from ctypes import wintypes

        raw_process_handle = getattr(process, "_handle", None)
        if raw_process_handle is None:
            self.error = "Popen process handle unavailable"
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        ok = kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self.handle),
            wintypes.HANDLE(int(raw_process_handle)),
        )
        self.attached = bool(ok)
        if not self.attached:
            self.error = f"AssignProcessToJobObject:{ctypes.get_last_error()}"
        return self.attached

    def active_processes(self) -> int:
        if os.name != "nt" or not self.handle or not self.attached:
            raise ProcessSupervisionError("PROCESS_SUPERVISION_WINDOWS_JOB_UNAVAILABLE")
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        ok = kernel32.QueryInformationJobObject(
            wintypes.HANDLE(self.handle),
            self.JobObjectBasicAccountingInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        if not ok:
            raise ProcessSupervisionError(
                f"PROCESS_SUPERVISION_WINDOWS_QUERY_FAILED:{ctypes.get_last_error()}"
            )
        return int(info.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> bool:
        if os.name != "nt" or not self.handle or not self.attached:
            return False
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        return bool(
            kernel32.TerminateJobObject(
                wintypes.HANDLE(self.handle), wintypes.UINT(exit_code)
            )
        )

    def close(self) -> None:
        if os.name != "nt" or not self.handle:
            return
        from ctypes import wintypes

        handle, self.handle = self.handle, 0
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _emit(progress: Optional[ProgressCallback], message: str) -> None:
    if progress is not None:
        progress(message)


def _enable_linux_subreaper() -> bool:
    if not (os.name == "posix" and os.path.isdir("/proc") and "linux" in os.uname().sysname.lower()):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # PR_SET_CHILD_SUBREAPER = 36
        return int(libc.prctl(36, 1, 0, 0, 0)) == 0
    except Exception:
        return False


_LINUX_SUBREAPER = _enable_linux_subreaper()


def _linux_token_processes(token: str) -> list[int]:
    marker = f"DEPLOOM_SUPERVISION_TOKEN={token}".encode("utf-8")
    result: list[int] = []
    if not _LINUX_SUBREAPER:
        return result
    try:
        names = os.listdir("/proc")
    except OSError as exc:
        raise ProcessSupervisionError(f"PROCESS_SUPERVISION_PROC_UNAVAILABLE:{exc}") from exc
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == os.getpid():
            continue
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        if marker in environ.split(b"\0"):
            result.append(pid)
    return sorted(result)


def _pid_is_zombie(pid: int) -> bool:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    end = stat_text.rfind(")")
    fields = stat_text[end + 2 :].split() if end >= 0 else []
    return bool(fields and fields[0] == "Z")


def _kill_posix_process_group(pgid: int) -> int:
    try:
        os.killpg(pgid, signal.SIGKILL)
        return 1
    except ProcessLookupError:
        return 0
    except OSError:
        return 0


def _resume_windows_process(process: subprocess.Popen[str]) -> None:
    """Resume a CREATE_SUSPENDED process only after Job Object association."""
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = int(ntdll.NtResumeProcess(ctypes.c_void_p(int(process._handle))))
    except Exception as exc:
        raise ProcessSupervisionError(f"PROCESS_SUPERVISION_RESUME_FAILED: {exc}") from exc
    if status != 0:
        raise ProcessSupervisionError(
            f"PROCESS_SUPERVISION_RESUME_FAILED: NTSTATUS=0x{status & 0xffffffff:08x}"
        )


def _quiesce_owned_tree(
    process: subprocess.Popen[str],
    *,
    job: Optional[_WindowsJob],
    pgid: int,
    linux_token: str,
    progress: Optional[ProgressCallback],
    progress_label: str,
    hard_timeout_seconds: float = 12.0,
) -> tuple[str, int, int, int]:
    """Terminate descendants and prove the strongest available quiescence."""
    started = time.monotonic()
    terminated = 0

    if os.name == "nt":
        if job is None or not job.attached:
            raise ProcessSupervisionError(
                "PROCESS_SUPERVISION_UNAVAILABLE: Windows Job Object attach failed"
                + (f" ({job.error})" if job is not None and job.error else "")
            )
        active = job.active_processes()
        if active:
            if job.terminate(exit_code=1):
                terminated += max(0, active - (1 if process.poll() is None else 0))
            deadline = started + hard_timeout_seconds
            while time.monotonic() < deadline:
                active = job.active_processes()
                if active == 0:
                    break
                time.sleep(0.05)
            remaining = job.active_processes()
            if remaining:
                raise ProcessSupervisionError(
                    f"PROCESS_SUPERVISION_DESCENDANTS_REMAIN: {remaining} Windows Job process(es)"
                )
        quiescence_ms = int((time.monotonic() - started) * 1000)
        return "guaranteed-tree", terminated, 0, quiescence_ms

    if pgid > 0:
        terminated += _kill_posix_process_group(pgid)

    if _LINUX_SUBREAPER:
        deadline = started + hard_timeout_seconds
        stable_empty = 0
        while time.monotonic() < deadline:
            owned = [pid for pid in _linux_token_processes(linux_token) if pid != process.pid]
            live = [pid for pid in owned if not _pid_is_zombie(pid)]
            if not live:
                stable_empty += 1
                if stable_empty >= 2:
                    break
                time.sleep(0.05)
                continue
            stable_empty = 0
            for pid in live:
                try:
                    os.kill(pid, signal.SIGKILL)
                    terminated += 1
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    raise ProcessSupervisionError(
                        f"PROCESS_SUPERVISION_DESCENDANT_KILL_FAILED:{pid}:{exc}"
                    ) from exc
            time.sleep(0.05)
        remaining_pids = [
            pid for pid in _linux_token_processes(linux_token)
            if pid != process.pid and not _pid_is_zombie(pid)
        ]
        if remaining_pids:
            raise ProcessSupervisionError(
                "PROCESS_SUPERVISION_DESCENDANTS_REMAIN: "
                + ",".join(str(pid) for pid in remaining_pids[:16])
            )
        quiescence_ms = int((time.monotonic() - started) * 1000)
        # /proc environment tokens are observable aids, not complete descendant
        # authority: descendants can scrub the token or become unreadable.
        return "best-effort", terminated, 0, quiescence_ms

    # macOS/other POSIX: a fresh session gives strong process-group ownership,
    # but a child can deliberately detach with setsid().  Verification remains
    # private-copy safe, but callers must not reuse/shared-mount based on this.
    deadline = started + min(hard_timeout_seconds, 2.0)
    remaining = 0
    while time.monotonic() < deadline and pgid > 0:
        try:
            os.killpg(pgid, 0)
            remaining = 1
            _kill_posix_process_group(pgid)
            time.sleep(0.05)
        except ProcessLookupError:
            remaining = 0
            break
        except OSError:
            remaining = 0
            break
    quiescence_ms = int((time.monotonic() - started) * 1000)
    return "best-effort", terminated, remaining, quiescence_ms


def terminate_process_tree(
    process: subprocess.Popen[str],
    *,
    job: Optional[_WindowsJob] = None,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "command",
) -> None:
    if os.name == "nt":
        if job is not None and job.attached and job.terminate():
            _emit(progress, f"{progress_label}: Windows Job Object termination requested; pid={process.pid}")
        elif process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        return
    if process.pid:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass


def run_supervised(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout_seconds: int,
    env: Optional[Mapping[str, str]] = None,
    base_env: Optional[Mapping[str, str]] = None,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "command",
    progress_interval_seconds: int = 15,
    output_observer: Optional[OutputObserver] = None,
    max_output_bytes: int = 1024 * 1024,
) -> subprocess.CompletedProcess[str]:
    merged_env = dict(base_env) if base_env is not None else os.environ.copy()
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})
    linux_token = uuid.uuid4().hex
    if _LINUX_SUBREAPER:
        merged_env["DEPLOOM_SUPERVISION_TOKEN"] = linux_token

    started = time.monotonic()
    popen_kwargs = dict(
        cwd=str(cwd),
        env=merged_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        bufsize=1,
    )
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kwargs["creationflags"] = (
            int(subprocess.CREATE_NEW_PROCESS_GROUP)
            | int(getattr(subprocess, "CREATE_SUSPENDED", 0x00000004))
        )

    process: subprocess.Popen[str] = subprocess.Popen(list(argv), **popen_kwargs)
    # A handful of historical unit tests use minimal Popen doubles. Keep that
    # seam without weakening production: real subprocess.Popen always exposes
    # stdout/wait and therefore always takes the supervised path below.
    if not hasattr(process, "wait") or not hasattr(process, "stdout"):
        stdout, _stderr = process.communicate(timeout=max(1, int(timeout_seconds)))
        completed = subprocess.CompletedProcess(
            list(argv), int(getattr(process, "returncode", 0) or 0),
            stdout=stdout or "", stderr=None,
        )
        setattr(completed, "supervision", SupervisionMetadata(quality="test-double"))
        setattr(completed, "captured_bytes", len((stdout or "").encode("utf-8", errors="replace")))
        setattr(completed, "dropped_bytes", 0)
        setattr(completed, "output_truncated", False)
        return completed
    pgid = process.pid if os.name != "nt" else 0
    job = _WindowsJob() if os.name == "nt" else None
    if job is not None:
        attached = job.attach(process)
        if not attached:
            try:
                process.kill()
            except OSError:
                pass
            job.close()
            raise ProcessSupervisionError(
                "PROCESS_SUPERVISION_UNAVAILABLE: Windows Job Object attach failed"
                + (f" ({job.error})" if job.error else "")
            )
        try:
            _resume_windows_process(process)
        except ProcessSupervisionError:
            job.terminate(exit_code=1)
            job.close()
            raise
        _emit(progress, f"{progress_label}: process supervisor=windows-job-object; attachBeforeExecution=true; pid={process.pid}")
    elif _LINUX_SUBREAPER:
        _emit(progress, f"{progress_label}: process supervisor=linux-subreaper-session; pid={process.pid}")
    else:
        _emit(progress, f"{progress_label}: process supervisor=posix-process-group-best-effort; pid={process.pid}")

    capture = _BoundedOutput(max_output_bytes, output_observer)

    def reader() -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                capture.feed(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    reader_thread = threading.Thread(
        target=reader,
        name=f"deploom-output-{process.pid}",
        daemon=True,
    )
    reader_thread.start()

    interval = max(1, min(int(progress_interval_seconds or 15), max(1, int(timeout_seconds))))
    next_progress = started + interval
    timed_out = False
    return_code = 0
    metadata: Optional[SupervisionMetadata] = None
    try:
        while process.poll() is None:
            now = time.monotonic()
            elapsed = now - started
            if elapsed >= timeout_seconds:
                timed_out = True
                _emit(
                    progress,
                    f"{progress_label}: HARD_TIMEOUT after {int(elapsed)}s; "
                    f"terminating owned process tree pid={process.pid}",
                )
                terminate_process_tree(
                    process, job=job, progress=progress, progress_label=progress_label
                )
                break
            if now >= next_progress:
                _emit(
                    progress,
                    f"{progress_label}: running; elapsed={int(elapsed)}s; "
                    f"hardTimeout={timeout_seconds}s; pid={process.pid}",
                )
                next_progress = now + interval
            time.sleep(0.05)

        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            terminate_process_tree(
                process, job=job, progress=progress, progress_label=progress_label
            )
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired as exc:
                raise ProcessSupervisionError(
                    f"PROCESS_SUPERVISION_PARENT_REMAINS: pid={process.pid}"
                ) from exc
        return_code = int(process.returncode or 0)

        quality, killed, remaining, quiescence_ms = _quiesce_owned_tree(
            process,
            job=job,
            pgid=pgid,
            linux_token=linux_token,
            progress=progress,
            progress_label=progress_label,
        )
        reader_thread.join(timeout=3.0)
        output, captured_bytes, dropped_bytes, truncated = capture.render()
        metadata = SupervisionMetadata(
            quality=quality,
            descendants_terminated=killed,
            descendants_remaining=remaining,
            quiescence_ms=quiescence_ms,
            captured_bytes=captured_bytes,
            dropped_bytes=dropped_bytes,
            output_truncated=truncated,
            attach_before_execution=(os.name == "nt"),
        )
        emit_observability_event(
            "process.supervision",
            quality=metadata.quality,
            attachBeforeExecution=metadata.attach_before_execution,
            descendantsKilled=metadata.descendants_terminated,
            descendantsRemaining=metadata.descendants_remaining,
        )
        if timed_out:
            exc = subprocess.TimeoutExpired(list(argv), timeout_seconds, output=output)
            setattr(exc, "supervision", metadata)
            raise exc
        completed = subprocess.CompletedProcess(
            list(argv), return_code, stdout=output, stderr=None
        )
        setattr(completed, "supervision", metadata)
        setattr(completed, "captured_bytes", captured_bytes)
        setattr(completed, "dropped_bytes", dropped_bytes)
        setattr(completed, "output_truncated", truncated)
        return completed
    finally:
        if process.poll() is None:
            terminate_process_tree(
                process, job=job, progress=progress, progress_label=progress_label
            )
        if job is not None:
            job.close()
