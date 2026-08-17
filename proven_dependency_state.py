from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

PROVEN_DEPENDENCY_STATE_SCHEMA_VERSION = 1
PROVEN_DEPENDENCY_ENVELOPE_SCHEMA_VERSION = 3
RESOLVER_PROOF_STATUS_PASSED = "passed"
RESOLVER_PROOF_STATUS_NOT_REQUIRED_NO_OP = "not-required-no-op"
RESOLVER_PROOF_STATUSES = frozenset({
    RESOLVER_PROOF_STATUS_PASSED,
    RESOLVER_PROOF_STATUS_NOT_REQUIRED_NO_OP,
})
PREPARATION_PROOF_STATUSES = frozenset({"passed", "not-required", "missing"})
PROJECT_PROOF_STATUSES = frozenset({"passed", "not-required", "diagnostic-red", "missing"})


def _canonical_json(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(str(key), ensure_ascii=False) + ":" + _canonical_json(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def proof_envelope_key(payload: Mapping[str, Any]) -> str:
    normalized = {
        str(key): value
        for key, value in payload.items()
        if key != "envelopeKey"
    }
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


def build_proven_dependency_envelope(
    *,
    project: str,
    mode: str,
    proof_schema: str,
    source_head: str,
    source_snapshot_key: str,
    assignment_key: str,
    resolver_input_key: str,
    preparation_proof_key: str,
    project_proof_key: str,
    observed_resolved_hash: str,
    resolved_state_key: str,
    resolved_lockfile_path: str,
    resolved_lockfile_hash: str,
    assignment: Mapping[str, str],
    removals: Sequence[str],
    verification_commands: Sequence[str],
    project_checks: str,
    resolver_proof_status: str,
    preparation_proof_status: str,
    project_proof_status: str,
) -> dict[str, Any]:
    resolver_status = str(resolver_proof_status)
    preparation_status = str(preparation_proof_status)
    project_status = str(project_proof_status)
    if resolver_status not in RESOLVER_PROOF_STATUSES:
        raise ValueError(f"resolver proof status invalid: {resolver_status or 'missing'}")
    if preparation_status not in PREPARATION_PROOF_STATUSES:
        raise ValueError(f"preparation proof status invalid: {preparation_status or 'missing'}")
    if project_status not in PROJECT_PROOF_STATUSES:
        raise ValueError(f"project proof status invalid: {project_status or 'missing'}")

    payload: dict[str, Any] = {
        "schemaVersion": PROVEN_DEPENDENCY_ENVELOPE_SCHEMA_VERSION,
        "proofSchema": str(proof_schema),
        "project": str(project),
        "mode": str(mode),
        "sourceHead": str(source_head),
        "sourceSnapshotKey": str(source_snapshot_key),
        "assignmentKey": str(assignment_key),
        "resolverInputKey": str(resolver_input_key),
        "preparationProofKey": str(preparation_proof_key),
        "projectProofKey": str(project_proof_key),
        "observedResolvedHash": str(observed_resolved_hash),
        "resolvedStateKey": str(resolved_state_key),
        "resolvedLockfilePath": str(resolved_lockfile_path),
        "resolvedLockfileHash": str(resolved_lockfile_hash),
        "exactDirectAssignment": {
            str(name): str(version)
            for name, version in sorted(assignment.items())
        },
        "removals": sorted({str(name) for name in removals}),
        "verificationCommands": [str(command) for command in verification_commands],
        "projectChecks": str(project_checks),
        "resolverProofStatus": resolver_status,
        "preparationProofStatus": preparation_status,
        "projectProofStatus": project_status,
    }
    return {**payload, "envelopeKey": proof_envelope_key(payload)}


def validate_proven_dependency_envelope(
    envelope: Mapping[str, Any],
) -> tuple[bool, str]:
    if int(envelope.get("schemaVersion", 0) or 0) != PROVEN_DEPENDENCY_ENVELOPE_SCHEMA_VERSION:
        return False, "unsupported envelope schema"
    expected = str(envelope.get("envelopeKey") or "")
    if not expected:
        return False, "envelope key missing"
    actual = proof_envelope_key(envelope)
    if expected != actual:
        return False, "envelope key mismatch"
    assignment = envelope.get("exactDirectAssignment")
    if not isinstance(assignment, dict) or not all(
        isinstance(name, str)
        and isinstance(version, str)
        and name
        and version
        for name, version in assignment.items()
    ):
        return False, "exactDirectAssignment invalid"
    removals = envelope.get("removals")
    if not isinstance(removals, list) or not all(
        isinstance(name, str) and name for name in removals
    ):
        return False, "removals invalid"
    if any(name not in assignment for name in removals):
        return False, "removal is not part of exactDirectAssignment"
    resolver_status = str(envelope.get("resolverProofStatus") or "")
    if resolver_status not in RESOLVER_PROOF_STATUSES:
        return False, f"resolver proof status invalid: {resolver_status or 'missing'}"
    observed_hash = str(envelope.get("observedResolvedHash") or "")
    resolved_state_key = str(envelope.get("resolvedStateKey") or "")
    resolved_lockfile_path = str(envelope.get("resolvedLockfilePath") or "")
    resolved_lockfile_hash = str(envelope.get("resolvedLockfileHash") or "")
    if resolver_status == RESOLVER_PROOF_STATUS_PASSED:
        if len(observed_hash) != 64 or any(
            ch not in "0123456789abcdef" for ch in observed_hash.lower()
        ):
            return False, "observedResolvedHash missing or invalid for resolver PASS"
        if len(resolved_state_key) != 64 or any(
            ch not in "0123456789abcdef" for ch in resolved_state_key.lower()
        ):
            return False, "resolvedStateKey missing or invalid for resolver PASS"
        if not resolved_lockfile_path:
            return False, "resolvedLockfilePath missing for resolver PASS"
        if len(resolved_lockfile_hash) != 64 or any(
            ch not in "0123456789abcdef" for ch in resolved_lockfile_hash.lower()
        ):
            return False, "resolvedLockfileHash missing or invalid for resolver PASS"
    elif observed_hash or resolved_state_key or resolved_lockfile_path or resolved_lockfile_hash:
        return False, "no-op envelope must not claim a resolved package-manager state"

    preparation_status = str(envelope.get("preparationProofStatus") or "")
    if preparation_status not in PREPARATION_PROOF_STATUSES:
        return False, f"preparation proof status invalid: {preparation_status or 'missing'}"
    project_status = str(envelope.get("projectProofStatus") or "")
    if project_status not in PROJECT_PROOF_STATUSES:
        return False, f"project proof status invalid: {project_status or 'missing'}"
    if resolver_status == RESOLVER_PROOF_STATUS_NOT_REQUIRED_NO_OP:
        if preparation_status != "not-required" or project_status != "not-required":
            return False, "no-op envelope must not claim preparation/project proof work"
    return True, "proof envelope valid"


def write_proven_dependency_state(
    path: Path,
    envelopes: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    projects = {
        str(project): {
            str(mode): dict(envelope)
            for mode, envelope in sorted(mode_envelopes.items())
        }
        for project, mode_envelopes in sorted(envelopes.items())
    }
    for project, mode_envelopes in projects.items():
        for mode, envelope in mode_envelopes.items():
            valid, reason = validate_proven_dependency_envelope(envelope)
            if not valid:
                raise ValueError(
                    f"PROVEN_DEPENDENCY_ENVELOPE_INVALID: {project}/{mode}: {reason}"
                )

    payload: dict[str, Any] = {
        "schemaVersion": PROVEN_DEPENDENCY_STATE_SCHEMA_VERSION,
        "type": "deploom-proven-dependency-state",
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "projects": projects,
    }

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return payload
