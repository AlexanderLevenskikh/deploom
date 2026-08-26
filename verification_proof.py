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
from block_vex_storage import semantic_verification_environment
from verification_observability import emit_observability_event
from substrate_identity import tool_build_id
from source_snapshot import (
    SourceCaptureError,
    active_source_snapshot,
    proof_subject_project_dir,
    source_snapshot_fingerprint as captured_source_snapshot_fingerprint,
)
from project_topology import (
    ProjectTopologyError,
    canonical_lockfile,
    topology_identity_payload,
)
# BLOCK_Z_PROJECT_TOPOLOGY_V1

# BLOCK_X_SOURCE_TRUTH_V1
PROOF_SCHEMA_VERSION = "baseline-proof-v7-tool-build"
RESOLVER_CONTEXT_SCHEMA_VERSION = "resolver-context-v3-tool-build"
TRIAL_PROOF_KEY_SCHEMA_VERSION = "trial-proof-key-v2-tool-build"

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
    """Hash semantic child environment without persisting secrets."""
    semantic = semantic_verification_environment(environment)
    payload = [
        (str(key), hashlib.sha256(str(value).encode("utf-8")).hexdigest())
        for key, value in sorted(semantic.items())
    ]
    return _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "environment": payload,
    })


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



_GIT_FIXED_PREFIXES = (
    "git+",
    "git://",
    "ssh://",
    "github:",
    "gitlab:",
    "bitbucket:",
)
_HTTP_FIXED_PREFIXES = ("http://", "https://")
_IMMUTABLE_GIT_COMMIT = re.compile(r"^[0-9a-fA-F]{40,64}$")
_IMMUTABLE_FRAGMENT_DIGEST = re.compile(r"^[0-9a-fA-F]{40,128}$")
_SRI_TOKEN = re.compile(r"^(?:sha1|sha256|sha384|sha512)-[A-Za-z0-9+/=_-]+$", re.IGNORECASE)
_GITHUB_SHORTHAND = re.compile(r"^[^@./\\s][^/\\s]*/[^/\\s#]+(?:#.+)?$")
_FIXED_SOURCE_CONTROL_PATHS = (
    ("resolutions",),
    ("overrides",),
    ("pnpm", "overrides"),
)


def _immutable_fragment(value: str, pattern: re.Pattern[str]) -> str:
    text = str(value or "").strip()
    if "#" not in text:
        return ""
    fragment = text.rsplit("#", 1)[1].strip()
    return fragment.lower() if pattern.fullmatch(fragment) else ""


