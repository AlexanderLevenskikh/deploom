"""Verification observability and performance-intelligence contract.

Observability is deliberately non-authoritative: correlation IDs, timings,
resource counters and performance summaries explain proof work. Observability
can never participate in proof, solver, cache-authority, or learned-constraint identities.
"""
from __future__ import annotations

import contextvars
import ctypes
import dataclasses
import datetime as dt
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, Optional

from bounded_telemetry import append_bounded_telemetry

# BLOCK_Y_OBSERVABILITY_CONTRACT_V1
# BLOCK_Y_FULL_OBSERVABILITY_V1
OBSERVABILITY_SCHEMA = "verification-observability-v2-performance"

_RUN_ID = str(os.environ.get("DEPLOOM_RUN_ID") or "").strip() or uuid.uuid4().hex
_PROCESS_STARTED = time.monotonic()
_SESSION_STARTED = _PROCESS_STARTED
_SESSION_CONTEXT: dict[str, object] = {}
_SESSION_ID = f"session-{uuid.uuid4().hex}"
_SESSION_START_PENDING = False
_CONFIGURED_PATH: Optional[Path] = None

_SEQUENCE = 0
_SEQUENCE_LOCK = threading.Lock()
_CONTEXT: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "deploom_verification_observability_context", default=None
)
_STAGES: contextvars.ContextVar[dict[str, list[dict[str, object]]] | None] = (
    contextvars.ContextVar("deploom_verification_observability_stages", default=None)
)
_OBSERVABILITY_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "deploom_verification_observability_suppressed", default=False
)

_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()
_METRICS_LOCK = threading.Lock()


def _new_metrics() -> dict[str, object]:
    return {
        "requestsStarted": 0,
        "requestsFinished": 0,
        "attemptsStarted": 0,
        "attemptsFinished": 0,
        "attemptOutcomes": {},
        "eventCounts": {},
        "stageDurationMs": {},
        "stageCategoryDurationMs": {},
        "stageCategoryCounts": {},
        "stageCategoryBytes": {},
        "stageCategoryFiles": {},
        "cacheLookups": 0,
        "cacheHits": 0,
        "cacheMisses": 0,
        "cacheRejected": 0,
        "cachePublishes": 0,
        "solverStatuses": {},
        "solverInvocations": 0,
        "localizationChecks": 0,
        "localizationConfirmations": 0,
        "projectChecksPassed": 0,
        "projectChecksFailed": 0,
        "sourceFilesObservedMax": 0,
        "sourceBytesObservedMax": 0,
        "orphanStageFinishes": 0,
        "materializationMethods": {},
        "preparedArtifactBuilds": 0,
        "sameRunPreparedArtifactHits": 0,
        "lifecycleInstallsAvoided": 0,
        "integritySealsAvoided": 0,
        "controlProofHits": 0,
        "projectObservationHits": 0,
        "independentReproductions": 0,
        "artifactInvalidations": 0,
        "preparationRequestsCoalesced": 0,
        "totalArtifactPrepareMs": 0,
        "totalProjectTrialMs": 0,
    }


_METRICS: dict[str, object] = _new_metrics()
_INTERVALS: list[tuple[int, int, str]] = []
_SLOWEST: list[dict[str, object]] = []


@dataclasses.dataclass(frozen=True)
class ProcessResources:
    cpu_ms: int
    rss_bytes: int
    peak_rss_bytes: int


def verification_run_id() -> str:
    return _RUN_ID


def configured_observability_path() -> Optional[Path]:
    return _CONFIGURED_PATH


