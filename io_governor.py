#!/usr/bin/env python3
"""Process-wide, proof-neutral coordination for heavy filesystem work."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from verification_observability import emit_observability_event, new_observability_id


def _configured_slots(kind: str) -> int:
    defaults = {"copy": 2 if os.name == "nt" else 2, "hash": 2, "pm": 1}
    raw = str(os.environ.get(f"DEPLOOM_IO_{kind.upper()}_SLOTS") or "").strip()
    if raw:
        try:
            return max(1, min(16, int(raw)))
        except ValueError:
            pass
    return defaults[kind]


@dataclass
class _Pool:
    slots: int
    semaphore: threading.BoundedSemaphore
    lock: threading.Lock
    active: int = 0


_POOLS_LOCK = threading.Lock()
_POOLS: dict[tuple[str, int], _Pool] = {}


def _pool(kind: str) -> _Pool:
    slots = _configured_slots(kind)
    key = (kind, slots)
    with _POOLS_LOCK:
        value = _POOLS.get(key)
        if value is None:
            value = _Pool(slots, threading.BoundedSemaphore(slots), threading.Lock())
            _POOLS[key] = value
        return value


@contextmanager
def io_slot(kind: str, *, label: str = "") -> Iterator[dict[str, int]]:
    """Acquire one bounded I/O slot. Concurrency never affects proof identity."""
    if kind not in {"copy", "hash", "pm"}:
        raise ValueError(f"IO_GOVERNOR_KIND_INVALID: {kind}")
    pool = _pool(kind)
    operation_id = new_observability_id("io-governor")
    started = time.monotonic()
    pool.semaphore.acquire()
    wait_ms = max(0, int((time.monotonic() - started) * 1000))
    with pool.lock:
        pool.active += 1
        active = pool.active
    emit_observability_event(
        "filesystem.io-governor.acquired",
        operationId=operation_id,
        ioKind=kind,
        label=label,
        ioGovernorSlots=pool.slots,
        ioGovernorActiveSlots=active,
        ioGovernorWaitMs=wait_ms,
    )
    try:
        yield {"slots": pool.slots, "active": active, "waitMs": wait_ms}
    finally:
        with pool.lock:
            pool.active -= 1
            remaining = pool.active
        pool.semaphore.release()
        emit_observability_event(
            "filesystem.io-governor.released",
            operationId=operation_id,
            ioKind=kind,
            label=label,
            ioGovernorSlots=pool.slots,
            ioGovernorActiveSlots=remaining,
            ioGovernorWaitMs=wait_ms,
        )


def reset_io_governor_for_tests() -> None:
    with _POOLS_LOCK:
        _POOLS.clear()
