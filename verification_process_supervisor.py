#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

ProgressCallback = Callable[[str], None]


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
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJob:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    def __init__(self) -> None:
        self.handle = 0
        self.attached = False
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
            return
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        ok = kernel32.SetInformationJobObject(
            handle,
            self.JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(handle)
            return
        self.handle = int(ctypes.cast(handle, ctypes.c_void_p).value or 0)

    def attach(self, process: subprocess.Popen[str]) -> bool:
        if os.name != "nt" or not self.handle or process.pid <= 0:
            return False
        from ctypes import wintypes

        raw_process_handle = getattr(process, "_handle", None)
        if raw_process_handle is None:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        ok = kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(self.handle),
            wintypes.HANDLE(int(raw_process_handle)),
        )
        self.attached = bool(ok)
        return self.attached

    def terminate(self, exit_code: int = 1) -> bool:
        if os.name != "nt" or not self.handle or not self.attached:
            return False
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
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


def _bounded_windows_tree_kill(
    process: subprocess.Popen[str],
    *,
    progress: Optional[ProgressCallback],
    progress_label: str,
    hard_timeout_seconds: int = 12,
) -> None:
    if process.poll() is not None or not process.pid:
        return
    taskkill = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "taskkill.exe",
    )
    if not os.path.isfile(taskkill):
        taskkill = "taskkill"
    try:
        killer = subprocess.Popen(
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError:
        killer = None

    if killer is not None:
        started = time.monotonic()
        last_heartbeat = -1.0
        while killer.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed >= hard_timeout_seconds:
                try:
                    killer.kill()
                except OSError:
                    pass
                break
            if elapsed - last_heartbeat >= 1.0:
                _emit(
                    progress,
                    f"{progress_label}: terminating Windows process tree; "
                    f"pid={process.pid}; cleanupElapsed={int(elapsed)}s",
                )
                last_heartbeat = elapsed
            time.sleep(0.10)

    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def terminate_process_tree(
    process: subprocess.Popen[str],
    *,
    job: Optional[_WindowsJob] = None,
    progress: Optional[ProgressCallback] = None,
    progress_label: str = "command",
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        if job is not None and job.terminate():
            _emit(
                progress,
                f"{progress_label}: Windows Job Object termination requested; "
                f"pid={process.pid}",
            )
        else:
            _bounded_windows_tree_kill(
                process,
                progress=progress,
                progress_label=progress_label,
            )
        return

    if process.pid:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
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
) -> subprocess.CompletedProcess[str]:
    merged_env = dict(base_env) if base_env is not None else os.environ.copy()
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})

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
    )
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kwargs["creationflags"] = int(
            subprocess.CREATE_NEW_PROCESS_GROUP
        )

    process: subprocess.Popen[str] = subprocess.Popen(
        list(argv), **popen_kwargs
    )
    job = _WindowsJob() if os.name == "nt" else None
    if job is not None:
        attached = job.attach(process)
        _emit(
            progress,
            f"{progress_label}: process supervisor="
            f"{'windows-job-object' if attached else 'windows-bounded-tree-fallback'}; "
            f"pid={process.pid}",
        )

    interval = max(
        1,
        min(
            int(progress_interval_seconds or 15),
            max(1, int(timeout_seconds)),
        ),
    )
    try:
        while True:
            elapsed = time.monotonic() - started
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                _emit(
                    progress,
                    f"{progress_label}: HARD_TIMEOUT after {int(elapsed)}s; "
                    f"terminating owned process tree pid={process.pid}",
                )
                terminate_process_tree(
                    process,
                    job=job,
                    progress=progress,
                    progress_label=progress_label,
                )
                cleanup_started = time.monotonic()
                cleanup_deadline = cleanup_started + 20.0
                last_cleanup_heartbeat = -1.0
                while (
                    process.poll() is None
                    and time.monotonic() < cleanup_deadline
                ):
                    cleanup_elapsed = time.monotonic() - cleanup_started
                    if cleanup_elapsed - last_cleanup_heartbeat >= 1.0:
                        _emit(
                            progress,
                            f"{progress_label}: timeout cleanup running; "
                            f"cleanupElapsed={int(cleanup_elapsed)}s; "
                            f"pid={process.pid}",
                        )
                        last_cleanup_heartbeat = cleanup_elapsed
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        terminate_process_tree(
                            process,
                            job=job,
                            progress=progress,
                            progress_label=progress_label,
                        )
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                raise subprocess.TimeoutExpired(
                    list(argv), timeout_seconds
                )

            try:
                stdout, _stderr = process.communicate(
                    timeout=min(interval, remaining)
                )
                return subprocess.CompletedProcess(
                    list(argv),
                    process.returncode or 0,
                    stdout=stdout or "",
                    stderr=None,
                )
            except subprocess.TimeoutExpired:
                _emit(
                    progress,
                    f"{progress_label}: running; "
                    f"elapsed={int(time.monotonic() - started)}s; "
                    f"hardTimeout={timeout_seconds}s; pid={process.pid}",
                )
    finally:
        if job is not None:
            job.close()
