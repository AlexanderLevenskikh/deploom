#!/usr/bin/env python3
"""On-demand public migration facts for agents; no install/audit/proof authority."""
from __future__ import annotations

import argparse
import json
import re
import queue
import threading
from urllib.parse import urlsplit, quote
import requests

from planning_cache import cached_query

MAX_RESPONSE = 2 * 1024 * 1024


def _public_json(url, *, payload=None, session=None, wall_timeout=30):
    # requests read timeouts alone do not bound a slow-dripping response.
    # This worker has no filesystem/proof writes and cannot hold CLI exit open.
    result = queue.Queue(maxsize=1)
    fetch_json = _fetch_public_json
    def fetch():
        try:
            result.put(fetch_json(url, payload=payload, session=session))
        except Exception:
            result.put((None, 1, "source-unavailable", "public-source"))
    threading.Thread(target=fetch, daemon=True).start()
    try:
        return result.get(timeout=wall_timeout)
    except queue.Empty:
        return None, 1, "source-timeout", "public-source"


def _fetch_public_json(url, *, payload=None, session=None):
    # No credentials, cookies or inherited proxies are used for public sources.
    session = session or requests.Session()
    session.trust_env = False
    try:
        response = session.post(url, json=payload, timeout=(5, 20), stream=True) if payload is not None else session.get(url, timeout=(5, 20), stream=True, headers={"Accept": "application/vnd.github+json"})
        with response:
            response.raise_for_status()
            if urlsplit(response.url).hostname not in {"api.github.com", "api.osv.dev"}:
                raise ValueError("unexpected source redirect")
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE:
                    raise ValueError("public response exceeds bound")
            return json.loads(body), 1, "", "public-source"
    except (requests.RequestException, ValueError, TypeError):
        return None, 1, "source-unavailable", "public-source"
    finally:
        session.close()


def github_repository(value):
    match = re.fullmatch(r"(?:https://github\.com/|git\+https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", value)
    return match.group(1) if match else None


def collect(package, before, after, *, repository="", include_osv=False, fetch=_public_json, root=None):
    result = {"schemaVersion": 1, "package": package, "from": before, "to": after,
              "authority": "guidance-only", "unknown": [], "releases": []}
    repo = github_repository(repository)
    if repo:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
        data, *_ = cached_query(["releases-v1", repo], lambda: fetch(url), root=root, ttl=3600)
        result["source"] = f"https://github.com/{repo}/releases"
        result["coverage"] = "latest-30-releases plus bounded exact tags; incomplete migration history"
        if isinstance(data, list):
            # Package/major matching is a hint, never an assertion that all
            # intervening releases have been collected (monorepo tags vary).
            from semantic_version import Version
            try:
                lower, upper = Version(before.lstrip("v")), Version(after.lstrip("v"))
            except ValueError:
                lower = upper = None
            rows = list(data)
            priority_tags = []
            if lower is not None and upper is not None:
                if upper.major > lower.major:
                    priority_tags.append(f"v{upper.major}.0.0")
                priority_tags.append("v" + after.lstrip("v"))
                # Common monorepo tagging uses the complete package name.
                if not any(isinstance(row, dict) and row.get("tag_name") in priority_tags for row in rows):
                    priority_tags.append(package + "@" + after.lstrip("v"))
                priority_tags = list(dict.fromkeys(priority_tags))[:3]
                for tag in priority_tags:
                    if tag.startswith(package + "@") and any(isinstance(row, dict) and row.get("tag_name") == "v" + after.lstrip("v") for row in rows):
                        continue
                    if any(isinstance(row, dict) and row.get("tag_name") == tag for row in rows):
                        continue
                    exact_url = f"https://api.github.com/repos/{repo}/releases/tags/{quote(tag, safe='')}"
                    exact, *_ = cached_query(["release-tag-v1", repo, tag], lambda url=exact_url: fetch(url), root=root, ttl=3600)
                    if isinstance(exact, dict) and exact.get("tag_name") == tag:
                        rows.insert(0, exact)
                rows.sort(key=lambda row: priority_tags.index(row.get("tag_name")) if isinstance(row, dict) and row.get("tag_name") in priority_tags else len(priority_tags))
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tag = str(row.get("tag_name", ""))
                matched = re.fullmatch(r"(?:v|" + re.escape(package) + r"@)?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", tag)
                try:
                    relevant = bool(matched and lower is not None and lower < Version(matched.group(1)) <= upper)
                except ValueError:
                    relevant = False
                if relevant:
                    result["releases"].append({"tag": tag[:200], "url": str(row.get("html_url", ""))[:500],
                                               "notes": str(row.get("body") or "")[:2500],
                                               "truncated": len(str(row.get("body") or "")) > 2500})
                if len(result["releases"]) >= 3:
                    break
            if not result["releases"]:
                result["unknown"].append("no matching release in bounded source window")
        else:
            result["unknown"].append("release source unavailable")
    elif repository:
        result["unknown"].append("unsupported repository; provide official guide separately")
    if include_osv:
        payload = {"queries": [{"package": {"ecosystem": "npm", "name": package}, "version": after}]}
        data, *_ = cached_query(["osv-query-v1", package, after],
                               lambda: fetch("https://api.osv.dev/v1/querybatch", payload=payload), root=root, ttl=600)
        rows = data.get("results") if isinstance(data, dict) else None
        valid = isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
        if valid:
            advisories = rows[0].get("vulns", [])
            valid = isinstance(advisories, list) and all(isinstance(row, dict) and isinstance(row.get("id"), str)
                                                       and bool(row["id"]) and isinstance(row.get("modified"), str)
                                                       for row in advisories)
        if valid:
            result["osv"] = {"advisories": rows[0].get("vulns", []),
                             "coverage": "public OSV lookup only; not the configured audit",
                             "complete": not bool(rows[0].get("next_page_token"))}
            if not result["osv"]["complete"]:
                result["unknown"].append("OSV pagination incomplete")
        else:
            result["unknown"].append("OSV evidence unavailable")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--from-version", required=True)
    parser.add_argument("--to-version", required=True)
    parser.add_argument("--repository", default="", help="Official GitHub repository URL; public content only")
    parser.add_argument("--include-osv", action="store_true", help="Send this explicitly selected name/version to public OSV; never replaces independent audit")
    args = parser.parse_args()
    from cli_io import configure_utf8_stdio
    configure_utf8_stdio()
    result = collect(args.package, args.from_version, args.to_version, repository=args.repository, include_osv=args.include_osv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