def configure_observability_path(
    path: str | Path | None,
    *,
    reset: bool = False,
    context: Optional[Mapping[str, object]] = None,
) -> Optional[Path]:
    """Configure the sink used by solver/filesystem/localization instrumentation.

    Verification events may still pass an explicit path. This default sink exists
    for lower-level components that intentionally know nothing about Baseline
    configuration. Reset starts a new performance-accounting session, not a new
    proof/cache identity.
    """
    global _CONFIGURED_PATH, _SESSION_STARTED, _SESSION_CONTEXT, _SESSION_ID
    global _SESSION_START_PENDING, _METRICS, _INTERVALS, _SLOWEST

    resolved: Optional[Path]
    if path is None or not str(path).strip():
        resolved = None
    else:
        resolved = Path(path).expanduser().resolve()

    with _METRICS_LOCK:
        _CONFIGURED_PATH = resolved
        if reset:
            _SESSION_STARTED = time.monotonic()
            _SESSION_CONTEXT = dict(context or {})
            _SESSION_ID = f"session-{uuid.uuid4().hex}"
            _SESSION_START_PENDING = False
            _METRICS = _new_metrics()
            _INTERVALS = []
            _SLOWEST = []
        elif context:
            _SESSION_CONTEXT.update(dict(context))

    # Configuration must be side-effect free. The sink may temporarily point
    # inside a semantic source tree; SourceSnapshot gets the first chance to
    # suppress unsafe in-tree observation before any file is created.
    return resolved


def new_observability_id(kind: str) -> str:
    prefix = "".join(ch for ch in str(kind).lower() if ch.isalnum() or ch in "-_")
    return f"{prefix}-{uuid.uuid4().hex}" if prefix else uuid.uuid4().hex


def _next_sequence() -> int:
    global _SEQUENCE
    with _SEQUENCE_LOCK:
        _SEQUENCE += 1
        return _SEQUENCE


def _session_offset_ms() -> int:
    return max(0, int((time.monotonic() - _SESSION_STARTED) * 1000))


def current_request_id() -> str:
    return str((_CONTEXT.get() or {}).get("requestId") or "")


def current_attempt_id() -> str:
    return str((_CONTEXT.get() or {}).get("attemptId") or "")


@contextmanager
def suppress_observability() -> Iterator[None]:
    """Suppress telemetry where emitting it would mutate the proof subject."""
    token = _OBSERVABILITY_SUPPRESSED.set(True)
    try:
        yield
    finally:
        _OBSERVABILITY_SUPPRESSED.reset(token)


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
        "verify.request.start",
        "verify.request.finish",
        "verify.attempt.start",
        "verify.attempt.finish",
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
    result.setdefault("sessionId", _SESSION_ID)
    result.setdefault("eventId", new_observability_id("event"))
    result.setdefault("sequence", _next_sequence())
    result.setdefault("threadId", threading.get_ident())
    result.setdefault("runOffsetMs", _session_offset_ms())
    if _SESSION_CONTEXT:
        result.setdefault("sessionContext", dict(_SESSION_CONTEXT))
    for key, value in (_CONTEXT.get() or {}).items():
        if value:
            result.setdefault(key, value)

    base, kind = _stage_base(event)
    stages = _STAGES.get()
    if base and stages is not None:
        if kind == "start":
            stage_id = str(result.get("stageId") or new_observability_id("stage"))
            offset = int(result["runOffsetMs"])
            result["stageId"] = stage_id
            result.setdefault("stageStartedOffsetMs", offset)
            stages.setdefault(base, []).append(
                {
                    "stageId": stage_id,
                    "started": time.monotonic(),
                    "startedOffsetMs": offset,
                }
            )
        elif kind == "finish":
            stack = stages.get(base) or []
            if stack:
                state = stack.pop()
                if not stack:
                    stages.pop(base, None)
                result.setdefault("stageId", str(state["stageId"]))
                result.setdefault("stageStartedOffsetMs", int(state["startedOffsetMs"]))
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


def performance_category(event: str) -> str:
    """Map non-overlapping low-level work events to performance categories."""
    exact = {
        "solver.z3.finish": "solver-z3",
        "filesystem.materialize.finish": "filesystem-materialize",
        "filesystem.integrity.finish": "dependency-integrity",
        "source.manifest.finish": "source-manifest",
        "verify.resolver.finish": "resolver-install",
        "verify.preparation.finish": "lifecycle-preparation",
        "verify.preparation.snapshot-publish.finish": "snapshot-publish",
        "verify.project-check.clone.finish": "project-clone",
        "verify.project-check.finish": "project-check",
        "localization.ddmin.check-finish": "localization-check",
        "localization.ddmin.confirmation-finish": "localization-confirmation",
    }
    return exact.get(str(event), "")


