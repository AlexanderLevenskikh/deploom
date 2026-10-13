"""Durable, short-lived planning progress; never verification authority."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from resolution_inventory import canonical_lockfile_path

TTL_SECONDS = 24 * 3600
SETTLED = {"discovered", "discovered-compatible", "up-to-date", "no-newer-compatible"}


def discovery_identity(project: Path, context: dict) -> str:
    """Bind lock/manifest/registry/auth/runtime/policy; persist only a digest."""
    project = project.resolve()
    files = set()
    lock = canonical_lockfile_path(project)
    if lock is not None:
        files.add(lock)
    # Manager/workspace manifests and inherited npm/Yarn configuration matter.
    for parent in (project, *project.parents, Path.home()):
        for name in ("package.json", ".npmrc", ".yarnrc.yml", ".yarnrc"):
            files.add(parent / name)
    for name in ("iterative_migration.py", "iterative_registry_metadata.py", "iterative_registry_yarn.py", "iterative_discovery_checkpoint.py"):
        files.add(Path(__file__).parent / name)
    for name in ("NPM_CONFIG_USERCONFIG", "NPM_CONFIG_GLOBALCONFIG"):
        value = next((v for k, v in os.environ.items() if k.upper() == name), "")
        if value:
            files.add(Path(value).expanduser())
    hashes = {}
    for path in sorted(files, key=str):
        # Unexpected access failures abort rather than silently reusing evidence.
        hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    payload = {"schema": 1, "project": str(project), "context": context,
               "files": hashes, "environment": dict(os.environ)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class DiscoveryCheckpoint:
    def __init__(self, path: Path, identity: str):
        self.path = path
        self.identity = identity
        self.entries: dict[str, Any] = {}
        try:
            saved = json.loads(path.read_text(encoding="utf-8-sig"))
            if saved.get("schema") == 1 and saved.get("identity") == identity and isinstance(saved.get("entries"), dict):
                self.entries = saved["entries"]
        except (OSError, ValueError, AttributeError):
            pass

    def lookup(self, name: str, declared: str):
        item = self.entries.get(name)
        if not isinstance(item, dict):
            return None
        row = item.get("evidence")
        stamp = item.get("savedAt")
        if not isinstance(stamp, (int, float)) or not 0 <= time.time() - stamp < TTL_SECONDS:
            return None
        if not isinstance(row, dict) or row.get("package") != name or row.get("declared") != declared or row.get("status") not in SETTLED:
            return None
        metadata = row.get("registryMetadata") or {}
        if not isinstance(metadata, dict) or metadata.get("errorClass"):
            return None
        target = item.get("target")
        if row["status"] in {"discovered", "discovered-compatible"}:
            selected = row.get("target") if row["status"] == "discovered-compatible" else row.get("latest")
            if not isinstance(target, str) or not target or target != selected:
                return None
        elif target is not None:
            return None
        evidence = dict(row)
        evidence["discoveryCheckpoint"] = {"savedAt": stamp, "scope": "planning-only"}
        return target, evidence

    def record(self, name: str, target: str | None, row: dict) -> None:
        # Preserve diagnostics independently of Baseline. lookup never reuses
        # UNKNOWN, failed requests or incomplete metadata as settled planning.
        previous = self.lookup(name, str(row.get("declared") or ""))
        comparable = dict(previous[1]) if previous is not None else {}
        comparable.pop("discoveryCheckpoint", None)
        incoming = dict(row)
        incoming.pop("discoveryCheckpoint", None)
        if previous is not None and previous[0] == target and comparable == incoming:
            return  # Replaying progress must not extend its freshness deadline.
        self.entries[name] = {"savedAt": time.time(), "target": target, "evidence": incoming}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"schema": 1, "identity": self.identity, "scope": "planning-only",
                                   "entries": self.entries}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
