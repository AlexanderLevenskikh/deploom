#!/usr/bin/env python3
"""Long-lived Desktop worker for DepLoom Baseline.

Protocol stdout is newline-delimited UTF-8 JSON. Generator stdout/stderr are
framed inside protocol messages. An unsafe request retires the whole worker.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from worker_runtime_state import (
    reset_worker_reuse_state,
    worker_reuse_unsafe_reason,
)


def _configure_protocol_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


_configure_protocol_stdio()
_PROTOCOL_OUT = sys.stdout
_PROTOCOL_LOCK = threading.Lock()


def _send(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _PROTOCOL_LOCK:
        _PROTOCOL_OUT.write(text + "\n")
        _PROTOCOL_OUT.flush()


class _FramedWriter:
    def __init__(self, request_id: str, stream: str) -> None:
        self.request_id = request_id
        self.stream = stream

    def write(self, value: str) -> int:
        text = str(value)
        if text:
            _send({
                "type": "stream",
                "id": self.request_id,
                "stream": self.stream,
                "data": text,
            })
        return len(text)

    def flush(self) -> None:
        return None

    @property
    def encoding(self) -> str:
        return "utf-8"


def _run_request(
    generator: Any,
    payload: dict[str, Any],
    base_env: dict[str, str],
) -> int:
    request_id = str(payload.get("id") or "")
    argv = payload.get("argv")
    cwd = payload.get("cwd")
    env = payload.get("env")
    if not request_id:
        raise ValueError("BASELINE_WORKER_REQUEST_ID_REQUIRED")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise ValueError("BASELINE_WORKER_ARGV_INVALID")
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("BASELINE_WORKER_CWD_INVALID")
    if not isinstance(env, dict):
        env = {}

    old_argv = list(sys.argv)
    old_cwd = os.getcwd()
    old_env = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(base_env)
        os.environ.update({
            str(key): str(value)
            for key, value in env.items()
            if value is not None
        })
        os.chdir(cwd)
        sys.argv = [str(Path(generator.__file__).resolve()), *argv]
        runner = getattr(generator, "run_cli", None)
        if not callable(runner):
            raise RuntimeError("BASELINE_WORKER_RUN_CLI_MISSING")
        with contextlib.redirect_stdout(_FramedWriter(request_id, "stdout")):
            with contextlib.redirect_stderr(_FramedWriter(request_id, "stderr")):
                return int(runner())
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


def main() -> int:
    base_env = dict(os.environ)
    generator = importlib.import_module("dependency_live_roadmap_generator")
    _send({"type": "ready", "pid": os.getpid(), "encoding": "utf-8"})
    for raw in sys.stdin:
        if not raw.strip():
            continue
        request_id = ""
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
            request_id = str(payload.get("id") or "")

            reset_worker_reuse_state()
            code = _run_request(generator, payload, base_env)
            unsafe_reason = worker_reuse_unsafe_reason()
            reusable = code in {0, 3} and not unsafe_reason
            _send({
                "type": "complete",
                "id": request_id,
                "code": code,
                "reusable": reusable,
                **({"retirementReason": unsafe_reason} if unsafe_reason else {}),
            })
            if not reusable:
                return code
        except BaseException as exc:
            _send({
                "type": "stream",
                "id": request_id,
                "stream": "stderr",
                "data": (
                    "BASELINE_WORKER_REQUEST_ERROR: "
                    f"{type(exc).__name__}: {exc}\n"
                ),
            })
            _send({
                "type": "complete",
                "id": request_id,
                "code": 4,
                "reusable": False,
                "retirementReason": "worker-request-exception",
                "error": traceback.format_exc()[-12000:],
            })
            return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
