from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import platform
import re
import subprocess
import threading
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import unquote, urlparse

from semantic_version import NpmSpec

PROOF_SCHEMA_VERSION = "baseline-proof-v4-fixed-source-identity"

_RESOLVER_FILES = (
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

_EXCLUDED_FALLBACK_DIRS = {
    ".git", "node_modules", "dist", "build", ".cache", ".dependency-roadmap",
    ".idea", ".vs", ".vscode", ".fleet",
}

_TELEMETRY_LOCKS: dict[str, threading.Lock] = {}
_TELEMETRY_LOCKS_GUARD = threading.Lock()


class SourceIdentityUnavailable(RuntimeError):
    """Raised when a proof identity cannot be computed without guessing."""



def _git_marker_exists(project_dir: Path) -> bool:
    current = project_dir.resolve()
    while True:
        if (current / ".git").exists():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _canonical_hash(value: object, *, length: int = 32) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SourceIdentityUnavailable(
            f"SOURCE_IDENTITY_FILE_UNREADABLE: {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def environment_snapshot_fingerprint(environment: Mapping[str, str]) -> str:
    """Hash the complete inherited process environment without persisting secrets."""
    payload = [
        (str(key), hashlib.sha256(str(value).encode("utf-8")).hexdigest())
        for key, value in sorted(environment.items())
    ]
    return _canonical_hash({"schema": PROOF_SCHEMA_VERSION, "environment": payload})


def _run_git(project_dir: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(project_dir), *args],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(["git", *args], 127, stdout="", stderr=str(exc))


def _git_root_or_none(project_dir: Path) -> Path | None:
    result = _run_git(project_dir, ["rev-parse", "--show-toplevel"])
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    if result.returncode == 127 or _git_marker_exists(project_dir):
        detail = (result.stderr or result.stdout or "git rev-parse failed").strip()
        raise SourceIdentityUnavailable(
            f"SOURCE_IDENTITY_GIT_UNAVAILABLE: rev-parse --show-toplevel: {detail}"
        )
    return None


def _git_success(result: subprocess.CompletedProcess[str], operation: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise SourceIdentityUnavailable(
            f"SOURCE_IDENTITY_GIT_UNAVAILABLE: {operation}: {detail}"
        )
    return result.stdout or ""


def _fallback_source_fingerprint(project_dir: Path) -> str:
    files: list[tuple[str, str]] = []
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(project_dir)
        except ValueError:
            continue
        if any(part in _EXCLUDED_FALLBACK_DIRS for part in relative.parts):
            continue
        files.append((relative.as_posix(), _hash_file(path)))
    return _canonical_hash({"kind": "content-tree", "files": files})


_REGISTRY_DIST_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_FIXED_SPEC_PREFIXES = (
    "workspace:",
    "file:",
    "link:",
    "portal:",
    "git+",
    "git://",
    "ssh://",
    "github:",
    "gitlab:",
    "bitbucket:",
    "http://",
    "https://",
    "npm:",
    "patch:",
    "catalog:",
)
_LOCAL_FIXED_PREFIXES = ("file:", "link:", "portal:")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_DIRECT_DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


def is_fixed_manifest_spec(spec: str) -> bool:
    # Fixed by default: only a proven semver selector or ordinary dist-tag is
    # allowed into the registry-managed solver domain.
    value = str(spec or "").strip()
    if not value:
        return True
    lowered = value.lower()
    if lowered.startswith(_FIXED_SPEC_PREFIXES):
        return True
    try:
        NpmSpec(value)
        return False
    except ValueError:
        return _REGISTRY_DIST_TAG.fullmatch(value) is None


def _looks_like_local_fixed_path(spec: str) -> bool:
    value = str(spec or "").strip()
    lowered = value.lower()
    return (
        lowered.startswith(_LOCAL_FIXED_PREFIXES)
        or value.startswith(("./", "../", "~/", "/"))
        or _WINDOWS_ABSOLUTE_PATH.match(value) is not None
    )


def _local_fixed_target(project_dir: Path, spec: str) -> Path:
    lowered = spec.lower()
    if lowered.startswith(_LOCAL_FIXED_PREFIXES):
        prefix, raw = spec.split(":", 1)
        payload = raw.strip()
        if prefix.lower() == "file":
            parsed = urlparse(spec)
            if parsed.scheme.lower() == "file" and parsed.path:
                payload = unquote(parsed.path)
                if os.name == "nt" and re.match(r"^/[A-Za-z]:/", payload):
                    payload = payload[1:]
    else:
        payload = spec.strip()
    path = Path(payload).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return path.resolve()


def _external_fixed_target_identity(project_dir: Path, spec: str) -> Mapping[str, object]:
    target = _local_fixed_target(project_dir, spec)
    base = {
        "path": str(target).replace("\\", "/"),
        "spec": spec,
    }
    if not target.exists():
        return {**base, "kind": "missing"}
    if target.is_file():
        return {**base, "kind": "file", "sha256": _hash_file(target)}
    if target.is_dir():
        return {
            **base,
            "kind": "directory",
            "contentTree": _fallback_source_fingerprint(target),
        }
    return {**base, "kind": "other"}


def fixed_resolver_input_fingerprint(project_dir: Path) -> str:
    # Root manifest/lock/config files are hashed separately by ResolverInputKey.
    # This key adds the source/content identity that those files cannot capture.
    project_dir = project_dir.resolve()
    manifest_path = project_dir / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SourceIdentityUnavailable(
            f"SOURCE_IDENTITY_MANIFEST_UNAVAILABLE: {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise SourceIdentityUnavailable(
            f"SOURCE_IDENTITY_MANIFEST_UNAVAILABLE: {manifest_path}: root must be an object"
        )

    fixed: list[Mapping[str, object]] = []
    workspace_snapshot = ""
    for section in _DIRECT_DEPENDENCY_SECTIONS:
        values = manifest.get(section)
        if not isinstance(values, dict):
            continue
        for name, raw_spec in sorted(values.items(), key=lambda item: str(item[0]).lower()):
            spec = str(raw_spec or "").strip()
            if not is_fixed_manifest_spec(spec):
                continue
            entry: dict[str, object] = {
                "section": section,
                "name": str(name),
                "spec": spec,
            }
            lowered = spec.lower()
            if lowered.startswith("workspace:"):
                if not workspace_snapshot:
                    workspace_snapshot = source_snapshot_fingerprint(project_dir)
                entry["workspaceSourceSnapshotKey"] = workspace_snapshot
            elif _looks_like_local_fixed_path(spec):
                entry["target"] = dict(_external_fixed_target_identity(project_dir, spec))
            fixed.append(entry)

    return _canonical_hash(
        {
            "schema": PROOF_SCHEMA_VERSION,
            "fixedResolverInputs": fixed,
        },
        length=64,
    )


def source_snapshot_fingerprint(project_dir: Path) -> str:
    """Hash the repository-wide source snapshot relevant to proof reuse."""
    project_dir = project_dir.resolve()
    git_root = _git_root_or_none(project_dir)
    if git_root is None:
        return _fallback_source_fingerprint(project_dir)

    head = _git_success(
        _run_git(git_root, ["rev-parse", "HEAD"]),
        "rev-parse HEAD",
    ).strip()
    if not head:
        raise SourceIdentityUnavailable("SOURCE_IDENTITY_GIT_UNAVAILABLE: empty HEAD")

    try:
        relative = project_dir.relative_to(git_root).as_posix() or "."
    except ValueError as exc:
        raise SourceIdentityUnavailable(
            f"SOURCE_IDENTITY_PROJECT_OUTSIDE_GIT_ROOT: {project_dir} vs {git_root}"
        ) from exc

    pathspec = [
        ".",
        ":(exclude).dependency-roadmap/**",
        ":(glob,exclude)**/.dependency-roadmap/**",
    ]
    status = _git_success(
        _run_git(
            git_root,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspec],
        ),
        "git status",
    )
    if not status:
        return _canonical_hash({
            "kind": "git-clean",
            "head": head,
            "relative": relative,
        })

    diff = _git_success(
        _run_git(git_root, ["diff", "--binary", "HEAD", "--", *pathspec]),
        "git diff",
    )
    untracked = _git_success(
        _run_git(
            git_root,
            ["ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec],
        ),
        "git ls-files --others",
    )
    untracked_hashes: list[tuple[str, str]] = []
    for raw in untracked.split("\0"):
        raw = raw.strip()
        if not raw:
            continue
        path = git_root / raw
        if path.is_file():
            untracked_hashes.append(
                (raw.replace("\\", "/"), _hash_file(path))
            )

    return _canonical_hash({
        "kind": "git-dirty",
        "head": head,
        "relative": relative,
        "diff": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "untracked": sorted(untracked_hashes),
    })


def _resolver_ancestor_files(project_dir: Path) -> list[tuple[str, str]]:
    project_dir = project_dir.resolve()
    stop = _git_root_or_none(project_dir) or project_dir

    chain: list[Path] = []
    current = project_dir
    while True:
        chain.append(current)
        if current == stop or current.parent == current:
            break
        current = current.parent

    result: list[tuple[str, str]] = []
    for directory in chain:
        for name in _RESOLVER_FILES:
            candidate = directory / name
            if candidate.is_file():
                label = str(candidate.resolve()).replace("\\", "/")
                result.append((label, _hash_file(candidate)))
    return sorted(result)


def _user_config_files(environment: Mapping[str, str]) -> list[tuple[str, str]]:
    candidates: set[Path] = set()
    explicit = environment.get("NPM_CONFIG_USERCONFIG") or environment.get("npm_config_userconfig")
    if explicit:
        candidates.add(Path(explicit).expanduser())
    for key in ("HOME", "USERPROFILE"):
        raw = environment.get(key)
        if not raw:
            continue
        home = Path(raw)
        candidates.update({home / ".npmrc", home / ".yarnrc", home / ".yarnrc.yml"})

    result: list[tuple[str, str]] = []
    for candidate in sorted(candidates, key=lambda item: str(item).lower()):
        if candidate.is_file():
            result.append((str(candidate.resolve()).replace("\\", "/"), _hash_file(candidate)))
    return result


def _version_identity(executable: str, environment: Mapping[str, str]) -> str:
    path = Path(executable)
    argv: list[str]
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        comspec = environment.get("COMSPEC") or os.environ.get("COMSPEC") or "cmd.exe"
        quoted = subprocess.list2cmdline([str(path), "--version"])
        argv = [comspec, "/d", "/s", "/c", quoted]
    else:
        argv = [str(path), "--version"]
    try:
        completed = subprocess.run(
            argv,
            env=dict(environment),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        version = (completed.stdout or "").strip().splitlines()
        version_text = version[0] if version else f"exit:{completed.returncode}"
    except (OSError, subprocess.SubprocessError):
        version_text = "unavailable"
    return _canonical_hash({
        "path": str(path.resolve()) if path.exists() else str(path),
        "file": _hash_file(path) if path.is_file() else "missing",
        "version": version_text,
    })


def _node_identity(environment: Mapping[str, str]) -> str:
    path_value = environment.get("PATH") or os.defpath
    names = ["node.exe", "node"] if os.name == "nt" else ["node"]
    for directory in path_value.split(os.pathsep):
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_file():
                return _version_identity(str(candidate), environment)
    return _canonical_hash({"node": "missing"})


@dataclasses.dataclass(frozen=True)
class VerificationProofIdentity:
    schema_version: str
    assignment_key: str
    environment_key: str
    source_snapshot_key: str
    resolver_input_key: str
    preparation_proof_key: str
    project_proof_key: str
    localization_experiment_key: str

    def event_fields(self) -> dict[str, str]:
        return {
            "proofSchema": self.schema_version,
            "assignmentKey": self.assignment_key,
            "environmentKey": self.environment_key,
            "sourceSnapshotKey": self.source_snapshot_key,
            "resolverInputKey": self.resolver_input_key,
            "preparationProofKey": self.preparation_proof_key,
            "projectProofKey": self.project_proof_key,
            "localizationExperimentKey": self.localization_experiment_key,
        }


def build_verification_proof_identity(
    project_dir: Path,
    *,
    assignment: Mapping[str, str],
    remove_packages: Sequence[str],
    manager: str,
    manager_executable: str,
    registry: str,
    project_checks: str,
    commands: Sequence[str],
    environment: Mapping[str, str],
) -> VerificationProofIdentity:
    project_dir = project_dir.resolve()
    environment_key = environment_snapshot_fingerprint(environment)
    source_key = source_snapshot_fingerprint(project_dir)
    fixed_resolver_inputs_key = fixed_resolver_input_fingerprint(project_dir)
    assignment_key = _canonical_hash({
        "assignment": sorted((str(k), str(v)) for k, v in assignment.items()),
        "removals": sorted(str(item) for item in remove_packages),
    })

    resolver_inputs = {
        "schema": PROOF_SCHEMA_VERSION,
        "projectResolverFiles": _resolver_ancestor_files(project_dir),
        "userConfigFiles": _user_config_files(environment),
        "fixedResolverInputsKey": fixed_resolver_inputs_key,
        "assignmentKey": assignment_key,
        "environmentKey": environment_key,
        "registry": str(registry or "").rstrip("/"),
        "platform": platform.system().lower(),
        "machine": platform.machine().lower(),
        "manager": manager,
        "managerIdentity": _version_identity(manager_executable, environment),
        "nodeIdentity": _node_identity(environment),
        "resolverPolicy": "real-package-manager:scripts-off",
    }
    resolver_key = _canonical_hash(resolver_inputs)

    preparation_key = _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "resolverInputKey": resolver_key,
        "sourceSnapshotKey": source_key,
        "preparationPolicy": "same-assignment:lifecycle-scripts-on",
    })
    project_key = _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "preparationProofKey": preparation_key,
        "sourceSnapshotKey": source_key,
        "projectChecks": project_checks,
        "commands": list(commands),
    })
    localization_key = _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "projectProofKey": project_key,
        "algorithm": "same-origin-localization-v1",
    })
    return VerificationProofIdentity(
        schema_version=PROOF_SCHEMA_VERSION,
        assignment_key=assignment_key,
        environment_key=environment_key,
        source_snapshot_key=source_key,
        resolver_input_key=resolver_key,
        preparation_proof_key=preparation_key,
        project_proof_key=project_key,
        localization_experiment_key=localization_key,
    )



