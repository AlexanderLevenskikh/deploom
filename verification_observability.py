"""Verification observability contract.

Observability is non-authoritative: correlation IDs, timings and resource
counters explain proof work but can never participate in proof, solver,
cache-authority, or learned-constraint identities.
"""
from __future__ import annotations

import contextvars
import ctypes
import dataclasses
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterator, Mapping

# BLOCK_Y_OBSERVABILITY_CONTRACT_V1
OBSERVABILITY_SCHEMA = "verification-observability-v1"

_RUN_ID = str(os.environ.get("DEPLOOM_RUN_ID") or "").strip() or uuid.uuid4().hex
_PROCESS_STARTED = time.monotonic()
_SEQUENCE = 0
_SEQUENCE_LOCK = threading.Lock()
_CONTEXT: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "deploom_verification_observability_context", default=None
)
_STAGES: contextvars.ContextVar[dict[str, list[dict[str, object]]] | None] = (
    contextvars.ContextVar("deploom_verification_observability_stages", default=None)
)

_METRICS_LOCK = threading.Lock()
_METRICS: dict[str, object] = {
    "requestsStarted": 0,
    "requestsFinished": 0,
    "attemptsStarted": 0,
    "attemptsFinished": 0,
    "attemptOutcomes": {},
    "stageDurationMs": {},
    "cacheLookups": 0,
    "cacheHits": 0,
    "cacheMisses": 0,
    "cacheRejected": 0,
    "sourceFilesObservedMax": 0,
    "sourceBytesObservedMax": 0,
}


@dataclasses.dataclass(frozen=True)
class ProcessResources:
    cpu_ms: int
    rss_bytes: int
    peak_rss_bytes: int


def verification_run_id() -> str:
    return _RUN_ID


def new_observability_id(kind: str) -> str:
    prefix = "".join(ch for ch in str(kind).lower() if ch.isalnum() or ch in "-_")
    return f"{prefix}-{uuid.uuid4().hex}" if prefix else uuid.uuid4().hex


def _next_sequence() -> int:
    global _SEQUENCE
    with _SEQUENCE_LOCK:
        _SEQUENCE += 1
        return _SEQUENCE


def current_attempt_id() -> str:
    return str((_CONTEXT.get() or {}).get("attemptId") or "")


@contextmanager
def request_scope(request_id: str) -> Iterator[None]:
    current = dict(_CONTEXT.get() or {})
    current["requestId"] = str(request_id)
    token = _CONTEXT.set(current)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


@contextmanager
def attempt_scope(attempt_id: str, *, parent_attempt_id: str = "") -> Iterator[None]:
    current = dict(_CONTEXT.get() or {})
    current["attemptId"] = str(attempt_id)
    if parent_attempt_id:
        current["parentAttemptId"] = str(parent_attempt_id)
    else:
        current.pop("parentAttemptId", None)
    context_token = _CONTEXT.set(current)
    stages_token = _STAGES.set({})
    try:
        yield
    finally:
        _STAGES.reset(stages_token)
        _CONTEXT.reset(context_token)


def _stage_base(event: str) -> tuple[str, str]:
    if event in {
        "verify.request.start", "verify.request.finish",
        "verify.attempt.start", "verify.attempt.finish",
    }:
        return "", ""
    if event.endswith(".start"):
        return event[:-6], "start"
    if event.endswith(".finish"):
        return event[:-7], "finish"
    return "", ""


def decorate_verification_event(
    event: str,
    fields: Mapping[str, object],
) -> dict[str, object]:
    result: dict[str, object] = dict(fields)
    result.setdefault("observabilitySchema", OBSERVABILITY_SCHEMA)
    result.setdefault("runId", _RUN_ID)
    result.setdefault("eventId", new_observability_id("event"))
    result.setdefault("sequence", _next_sequence())
    result.setdefault("threadId", threading.get_ident())
    for key, value in (_CONTEXT.get() or {}).items():
        if value:
            result.setdefault(key, value)

    base, kind = _stage_base(event)
    stages = _STAGES.get()
    if base and stages is not None:
        if kind == "start":
            stage_id = str(result.get("stageId") or new_observability_id("stage"))
            result["stageId"] = stage_id
            stages.setdefault(base, []).append(
                {"stageId": stage_id, "started": time.monotonic()}
            )
        elif kind == "finish":
            stack = stages.get(base) or []
            if stack:
                state = stack.pop()
                if not stack:
                    stages.pop(base, None)
                result.setdefault("stageId", str(state["stageId"]))
                result.setdefault(
                    "durationMs",
                    max(0, int((time.monotonic() - float(state["started"])) * 1000)),
                )
            else:
                result.setdefault("stagePairing", "orphan-finish")
    return result