def _content_integrity(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    tokens = [token for token in text.split() if token]
    return text if tokens and all(_SRI_TOKEN.fullmatch(token) for token in tokens) else ""


def _string_leaves(value: object, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    if isinstance(value, str):
        return [(path, value)]
    if not isinstance(value, dict):
        return []
    result: list[tuple[tuple[str, ...], str]] = []
    for key in sorted(value, key=lambda item: str(item).lower()):
        result.extend(_string_leaves(value[key], (*path, str(key))))
    return result


def _manifest_fixed_control_identity(project_dir: Path, spec: str) -> Mapping[str, object]:
    lowered = spec.lower()
    if lowered.startswith("workspace:"):
        return {"kind": "workspace", "sourceSnapshotKey": source_snapshot_fingerprint(project_dir)}
    if _looks_like_local_fixed_path(spec):
        return dict(_external_fixed_target_identity(project_dir, spec))
    commit = _immutable_fragment(spec, _IMMUTABLE_GIT_COMMIT)
    if commit and _looks_like_git_fixed_source(spec, spec):
        return {"kind": "git-commit", "locator": spec.rsplit("#", 1)[0], "commit": commit}
    raise SourceIdentityUnavailable(
        "FIXED_SOURCE_IMMUTABLE_IDENTITY_UNAVAILABLE: resolver control contains "
        f"non-registry source {spec!r} without a manifest-pinned immutable identity"
    )


def _manifest_direct_reference(manifest: Mapping[str, object], name: str) -> str:
    specs: list[str] = []
    for section in _DIRECT_DEPENDENCY_SECTIONS:
        values = manifest.get(section)
        if isinstance(values, dict) and name in values:
            specs.append(str(values[name] or "").strip())
    unique = sorted({item for item in specs if item})
    if len(unique) != 1:
        raise SourceIdentityUnavailable(
            "FIXED_SOURCE_IMMUTABLE_IDENTITY_UNAVAILABLE: resolver control "
            f"references ${name} but the direct manifest identity is missing or ambiguous"
        )
    return unique[0]


def _split_yarn_lock_keys(header: str) -> list[str]:
    text = header.strip().rstrip(":").strip()
    result: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote:
            escaped = True
            current.append(char)
            continue
        if char in {"'", '"'}:
            if not quote:
                quote = char
                continue
            if quote == char:
                quote = ""
                continue
        if char == "," and not quote:
            value = "".join(current).strip().strip("'\"")
            if value:
                result.append(value)
            current = []
            continue
        current.append(char)
    value = "".join(current).strip().strip("'\"")
    if value:
        result.append(value)
    return result


def _yarn_scalar(line: str, field: str) -> str:
    stripped = line.strip()
    prefix = field + " "
    if not stripped.startswith(prefix):
        return ""
    return stripped[len(prefix):].strip().strip("'\"")


def _yarn_lock_fixed_record(
    project_dir: Path,
    package_name: str,
    spec: str,
) -> Mapping[str, object]:
    try:
        lock = canonical_lockfile(project_dir)
    except ProjectTopologyError as exc:
        raise SourceIdentityUnavailable(
            f"PROJECT_TOPOLOGY_UNAVAILABLE: {exc}"
        ) from exc
    if lock.name != "yarn.lock" or not lock.is_file():
        return {}
    try:
        lines = lock.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise SourceIdentityUnavailable(
            f"FIXED_SOURCE_LOCKFILE_UNREADABLE: {lock}: {exc}"
        ) from exc

    wanted = f"{package_name}@{spec}"
    keys: list[str] = []
    record: dict[str, object] = {}

    def finish() -> Mapping[str, object]:
        if wanted in keys:
            return dict(record)
        return {}

    for raw in lines:
        if raw and not raw[0].isspace() and raw.rstrip().endswith(":"):
            found = finish()
            if found:
                return found
            keys = _split_yarn_lock_keys(raw)
            record = {}
            continue
        if not keys:
            continue
        for field in ("version", "resolved", "integrity"):
            value = _yarn_scalar(raw, field)
            if value:
                record[field] = value
    return finish()


def _npm_lock_fixed_record(
    project_dir: Path,
    package_name: str,
) -> Mapping[str, object]:
    try:
        lock = canonical_lockfile(project_dir)
    except ProjectTopologyError as exc:
        raise SourceIdentityUnavailable(
            f"PROJECT_TOPOLOGY_UNAVAILABLE: {exc}"
        ) from exc
    if lock.name not in {"npm-shrinkwrap.json", "package-lock.json"}:
        return {}
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise SourceIdentityUnavailable(
            f"FIXED_SOURCE_LOCKFILE_UNREADABLE: {lock}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise SourceIdentityUnavailable(
            f"FIXED_SOURCE_LOCKFILE_INVALID: {lock}: root must be an object"
        )

    packages = payload.get("packages")
    if isinstance(packages, dict):
        entry = packages.get(f"node_modules/{package_name}")
        if isinstance(entry, dict):
            return dict(entry)
    dependencies = payload.get("dependencies")
    if isinstance(dependencies, dict):
        entry = dependencies.get(package_name)
        if isinstance(entry, dict):
            return dict(entry)
    return {}


def _fixed_lock_record(
    project_dir: Path,
    *,
    manager: str,
    package_name: str,
    spec: str,
) -> Mapping[str, object]:
    normalized = str(manager or "").strip().lower()
    if not normalized:
        if (project_dir / "yarn.lock").is_file():
            normalized = "yarn"
        elif (project_dir / "pnpm-lock.yaml").is_file():
            normalized = "pnpm"
        else:
            normalized = "npm"
    if normalized == "yarn":
        return _yarn_lock_fixed_record(project_dir, package_name, spec)
    if normalized == "npm":
        return _npm_lock_fixed_record(project_dir, package_name)
    # pnpm/catalog/other remote-source closure needs a parser for the exact
    # manager lock semantics. Until that exists, authority fails closed rather
    # than reusing proof from the manifest string alone.
    return {}


def _looks_like_git_fixed_source(spec: str, resolved: str) -> bool:
    spec_value = str(spec or "").strip()
    resolved_value = str(resolved or "").strip()
    spec_lower = spec_value.lower()
    resolved_lower = resolved_value.lower()
    return (
        spec_lower.startswith(_GIT_FIXED_PREFIXES)
        or spec_lower.startswith("git@")
        or ".git#" in spec_lower
        or resolved_lower.startswith(("git+", "git://", "ssh://"))
        or resolved_lower.startswith("git@")
        or ".git#" in resolved_lower
        or _GITHUB_SHORTHAND.fullmatch(spec_value) is not None
    )


def _immutable_remote_fixed_identity(
    project_dir: Path,
    *,
    manager: str,
    package_name: str,
    spec: str,
) -> Mapping[str, object]:
    # A manifest-pinned full Git object id is already immutable. Keep the source
    # locator in the aggregate hash, but never persist it outside that hash.
    manifest_commit = _immutable_fragment(spec, _IMMUTABLE_GIT_COMMIT)
    if manifest_commit and _looks_like_git_fixed_source(spec, spec):
        return {"kind": "git-commit", "locator": spec.rsplit("#", 1)[0], "commit": manifest_commit}

    record = _fixed_lock_record(
        project_dir,
        manager=manager,
        package_name=package_name,
        spec=spec,
    )
    resolved = str(record.get("resolved") or "").strip()
    locked_version = str(record.get("version") or "").strip()
    integrity = _content_integrity(record.get("integrity"))

    if _looks_like_git_fixed_source(spec, resolved or locked_version):
        commit = (
            _immutable_fragment(resolved, _IMMUTABLE_GIT_COMMIT)
            or _immutable_fragment(locked_version, _IMMUTABLE_GIT_COMMIT)
            or manifest_commit
        )
        if not commit:
            raise SourceIdentityUnavailable(
                "FIXED_SOURCE_IMMUTABLE_IDENTITY_UNAVAILABLE: "
                f"{package_name}: git source {spec!r} has no resolved immutable commit in the canonical lockfile"
            )
        return {
            "kind": "git-commit",
            "resolved": (resolved or locked_version or spec).rsplit("#", 1)[0],
            "commit": commit,
        }

    # HTTP tarballs, npm aliases and future fixed protocols may be authoritative
    # only when the canonical lockfile supplies a content identity.
    if integrity:
        return {
            "kind": "content-integrity",
            "resolved": resolved,
            "integrity": integrity,
        }

    # A digest fragment is authoritative here only when it came from the
    # canonical lockfile's resolved record. An arbitrary `https://...#deadbeef`
    # manifest fragment is just a URL fragment unless the package manager has
    # recorded it as resolved evidence; treating it as content proof would be
    # unsound.
    digest = _immutable_fragment(resolved, _IMMUTABLE_FRAGMENT_DIGEST)
    if digest:
        return {
            "kind": "resolved-fragment-digest",
            "resolved": resolved.rsplit("#", 1)[0] if resolved else spec.rsplit("#", 1)[0],
            "digest": digest,
        }

    raise SourceIdentityUnavailable(
        "FIXED_SOURCE_IMMUTABLE_IDENTITY_UNAVAILABLE: "
        f"{package_name}: fixed source {spec!r} has no resolved commit/content/integrity identity "
        f"for package manager {str(manager or 'unknown')!r}"
    )


def remote_fixed_resolver_input_fingerprint(
    project_dir: Path,
    *,
    manager: str = "",
) -> str:
    """Hash only remote fixed inputs that can resolve differently during install.

    Local file/link/portal and workspace sources remain bound by the ordinary
    fixedResolverInputsKey on the original checkout. They must not be
    re-identified from a temporary verifier clone because absolute paths and
    the clone's dirty assignment snapshot are intentionally different.
    """
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

    remote: list[Mapping[str, object]] = []
    for section in _DIRECT_DEPENDENCY_SECTIONS:
        values = manifest.get(section)
        if not isinstance(values, dict):
            continue
        for name, raw_spec in sorted(values.items(), key=lambda item: str(item[0]).lower()):
            spec = str(raw_spec or "").strip()
            if not is_fixed_manifest_spec(spec):
                continue
            lowered = spec.lower()
            if lowered.startswith("workspace:") or _looks_like_local_fixed_path(spec):
                continue
            remote.append({
                "section": section,
                "name": str(name),
                "spec": spec,
                "resolvedSource": dict(_immutable_remote_fixed_identity(
                    project_dir,
                    manager=manager,
                    package_name=str(name),
                    spec=spec,
                )),
            })

    controls: list[Mapping[str, object]] = []
    for control_path in _FIXED_SOURCE_CONTROL_PATHS:
        value: object = manifest
        for segment in control_path:
            if not isinstance(value, dict) or segment not in value:
                value = None
                break
            value = value[segment]
        for leaf_path, raw_spec in _string_leaves(value):
            spec = str(raw_spec or "").strip()
            if spec.startswith("$") and len(spec) > 1:
                reference_name = spec[1:]
                referenced_spec = _manifest_direct_reference(manifest, reference_name)
                if (
                    is_fixed_manifest_spec(referenced_spec)
                    and not referenced_spec.lower().startswith("workspace:")
                    and not _looks_like_local_fixed_path(referenced_spec)
                ):
                    controls.append({
                        "path": list((*control_path, *leaf_path)),
                        "spec": spec,
                        "reference": reference_name,
                        "referencedSpec": referenced_spec,
                    })
                continue
            if not is_fixed_manifest_spec(spec):
                continue
            if spec.lower().startswith("workspace:") or _looks_like_local_fixed_path(spec):
                continue
            controls.append({
                "path": list((*control_path, *leaf_path)),
                "spec": spec,
                "identity": dict(_manifest_fixed_control_identity(project_dir, spec)),
            })

    return _canonical_hash(
        {
            "schema": PROOF_SCHEMA_VERSION,
            "toolBuildId": tool_build_id(),
            "remoteFixedResolverInputs": remote,
            "remoteFixedResolverControlInputs": controls,
        },
        length=64,
    )


def fixed_resolver_input_fingerprint(
    project_dir: Path,
    *,
    manager: str = "",
) -> str:
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
            else:
                entry["resolvedSource"] = dict(_immutable_remote_fixed_identity(
                    project_dir,
                    manager=manager,
                    package_name=str(name),
                    spec=spec,
                ))
            fixed.append(entry)

    fixed_controls: list[Mapping[str, object]] = []
    for control_path in _FIXED_SOURCE_CONTROL_PATHS:
        value: object = manifest
        for segment in control_path:
            if not isinstance(value, dict) or segment not in value:
                value = None
                break
            value = value[segment]
        for leaf_path, raw_spec in _string_leaves(value):
            spec = str(raw_spec or "").strip()
            if spec.startswith("$") and len(spec) > 1:
                reference_name = spec[1:]
                referenced_spec = _manifest_direct_reference(manifest, reference_name)
                fixed_controls.append({
                    "path": list((*control_path, *leaf_path)),
                    "spec": spec,
                    "identity": {
                        "kind": "manifest-reference",
                        "name": reference_name,
                        "referencedSpec": referenced_spec,
                    },
                })
                continue
            if not is_fixed_manifest_spec(spec):
                continue
            fixed_controls.append({
                "path": list((*control_path, *leaf_path)),
                "spec": spec,
                "identity": dict(_manifest_fixed_control_identity(project_dir, spec)),
            })

    return _canonical_hash(
        {
            "schema": PROOF_SCHEMA_VERSION,
            "toolBuildId": tool_build_id(),
            "fixedResolverInputs": fixed,
            "fixedResolverControlInputs": fixed_controls,
        },
        length=64,
    )



def source_snapshot_fingerprint(project_dir: Path) -> str:
    """Hash the exact content subject used by authoritative verification."""
    try:
        return captured_source_snapshot_fingerprint(project_dir)
    except SourceCaptureError as exc:
        raise SourceIdentityUnavailable(str(exc)) from exc


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
                try:
                    label = "repo:" + candidate.resolve().relative_to(stop).as_posix()
                except ValueError:
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



def _yarn_config_chain(project_dir: Path, environment: Mapping[str, str]) -> list[Path]:
    project_dir = project_dir.resolve()
    chain: list[Path] = []
    current = project_dir
    while True:
        candidate = current / ".yarnrc"
        if candidate.is_file():
            chain.append(candidate.resolve())
        if current.parent == current:
            break
        current = current.parent
    for key in ("HOME", "USERPROFILE"):
        raw = environment.get(key)
        if not raw:
            continue
        candidate = Path(raw).expanduser() / ".yarnrc"
        if candidate.is_file():
            resolved = candidate.resolve()
            if resolved not in chain:
                chain.append(resolved)
    return chain


def _effective_yarn_config_identity(
    project_dir: Path,
    environment: Mapping[str, str],
    manager: str,
) -> Mapping[str, object]:
    if str(manager).lower() != "yarn":
        return {}
    chain = _yarn_config_chain(project_dir, environment)
    files = [
        (str(path).replace("\\", "/"), _hash_file(path))
        for path in chain
    ]
    yarn_path: Path | None = None
    yarn_path_source = ""
    pattern = re.compile(r"^\s*yarn-path\s+(?:[\"']([^\"']+)[\"']|(\S+))\s*$", re.IGNORECASE)
    # Yarn rc resolution is nearest-config-wins. Scan from filesystem root
    # toward the project so the closest declaration replaces the parent one.
    for path in reversed(chain):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = pattern.match(line)
            if not match:
                continue
            raw = (match.group(1) or match.group(2) or "").strip()
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            yarn_path = candidate.resolve()
            yarn_path_source = str(path).replace("\\", "/")
    delegated: Mapping[str, object] = {}
    if yarn_path is not None:
        delegated = {
            "source": yarn_path_source,
            "path": str(yarn_path).replace("\\", "/"),
            "identity": _version_identity(str(yarn_path), environment),
        }
    return {
        "files": files,
        "delegatedYarnPath": delegated,
    }


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



def _require_hex_authority_key(value: str, name: str, *, length: int = 64) -> str:
    normalized = str(value or "").lower()
    if len(normalized) != length or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"{name}_INVALID")
    return normalized


def _resolver_context_payload(
    project_dir: Path,
    *,
    manager: str,
    manager_executable: str,
    registry: str,
    environment: Mapping[str, str],
) -> dict[str, object]:
    """Canonical effective package-manager context shared by every authority path.

    Assignment/removals deliberately do not live here. Persistent nogoods bind
    to this context; individual resolver proofs then add their exact assignment.
    Keeping one builder prevents a weaker durable-cache identity from drifting
    away from the real package-manager proof identity again.
    """
    # BLOCK_X_SOURCE_TRUTH_V1: once an epoch is active, resolver identity
    # is read from the same sealed source bytes that package-manager verification
    # consumes, not from a live checkout that may change mid-run.
    project_dir = proof_subject_project_dir(project_dir)
    environment_key = environment_snapshot_fingerprint(environment)
    try:
        topology_payload = topology_identity_payload(project_dir)
    except ProjectTopologyError as exc:
        raise SourceIdentityUnavailable(
            f"PROJECT_TOPOLOGY_UNAVAILABLE: {exc}"
        ) from exc
    return {
        "schema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "projectTopology": topology_payload,
        "projectResolverFiles": _resolver_ancestor_files(project_dir),
        "userConfigFiles": _user_config_files(environment),
        "effectiveYarnConfig": _effective_yarn_config_identity(
            project_dir, environment, manager
        ),
        "fixedResolverInputsKey": fixed_resolver_input_fingerprint(project_dir, manager=manager),
        "environmentKey": environment_key,
        "registry": str(registry or "").rstrip("/"),
        "platform": platform.system().lower(),
        "machine": platform.machine().lower(),
        "manager": str(manager).lower(),
        "managerIdentity": _version_identity(manager_executable, environment),
        "nodeIdentity": _node_identity(environment),
        "resolverPolicy": "real-package-manager:scripts-off",
    }


def build_resolver_context_key(
    project_dir: Path,
    *,
    manager: str,
    manager_executable: str,
    registry: str,
    environment: Mapping[str, str],
) -> str:
    payload = _resolver_context_payload(
        project_dir,
        manager=manager,
        manager_executable=manager_executable,
        registry=registry,
        environment=environment,
    )
    return _canonical_hash(
        {
            "schema": RESOLVER_CONTEXT_SCHEMA_VERSION,
            "resolverContext": payload,
        },
        length=64,
    )


def build_resolver_trial_key(
    *,
    resolver_context_key: str,
    assignment: Mapping[str, str],
    remove_packages: Sequence[str],
) -> str:
    context_key = _require_hex_authority_key(
        resolver_context_key, "RESOLVER_CONTEXT_KEY"
    )
    return _canonical_hash(
        {
            "schema": TRIAL_PROOF_KEY_SCHEMA_VERSION,
            "toolBuildId": tool_build_id(),
            "kind": "resolver-trial",
            "resolverContextKey": context_key,
            "assignment": sorted((str(k), str(v)) for k, v in assignment.items()),
            "removals": sorted(str(item) for item in remove_packages),
        },
        length=64,
    )


def build_project_trial_key(
    *,
    resolver_trial_key: str,
    resolved_state_key: str,
    source_snapshot_key: str,
    project_checks: str,
    commands: Sequence[str],
    predicate_identity: str = "",
) -> str:
    resolver_key = _require_hex_authority_key(
        resolver_trial_key, "RESOLVER_TRIAL_KEY"
    )
    state_key = _require_hex_authority_key(
        resolved_state_key, "RESOLVED_STATE_KEY"
    )
    if not str(source_snapshot_key or ""):
        raise ValueError("SOURCE_SNAPSHOT_KEY_INVALID")
    return _canonical_hash(
        {
            "schema": TRIAL_PROOF_KEY_SCHEMA_VERSION,
            "toolBuildId": tool_build_id(),
            "kind": "project-trial",
            "resolverTrialKey": resolver_key,
            "resolvedStateKey": state_key,
            "sourceSnapshotKey": str(source_snapshot_key),
            "projectChecks": str(project_checks),
            "commands": [str(item) for item in commands],
            "predicateIdentity": str(predicate_identity or ""),
            "proofPolicy": "fresh-project-observation-v1",
        },
        length=64,
    )


def _valid_hex_identity(value: object, *, lengths: tuple[int, ...] = (32, 64)) -> bool:
    text = str(value or "").lower()
    return len(text) in lengths and all(ch in "0123456789abcdef" for ch in text)


def _proof_record_identity_valid(
    proof_type: str,
    key: str,
    identity: Mapping[str, str],
) -> bool:
    if identity.get("proofSchema") != PROOF_SCHEMA_VERSION:
        return False
    if identity.get("toolBuildId") != tool_build_id():
        return False
    key_field = {
        "resolver": "resolverInputKey",
        "preparation": "preparationProofKey",
        "project": "projectProofKey",
    }.get(proof_type)
    if key_field is None or identity.get(key_field) != key:
        return False
    for field in (
        "assignmentKey",
        "environmentKey",
        "sourceSnapshotKey",
        "resolverInputKey",
        "preparationProofKey",
        "projectProofKey",
        "localizationExperimentKey",
    ):
        if not _valid_hex_identity(identity.get(field), lengths=(32,)):
            return False
    if not _valid_hex_identity(identity.get("fixedResolverInputsKey"), lengths=(64,)):
        return False
    if not _valid_hex_identity(identity.get("resolvedStateKey"), lengths=(64,)):
        return False
    return True


def _proof_record_metadata_valid(
    metadata: Mapping[str, object],
    identity: Mapping[str, str],
) -> bool:
    observed = metadata.get("observedResolvedVersions")
    observed_hash = metadata.get("observedResolvedHash")
    resolved_state_key = metadata.get("resolvedStateKey")
    resolved_resolver_key = metadata.get("resolvedStateResolverInputKey")
    resolved_manager = metadata.get("resolvedPackageManager")
    resolved_lock_path = metadata.get("resolvedLockfilePath")
    resolved_lock_hash = metadata.get("resolvedLockfileHash")
    resolved_artifact = metadata.get("resolvedStateArtifact")
    resolved_observed_hash = metadata.get("resolvedStateObservedHash")
    return bool(
        isinstance(observed, dict)
        and all(
            isinstance(name, str) and isinstance(version, str)
            for name, version in observed.items()
        )
        and isinstance(observed_hash, str)
        and len(observed_hash) == 64
        and _canonical_hash(dict(sorted(observed.items())), length=64) == observed_hash
        and isinstance(resolved_state_key, str)
        and resolved_state_key == identity.get("resolvedStateKey")
        and _valid_hex_identity(resolved_state_key, lengths=(64,))
        and isinstance(resolved_resolver_key, str)
        and resolved_resolver_key == identity.get("resolverInputKey")
        and isinstance(resolved_manager, str)
        and bool(resolved_manager)
        and isinstance(resolved_lock_path, str)
        and bool(resolved_lock_path)
        and isinstance(resolved_lock_hash, str)
        and _valid_hex_identity(resolved_lock_hash, lengths=(64,))
        and isinstance(resolved_artifact, str)
        and bool(resolved_artifact)
        and isinstance(resolved_observed_hash, str)
        and resolved_observed_hash == observed_hash
    )


@dataclasses.dataclass(frozen=True)
class VerificationProofIdentity:
    schema_version: str
    tool_build_id: str
    assignment_key: str
    environment_key: str
    source_snapshot_key: str
    resolver_input_key: str
    fixed_resolver_inputs_key: str
    resolved_state_key: str
    preparation_proof_key: str
    project_proof_key: str
    localization_experiment_key: str

    def event_fields(self) -> dict[str, str]:
        return {
            "proofSchema": self.schema_version,
            "toolBuildId": self.tool_build_id,
            "assignmentKey": self.assignment_key,
            "environmentKey": self.environment_key,
            "sourceSnapshotKey": self.source_snapshot_key,
            "resolverInputKey": self.resolver_input_key,
            "fixedResolverInputsKey": self.fixed_resolver_inputs_key,
            "resolvedStateKey": self.resolved_state_key,
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
    source_snapshot_key: str = "",
) -> VerificationProofIdentity:
    logical_project_dir = project_dir.resolve()
    active = active_source_snapshot(logical_project_dir)
    project_dir = active.project_path if active is not None else logical_project_dir
    source_key = str(source_snapshot_key or (active.key if active is not None else source_snapshot_fingerprint(logical_project_dir)))
    assignment_key = _canonical_hash({
        "assignment": sorted((str(k), str(v)) for k, v in assignment.items()),
        "removals": sorted(str(item) for item in remove_packages),
    })

    # IMPORTANT: persistent learned constraints and resolver proofs share this
    # exact effective package-manager context builder. The assignment is added
    # only at the resolver-proof layer below.
    resolver_context = _resolver_context_payload(
        project_dir,
        manager=manager,
        manager_executable=manager_executable,
        registry=registry,
        environment=environment,
    )
    environment_key = str(resolver_context["environmentKey"])
    resolver_inputs = {
        **resolver_context,
        "assignmentKey": assignment_key,
    }
    resolver_key = _canonical_hash(resolver_inputs)

    preparation_key = _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "resolverInputKey": resolver_key,
        "resolvedStateKey": "",
        "sourceSnapshotKey": source_key,
        "preparationPolicy": "same-resolved-state:lifecycle-scripts-on:frozen-lockfile",
    })
    project_key = _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "preparationProofKey": preparation_key,
        "sourceSnapshotKey": source_key,
        "projectChecks": project_checks,
        "commands": list(commands),
    })
    localization_key = _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "projectProofKey": project_key,
        "algorithm": "same-origin-localization-v1",
    })
    return VerificationProofIdentity(
        schema_version=PROOF_SCHEMA_VERSION,
        tool_build_id=tool_build_id(),
        assignment_key=assignment_key,
        environment_key=environment_key,
        source_snapshot_key=source_key,
        resolver_input_key=resolver_key,
        fixed_resolver_inputs_key=str(resolver_context["fixedResolverInputsKey"]),
        resolved_state_key="",
        preparation_proof_key=preparation_key,
        project_proof_key=project_key,
        localization_experiment_key=localization_key,
    )



