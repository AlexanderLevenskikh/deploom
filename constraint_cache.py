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
import threading
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from verification_proof import (
    PROOF_SCHEMA_VERSION,
    build_resolver_context_key,
)
from substrate_identity import tool_build_id

from typing import (
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
)

CACHE_SCHEMA_VERSION = 3
CONSTRAINT_ENTRY_SCHEMA = "verified-resolver-nogood-v3-tool-build"
SOLVER_SCHEMA_VERSION = "peer-ir-v3-fixed-source-identity"
_CACHE_WRITE_THREAD_LOCK = threading.RLock()
_WINDOWS_PERMISSION_GRACE_SECONDS = 0.25
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
    """Compatibility wrapper for the canonical resolver context key.

    Production loading/persistence receives this key from the same canonical
    builder used by VerificationProofIdentity. ``solver_schema`` is stored and
    validated separately in each durable constraint record rather than being
    folded into a second, weaker environment fingerprint.
    """
    del solver_schema
    project_dir = project_dir.resolve()
    manager = _package_manager_name(project_dir)
    executable = shutil.which(manager) or manager
    return build_resolver_context_key(
        project_dir,
        manager=manager,
        manager_executable=executable,
        registry=registry,
        environment=dict(os.environ),
    )


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
    # Legacy Python field name retained for API compatibility. In cache schema
    # v2 this MUST be the canonical 64-hex ResolverContextKey.
    environment_fingerprint: str
    literals: Dict[str, str]
    failure_signature: str
    source: str = "package-manager-resolver"
    verified_count: int = 2
    created_at: str = ""
    solver_schema: str = SOLVER_SCHEMA_VERSION

    def normalized_literals(self) -> Dict[str, str]:
        return {str(name): str(version) for name, version in sorted(self.literals.items())}

    def resolver_context_key(self) -> str:
        value = str(self.environment_fingerprint or "").lower()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("CONSTRAINT_CACHE_RESOLVER_CONTEXT_KEY_INVALID")
        return value

    def to_json(self) -> Dict[str, object]:
        literals = self.normalized_literals()
        context_key = self.resolver_context_key()
        payload = {
            "entrySchema": CONSTRAINT_ENTRY_SCHEMA,
            "proofSchema": PROOF_SCHEMA_VERSION,
            "toolBuildId": tool_build_id(),
            "solverSchema": str(self.solver_schema),
            "projectPath": self.project_path,
            "resolverContextKey": context_key,
            "literals": literals,
            "failureSignature": self.failure_signature,
            "source": self.source,
        }
        return {
            **payload,
            "entryKey": _constraint_entry_key(payload),
            "verifiedCount": int(self.verified_count),
            "createdAt": self.created_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        }


