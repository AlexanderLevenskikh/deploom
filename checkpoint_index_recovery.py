"""Inspect index-only drift as input for fresh checkpoint verification.

This grants no proof authority and never modifies a historical snapshot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from source_snapshot import (
    SOURCE_SNAPSHOT_SCHEMA, SourceCaptureError, SourceInputPolicy,
    _canonical_hash, _validate_git_layout, build_source_tree_manifest,
)


@dataclass(frozen=True)
class IndexRecoveryInput:
    project_path: Path
    manifest_key: str
    recovery: dict


def inspect_checkpoint_index_drift(container, expected_key, *, progress):
    container = Path(container).resolve()
    raw = json.loads((container / "manifest.json").read_text(encoding="utf-8"))
    policy = SourceInputPolicy()
    entries = raw.get("entries")
    if (raw.get("schemaVersion") != 1 or raw.get("type") != "deploom-source-snapshot"
            or raw.get("policyKey") != policy.key or not isinstance(entries, list)
            or not isinstance(raw.get("toolBuildId"), str) or not raw["toolBuildId"]):
        raise SourceCaptureError("SOURCE_SNAPSHOT_RECOVERY_MANIFEST_INVALID")
    recorded = _canonical_hash({"schema": SOURCE_SNAPSHOT_SCHEMA,
        "toolBuildId": raw["toolBuildId"], "policyKey": policy.key, "entries": entries}, length=64)
    identity = _canonical_hash({"schema": SOURCE_SNAPSHOT_SCHEMA,
        "toolBuildId": raw["toolBuildId"], "policyKey": policy.key,
        "manifestKey": recorded, "projectRelative": raw.get("projectRelative")}, length=32)
    if (recorded != raw.get("manifestKey") or identity != expected_key
            or raw.get("sourceSnapshotKey") != expected_key):
        raise SourceCaptureError("SOURCE_SNAPSHOT_RECOVERY_MANIFEST_IDENTITY_MISMATCH")
    root = (container / "tree").resolve()
    relative = Path(str(raw.get("projectRelative") or "."))
    project = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not project.is_relative_to(root) or not project.is_dir():
        raise SourceCaptureError("SOURCE_SNAPSHOT_RECOVERY_PROJECT_INVALID")
    _validate_git_layout(root, policy=policy)
    observed = build_source_tree_manifest(root, policy=policy, timeout_seconds=0,
        progress=progress, progress_label="Inspecting checkpoint index drift")
    before = {entry["path"]: entry for entry in entries}
    after = {entry["path"]: entry for entry in observed.entries}
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    if (len(before) != len(entries) or changed != [".git/index"]
            or before.get(".git/index", {}).get("kind") != "file"
            or after.get(".git/index", {}).get("kind") != "file"):
        raise SourceCaptureError("SOURCE_SNAPSHOT_CONTENT_MISMATCH: recovery refused; changed paths="
            + ", ".join(changed[:10]))
    progress("Only .git/index changed; preparing a separate snapshot and fresh selected checks")
    return IndexRecoveryInput(project, observed.key, {
        "kind": "git-index-only", "originalManifestKey": recorded,
        "observedManifestKey": observed.key, "changedPaths": changed,
    })
