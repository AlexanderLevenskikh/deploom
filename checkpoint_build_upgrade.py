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

from source_snapshot import SourceCaptureError, bind_source_snapshot, capture_durable_source_snapshot, open_source_snapshot
from substrate_identity import tool_build_id
from baseline_constraint_verifier import assignment_fingerprint, detect_package_manager, resolve_executable
from verification_proof import build_resolver_context_key, project_verification_environment


class CheckpointBuildUpgradeError(RuntimeError):
    def __init__(self, message, *, command="", diagnostic_path=""):
        super().__init__(message)
        self.command = command
        self.diagnostic_path = diagnostic_path


def content_mismatch(directory, key, old, current):
    """Describe saved index differences without a second filesystem inventory."""
    def saved(snapshot):
        raw = json.loads((Path(snapshot.container) / 'manifest.json').read_text(encoding='utf-8-sig'))
        return raw, {entry['path']: entry for entry in raw['entries']}
    previous, left = saved(old)
    latest, right = saved(current)
    added, removed = sorted(right.keys() - left.keys()), sorted(left.keys() - right.keys())
    changed = sorted(name for name in left.keys() & right.keys() if left[name] != right[name])
    facts = lambda raw: {name: raw.get(name) for name in ('schemaVersion', 'toolBuildId', 'inputPolicy', 'manifestKey', 'sourceSnapshotKey')}
    payload = {'schemaVersion': 1, 'type': 'checkpoint-build-upgrade-content-mismatch',
        'previous': facts(previous), 'current': facts(latest),
        'differences': {name: {'count': len(paths), 'samples': paths[:30]} for name, paths in
                        (('added', added), ('removed', removed), ('changed', changed))},
        'recovery': 'Preserve the historical checkpoint and proofs. Materialize a separate subject with the recorded capture policy, then run fresh verification of that exact subject; never infer compatibility from excluded path names.'}
    diagnostics = Path(directory) / 'diagnostics'
    diagnostics.mkdir(parents=True, exist_ok=True)
    path = diagnostics / (key + '-content-mismatch-' + uuid.uuid4().hex + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    return CheckpointBuildUpgradeError(
        f'CHECKPOINT_BUILD_UPGRADE_CONTENT_MISMATCH: added={len(added)}, removed={len(removed)}, changed={len(changed)}. Historical checkpoint preserved. Diagnostic: {path}', diagnostic_path=str(path))


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
        record = json.loads(record_path.read_text(encoding="utf-8-sig"))
        receipt = record.get('verification') or {}
        if (record.get("identity") != identity or receipt.get("status") != "passed"
                or receipt.get('verifiedSourceSnapshotKey') != record.get('key')
                or receipt.get('verifiedAssignmentFingerprint') != assignment_fingerprint(checkpoint['fullAssignment'])):
            raise CheckpointBuildUpgradeError("CHECKPOINT_BUILD_UPGRADE_RECORD_INVALID")
        # Explicit maintenance may evict this reproducible copy. Missing bytes
        # require materialization and fresh verification, never reuse of proof.
        if Path(record["container"]).is_dir():
            snapshot = open_source_snapshot(Path(record["container"]), expected_key=record["key"], timeout_seconds=0, progress=progress)
            if snapshot.manifest_key != old.manifest_key:
                raise content_mismatch(directory, key, old, snapshot)
            return snapshot
    progress("Preparing checkpoint copy for fresh verification with the updated DepLoom")
    destination = directory / (key + "-source")
    snapshot = (open_source_snapshot(destination, timeout_seconds=0, progress=progress) if destination.exists()
                else capture_durable_source_snapshot(old.project_path, destination, policy=old.input_policy, timeout_seconds=0, progress=progress))
    if snapshot.manifest_key != old.manifest_key:
        raise content_mismatch(directory, key, old, snapshot)
    progress("Re-running the selected checks on the preserved checkpoint; old proof is not reused")
    with bind_source_snapshot(snapshot):
        result = verify(snapshot.project_path, checkpoint["fullAssignment"])
    if not result.ok:
        # Failure evidence has no authority. Preserve captured output after
        # isolated workspace cleanup; never publish a pass record for failure.
        diagnostics = directory / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        diagnostic_path = diagnostics / (key + "-" + uuid.uuid4().hex + ".json")
        payload = {
            "schemaVersion": 1, "type": "checkpoint-reverification-failure",
            "checkpointId": checkpoint.get("checkpointId"),
            "sourceSnapshotKey": old_key, "toolBuildId": tool_build_id(),
            "kind": result.kind, "summary": result.summary,
            "command": result.command, "output": result.output,
            "workspace": result.workspace,
        }
        temporary = diagnostic_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, diagnostic_path)
        tail = "\n".join(str(result.output or "").splitlines()[-12:])[-2000:]
        detail = ("\nCommand: " + result.command if result.command else "")
        detail += ("\n" + tail if tail else "")
        detail += "\nDiagnostic: " + str(diagnostic_path)
        raise CheckpointBuildUpgradeError(
            "CHECKPOINT_BUILD_UPGRADE_UNCONFIRMED: " + str(result.kind) + ": " + str(result.summary) + detail,
            command=result.command, diagnostic_path=str(diagnostic_path),
        )
    if (getattr(result, 'verified_source_snapshot_key', '') != snapshot.key
            or getattr(result, 'verified_assignment_fingerprint', '') != assignment_fingerprint(checkpoint['fullAssignment'])):
        raise CheckpointBuildUpgradeError('CHECKPOINT_BUILD_UPGRADE_SUBJECT_MISMATCH: preserve the historical checkpoint and require verification of the exact new subject')
    # The verifier may never mutate the sealed input it claims to verify.
    open_source_snapshot(snapshot.container, expected_key=snapshot.key, timeout_seconds=0, progress=progress)
    # No old checkpoint is rewritten; publishing this record is the only commit.
    record = {
        "schemaVersion": 1, "identity": identity, "key": snapshot.key,
        "container": str(snapshot.container), "verification": {
            "status": "passed", "kind": result.kind,
            "verifiedSourceSnapshotKey": result.verified_source_snapshot_key,
            "verifiedAssignmentFingerprint": result.verified_assignment_fingerprint,
            "resolvedStateKey": result.resolved_state_key or "",
            "preparationProofKey": result.preparation_proof_key or "",
        },
    }
    temporary = record_path.with_name(record_path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(temporary, record_path)
    progress("Preserved checkpoint verified with the updated DepLoom; continuing migration")
    return snapshot