_ALLOWED_PROOF_TYPES = frozenset({"resolver", "preparation", "project"})


@dataclasses.dataclass(frozen=True)
class CachedProofRecord:
    proof_type: str
    key: str
    created_at: str
    identity: Mapping[str, str]
    metadata: Mapping[str, object]


class VerificationProofStore:
    """PASS-only CAS for exact proof identities."""

    def __init__(self, root: Path | None):
        self.root = root.resolve() if root is not None else None

    def _path(self, proof_type: str, key: str) -> Path | None:
        if self.root is None or proof_type not in _ALLOWED_PROOF_TYPES:
            return None
        normalized = key.lower()
        if not normalized or any(ch not in "0123456789abcdef" for ch in normalized):
            return None
        return self.root / proof_type / f"{normalized}.json"

    def lookup_pass(self, proof_type: str, key: str) -> CachedProofRecord | None:
        path = self._path(proof_type, key)
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schemaVersion") != 1:
            return None
        if payload.get("proofSchema") != PROOF_SCHEMA_VERSION:
            return None
        if payload.get("proofType") != proof_type or payload.get("key") != key:
            return None
        if payload.get("outcome") != "passed":
            return None
        identity = payload.get("identity")
        if not isinstance(identity, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in identity.items()
        ):
            return None
        metadata = payload.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            return None
        if proof_type == "resolver":
            observed = metadata.get("observedResolvedVersions")
            observed_hash = metadata.get("observedResolvedHash")
            if (
                not isinstance(observed, dict)
                or not all(
                    isinstance(name, str) and isinstance(version, str)
                    for name, version in observed.items()
                )
                or not isinstance(observed_hash, str)
                or len(observed_hash) != 64
                or _canonical_hash(
                    dict(sorted(observed.items())), length=64
                ) != observed_hash
            ):
                return None
        return CachedProofRecord(
            proof_type=proof_type,
            key=key,
            created_at=str(payload.get("createdAt") or ""),
            identity=dict(identity),
            metadata=dict(metadata),
        )

    def publish_pass(
        self,
        proof_type: str,
        key: str,
        identity: VerificationProofIdentity,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> bool:
        path = self._path(proof_type, key)
        if path is None:
            return False
        payload = {
            "schemaVersion": 1,
            "proofSchema": PROOF_SCHEMA_VERSION,
            "proofType": proof_type,
            "key": key,
            "outcome": "passed",
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "identity": identity.event_fields(),
            "metadata": dict(metadata or {}),
        }
        temp: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(
                path.name + f".tmp-{os.getpid()}-{threading.get_ident()}"
            )
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.replace(temp, path)
            return True
        except OSError:
            if temp is not None:
                try:
                    temp.unlink()
                except OSError:
                    pass
            return False


def _telemetry_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _TELEMETRY_LOCKS_GUARD:
        lock = _TELEMETRY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _TELEMETRY_LOCKS[key] = lock
        return lock


def emit_verification_event(path: Path | None, event: str, **fields: object) -> None:
    """Best-effort JSONL telemetry. It is observability, never proof authority."""
    if path is None:
        return
    payload = {
        "schemaVersion": 1,
        "event": event,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pid": os.getpid(),
        **fields,
    }
    try:
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with _telemetry_lock(path):
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
    except OSError:
        return