@contextmanager
def _exclusive_cache_write_lock(path: Path):
    """Serialize cache writers across threads and processes.

    Same-process writers are serialized by an RLock. The lock file provides
    cross-process exclusion. On Windows CREATE_NEW/O_EXCL can transiently return
    PermissionError/EACCES while another process is releasing the lock file.
    That race can be observed after the path has already disappeared, so a
    short bounded grace retry is required before classifying it as a real
    permissions failure.

    Authority is fail-closed:
    - transient sharing races are retried;
    - persistent permissions failures are raised;
    - lock acquisition timeout is raised;
    - no failed acquisition can publish cache contents.
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with _CACHE_WRITE_THREAD_LOCK:
        deadline = time.monotonic() + 10.0
        permission_grace_started: Optional[float] = None
        fd: Optional[int] = None

        while True:
            try:
                fd = os.open(
                    str(lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
                os.write(
                    fd,
                    f"pid={os.getpid()} created={time.time()}\n".encode("utf-8"),
                )
                break
            except FileExistsError:
                # Normal contention. Any earlier "path disappeared" grace is no
                # longer relevant because a lock is visibly present again.
                permission_grace_started = None
            except PermissionError:
                now = time.monotonic()
                try:
                    lock_exists = lock_path.exists()
                except OSError:
                    # If even probing the path is denied, keep the operation
                    # fail-closed rather than guessing that the lock is absent.
                    raise

                if lock_exists:
                    # Windows may report EACCES instead of EEXIST while another
                    # process owns CREATE_NEW. Treat it as ordinary contention.
                    permission_grace_started = None
                else:
                    # TOCTOU: the owning process may have deleted the lock after
                    # CREATE_NEW returned its sharing violation but before this
                    # existence check. Retry briefly; a persistent ACL failure
                    # still escapes after the bounded grace period.
                    if permission_grace_started is None:
                        permission_grace_started = now
                    elif now - permission_grace_started >= _WINDOWS_PERMISSION_GRACE_SECONDS:
                        raise

            try:
                stat = lock_path.stat()
            except FileNotFoundError:
                stat = None
            except PermissionError:
                # Active Windows sharing contention. Acquisition retry below is
                # still bounded by the main deadline.
                stat = None

            if stat is not None and time.time() - stat.st_mtime > 120.0:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                except PermissionError:
                    # A live Windows owner can deny delete sharing.
                    pass
                else:
                    permission_grace_started = None
                    continue

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"constraint cache lock timed out: {lock_path}"
                )

            time.sleep(
                0.01
                if permission_grace_started is not None
                else 0.05
            )

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

def _constraint_entry_key(payload: Mapping[str, object]) -> str:
    canonical = {
        "entrySchema": str(payload.get("entrySchema") or ""),
        "proofSchema": str(payload.get("proofSchema") or ""),
        "toolBuildId": str(payload.get("toolBuildId") or ""),
        "solverSchema": str(payload.get("solverSchema") or ""),
        "projectPath": str(payload.get("projectPath") or ""),
        "resolverContextKey": str(payload.get("resolverContextKey") or "").lower(),
        "literals": dict(sorted(
            (str(k), str(v))
            for k, v in dict(payload.get("literals") or {}).items()
        )),
        "failureSignature": str(payload.get("failureSignature") or ""),
        "source": str(payload.get("source") or ""),
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _empty_cache() -> Dict[str, object]:
    return {
        "schemaVersion": CACHE_SCHEMA_VERSION,
        "proofSchema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "entrySchema": CONSTRAINT_ENTRY_SCHEMA,
        "entries": [],
    }


def _valid_context_key(value: object) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


def _validated_constraint_entry(raw: object) -> Optional[Dict[str, object]]:
    if not isinstance(raw, dict):
        return None
    if raw.get("entrySchema") != CONSTRAINT_ENTRY_SCHEMA:
        return None
    if raw.get("proofSchema") != PROOF_SCHEMA_VERSION:
        return None
    if raw.get("toolBuildId") != tool_build_id():
        return None
    if raw.get("solverSchema") != SOLVER_SCHEMA_VERSION:
        return None
    project_path = str(raw.get("projectPath") or "")
    context_key = str(raw.get("resolverContextKey") or "").lower()
    failure_signature = str(raw.get("failureSignature") or "")
    source = str(raw.get("source") or "")
    if not project_path or not _valid_context_key(context_key):
        return None
    if not failure_signature or source != "package-manager-resolver":
        return None
    try:
        verified_count = int(raw.get("verifiedCount") or 0)
    except (TypeError, ValueError):
        return None
    if verified_count < 2:
        return None
    literals_raw = raw.get("literals")
    if not isinstance(literals_raw, dict) or not literals_raw:
        return None
    literals = {
        str(name): str(version)
        for name, version in sorted(literals_raw.items())
        if isinstance(name, str) and isinstance(version, str) and name and version
    }
    if len(literals) != len(literals_raw) or not literals:
        return None
    authority_payload = {
        "entrySchema": CONSTRAINT_ENTRY_SCHEMA,
        "proofSchema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "solverSchema": SOLVER_SCHEMA_VERSION,
        "projectPath": project_path,
        "resolverContextKey": context_key,
        "literals": literals,
        "failureSignature": failure_signature,
        "source": source,
    }
    entry_key = _constraint_entry_key(authority_payload)
    if str(raw.get("entryKey") or "").lower() != entry_key:
        return None
    return {
        **authority_payload,
        "entryKey": entry_key,
        "verifiedCount": verified_count,
        "createdAt": str(raw.get("createdAt") or ""),
    }


def _read_cache(path: Optional[Path]) -> Dict[str, object]:
    if path is None or not path.is_file():
        return _empty_cache()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _empty_cache()
    # Legacy schema v1 is intentionally NOT migrated into authority. It had a
    # weaker environment identity. A later successful fresh proof may persist
    # a new v2 record under the canonical ResolverContextKey.
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != CACHE_SCHEMA_VERSION
        or value.get("proofSchema") != PROOF_SCHEMA_VERSION
        or value.get("toolBuildId") != tool_build_id()
        or value.get("entrySchema") != CONSTRAINT_ENTRY_SCHEMA
    ):
        return _empty_cache()
    if not isinstance(value.get("entries"), list):
        return _empty_cache()
    return value


def load_verified_nogoods(
    path: Optional[Path],
    *,
    project_path: Path,
    environment_fingerprint: str,
) -> List[Dict[str, str]]:
    context_key = str(environment_fingerprint or "").lower()
    if not _valid_context_key(context_key):
        return []
    cache = _read_cache(path)
    project_key = str(project_path.resolve())
    result: List[Dict[str, str]] = []
    seen = set()
    for raw in cache.get("entries", []):
        validated = _validated_constraint_entry(raw)
        if validated is None:
            continue
        raw = validated
        if raw.get("entrySchema") != CONSTRAINT_ENTRY_SCHEMA:
            continue
        if raw.get("proofSchema") != PROOF_SCHEMA_VERSION:
            continue
        if raw.get("toolBuildId") != tool_build_id():
            continue
        if raw.get("solverSchema") != SOLVER_SCHEMA_VERSION:
            continue
        if str(raw.get("projectPath") or "") != project_key:
            continue
        if str(raw.get("resolverContextKey") or "").lower() != context_key:
            continue
        if str(raw.get("source") or "") != "package-manager-resolver":
            continue
        if not _valid_context_key(raw.get("resolverContextKey")):
            continue
        try:
            verified_count = int(raw.get("verifiedCount") or 0)
        except (TypeError, ValueError):
            continue
        failure_signature = str(raw.get("failureSignature") or "")
        if verified_count < 2 or not failure_signature:
            continue
        literals_raw = raw.get("literals")
        if not isinstance(literals_raw, dict) or not literals_raw:
            continue
        literals = {
            str(name): str(version)
            for name, version in sorted(literals_raw.items())
            if isinstance(name, str) and isinstance(version, str) and name and version
        }
        if len(literals) != len(literals_raw) or not literals:
            continue
        authority_payload = {
            "entrySchema": raw.get("entrySchema"),
            "proofSchema": raw.get("proofSchema"),
            "toolBuildId": raw.get("toolBuildId"),
            "solverSchema": raw.get("solverSchema"),
            "projectPath": raw.get("projectPath"),
            "resolverContextKey": raw.get("resolverContextKey"),
            "literals": literals,
            "failureSignature": failure_signature,
            "source": raw.get("source"),
        }
        expected_entry_key = _constraint_entry_key(authority_payload)
        if str(raw.get("entryKey") or "").lower() != expected_entry_key:
            continue
        key = tuple(sorted(literals.items()))
        if key not in seen:
            seen.add(key)
            result.append(literals)
    return result


def persist_verified_nogood(
    path: Optional[Path],
    proof: LearnedConstraintProof,
    *,
    max_entries: int = 1000,
) -> bool:
    """Atomically persist one proven clause after full authority validation."""
    if path is None or proof.verified_count < 2 or not proof.failure_signature or not proof.literals:
        return False
    # Fail closed before touching the existing cache. A short/display hash or
    # old environment fingerprint can never become a durable Solver clause.
    normalized = proof.to_json()
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _exclusive_cache_write_lock(path):
            cache = _read_cache(path)
            entries = [
                validated
                for item in cache.get("entries", [])
                if (validated := _validated_constraint_entry(item)) is not None
            ]
            identity = str(normalized["entryKey"])
            for existing in entries:
                if str(existing.get("entryKey") or "") != identity:
                    continue
                changed = False
                if int(existing.get("verifiedCount") or 0) < int(normalized["verifiedCount"]):
                    existing["verifiedCount"] = normalized["verifiedCount"]
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

            payload = {
                "schemaVersion": CACHE_SCHEMA_VERSION,
                "proofSchema": PROOF_SCHEMA_VERSION,
                "toolBuildId": tool_build_id(),
                "entrySchema": CONSTRAINT_ENTRY_SCHEMA,
                "entries": entries,
            }
            fd, temp_name = tempfile.mkstemp(
                prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
            )
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
        # Persistence remains an optimization/evidence store. Failure to write
        # cannot weaken or change the already proven session-local constraint.
        warnings.warn(str(exc), RuntimeWarning, stacklevel=2)
        return False

