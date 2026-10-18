#!/usr/bin/env python3
"""Durable per-registry packument (npm metadata) cache for planning only.

``fetch_npm_metadata`` in ``dependency_live_roadmap_generator.py`` already
deduplicates metadata requests inside one CLI invocation through the in-memory
``npm_cache``. This module makes the same dedup survive process boundaries:
retries, resumes and re-plans of the same workspace re-issue the identical
latest/version/engines/history registry queries, which this cache merges into
one lookup per (registry, package) across those invocations.

Contract and safety boundaries:

* Planning-only. The cache is read/written exclusively by the metadata path
  that feeds Draft/planning. Physical install, verification and the
  vulnerability audit inventory are authoritative and never consult this
  cache: ``manual_dependency_audit.py`` and the Modern Yarn audit bridge always
  query fresh registry/lockfile evidence.
* Registry isolation. The registry URL is part of the durable key, so one
  configured registry can never read another registry's packument. A separate
  request-context fingerprint isolates transport/config views; authenticated
  clients do not use durable entries.
* Freshness is bounded by an explicit TTL; 0/None disables the cache. A stale
  entry is returned as a miss, never as data.
* Honest UNKNOWN. A transient infrastructure failure or a 404 is never
  persisted: an unavailable package is not invented from stale data and a
  transient failure is not cached as absence.
* Deadline/cancellation safe: the cache is local atomic file I/O only, with a
  bounded retry on Windows transient permission errors; it never blocks the
  run deadline and never issues a network request of its own.
* Provenance: every entry records the registry, package, fetch time and the
  tool build id that wrote it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from substrate_identity import tool_build_id

CACHE_SCHEMA_VERSION = 1
CACHE_ENTRY_SCHEMA = "planning-packument-v1"
# When a cache path is configured but no TTL is given, this bounds freshness.
DEFAULT_METADATA_CACHE_TTL_SECONDS = 600.0
_CACHE_WRITE_THREAD_LOCK = threading.RLock()
_WINDOWS_PERMISSION_GRACE_SECONDS = 0.25
_WINDOWS_PERMISSION_MAX_RETRIES = 4


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _registry_subdir(cache_dir: Path, registry: str) -> Path:
    # Short readable registry folder; the full registry URL is still stored in
    # the entry and checked on load, so a truncated hash cannot collide.
    return cache_dir / _sha256(registry)[:16]


def _entry_path(cache_dir: Path, registry: str, name: str, context: str = "") -> Path:
    # A package name may be scoped ("@scope/name") and contain '/', '@', '.'.
    # It is stored by hash and validated against the JSON entry on load.
    return _registry_subdir(cache_dir, registry) / (_sha256(context + "\0" + name if context else name) + ".json")


def _entry_is_fresh(entry: Dict[str, Any], registry: str, name: str, ttl_seconds: float, now: float, context: str = "") -> bool:
    if entry.get("schema") != CACHE_SCHEMA_VERSION:
        return False
    if entry.get("entrySchema") != CACHE_ENTRY_SCHEMA:
        return False
    if entry.get("registry") != registry:
        return False
    if entry.get("name") != name:
        return False
    if entry.get("context", "") != context:
        return False
    data = entry.get("data")
    if not isinstance(data, dict):
        return False
    fetched_at_unix = entry.get("fetchedAtUnix")
    if isinstance(fetched_at_unix, bool) or not isinstance(fetched_at_unix, (int, float)):
        return False
    age = now - float(fetched_at_unix)
    return math.isfinite(now) and math.isfinite(float(fetched_at_unix)) and math.isfinite(ttl_seconds) and 0 <= age <= ttl_seconds


def load_packument(
    cache_dir: Optional[Path],
    registry: str,
    name: str,
    ttl_seconds: float,
    *,
    now: Optional[float] = None,
    context: str = "",
) -> Optional[Dict[str, Any]]:
    """Return a fresh cached packument for (registry, name), or None.

    None means the cache is disabled, the entry is missing, stale, invalid or
    unreadable -- in every case the caller proceeds to the live registry fetch.
    """
    if cache_dir is None or ttl_seconds <= 0:
        return None
    try:
        raw = _entry_path(cache_dir, registry, name, context).read_text(encoding="utf-8-sig")
        entry = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(entry, dict):
        return None
    current = time.time() if now is None else float(now)
    if not _entry_is_fresh(entry, registry, name, ttl_seconds, current, context):
        return None
    return entry["data"]


def load_revalidation_entry(cache_dir, registry, name, *, context=""):
    """Stale bytes are only a conditional request subject, never a fresh hit."""
    if cache_dir is None:
        return None
    try:
        path = _entry_path(cache_dir, registry, name, context)
        if path.stat().st_size > 16 * 1024 * 1024:
            return None
        entry = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(entry, dict) or not _entry_is_fresh(entry, registry, name, 30 * 86400, time.time(), context):
            return None
        validators = entry.get("validators")
        if not isinstance(validators, dict):
            return None
        if any(not isinstance(v, str) or len(v) > 1024 or "\r" in v or "\n" in v for v in validators.values()):
            return None
        return entry if validators.get("ETag") or validators.get("Last-Modified") else None
    except (OSError, ValueError, TypeError):
        return None


def store_packument(
    cache_dir: Optional[Path],
    registry: str,
    name: str,
    data: Dict[str, Any],
    tool_version: str = "",
    *,
    now: Optional[float] = None,
    context: str = "",
    validators: Optional[Dict[str, str]] = None,
) -> None:
    """Best-effort atomic persistence of one packument entry. Never raises.

    Only successful metadata dictionaries are stored; failed/incomplete fetches
    (404, transient infra errors) are deliberately not persisted so a later run
    cannot mistake them for registry fact.
    """
    if cache_dir is None or not isinstance(data, dict):
        return
    target = _entry_path(cache_dir, registry, name, context)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A cache directory that cannot be created (permissions, read-only
        # filesystem, a path component that is a file) is not a registry
        # failure: skip persistence instead of failing the fetch.
        return
    current = time.time() if now is None else float(now)
    if not math.isfinite(current):
        return
    entry = {
        "context": context,
        "schema": CACHE_SCHEMA_VERSION,
        "entrySchema": CACHE_ENTRY_SCHEMA,
        "registry": registry,
        "name": name,
        "fetchedAt": dt.datetime.fromtimestamp(current, dt.timezone.utc).isoformat(),
        "fetchedAtUnix": current,
        "toolVersion": tool_version or tool_build_id(),
        "data": data,
        "validators": {key: str(value)[:1024] for key, value in (validators or {}).items()
                       if key in {"ETag", "Last-Modified"} and "\r" not in str(value) and "\n" not in str(value)},
    }
    payload = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with _CACHE_WRITE_THREAD_LOCK:
        for attempt in range(_WINDOWS_PERMISSION_MAX_RETRIES):
            fd = None
            tmp_name = ""
            try:
                fd, tmp_name = _temporary_path(target)
                with os.fdopen(fd, "wb") as stream:
                    fd = None
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_name, target)
                return
            except OSError:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if tmp_name:
                    try:
                        os.remove(tmp_name)
                    except OSError:
                        pass
                if attempt < _WINDOWS_PERMISSION_MAX_RETRIES - 1:
                    # Transient Windows sharing/permission errors (AV scanner,
                    # indexer) resolve quickly; retry before giving up.
                    time.sleep(_WINDOWS_PERMISSION_GRACE_SECONDS)
            except Exception:
                # Never let a cache write abort a run.
                try:
                    if tmp_name:
                        os.remove(tmp_name)
                except OSError:
                    pass
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                return


def _temporary_path(target: Path):
    import tempfile

    return tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
