"""Transport context for planning metadata; credentials never enter disk entries."""
from __future__ import annotations
import hashlib
import json
from urllib.parse import urlsplit
import requests


def request_context(session, url: str, tool_version: str = "") -> tuple[str | None, bool]:
    # Arbitrary auth hooks may sign/mutate a request. Do not invoke them merely
    # to key a cache, or reuse metadata obtained under their previous state.
    auth = getattr(session, "auth", None)
    if (auth is not None and not isinstance(auth, tuple)) or getattr(session, "cert", None) or isinstance(getattr(session, "verify", True), str) or any(getattr(session, "hooks", {}).values()):
        return None, False
    if not isinstance(session, requests.Session) or any(type(adapter) is not requests.adapters.HTTPAdapter for adapter in session.adapters.values()):
        return None, False
    try:
        prepared = session.prepare_request(requests.Request("GET", url))
        settings = session.merge_environment_settings(url, {}, True, None, None)
        headers = {str(k).lower(): str(v) for k, v in prepared.headers.items()}
        parts = urlsplit(url)
        cookies = [(c.domain, c.path, c.name, c.value, c.secure, c.expires) for c in session.cookies]
        payload = {"url": prepared.url, "headers": headers, "cookies": cookies, "transport": settings, "redirects": session.max_redirects, "tool": tool_version}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        private_proxy = any(urlsplit(str(value)).username for value in settings.get("proxies", {}).values() if value)
    except Exception:
        # A cache-key failure must never become a registry/network failure.
        return None, False
    # Authenticated/cookie/environment-dependent requests retain per-context
    # in-memory dedup, but never persist or consume cross-process metadata.
    durable = not (headers.get("authorization") or cookies or headers.get("cookie")
                   or parts.username or parts.password or session.trust_env or session.params
                   or private_proxy or set(headers) - {key.lower() for key in requests.utils.default_headers()})
    return key, durable