def pending_stage_finishes(*, terminal_reason: str) -> list[tuple[str, dict[str, object]]]:
    stages = _STAGES.get()
    if not stages:
        return []
    pending: list[tuple[float, str, str]] = []
    for base, stack in stages.items():
        for state in stack:
            pending.append((float(state["started"]), base, str(state["stageId"])))
    pending.sort(reverse=True)
    return [
        (
            f"{base}.finish",
            {
                "stageId": stage_id,
                "outcome": "abandoned",
                "terminalReason": str(terminal_reason or "unknown"),
                "synthetic": True,
            },
        )
        for _started, base, stage_id in pending
    ]


def _windows_resources() -> ProcessResources:
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]
    rss = 0
    peak = 0
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            handle, ctypes.byref(counters), counters.cb
        )
        if ok:
            rss = int(counters.WorkingSetSize)
            peak = int(counters.PeakWorkingSetSize)
    except Exception:
        pass
    return ProcessResources(int(time.process_time() * 1000), rss, peak)


def process_resource_snapshot() -> ProcessResources:
    if os.name == "nt":
        return _windows_resources()
    rss = 0
    peak = 0
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        raw = int(usage.ru_maxrss)
        peak = raw if sys.platform == "darwin" else raw * 1024
        rss = peak
    except Exception:
        pass
    return ProcessResources(int(time.process_time() * 1000), rss, peak)


def record_verification_event(event: str, payload: Mapping[str, object]) -> None:
    if event == "verification.run.summary":
        return
    with _METRICS_LOCK:
        if event == "verify.request.start":
            _METRICS["requestsStarted"] = int(_METRICS["requestsStarted"]) + 1
        elif event == "verify.request.finish":
            _METRICS["requestsFinished"] = int(_METRICS["requestsFinished"]) + 1
        elif event == "verify.attempt.start":
            _METRICS["attemptsStarted"] = int(_METRICS["attemptsStarted"]) + 1
        elif event == "verify.attempt.finish":
            _METRICS["attemptsFinished"] = int(_METRICS["attemptsFinished"]) + 1
            outcomes = dict(_METRICS["attemptOutcomes"])
            outcome = str(payload.get("outcome") or "unknown")
            outcomes[outcome] = int(outcomes.get(outcome, 0)) + 1
            _METRICS["attemptOutcomes"] = outcomes

        if event.endswith(".finish") and isinstance(payload.get("durationMs"), int):
            durations = dict(_METRICS["stageDurationMs"])
            durations[event] = int(durations.get(event, 0)) + int(payload["durationMs"])
            _METRICS["stageDurationMs"] = durations

        if event == "proof.cache.lookup":
            _METRICS["cacheLookups"] = int(_METRICS["cacheLookups"]) + 1
        elif event == "proof.cache.hit":
            _METRICS["cacheHits"] = int(_METRICS["cacheHits"]) + 1
        elif event == "proof.cache.miss":
            _METRICS["cacheMisses"] = int(_METRICS["cacheMisses"]) + 1
        elif event == "proof.cache.rejected":
            _METRICS["cacheRejected"] = int(_METRICS["cacheRejected"]) + 1

        if event == "verify.workspace.finish":
            try:
                _METRICS["sourceFilesObservedMax"] = max(
                    int(_METRICS["sourceFilesObservedMax"]),
                    int(payload.get("sourceFiles") or 0),
                )
                _METRICS["sourceBytesObservedMax"] = max(
                    int(_METRICS["sourceBytesObservedMax"]),
                    int(payload.get("sourceBytes") or 0),
                )
            except (TypeError, ValueError):
                pass


def run_summary_payload() -> dict[str, object]:
    with _METRICS_LOCK:
        metrics = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in _METRICS.items()
        }
    resources = process_resource_snapshot()
    return {
        "provisional": True,
        "processUptimeMs": int((time.monotonic() - _PROCESS_STARTED) * 1000),
        "cpuMs": resources.cpu_ms,
        "rssBytes": resources.rss_bytes,
        "peakRssBytes": resources.peak_rss_bytes,
        **metrics,
    }