def _int_field(payload: Mapping[str, object], names: tuple[str, ...]) -> int:
    for name in names:
        value = payload.get(name)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _bump_dict(metric: str, key: str, amount: int = 1) -> None:
    values = dict(_METRICS[metric])
    values[str(key)] = int(values.get(str(key), 0)) + int(amount)
    _METRICS[metric] = values


def _remember_slow(event: str, category: str, payload: Mapping[str, object]) -> None:
    duration = _int_field(payload, ("durationMs",))
    if duration <= 0:
        return
    item = {
        "event": event,
        "category": category,
        "durationMs": duration,
        "operationId": str(
            payload.get("operationId")
            or payload.get("stageId")
            or payload.get("attemptId")
            or ""
        ),
        "label": str(payload.get("label") or payload.get("command") or "")[:300],
    }
    _SLOWEST.append(item)
    _SLOWEST.sort(key=lambda value: int(value["durationMs"]), reverse=True)
    del _SLOWEST[20:]


def record_verification_event(event: str, payload: Mapping[str, object]) -> None:
    if event == "verification.run.summary":
        return

    category = performance_category(event)
    with _METRICS_LOCK:
        _bump_dict("eventCounts", event)

        if event == "verify.request.start":
            _METRICS["requestsStarted"] = int(_METRICS["requestsStarted"]) + 1
        elif event == "verify.request.finish":
            _METRICS["requestsFinished"] = int(_METRICS["requestsFinished"]) + 1
        elif event == "verify.attempt.start":
            _METRICS["attemptsStarted"] = int(_METRICS["attemptsStarted"]) + 1
        elif event == "verify.attempt.finish":
            _METRICS["attemptsFinished"] = int(_METRICS["attemptsFinished"]) + 1
            _bump_dict("attemptOutcomes", str(payload.get("outcome") or "unknown"))

        if event.endswith(".finish") and isinstance(payload.get("durationMs"), int):
            _bump_dict("stageDurationMs", event, int(payload["durationMs"]))

        if category:
            duration = _int_field(payload, ("durationMs",))
            _bump_dict("stageCategoryCounts", category)
            _bump_dict("stageCategoryDurationMs", category, duration)

            byte_count = _int_field(
                payload,
                (
                    "byteCount",
                    "bytesCopied",
                    "bytesHashed",
                    "bytesRead",
                    "sourceBytes",
                ),
            )
            file_count = _int_field(
                payload,
                ("fileCount", "files", "sourceFiles", "candidateFiles"),
            )
            if byte_count:
                _bump_dict("stageCategoryBytes", category, byte_count)
            if file_count:
                _bump_dict("stageCategoryFiles", category, file_count)

            finish_offset = _int_field(payload, ("runOffsetMs",))
            if duration > 0 and finish_offset >= 0:
                start_offset = max(
                    0,
                    _int_field(payload, ("stageStartedOffsetMs",))
                    or finish_offset - duration,
                )
                _INTERVALS.append((start_offset, finish_offset, category))
            _remember_slow(event, category, payload)

        if event == "proof.cache.lookup":
            _METRICS["cacheLookups"] = int(_METRICS["cacheLookups"]) + 1
        elif event == "proof.cache.hit":
            _METRICS["cacheHits"] = int(_METRICS["cacheHits"]) + 1
        elif event == "proof.cache.miss":
            _METRICS["cacheMisses"] = int(_METRICS["cacheMisses"]) + 1
        elif event == "proof.cache.rejected":
            _METRICS["cacheRejected"] = int(_METRICS["cacheRejected"]) + 1
        elif event == "proof.cache.publish":
            _METRICS["cachePublishes"] = int(_METRICS["cachePublishes"]) + 1

        if event == "solver.z3.finish":
            _METRICS["solverInvocations"] = int(_METRICS["solverInvocations"]) + 1
            _bump_dict("solverStatuses", str(payload.get("status") or "unknown"))

        if event == "localization.ddmin.check-finish":
            _METRICS["localizationChecks"] = int(_METRICS["localizationChecks"]) + 1
        elif event == "localization.ddmin.confirmation-finish":
            _METRICS["localizationConfirmations"] = (
                int(_METRICS["localizationConfirmations"]) + 1
            )

        if event == "verify.project-check.finish":
            if str(payload.get("outcome") or "") == "passed":
                _METRICS["projectChecksPassed"] = int(_METRICS["projectChecksPassed"]) + 1
            else:
                _METRICS["projectChecksFailed"] = int(_METRICS["projectChecksFailed"]) + 1

        if event == "verify.workspace.finish":
            _METRICS["sourceFilesObservedMax"] = max(
                int(_METRICS["sourceFilesObservedMax"]),
                _int_field(payload, ("sourceFiles",)),
            )
            _METRICS["sourceBytesObservedMax"] = max(
                int(_METRICS["sourceBytesObservedMax"]),
                _int_field(payload, ("sourceBytes",)),
            )

        if event == "filesystem.materialize.finish":
            method = str(payload.get("method") or "unknown")
            _bump_dict("materializationMethods", method)

        if event == "same-run.prepared-artifact.build":
            _METRICS["preparedArtifactBuilds"] = int(_METRICS["preparedArtifactBuilds"]) + 1
        elif event == "same-run.prepared-artifact.hit":
            _METRICS["sameRunPreparedArtifactHits"] = int(_METRICS["sameRunPreparedArtifactHits"]) + 1
            _METRICS["lifecycleInstallsAvoided"] = int(_METRICS["lifecycleInstallsAvoided"]) + 1
            _METRICS["integritySealsAvoided"] = int(_METRICS["integritySealsAvoided"]) + 1
        elif event == "same-run.prepared-artifact.coalesced":
            _METRICS["preparationRequestsCoalesced"] = int(_METRICS["preparationRequestsCoalesced"]) + 1
        elif event == "same-run.control-proof.hit":
            _METRICS["controlProofHits"] = int(_METRICS["controlProofHits"]) + 1
        elif event == "same-run.project-observation.hit":
            _METRICS["projectObservationHits"] = int(_METRICS["projectObservationHits"]) + 1
        elif event == "same-run.independent-reproduction":
            _METRICS["independentReproductions"] = int(_METRICS["independentReproductions"]) + 1
        elif event == "same-run.prepared-artifact.invalidated":
            _METRICS["artifactInvalidations"] = int(_METRICS["artifactInvalidations"]) + 1

        if event == "verify.preparation.finish":
            _METRICS["totalArtifactPrepareMs"] = int(_METRICS["totalArtifactPrepareMs"]) + _int_field(payload, ("durationMs",))
        elif event == "verify.project-check.finish":
            _METRICS["totalProjectTrialMs"] = int(_METRICS["totalProjectTrialMs"]) + _int_field(payload, ("durationMs",))

        if str(payload.get("stagePairing") or "") == "orphan-finish":
            _METRICS["orphanStageFinishes"] = int(_METRICS["orphanStageFinishes"]) + 1


