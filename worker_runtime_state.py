#!/usr/bin/env python3
from __future__ import annotations

import threading

_LOCK = threading.RLock()
_UNSAFE_REASON = ""


def reset_worker_reuse_state() -> None:
    global _UNSAFE_REASON
    with _LOCK:
        _UNSAFE_REASON = ""


def mark_worker_reuse_unsafe(reason: str) -> None:
    global _UNSAFE_REASON
    value = str(reason or "unknown-request-state").strip()
    with _LOCK:
        if not _UNSAFE_REASON:
            _UNSAFE_REASON = value


def worker_reuse_unsafe_reason() -> str:
    with _LOCK:
        return _UNSAFE_REASON
