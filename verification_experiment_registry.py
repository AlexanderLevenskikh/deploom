#!/usr/bin/env python3
"""Ψ.5 process-local registry for physical verification experiments.

This module is performance/navigation state only. It never publishes proof
authority, never changes Solver constraints, and never substitutes an
independent confirmation proof.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Callable, Generic, Optional, TypeVar

from verification_observability import emit_observability_event

T = TypeVar("T")


class ExperimentWaiterCancelled(RuntimeError):
    pass


def _digest(value: str) -> str:
    raw = str(value or "")
    if len(raw) == 64 and all(ch in "0123456789abcdef" for ch in raw.lower()):
        return raw.lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class PhysicalExperimentKey:
    strong_identity: str
    proof_slot: str
    purpose: str

    def normalized(self) -> tuple[str, str, str]:
        identity = _digest(self.strong_identity)
        slot = str(self.proof_slot or "").strip()
        purpose = str(self.purpose or "").strip().lower()
        if not slot:
            raise ValueError("PHYSICAL_EXPERIMENT_PROOF_SLOT_REQUIRED")
        if not purpose:
            raise ValueError("PHYSICAL_EXPERIMENT_PURPOSE_REQUIRED")
        return identity, slot, purpose

    @property
    def experiment_identity(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.normalized(),
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()


@dataclasses.dataclass
class _Entry(Generic[T]):
    condition: threading.Condition
    state: str = "running"
    value: Optional[T] = None
    error: Optional[BaseException] = None
    reusable: bool = False
    completed_at: float = 0.0


_LOCK = threading.RLock()
_ENTRIES: "OrderedDict[tuple[str, str, str], _Entry[object]]" = OrderedDict()
_NEGATIVE_NAVIGATION: "OrderedDict[str, OrderedDict[tuple[str, str], None]]" = OrderedDict()
_MAX_COMPLETED = 96
_MAX_NEGATIVE_CONTEXTS = 24
_MAX_NEGATIVES_PER_CONTEXT = 256


def reset_physical_experiment_registry() -> None:
    with _LOCK:
        _ENTRIES.clear()
        _NEGATIVE_NAVIGATION.clear()


def registry_snapshot() -> dict[str, int]:
    with _LOCK:
        counts = {
            "running": 0,
            "completed": 0,
            "cancelled": 0,
            "failed": 0,
        }
        for entry in _ENTRIES.values():
            state = entry.state if entry.state in counts else "failed"
            counts[state] += 1
        counts["negativeContexts"] = len(_NEGATIVE_NAVIGATION)
        counts["negativeCandidates"] = sum(
            len(values) for values in _NEGATIVE_NAVIGATION.values()
        )
        return counts


def _trim_completed() -> None:
    completed = [
        key for key, entry in _ENTRIES.items()
        if entry.state == "completed"
    ]
    while len(completed) > _MAX_COMPLETED:
        _ENTRIES.pop(completed.pop(0), None)


def navigation_negative_candidates(
    context_identity: str,
) -> set[tuple[str, str]]:
    context = _digest(context_identity)
    with _LOCK:
        values = _NEGATIVE_NAVIGATION.get(context)
        if values is None:
            return set()
        _NEGATIVE_NAVIGATION.move_to_end(context)
        return set(values)


def remember_navigation_negative(
    context_identity: str,
    navigation_key: str,
    candidate_fingerprint: str,
) -> None:
    context = _digest(context_identity)
    key = (str(navigation_key), str(candidate_fingerprint))
    if not key[0] or not key[1]:
        return
    with _LOCK:
        values = _NEGATIVE_NAVIGATION.setdefault(context, OrderedDict())
        values[key] = None
        values.move_to_end(key)
        while len(values) > _MAX_NEGATIVES_PER_CONTEXT:
            values.popitem(last=False)
        _NEGATIVE_NAVIGATION.move_to_end(context)
        while len(_NEGATIVE_NAVIGATION) > _MAX_NEGATIVE_CONTEXTS:
            _NEGATIVE_NAVIGATION.popitem(last=False)
    emit_observability_event(
        "navigation.negative.remembered",
        contextIdentity=context,
        navigationKey=key[0],
        candidate=key[1],
        authority="NAVIGATION_ONLY",
    )


def run_physical_experiment(
    key: PhysicalExperimentKey,
    producer: Callable[[], T],
    *,
    is_reusable: Optional[Callable[[T], bool]] = None,
    waiter_cancelled: Optional[Callable[[], bool]] = None,
    wait_poll_seconds: float = 0.05,
) -> tuple[T, str]:
    """Return ``(value, producer|coalesced|completed-hit)``.

    A cancelled waiter detaches without cancelling the producer. Producer
    exceptions and values rejected by ``is_reusable`` are delivered to current
    waiters but are not retained for future requests.
    """
    normalized = key.normalized()
    experiment_id = key.experiment_identity
    emit_observability_event(
        "physical-experiment.request",
        experimentIdentity=experiment_id,
        strongIdentity=normalized[0],
        proofSlot=normalized[1],
        purpose=normalized[2],
        authority="PERFORMANCE_ONLY",
    )

    producer_entry: _Entry[object] | None = None
    with _LOCK:
        entry = _ENTRIES.get(normalized)
        if (
            entry is not None
            and entry.state == "completed"
            and entry.reusable
            and entry.value is not None
        ):
            _ENTRIES.move_to_end(normalized)
            emit_observability_event(
                "physical-experiment.completed-reuse",
                experimentIdentity=experiment_id,
                strongIdentity=normalized[0],
                proofSlot=normalized[1],
                purpose=normalized[2],
                authority="PERFORMANCE_ONLY",
            )
            return copy.deepcopy(entry.value), "completed-hit"

        if entry is None or entry.state != "running":
            producer_entry = _Entry(
                condition=threading.Condition(_LOCK),
                state="running",
            )
            _ENTRIES[normalized] = producer_entry
            entry = producer_entry

        if producer_entry is None:
            while entry.state == "running":
                cancelled = False
                if waiter_cancelled is not None:
                    try:
                        cancelled = bool(waiter_cancelled())
                    except Exception:
                        cancelled = True
                if cancelled:
                    emit_observability_event(
                        "physical-experiment.waiter-cancelled",
                        experimentIdentity=experiment_id,
                        strongIdentity=normalized[0],
                        proofSlot=normalized[1],
                        purpose=normalized[2],
                        authority="PERFORMANCE_ONLY",
                    )
                    raise ExperimentWaiterCancelled(
                        f"PHYSICAL_EXPERIMENT_WAITER_CANCELLED: {experiment_id}"
                    )
                entry.condition.wait(timeout=max(0.01, float(wait_poll_seconds)))

            if entry.error is not None:
                raise entry.error
            if entry.value is None:
                raise RuntimeError("PHYSICAL_EXPERIMENT_RESULT_MISSING")
            emit_observability_event(
                "physical-experiment.coalesced",
                experimentIdentity=experiment_id,
                strongIdentity=normalized[0],
                proofSlot=normalized[1],
                purpose=normalized[2],
                authority="PERFORMANCE_ONLY",
            )
            return copy.deepcopy(entry.value), "coalesced"

    assert producer_entry is not None
    started = time.monotonic()
    emit_observability_event(
        "physical-experiment.execution.start",
        experimentIdentity=experiment_id,
        strongIdentity=normalized[0],
        proofSlot=normalized[1],
        purpose=normalized[2],
        authority="PERFORMANCE_ONLY",
    )
    try:
        value = producer()
    except BaseException as exc:
        with _LOCK:
            producer_entry.error = exc
            producer_entry.state = "failed"
            producer_entry.completed_at = time.monotonic()
            producer_entry.condition.notify_all()
            _ENTRIES.pop(normalized, None)
        emit_observability_event(
            "physical-experiment.execution.finish",
            experimentIdentity=experiment_id,
            outcome="failed",
            durationMs=int((time.monotonic() - started) * 1000),
            errorType=type(exc).__name__,
            authority="PERFORMANCE_ONLY",
        )
        raise

    reusable = True
    if is_reusable is not None:
        try:
            reusable = bool(is_reusable(value))
        except Exception:
            reusable = False

    with _LOCK:
        producer_entry.value = copy.deepcopy(value)
        producer_entry.reusable = reusable
        producer_entry.state = "completed"
        producer_entry.completed_at = time.monotonic()
        producer_entry.condition.notify_all()
        if reusable:
            _ENTRIES.move_to_end(normalized)
            _trim_completed()
        else:
            # Current waiters hold the Entry object and still receive this
            # observation; future requests cannot reuse it.
            _ENTRIES.pop(normalized, None)

    emit_observability_event(
        "physical-experiment.execution.finish",
        experimentIdentity=experiment_id,
        outcome="completed",
        reusable=reusable,
        durationMs=int((time.monotonic() - started) * 1000),
        authority="PERFORMANCE_ONLY",
    )
    return value, "producer"
