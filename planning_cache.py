"""Bounded, crash-released reuse of planning data. Never a verification store."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import tempfile
import time

from cross_process_io import _try_lock
from verification_observability import emit_observability_event


def cache_root():
    value = os.environ.get("DEPLOOM_PLANNING_CACHE_DIR")
    if value == "0":
        return None
    return Path(value) if value else Path.home() / ".cache" / "deploom" / "planning-v1"


def key_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextlib.contextmanager
def planning_lease(key, *, check=lambda: None, timeout=30):
    """Same-user per-key mutex. Death releases ownership; every wait checks budget."""
    release = None
    started = time.monotonic()
    try:
        while release is None:
            check()
            try:
                release = _try_lock("planning-" + key_digest(key), 0)
            except OSError:
                break  # An unavailable cache must not prevent live work.
            if release is None:
                if time.monotonic() - started >= timeout:
                    break
                time.sleep(0.05)
        emit_observability_event("planning.cache.lease", acquired=release is not None,
                                 waitMs=int((time.monotonic() - started) * 1000))
        yield release is not None
    finally:
        if release is not None:
            try:
                release()
            except OSError:
                pass  # Cache cleanup cannot replace the live query outcome.


def read_entry(root, key, ttl, *, stale=False):
    if root is None or ttl <= 0:
        return None
    try:
        path = root / (key_digest(key) + ".json")
        if path.stat().st_size > 16 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        age = time.time() - value["fetched"]
        if not isinstance(value.get("data"), (dict, list)) or value.get("key") != key_digest(key) or not 0 <= age <= (30 * 86400 if stale else ttl):
            return None
        return value
    except (OSError, ValueError, TypeError, KeyError):
        return None


def write_entry(root, key, data, **metadata):
    if root is None:
        return
    temporary = None
    try:
        payload = json.dumps(dict(metadata, key=key_digest(key), fetched=time.time(), data=data), ensure_ascii=False).encode()
        if len(payload) > 16 * 1024 * 1024:
            return
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=root, suffix=".tmp")
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / (key_digest(key) + ".json"))
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def cached_query(key, fetch, *, root=None, ttl=600, check=lambda: None):
    """Only complete successful query tuples persist; failures always remain live."""
    check()  # Expiration also applies to a warm hit and disabled-cache fallback.
    if ttl <= 0:
        return fetch()
    if root is None:
        root = cache_root()
    if root is None:
        return fetch()
    value = read_entry(root, key, ttl)
    if value is not None:
        emit_observability_event("planning.cache.hit", layer="query")
        return value["data"], 0, "", value.get("source", "planning-cache")
    with planning_lease([str(root.resolve()), key], check=check):
        check()
        value = read_entry(root, key, ttl)
        if value is not None:
            emit_observability_event("planning.cache.hit", layer="query", follower=True)
            return value["data"], 0, "", value.get("source", "planning-cache")
        result = fetch()
        if isinstance(result, (list, tuple)) and len(result) >= 4 and not result[2] and isinstance(result[0], (dict, list)):
            write_entry(root, key, result[0], source=result[3])
        emit_observability_event("planning.cache.miss", layer="query")
        return result


def public_cli_context(project, runtime_env):
    """Conservative opt-in: delegate any custom/auth/configured transport to PM.

    No home/user/global/project config is interpreted or copied into this store.
    Custom environments and Yarn configuration keep their native PM cache only.
    """
    env = {**os.environ, **(runtime_env or {})}
    harmless = {"npm_config_cache", "npm_config_update_notifier", "npm_config_fund", "npm_config_audit"}
    if any((k.lower().startswith(("npm_config_", "yarn_")) and k.lower() not in harmless)
           or k.upper() in {"NODE_AUTH_TOKEN", "NPM_TOKEN", "NODE_OPTIONS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"} and v
           for k, v in env.items()):
        return None
    executable = shutil.which("npm", path=env.get("PATH", env.get("Path", "")))
    if not executable:
        return None
    binary = Path(executable).resolve()
    paths = [Path.home() / ".npmrc", Path.home() / ".yarnrc.yml", Path.home() / ".yarnrc",
             binary.parent / "etc" / "npmrc", binary.parent.parent / "etc" / "npmrc",
             Path(env.get("APPDATA", str(Path.home()))) / "npm" / "etc" / "npmrc",
             Path("/etc/npmrc"), Path("/usr/local/etc/npmrc")]
    for parent in (project, *project.parents):
        paths.extend(parent / name for name in (".npmrc", ".yarnrc", ".yarnrc.yml"))
    # Resolve the built-in prefix without executing package configuration.
    builtin_files = [binary.parent / "node_modules" / "npm" / "npmrc", binary.parent.parent / "npmrc"]
    builtin_data = []
    try:
        for candidate in builtin_files:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8-sig")
            builtin_data.append(text)
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if not line.startswith("prefix="):
                    return None
                prefix = re.sub(r"\$\{([^}]+)\}", lambda match: env.get(match[1], match[0]), line[7:].strip())
                if "${" in prefix or not Path(prefix).is_absolute():
                    return None
                paths.append(Path(prefix) / "etc" / "npmrc")
    except (OSError, UnicodeError):
        return None
    # npm's global prefix is installation-dependent. With no explicit global
    # config proof, cache only within this project to avoid cross-project views.
    try:
        configs = [(str(path), key_digest(path.read_text(encoding="utf-8-sig"))) for path in paths if path.is_file()]
    except (OSError, UnicodeError):
        return None
    if configs:
        return None
    try:
        builtin = key_digest(builtin_data)
        stat = binary.stat()
    except (OSError, UnicodeError):
        return None
    return {"root": str(project.resolve()), "environment": key_digest(env), "builtin": builtin,
            "binary": [str(binary), stat.st_size, stat.st_mtime_ns], "schema": 1}
