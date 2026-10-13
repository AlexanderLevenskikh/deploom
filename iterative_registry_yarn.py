"""Read *registry* metadata through the selected Yarn Berry configuration.

This is planning evidence, not install/proof authority.  In particular npm's
`.npmrc` registry/auth and Yarn's `.yarnrc.yml` npmScopes need not agree.  A
Yarn Berry project should query the same registry client that installs it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from resolution_inventory import canonical_lockfile_path

_VERSION = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def modern_yarn_root(project_dir: Path) -> Optional[Path]:
    """Return the canonical Yarn Berry manager root, never a nested workspace."""
    lockfile = canonical_lockfile_path(project_dir)
    if lockfile is None or lockfile.name != "yarn.lock":
        return None
    root = lockfile.parent
    if (root / ".yarnrc.yml").is_file():
        return root
    try:
        manifest = json.loads((root / "package.json").read_text(encoding="utf-8-sig"))
        manager = manifest.get("packageManager", "") if isinstance(manifest, dict) else ""
        match = re.match(r"^yarn@(\d+)(?:\.|$)", str(manager))
        if match and int(match.group(1)) >= 2:
            return root
    except (OSError, ValueError, TypeError):
        pass
    return None


def _json_objects(output: str) -> list[dict[str, Any]]:
    """Yarn --json is NDJSON; accept one JSON object or a tagged inspect row."""
    objects: list[dict[str, Any]] = []
    text = str(output or "").strip()
    if not text:
        return objects
    try:
        full = json.loads(text)
        if isinstance(full, dict):
            objects = [full]
        elif isinstance(full, list):
            objects = [row for row in full if isinstance(row, dict)]
    except ValueError:
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                objects.append(row)
    unwrapped: list[dict[str, Any]] = []
    for row in objects:
        if row.get("type") == "inspect" and isinstance(row.get("data"), dict):
            unwrapped.append(row["data"])
        elif "type" not in row or ("dist-tags" in row or "version" in row):
            unwrapped.append(row)
    return unwrapped


def normalize_yarn_packument(output: str, package_name: str) -> Optional[dict[str, Any]]:
    """Convert Yarn registry JSON into npm-view's version-bound evidence shape.

    Do not synthesize a latest version.  It MUST come from dist-tags.latest,
    and exist in the returned published versions or a matching version field.
    """
    for raw in _json_objects(output):
        if str(raw.get("name") or package_name) != package_name:
            continue
        tags = raw.get("dist-tags")
        if not isinstance(tags, dict):
            tags = raw.get("distTags")
        if not isinstance(tags, dict):
            continue
        latest = tags.get("latest")
        if not isinstance(latest, str) or not _VERSION.fullmatch(latest):
            continue
        versions_raw = raw.get("versions")
        engines_by_version: dict[str, str] = {}
        if isinstance(versions_raw, dict):
            versions = [v for v in versions_raw if isinstance(v, str) and _VERSION.fullmatch(v)]
            for version, manifest in versions_raw.items():
                if not isinstance(manifest, dict):
                    continue
                engines = manifest.get("engines")
                engine = engines.get("node") if isinstance(engines, dict) else None
                if isinstance(engine, str):
                    engines_by_version[str(version)] = engine[:256]
        elif isinstance(versions_raw, list):
            versions = [v for v in versions_raw if isinstance(v, str) and _VERSION.fullmatch(v)]
        else:
            versions = []
        current = raw.get("version")
        if not isinstance(current, str) or current.lstrip("vV") != latest.lstrip("vV"):
            if latest not in versions:
                continue
            current = latest  # the registry's *own* versions + dist tag prove it exists
        engines = raw.get("engines")
        engine = engines.get("node") if isinstance(engines, dict) else raw.get("engines.node")
        selected = raw.get("version")
        if isinstance(selected, str) and selected.lstrip("vV") == latest.lstrip("vV") and isinstance(engine, str):
            engines_by_version[latest] = engine[:256]
        return {"version": current, "dist-tags": {"latest": latest},
                "versions": versions, "engines.node": engines_by_version.get(latest, ""),
                "enginesByVersion": engines_by_version}
    return None


def normalize_yarn_alternative(output: str, version: str) -> Optional[dict[str, str]]:
    for row in _json_objects(output):
        candidate = row.get("version")
        if candidate == version:
            engines = row.get("engines")
            node = engines.get("node") if isinstance(engines, dict) else row.get("engines.node")
            return {"version": version, "engines.node": str(node or "")[:256]}
        versions = row.get("versions")
        if isinstance(versions, dict) and isinstance(versions.get(version), dict):
            manifest = versions[version]
            engines = manifest.get("engines")
            return {"version": version, "engines.node": str(engines.get("node") or "")[:256] if isinstance(engines, dict) else ""}
    return None
