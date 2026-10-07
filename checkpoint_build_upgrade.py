"""Revalidate durable checkpoint bytes after a verifier build changes.

Historical checkpoints and their proofs remain immutable. A separate record
binds a freshly checked copy to the new substrate and the exact check profile.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from source_snapshot import SourceCaptureError, capture_durable_source_snapshot, open_source_snapshot
from substrate_identity import tool_build_id
from baseline_constraint_verifier import detect_package_manager, resolve_executable
from verification_proof import build_resolver_context_key, project_verification_environment


class CheckpointBuildUpgradeError(RuntimeError):
    pass


def reopen_checkpoint_source(run_dir, checkpoint, config, *, verify, progress, runtime_env=None):
    container = Path(str(checkpoint["sourceSnapshotContainer"]))
    old_key = str(checkpoint["sourceSnapshotKey"])
    try:
        return open_source_snapshot(container, expected_key=old_key, timeout_seconds=0, progress=progress)
    except SourceCaptureError as exc:
        if not str(exc).startswith(("SOURCE_SNAPSHOT_TOOL_BUILD_MISMATCH:", "SOURCE_SNAPSHOT_CONTENT_MISMATCH:")):
            raise
    progress("Validating preserved checkpoint before continuing")
    try:
        old = open_source_snapshot(container, expected_key=old_key, timeout_seconds=0, allow_build_upgrade=True, progress=progress)
    except SourceCaptureError as exc:
        if not str(exc).startswith("SOURCE_SNAPSHOT_CONTENT_MISMATCH:"):
            raise
        from checkpoint_index_recovery import inspect_checkpoint_index_drift
        old = inspect_checkpoint_index_drift(container, old_key, progress=progress)
    manager = detect_package_manager(old.project_path)
    context = build_resolver_context_key(
        old.project_path, manager=manager, manager_executable=resolve_executable(manager) or manager,
        registry=str(config["verifyConfig"].get("registry") or ""),
        environment=project_verification_environment(runtime_env),
    )
    identity = {"resolverContextKey": context,
        "sourceSnapshotKey": old_key,
        "toolBuildId": tool_build_id(),
        "assignment": checkpoint["fullAssignment"],
        "verifyConfig": config["verifyConfig"],
        "validationProfile": config.get("validationProfile"),
        "runtime": config.get("runtime"),
        "requestedNode": config.get("requestedNode"),
    }
    if getattr(old, "recovery", None):
        identity["indexRecovery"] = old.recovery
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    directory = Path(run_dir) / "checkpoint-build-upgrades"
    directory.mkdir(parents=True, exist_ok=True)
    record_path = directory / (key + ".json")
    # The context binds actual Node/manager versions, user config and environment.
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("identity") != identity or record.get("verification", {}).get("status") != "passed":
            raise CheckpointBuildUpgradeError("CHECKPOINT_BUILD_UPGRADE_RECORD_INVALID")
        snapshot = open_source_snapshot(Path(record["container"]), expected_key=record["key"], timeout_seconds=0, progress=progress)
        if snapshot.manifest_key != old.manifest_key:
            raise CheckpointBuildUpgradeError("CHECKPOINT_BUILD_UPGRADE_CONTENT_MISMATCH")
        return snapshot
    progress("Preparing checkpoint copy for fresh verification with the updated DepLoom")
    destination = directory / (key + "-source")
    snapshot = (open_source_snapshot(destination, timeout_seconds=0, progress=progress) if destination.exists()
                else capture_durable_source_snapshot(old.project_path, destination, timeout_seconds=0, progress=progress))
    if snapshot.manifest_key != old.manifest_key:
        raise CheckpointBuildUpgradeError("CHECKPOINT_BUILD_UPGRADE_CONTENT_MISMATCH")
    progress("Re-running the selected checks on the preserved checkpoint; old proof is not reused")
    result = verify(snapshot.project_path, checkpoint["fullAssignment"])
    if not result.ok:
        raise CheckpointBuildUpgradeError(
            "CHECKPOINT_BUILD_UPGRADE_UNCONFIRMED: " + str(result.kind) + ": " + str(result.summary)
        )
    # The verifier may never mutate the sealed input it claims to verify.
    open_source_snapshot(snapshot.container, expected_key=snapshot.key, timeout_seconds=0, progress=progress)
    # No old checkpoint is rewritten; publishing this record is the only commit.
    record = {
        "schemaVersion": 1, "identity": identity, "key": snapshot.key,
        "container": str(snapshot.container), "verification": {
            "status": "passed", "kind": result.kind,
            "resolvedStateKey": result.resolved_state_key or "",
            "preparationProofKey": result.preparation_proof_key or "",
        },
    }
    temporary = record_path.with_name(record_path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(temporary, record_path)
    progress("Preserved checkpoint verified with the updated DepLoom; continuing migration")
    return snapshot