def _write_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path.resolve()))
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def emit_observability_event(
    event: str,
    *,
    path: str | Path | None = None,
    **fields: object,
) -> None:
    """Best-effort generic JSONL event emission; never proof authority."""
    global _SESSION_START_PENDING

    if _OBSERVABILITY_SUPPRESSED.get():
        return
    sink = (
        Path(path).expanduser().resolve()
        if path is not None and str(path).strip()
        else _CONFIGURED_PATH
    )
    if sink is None:
        return

    decorated = decorate_verification_event(event, fields)
    payload = {
        "schemaVersion": 1,
        "event": str(event),
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pid": os.getpid(),
        **decorated,
    }
    record_verification_event(str(event), payload)

    try:
        sink.parent.mkdir(parents=True, exist_ok=True)
        line = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        with _write_lock(sink):
            append_bounded_telemetry(sink, line)
    except OSError:
        return


def _merged_covered_ms(intervals: list[tuple[int, int, str]]) -> int:
    ranges = sorted(
        (max(0, int(start)), max(0, int(end)))
        for start, end, _category in intervals
        if int(end) > int(start)
    )
    if not ranges:
        return 0
    total = 0
    current_start, current_end = ranges[0]
    for start, end in ranges[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        total += current_end - current_start
        current_start, current_end = start, end
    return total + current_end - current_start


def run_summary_payload(*, final: bool = False) -> dict[str, object]:
    with _METRICS_LOCK:
        metrics = {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in _METRICS.items()
        }
        intervals = list(_INTERVALS)
        slowest = [dict(item) for item in _SLOWEST]

    resources = process_resource_snapshot()
    elapsed = _session_offset_ms()
    covered = min(elapsed, _merged_covered_ms(intervals))
    category_duration = {
        str(key): int(value)
        for key, value in dict(metrics["stageCategoryDurationMs"]).items()
    }
    category_counts = {
        str(key): int(value)
        for key, value in dict(metrics["stageCategoryCounts"]).items()
    }
    category_bytes = {
        str(key): int(value)
        for key, value in dict(metrics["stageCategoryBytes"]).items()
    }
    category_files = {
        str(key): int(value)
        for key, value in dict(metrics["stageCategoryFiles"]).items()
    }
    known_work = sum(category_duration.values())
    breakdown: list[dict[str, object]] = []
    for category in sorted(category_duration, key=lambda key: (-category_duration[key], key)):
        duration = category_duration[category]
        byte_count = category_bytes.get(category, 0)
        throughput = (
            round((byte_count / (1024 * 1024)) / (duration / 1000), 2)
            if byte_count > 0 and duration > 0
            else 0.0
        )
        breakdown.append(
            {
                "category": category,
                "count": category_counts.get(category, 0),
                "workMs": duration,
                "workPct": round((duration * 100.0 / known_work), 2)
                if known_work
                else 0.0,
                "bytes": byte_count,
                "files": category_files.get(category, 0),
                "throughputMiBPerSec": throughput,
            }
        )

    return {
        "observabilitySchema": OBSERVABILITY_SCHEMA,
        "final": bool(final),
        "provisional": not bool(final),
        "sessionId": _SESSION_ID,
        "sessionContext": dict(_SESSION_CONTEXT),
        "sessionElapsedMs": elapsed,
        "processUptimeMs": int((time.monotonic() - _PROCESS_STARTED) * 1000),
        "cpuMs": resources.cpu_ms,
        "rssBytes": resources.rss_bytes,
        "peakRssBytes": resources.peak_rss_bytes,
        **metrics,
        "performanceBreakdown": breakdown,
        "topSlowOperations": slowest[:10],
        "accounting": {
            "model": "interval-union-v1",
            "knownOperationWorkMs": known_work,
            "coveredWallMs": covered,
            "unattributedWallMs": max(0, elapsed - covered),
            "coveragePct": round((covered * 100.0 / elapsed), 2) if elapsed else 0.0,
            "parallelWorkFactor": round((known_work / covered), 3) if covered else 0.0,
            "note": (
                "knownOperationWorkMs may exceed coveredWallMs because parallel "
                "operations overlap; coveredWallMs is the union of instrumented intervals"
            ),
        },
    }


def emit_run_summary(
    *,
    path: str | Path | None = None,
    final: bool = False,
    reason: str = "",
) -> None:
    emit_observability_event(
        "verification.run.summary",
        path=path,
        reason=str(reason or ""),
        **run_summary_payload(final=final),
    )
