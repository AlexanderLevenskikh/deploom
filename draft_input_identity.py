"""Relocatable Draft input identity, separate from legacy Desktop byte hashes.

This binds local dependency/configuration inputs, not registry/OSV snapshots,
solver versions, source code or a physical migration proof. Workspace manifests
can be bound as inputs without being evaluated together by the planner.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from project_topology import ProjectTopologyError, resolve_project_topology
from package_manager_profile import PackageManagerProfileError

SEMANTIC_INPUT_SCHEMA = "draft-semantic-input-v1"
_CONTROL_FILES = (".npmrc", ".yarnrc", ".yarnrc.yml", "pnpm-workspace.yaml", "package-manager.json", ".nvmrc", ".node-version")
# Operational output/cache locations are not dependency-planning semantics.
# Keep settings filtering confined to settings documents, never manifests.
_OPERATIONAL_SETTINGS = frozenset({
    "out", "json-out", "jsonOut", "html-out", "htmlOut", "artifacts-dir", "artifactsDir",
    "events-log", "eventsLog", "runs-dir", "runsDir", "index-file", "indexFile",
    "history-dir", "historyDir", "knowledge-log", "knowledgeLog", "dashboard-state", "dashboardState",
    "groups-config", "groupsConfig", "registry-metadata-cache-dir", "registryMetadataCacheDir",
    "constraintCachePath", "runId", "generatedAt", "updatedAt",
})


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _settings_value(value: Any, roots: Sequence[tuple[Path, str]]) -> Any:
    if isinstance(value, dict):
        return {str(k): _settings_value(v, roots) for k, v in value.items() if k not in _OPERATIONAL_SETTINGS}
    if isinstance(value, list):
        return [_settings_value(v, roots) for v in value]
    if isinstance(value, str):
        candidate = value.replace("\\", "/")
        # Windows can represent one folder with an 8.3 short name and a
        # canonical long path. Resolve settings paths like workspace roots
        # before hashing. URLs and relative values remain unchanged.
        try:
            path = Path(value)
            if path.is_absolute():
                candidate = path.resolve().as_posix()
        except (OSError, ValueError):
            pass
        for root, label in roots:
            prefix = root.as_posix().rstrip("/")
            folded = candidate.casefold()
            if folded == prefix.casefold() or folded.startswith((prefix + "/").casefold()):
                return label + candidate[len(prefix):]
    return value


def capture_draft_semantic_inputs(
    project_path: Path,
    *,
    topology: Mapping[str, Any] | None = None,
    settings_sources: Sequence[Path] = (),
    dashboard_state_path: Path | None = None,
    groups_config_path: Path | None = None,
    effective_overrides: Mapping[str, Any] | None = None,
    effective_planner_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture before network/solving. Paths are evidence, never hash keys.

    Reuse the planner's resolved topology when supplied, including the canonical
    parent lock. A failed topology resolution remains UNKNOWN, not a guessed
    standalone project. Missing/unreadable required files are represented.
    """
    selected = Path(project_path).resolve()
    topology_error = ""
    if not topology:
        try:
            topology = resolve_project_topology(selected, require_supported=False).as_dict()
        except (ProjectTopologyError, PackageManagerProfileError, OSError, ValueError) as exc:
            topology = {}
            topology_error = str(getattr(exc, "code", "TOPOLOGY_UNAVAILABLE"))
    topo = dict(topology or {})
    manager_root = Path(str(topo.get("packageManagerRoot") or selected)).resolve()
    package_root = Path(str(topo.get("packageRoot") or selected)).resolve()
    source_root = Path(str(topo.get("sourceRoot") or manager_root)).resolve()
    roots = sorted(((manager_root, "$WORKSPACE"), (source_root, "$SOURCE")), key=lambda p: -len(str(p[0])))
    records: dict[str, dict[str, Any]] = {}

    def relative(path: Path) -> str:
        try:
            return path.resolve().relative_to(manager_root).as_posix()
        except ValueError:
            return "external/" + path.name

    def add_file(path: Path, name: str, *, required: bool = False, settings: bool = False, dashboard: bool = False) -> None:
        path = path.resolve()
        record: dict[str, Any] = {"name": name, "path": str(path), "required": required}
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            record.update(status="MISSING", contentHash=None, semanticHash=None)
        except OSError:
            record.update(status="UNREADABLE", contentHash=None, semanticHash=None)
        else:
            content_hash = hashlib.sha256(raw).hexdigest()
            record.update(status="PRESENT", contentHash=content_hash, semanticHash=content_hash)
            if settings or dashboard:
                try:
                    value = json.loads(raw.decode("utf-8-sig"))
                    if dashboard:
                        value = {"packageOverrides": value.get("packageOverrides") or value.get("package_overrides") or {}}
                    record["semanticHash"] = _digest(_settings_value(value, roots))
                except (UnicodeError, ValueError, AttributeError):
                    record["status"] = "INVALID_JSON"
        records[name] = record

    # Keep logical identities separate from actual paths. Reconstructing an
    # external path from its display name would hash the wrong file.
    manifest_paths: dict[str, Path] = {}
    external_manifests = False
    paths = [manager_root / "package.json", package_root / "package.json"]
    for item in topo.get("workspaceMemberManifests") or []:
        path = Path(str(item))
        paths.append(path if path.is_absolute() else manager_root / path)
    for path in paths:
        path = path.resolve()
        if path in manifest_paths.values():
            continue
        name = relative(path)
        if name.startswith("external/"):
            external_manifests = True
            name = f"external/{len(manifest_paths)}/{path.name}"
        manifest_paths[name] = path
    manifest_names = set(manifest_paths)
    for name, path in sorted(manifest_paths.items()):
        add_file(path, name, required=True)
    canonical_lock = str(topo.get("canonicalLockfile") or "")
    if canonical_lock:
        lock = Path(canonical_lock)
        if not lock.is_absolute():
            lock = manager_root / lock
        add_file(lock, relative(lock), required=True)
    for root in {manager_root, package_root}:
        for filename in _CONTROL_FILES:
            path = root / filename
            add_file(path, relative(path))
    for index, path in enumerate(settings_sources):
        add_file(Path(path), f"settings/{index}", required=True, settings=True)
    if groups_config_path is not None:
        add_file(Path(groups_config_path), "groups-config", required=True)
    # The effective overrides are what the planner consumed. Hash neither an
    # invocation-specific dashboard path nor unrelated UI timestamps/state.
    if effective_overrides is not None:
        value_hash = _digest(_settings_value(dict(effective_overrides), roots))
        records["effective-overrides"] = {"name": "effective-overrides", "status": "PRESENT", "semanticHash": value_hash, "required": True}
    elif dashboard_state_path is not None:
        add_file(Path(dashboard_state_path), "dashboard-overrides", dashboard=True)
    if effective_planner_config is not None:
        records["effective-planner-config"] = {"name": "effective-planner-config", "status": "PRESENT", "semanticHash": _digest(_settings_value(dict(effective_planner_config), roots)), "required": True}
    evaluated = [name for name, path in manifest_paths.items() if path == (package_root / "package.json").resolve()]
    unevaluated = sorted(manifest_names - set(evaluated))
    missing = sorted(name for name, r in records.items()
                     if (r.get("required") and r["status"] != "PRESENT")
                     or r["status"] in {"UNREADABLE", "INVALID_JSON"})
    if external_manifests and not topology_error:
        topology_error = "EXTERNAL_MANIFEST_OUTSIDE_WORKSPACE"
    bound = bool(topo and canonical_lock) and not missing and not external_manifests
    coverage = {
        "scope": "manifest-inventory",
        "status": "UNKNOWN" if not bound else ("PARTIAL" if unevaluated else "COMPLETE"),
        "discoveredManifests": sorted(manifest_names),
        "evaluatedManifests": evaluated,
        "unevaluatedManifests": unevaluated,
        "jointSolver": False,
        "dependencyGraph": "SELECTED_MANIFESTS_ONLY",
        "missingOrUnreadableInputs": missing,
        "topologyError": topology_error,
    }
    semantic_topology = {
        "selectedManifest": evaluated[0],
        "canonicalLockfile": relative(Path(canonical_lock) if Path(canonical_lock).is_absolute() else manager_root / canonical_lock) if canonical_lock else None,
        "workspacePatterns": sorted(str(p) for p in topo.get("workspacePatterns") or []),
        "discoveredManifests": sorted(manifest_names),
        "topologyBound": bound,
    }
    identity = {
        "schema": SEMANTIC_INPUT_SCHEMA,
        "topology": semantic_topology,
        "inputs": [{"name": r["name"], "status": r["status"], "hash": r.get("semanticHash")} for r in sorted(records.values(), key=lambda r: r["name"])],
    }
    return {
        "schema": SEMANTIC_INPUT_SCHEMA,
        "semanticInputHash": _digest(identity),
        "topology": semantic_topology,
        "files": sorted(records.values(), key=lambda r: r["name"]),
        "inputCoverage": coverage,
        "capturePhase": "LOCAL_INPUTS",
    }


def aggregate_semantic_inputs(by_project: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    values = list(by_project.values())
    hashes = [{"scope": value.get("topology", {}).get("selectedManifest"), "hash": value.get("semanticInputHash")} for value in values]
    hashes.sort(key=lambda item: (str(item["scope"]), str(item["hash"])))
    statuses = [value.get("inputCoverage", {}).get("status", "UNKNOWN") for value in values]
    status = "UNKNOWN" if not statuses or "UNKNOWN" in statuses else ("PARTIAL" if "PARTIAL" in statuses else "COMPLETE")
    return {
        "semanticInputHash": _digest({"schema": SEMANTIC_INPUT_SCHEMA, "projects": hashes}),
        "semanticInputsByProject": dict(by_project),
        "inputCoverage": {
            "scope": "manifest-inventory",
            "status": status,
            "jointSolver": False,
            "dependencyGraph": "SELECTED_MANIFESTS_ONLY",
            "byProject": {name: value.get("inputCoverage", {}) for name, value in by_project.items()},
        },
    }