def bind_resolved_state_identity(
    identity: VerificationProofIdentity,
    resolved_state_key: str,
    *,
    project_checks: str,
    commands: Sequence[str],
) -> VerificationProofIdentity:
    resolved_state_key = str(resolved_state_key)
    if len(resolved_state_key) != 64 or any(
        ch not in "0123456789abcdef" for ch in resolved_state_key.lower()
    ):
        raise ValueError("RESOLVED_STATE_KEY_INVALID")
    preparation_key = _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "resolverInputKey": identity.resolver_input_key,
        "resolvedStateKey": resolved_state_key,
        "sourceSnapshotKey": identity.source_snapshot_key,
        "preparationPolicy": "same-resolved-state:lifecycle-scripts-on:frozen-lockfile",
    })
    project_key = _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "preparationProofKey": preparation_key,
        "sourceSnapshotKey": identity.source_snapshot_key,
        "projectChecks": project_checks,
        "commands": list(commands),
    })
    localization_key = _canonical_hash({
        "schema": PROOF_SCHEMA_VERSION,
        "toolBuildId": tool_build_id(),
        "projectProofKey": project_key,
        "algorithm": "same-origin-localization-v1",
    })
    return dataclasses.replace(
        identity,
        resolved_state_key=resolved_state_key,
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
        if payload.get("toolBuildId") != tool_build_id():
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
        if not _proof_record_identity_valid(proof_type, key, identity):
            return None
        metadata = payload.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            return None
        if not _proof_record_metadata_valid(metadata, identity):
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
        identity_fields = identity.event_fields()
        effective_metadata = dict(metadata or {})
        if not _proof_record_identity_valid(proof_type, key, identity_fields):
            return False
        if not _proof_record_metadata_valid(effective_metadata, identity_fields):
            return False
        payload = {
            "schemaVersion": 1,
            "proofSchema": PROOF_SCHEMA_VERSION,
            "toolBuildId": tool_build_id(),
            "proofType": proof_type,
            "key": key,
            "outcome": "passed",
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "identity": identity_fields,
            "metadata": effective_metadata,
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
    emit_observability_event(event, path=path, **fields)

