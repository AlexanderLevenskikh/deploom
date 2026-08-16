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
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
)

CACHE_SCHEMA_VERSION = 1
SOLVER_SCHEMA_VERSION = "peer-ir-v2-fixed-inputs"
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
    """Legacy strict failure identity.

    This intentionally continues to hash the normalized complete output. It is
    the fallback whenever no narrow structured resolver predicate is available.
    """
    text = f"{summary}\n{output}".strip().lower()
    text = _FAILURE_PATH.sub("<workspace>", text)
    text = _WHITESPACE.sub(" ", text)
    return _sha256_bytes(text.encode("utf-8"))[:24]


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_NPM_ERESOLVE_PEER_BLOCK = re.compile(
    r"found:\s*(?P<found>@?[a-z0-9_.-]+(?:/[a-z0-9_.-]+)?@[^\s]+)"
    r".{0,5000}?"
    r"could not resolve dependency:\s*"
    r"(?:npm\s+(?:err!|error)\s*)?"
    r"peer(?:optional)?\s+(?P<peer>.+?)\s+from\s+"
    r"(?P<consumer>@?[a-z0-9_.-]+(?:/[a-z0-9_.-]+)?@[^\s,]+)",
    re.IGNORECASE | re.DOTALL,
)
_NPM_MISSING_VERSION = re.compile(
    r"no matching version found for\s+"
    r"(?P<spec>@?[a-z0-9_.-]+(?:/[a-z0-9_.-]+)?@[^\s]+)",
    re.IGNORECASE,
)
_YARN1_MISSING_VERSION = re.compile(
    r"""couldn't find any versions for\s+["'](?P<package>[^"']+)["']\s+"""
    r"""that matches\s+["'](?P<range>[^"']+)["']""",
    re.IGNORECASE,
)
_YARN_BERRY_NO_CANDIDATES = re.compile(
    r"\bYN0082\b(?P<body>[^\r\n]*)",
    re.IGNORECASE,
)


def _normalize_resolver_predicate_atom(value: str) -> str:
    text = _ANSI_ESCAPE.sub("", str(value))
    text = _FAILURE_PATH.sub("<workspace>", text)
    text = text.replace('"', "").replace("'", "").replace("`", "")
    text = _WHITESPACE.sub(" ", text.strip().lower())
    return text.rstrip(".,;:")


def _package_name_from_spec(value: str) -> str:
    normalized = _normalize_resolver_predicate_atom(value)
    head, separator, _version = normalized.rpartition("@")
    if separator and head:
        return head
    return ""


def dependency_failure_predicates(*, summary: str, output: str) -> Tuple[str, ...]:
    """Extract only narrow fatal resolver facts suitable for proof comparison.

    These facts are not authority on their own. They only compare two already
    failed fresh verifier runs. Unknown output yields no structured predicate
    and therefore falls back to the legacy strict whole-output identity.
    """
    text = _ANSI_ESCAPE.sub("", f"{summary}\n{output}")
    facts: List[str] = []

    # npm ERESOLVE: require both the concrete version actually found and the
    # exact peer requirement/consumer. Requirement alone would be too broad.
    if re.search(r"\bERESOLVE\b", text, re.IGNORECASE):
        for match in _NPM_ERESOLVE_PEER_BLOCK.finditer(text):
            found = _normalize_resolver_predicate_atom(match.group("found"))
            peer = _normalize_resolver_predicate_atom(match.group("peer"))
            consumer = _normalize_resolver_predicate_atom(match.group("consumer"))
            found_name = _package_name_from_spec(found)
            peer_name = _package_name_from_spec(peer)
            if (
                found
                and peer
                and consumer
                and found_name
                and peer_name
                and found_name == peer_name
            ):
                facts.append(
                    f"npm-eresolve-peer:found={found};required={peer};consumer={consumer}"
                )

    for match in _NPM_MISSING_VERSION.finditer(text):
        spec = _normalize_resolver_predicate_atom(match.group("spec"))
        if spec:
            facts.append(f"missing-version:{spec}")

    for match in _YARN1_MISSING_VERSION.finditer(text):
        package = _normalize_resolver_predicate_atom(match.group("package"))
        requested = _normalize_resolver_predicate_atom(match.group("range"))
        if package and requested:
            facts.append(f"missing-version:{package}@{requested}")

    # YN0082 is a fatal Yarn Berry no-candidates resolver code. Preserve the
    # normalized exact body rather than reducing it to a generic code.
    for match in _YARN_BERRY_NO_CANDIDATES.finditer(text):
        body = _normalize_resolver_predicate_atom(match.group("body"))
        if body:
            facts.append(f"yarn-yn0082:{body}")

    return tuple(dict.fromkeys(facts))


