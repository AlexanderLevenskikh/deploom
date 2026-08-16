from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

PROVEN_DEPENDENCY_STATE_SCHEMA_VERSION = 1
PROVEN_DEPENDENCY_ENVELOPE_SCHEMA_VERSION = 1


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
    assignment: Mapping[str, str],
    removals: Sequence[str],
    verification_commands: Sequence[str],
    project_checks: str,
    resolver_proof_status: str,
    preparation_proof_status: str,
    project_proof_status: str,
) -> dict[str, Any]:
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
        "exactDirectAssignment": {
            str(name): str(version)
            for name, version in sorted(assignment.items())
        },
        "removals": sorted({str(name) for name in removals}),
        "verificationCommands": [str(command) for command in verification_commands],
        "projectChecks": str(project_checks),
        "resolverProofStatus": str(resolver_proof_status),
        "preparationProofStatus": str(preparation_proof_status),
        "projectProofStatus": str(project_proof_status),
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
