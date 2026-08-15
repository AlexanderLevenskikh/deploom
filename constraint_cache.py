#!/usr/bin/env python3
"""Persistent, environment-scoped package-manager incompatibility proofs.

Only reproducible dependency-resolution failures belong here.  Entries are
scoped by a conservative resolver-environment fingerprint so a clause learned
under one manifest/lock/config/runtime context cannot silently become authority
in another.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

CACHE_SCHEMA_VERSION = 1
SOLVER_SCHEMA_VERSION = "peer-ir-v1"
_ENV_FILES = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    "pnpm-workspace.yaml",
    ".nvmrc",
    ".node-version",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()




def _command_identity(project_dir: Path, command: str) -> Dict[str, str]:
    executable = shutil.which(command)
    if not executable:
        return {"command": command, "executable": "missing", "version": "missing"}
    try:
        completed = subprocess.run(
            [executable, "--version"], cwd=project_dir, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10, check=False,
        )
        version = (completed.stdout or "").strip().splitlines()[0] if completed.stdout else f"exit:{completed.returncode}"
    except (OSError, subprocess.TimeoutExpired):
        version = "unavailable"
    return {"command": command, "executable": str(Path(executable).resolve()), "version": version}


def _package_manager_name(project_dir: Path) -> str:
    try:
        manifest = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
        raw = str(manifest.get("packageManager") or "") if isinstance(manifest, dict) else ""
        if raw:
            return raw.split("@", 1)[0].strip().lower()
    except (OSError, ValueError, TypeError):
        pass
    if (project_dir / "yarn.lock").is_file():
        return "yarn"
    if (project_dir / "pnpm-lock.yaml").is_file():
        return "pnpm"
    return "npm"

def resolver_environment_fingerprint(
    project_dir: Path,
    *,
    registry: str,
    solver_schema: str = SOLVER_SCHEMA_VERSION,
) -> str:
    """Return a conservative fingerprint for hard-clause reuse.

    The complete package manifest and lock/config files are intentionally part
    of the fingerprint.  This may invalidate more cache entries than strictly
    necessary, but never reuses a package-manager proof after material resolver
    inputs changed.
    """
    project_dir = project_dir.resolve()
    files: Dict[str, str] = {}
    for relative in _ENV_FILES:
        path = project_dir / relative
        if path.is_file():
            try:
                files[relative] = _sha256_bytes(path.read_bytes())
            except OSError:
                files[relative] = "unreadable"
    package_manager = _package_manager_name(project_dir)
    payload = {
        "schema": solver_schema,
        "registry": str(registry or "").rstrip("/"),
        "platform": platform.system().lower(),
        "machine": platform.machine().lower(),
        "runtime": {
            "node": _command_identity(project_dir, "node"),
            "packageManager": _command_identity(project_dir, package_manager),
            "nodeLinker": os.environ.get("YARN_NODE_LINKER") or os.environ.get("npm_config_node_linker") or "",
        },
        "files": files,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256_bytes(encoded.encode("utf-8"))[:32]


_FAILURE_PATH = re.compile(r"dependency-flow-baseline-verify-[^\\/\s]+", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def dependency_failure_signature(*, summary: str, output: str) -> str:
    """Normalize a resolver failure enough to compare fresh-workspace reruns."""
    text = f"{summary}\n{output}".strip().lower()
    text = _FAILURE_PATH.sub("<workspace>", text)
    text = _WHITESPACE.sub(" ", text)
    return _sha256_bytes(text.encode("utf-8"))[:24]


@dataclasses.dataclass(frozen=True)
class LearnedConstraintProof:
    project_path: str
    environment_fingerprint: str
    literals: Dict[str, str]
    failure_signature: str
    source: str = "package-manager-resolver"
    verified_count: int = 2
    created_at: str = ""

    def normalized_literals(self) -> Dict[str, str]:
        return {str(name): str(version) for name, version in sorted(self.literals.items())}

    def to_json(self) -> Dict[str, object]:
        return {
            "projectPath": self.project_path,
            "environmentFingerprint": self.environment_fingerprint,
            "literals": self.normalized_literals(),
            "failureSignature": self.failure_signature,
            "source": self.source,
            "verifiedCount": int(self.verified_count),
            "createdAt": self.created_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        }


def _read_cache(path: Optional[Path]) -> Dict[str, object]:
    if path is None or not path.is_file():
        return {"schemaVersion": CACHE_SCHEMA_VERSION, "entries": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"schemaVersion": CACHE_SCHEMA_VERSION, "entries": []}
    if not isinstance(value, dict) or value.get("schemaVersion") != CACHE_SCHEMA_VERSION:
        return {"schemaVersion": CACHE_SCHEMA_VERSION, "entries": []}
    if not isinstance(value.get("entries"), list):
        value["entries"] = []
    return value


def load_verified_nogoods(
    path: Optional[Path],
    *,
    project_path: Path,
    environment_fingerprint: str,
) -> List[Dict[str, str]]:
    cache = _read_cache(path)
    project_key = str(project_path.resolve())
    result: List[Dict[str, str]] = []
    seen = set()
    for raw in cache.get("entries", []):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("projectPath") or "") != project_key:
            continue
        if str(raw.get("environmentFingerprint") or "") != environment_fingerprint:
            continue
        if str(raw.get("source") or "") != "package-manager-resolver":
            continue
        try:
            verified_count = int(raw.get("verifiedCount") or 0)
        except (TypeError, ValueError):
            continue
        if verified_count < 2 or not str(raw.get("failureSignature") or ""):
            continue
        literals_raw = raw.get("literals")
        if not isinstance(literals_raw, dict) or not literals_raw:
            continue
        literals = {str(name): str(version) for name, version in sorted(literals_raw.items()) if str(name) and str(version)}
        key = tuple(sorted(literals.items()))
        if literals and key not in seen:
            seen.add(key)
            result.append(literals)
    return result


def persist_verified_nogood(path: Optional[Path], proof: LearnedConstraintProof, *, max_entries: int = 1000) -> bool:
    """Atomically persist one proven clause. Returns True when cache changed."""
    if path is None or proof.verified_count < 2 or not proof.failure_signature or not proof.literals:
        return False
    path = path.resolve()
    cache = _read_cache(path)
    entries = [item for item in cache.get("entries", []) if isinstance(item, dict)]
    normalized = proof.to_json()
    identity = (
        normalized["projectPath"],
        normalized["environmentFingerprint"],
        tuple(sorted(dict(normalized["literals"]).items())),
    )
    for existing in entries:
        existing_literals = existing.get("literals") if isinstance(existing.get("literals"), dict) else {}
        existing_identity = (
            str(existing.get("projectPath") or ""),
            str(existing.get("environmentFingerprint") or ""),
            tuple(sorted((str(k), str(v)) for k, v in existing_literals.items())),
        )
        if existing_identity == identity:
            # Preserve the original proof, but raise verification count if the
            # same clause was independently reproduced again.
            changed = False
            if int(existing.get("verifiedCount") or 0) < int(normalized["verifiedCount"]):
                existing["verifiedCount"] = normalized["verifiedCount"]
                changed = True
            if not existing.get("failureSignature"):
                existing["failureSignature"] = normalized["failureSignature"]
                changed = True
            if not changed:
                return False
            break
    else:
        entries.append(normalized)

    if len(entries) > max_entries:
        entries = entries[-max_entries:]
    payload = {"schemaVersion": CACHE_SCHEMA_VERSION, "entries": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass
    return True
