#!/usr/bin/env python3
"""Deterministic identity of the installed proof-producing substrate."""
from __future__ import annotations

import functools
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

TOOL_BUILD_ID_SCHEMA = "deploom-tool-build-v1"

_COMPONENT_FILES: Mapping[str, tuple[str, ...]] = {
    "source": (
        "source_snapshot.py",
        "project_topology.py",
        "verification_workspace_backend.py",
        "reparse_materialization.py",
    ),
    "resolver": (
        "verification_proof.py",
        "baseline_constraint_verifier.py",
        "constraint_cache.py",
        "project_topology.py",
    ),
    "preparation": (
        "baseline_constraint_verifier.py",
        "block_v_prepared_artifact.py",
        "prepared_workspace_fastpath.py",
        "verification_workspace_backend.py",
        "artifact_integrity.py",
    ),
    "project": (
        "baseline_constraint_verifier.py",
        "verification_process_supervisor.py",
        "verification_proof.py",
    ),
    "learning": (
        "dependency_live_roadmap_generator.py",
        "constraint_cache.py",
        "dependency_compatibility_evidence.py",
    ),
    "supervision": (
        "verification_process_supervisor.py",
        "baseline_constraint_verifier.py",
    ),
}


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "missing"
    return digest.hexdigest()


@functools.lru_cache(maxsize=1)
def tool_build_components() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    result: dict[str, str] = {}
    for component, names in sorted(_COMPONENT_FILES.items()):
        files = [
            {
                "path": name,
                "sha256": _file_digest(root / name),
            }
            for name in sorted(set(names))
        ]
        result[component] = _canonical_hash({
            "schema": TOOL_BUILD_ID_SCHEMA,
            "component": component,
            "files": files,
        })
    return result


@functools.lru_cache(maxsize=1)
def tool_build_id() -> str:
    override = str(os.environ.get("DEPLOOM_TOOL_BUILD_ID") or "").strip().lower()
    if (
        len(override) == 64
        and all(character in "0123456789abcdef" for character in override)
    ):
        return override
    return _canonical_hash({
        "schema": TOOL_BUILD_ID_SCHEMA,
        "components": tool_build_components(),
    })
