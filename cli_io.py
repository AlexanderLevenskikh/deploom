#!/usr/bin/env python3
"""Stable UTF-8 console output for CLI entrypoints.

Windows PowerShell can launch Python with a legacy code page such as cp1251,
which cannot encode report symbols like ≤, →, or —.  Reconfigure the current
process streams before any user-facing output so direct console use and
redirected/captured output are deterministic UTF-8.
"""
from __future__ import annotations

import sys
from typing import TextIO


def _reconfigure_utf8(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError, ValueError):
        # Some embedded/test streams do not support reconfiguration.  Their
        # owner is responsible for the encoding, so leave them untouched.
        return


def configure_utf8_stdio() -> None:
    """Make stdout/stderr deterministic and Unicode-safe for this process."""
    _reconfigure_utf8(sys.stdout)
    _reconfigure_utf8(sys.stderr)
