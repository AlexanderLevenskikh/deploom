"""Per-discovery npm CLI metadata batching; never an install/proof cache.

npm itself applies registry/scoped auth/proxy/.npmrc/runtime environment. Query
latest+history once, then engines for the bounded alternative set once. Avoid
requesting every historical manifest of packages with thousands of versions.
"""
from __future__ import annotations
import re
import json
from typing import Any, Callable


def registry_json(output: str):
    """Discard npm diagnostic lines; only JSON is registry evidence."""
    text = "\n".join(line for line in output.splitlines()
                     if not re.match(r"^npm (?:warn|notice)(?:\s|$)", line, re.I))
    return json.loads(text)


_EXACT = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def _engine(row: dict) -> str:
    value = row.get('engines.node')
    if value is None and isinstance(row.get('engines'), dict):
        value = row['engines'].get('node')
    return value.strip()[:256] if isinstance(value, str) else ''


class NpmDiscoveryMetadata:
    def __init__(self, name: str, query: Callable[[list[str]], Any]):
        self.name = name
        self.query = query
        self.queries = 0
        self.source = "npm-cli"
        self.failure_class = ""
        self._loaded = False
        self._data: dict = {}
        self._engines: dict[str, str] = {}
        self._alternatives_loaded = False

    def _ask(self, args: list[str]) -> Any:
        result = self.query(args)
        data, executed = result[:2]
        self.queries += int(executed)
        if len(result) >= 3 and result[2]:
            self.failure_class = str(result[2])[:64]
        if len(result) >= 4 and result[3]:
            self.source = str(result[3])[:64]
        return data

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        data = self._ask(['view', self.name, 'version', 'dist-tags', 'versions', 'engines.node', 'time', '--tag=latest', '--json'])
        if not isinstance(data, dict):
            return
        tags = data.get('dist-tags')
        latest = tags.get('latest') if isinstance(tags, dict) else None
        # Bind the selected manifest's engine fact to its exact version.
        version = data.get('version')
        normalize = lambda value: value[1:] if value[:1] in ('v', 'V') else value
        if not isinstance(latest, str) or not isinstance(version, str) or normalize(version) != normalize(latest):
            return
        self._data = data
        self._engines[latest] = _engine(data)
        self._engines[version] = _engine(data)
        by_version = data.get('enginesByVersion')
        if isinstance(by_version, dict):
            for item, node_range in by_version.items():
                if isinstance(item, str) and isinstance(node_range, str) and _EXACT.fullmatch(item):
                    self._engines[item] = node_range[:256]

    def latest(self) -> str:
        self._load()
        return str(self._data.get('dist-tags', {}).get('latest', '')).strip()[:128]

    def versions(self, top: int, sort_key: Callable) -> list[str]:
        self._load()
        raw = self._data.get('versions', [])
        versions = sorted([v for v in raw if isinstance(v, str) and _EXACT.fullmatch(v)], key=sort_key, reverse=True)[:top] if isinstance(raw, list) else []
        if not self._alternatives_loaded:
            self._alternatives_loaded = True
            pending = [v for v in versions if v not in self._engines]
            if pending:
                # Exact OR alternatives retain prerelease semantics; npm's @*
                # selects its default tag and is not an all-version query.
                data = self._ask(['view', self.name + '@' + ' || '.join(pending), 'version', 'engines.node', '--json'])
                rows = data if isinstance(data, list) else [data]
                for row in rows:
                    if isinstance(row, dict) and row.get('version') in pending:
                        self._engines[row['version']] = _engine(row)
        return versions

    def engines(self, version: str) -> str:
        self._load()
        return self._engines.get(version, '')