def _structured_dependency_predicate_signature(predicate: str) -> str:
    digest = _sha256_bytes(
        f"resolver-predicate-v2\0{predicate}".encode("utf-8")
    )[:24]
    return f"resolver-predicate-v2:{digest}"


def matching_dependency_failure_signature(
    *,
    expected_summary: str,
    expected_output: str,
    observed_summary: str,
    observed_output: str,
) -> str:
    """Match two failed resolver runs without trusting unrelated output noise.

    Preferred path is an exact shared structured fatal fact. Opaque failures
    retain the pre-existing strict normalized-whole-output equality.
    """
    expected_predicates = dependency_failure_predicates(
        summary=expected_summary,
        output=expected_output,
    )
    observed_predicates = set(
        dependency_failure_predicates(
            summary=observed_summary,
            output=observed_output,
        )
    )
    for predicate in expected_predicates:
        if predicate in observed_predicates:
            return _structured_dependency_predicate_signature(predicate)

    expected_legacy = dependency_failure_signature(
        summary=expected_summary,
        output=expected_output,
    )
    observed_legacy = dependency_failure_signature(
        summary=observed_summary,
        output=observed_output,
    )
    return expected_legacy if expected_legacy == observed_legacy else ""


def dependency_failure_navigation_signature(*, summary: str, output: str) -> str:
    """Stable navigation key only; never solver authority."""
    predicates = dependency_failure_predicates(summary=summary, output=output)
    if predicates:
        return _structured_dependency_predicate_signature(predicates[0])
    return dependency_failure_signature(summary=summary, output=output)



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


@contextmanager
def _exclusive_cache_write_lock(path: Path):
    """Cross-process lock for the tiny read-modify-replace cache transaction.

    On Windows, CREATE_NEW/O_EXCL against an existing lock file may surface as
    PermissionError (sharing violation) rather than FileExistsError. Treat that
    as contention only while the lock path exists. A PermissionError without
    an existing lock remains a real infrastructure/permissions failure.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 10.0
    fd: Optional[int] = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} created={time.time()}\n".encode("utf-8"))
            break
        except FileExistsError:
            pass
        except PermissionError:
            # Windows sharing violations can be EACCES while another writer
            # owns the already-existing lock. Do not hide a genuine ACL/path
            # permission failure when no lock file exists.
            try:
                if not lock_path.exists():
                    raise
            except OSError:
                raise

        try:
            # A crashed writer must not deadlock future Baselines forever.
            if time.time() - lock_path.stat().st_mtime > 120.0:
                try:
                    lock_path.unlink()
                except PermissionError:
                    # A live Windows owner can deny delete sharing. This is
                    # active contention, not stale-lock authority.
                    pass
                continue
        except FileNotFoundError:
            continue
        except PermissionError:
            # Same Windows sharing behavior while the owner is still active.
            pass

        if time.monotonic() >= deadline:
            raise TimeoutError(f"constraint cache lock timed out: {lock_path}")
        time.sleep(0.05)

    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        cleanup_deadline = time.monotonic() + 1.0
        while True:
            try:
                lock_path.unlink()
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if time.monotonic() >= cleanup_deadline:
                    raise
                time.sleep(0.01)

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
    """Atomically persist one proven clause. Returns True when cache changed.

    ``max_entries`` is intentionally a per-project retention bound. A busy
    project must never evict verified clauses belonging to another project.
    """
    if path is None or proof.verified_count < 2 or not proof.failure_signature or not proof.literals:
        return False
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _exclusive_cache_write_lock(path):
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

            if max_entries > 0:
                project_key = str(normalized["projectPath"])
                project_count = sum(
                    1 for item in entries
                    if str(item.get("projectPath") or "") == project_key
                )
                drop = max(0, project_count - max_entries)
                if drop:
                    retained = []
                    for item in entries:
                        if drop and str(item.get("projectPath") or "") == project_key:
                            drop -= 1
                            continue
                        retained.append(item)
                    entries = retained

            payload = {"schemaVersion": CACHE_SCHEMA_VERSION, "entries": entries}
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
    except TimeoutError as exc:
        # Durable cache is an optimization/evidence store, never solver
        # authority for the current run. Losing the persistence opportunity must
        # not invalidate an otherwise proven session-local result.
        warnings.warn(str(exc), RuntimeWarning, stacklevel=2)
        return False
