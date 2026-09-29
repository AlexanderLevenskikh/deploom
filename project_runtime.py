"""Explicit project/CI Node runtime: discovery, resolution and contract.

D2-D4: the user may OPTIONALLY pin the Node.js version their project/CI must
run under. An empty setting means "not set by the user" — never "compatible
with any Node" and never an automatic latest choice. When set, every install /
precheck / verification / audit / project-check child must use EXACTLY that
runtime (its node and its npm), resolved by a real version probe — a text note
in a prompt or an env field that is never applied does not count.

This module stays producer-side and deterministic:

- ``discover_node_runtimes`` probes the installed executables by actually
  running ``node --version`` (a path alone is not a runtime: its content or
  version can change), returning exact versions.
- ``resolve_requested_node`` turns the requested value into a concrete
  executable+exact-version pair. A requested version that is not found is
  ``ENVIRONMENT_UNAVAILABLE`` — there is deliberately NO silent fallback to
  PATH. A major/alias is resolved to the exact installed version.
- ``node_range_satisfied`` matches an ``engines.node`` range (including ``||``,
  minimal minor/patch and prerelease semantics) against an exact version with
  npm semver, never custom string comparison.
- ``runtime_contract`` hashes the effective runtime so identity-bearing layers
  (task, checkpoint, verification, cache) can bind to the exact toolchain.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:  # pragma: no cover - import parity with dependency_live_roadmap_generator
    from semantic_version import NpmSpec, Version  # type: ignore
except Exception:  # pragma: no cover
    NpmSpec = None  # type: ignore
    Version = None  # type: ignore

NODE_VERSION_PATTERN = re.compile(r"(?:^|[^0-9])(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?:[^0-9]|$)")

DISCOVERY_DIR_ENV = (
    ("NVM_SYMLINK", ""),
    ("NVM_HOME", ""),
    ("VOLTA_HOME", "bin"),
    ("FNM_MULTISHELL_PATH", ""),
    ("APPDATA", "npm"),
)

NODE_EXECUTABLE_NAMES = ("node.exe", "node") if os.name == "nt" else ("node",)


def safe_version(value: str) -> Optional["Version"]:
    if Version is None:
        return None
    try:
        return Version.coerce(str(value), partial=False)
    except Exception:
        return None


def normalize_node_version(value: Any) -> str:
    """Exact dotted version from arbitrary node text ("v22.18.0\n" -> "22.18.0")."""
    match = NODE_VERSION_PATTERN.search(str(value or "").strip())
    return match.group(1) if match else ""


def _candidate_directories() -> List[Path]:
    values: List[Path] = []
    for key, suffix in DISCOVERY_DIR_ENV:
        raw = os.environ.get(key)
        if raw:
            values.append(Path(raw) / suffix if suffix else Path(raw))
    if os.name == "nt":
        for key in ("ProgramFiles", "LOCALAPPDATA"):
            raw = os.environ.get(key)
            if raw:
                values.append(Path(raw) / "nodejs" if key == "ProgramFiles" else Path(raw) / "Programs" / "nodejs")
        user = os.environ.get("USERPROFILE")
        if user:
            values.append(Path(user) / "scoop" / "shims")
    else:
        values.append(Path("/usr/local/bin"))
        values.append(Path("/usr/bin"))
    return values


def probe_node_version(executable: str, timeout_seconds: int = 5) -> str:
    """Run ``node --version`` for the executable; exact version or ''."""
    path = Path(executable)
    argv: List[str]
    if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        argv = [comspec, "/d", "/s", "/c", subprocess.list2cmdline([str(path), "--version"])]
    else:
        argv = [str(path), "--version"]
    try:
        completed = subprocess.run(
            argv,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (completed.stdout or "").strip().splitlines()
    if not first:
        return ""
    return normalize_node_version(first[0])


def discover_node_runtimes(
    *,
    include_path: bool = True,
    timeout_seconds: int = 5,
) -> List[Tuple[str, str]]:
    """(executable, exact_version) for every reachable installed Node runtime.

    Running the binary is the only honest probe — a path whose content/version
    changed must not be reported as the old runtime. Results are deduplicated by
    the resolved executable path and sorted by version.
    """
    seen: Dict[str, str] = {}
    directories: List[Path] = []
    if include_path:
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if directory:
                directories.append(Path(directory))
    directories.extend(_candidate_directories())
    for directory in directories:
        for name in NODE_EXECUTABLE_NAMES:
            candidate = directory / name
            if not candidate.is_file():
                continue
            key = os.path.normcase(str(candidate.resolve()))
            if key in seen:
                continue
            version = probe_node_version(str(candidate), timeout_seconds=timeout_seconds)
            if version:
                seen[key] = version
            else:
                seen[key] = ""
    result = [
        (executable, version)
        for executable, version in seen.items()
        if version
    ]
    result.sort(
        key=lambda item: (safe_version(item[1]) or _ZeroVersion(), item[0])
    )
    return result


class _ZeroVersion:
    def __lt__(self, other: object) -> bool:  # pragma: no cover - simple sentinel
        return True


@dataclass(frozen=True)
class NodeRuntimeResolution:
    requested: str
    effective_version: str
    node_path: str
    npm_path: str
    source: str
    found: bool = True
    available_versions: Tuple[str, ...] = ()
    unavailable_reason: str = ""

    def contract_hash(self) -> str:
        return _contract_hash(
            requested=self.requested,
            effective_version=self.effective_version,
            node_path=self.node_path,
            npm_path=self.npm_path,
            source=self.source,
        )

    def to_json(self) -> Dict[str, object]:
        return {
            "requested": self.requested,
            "effectiveVersion": self.effective_version,
            "nodePath": self.node_path,
            "npmPath": self.npm_path,
            "source": self.source,
            "found": self.found,
            "availableVersions": list(self.available_versions),
            "unavailableReason": self.unavailable_reason,
            "contractHash": self.contract_hash(),
        }


def _contract_hash(**fields: str) -> str:
    payload = json.dumps(dict(sorted(fields.items())), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def npm_sibling(node_path: str) -> str:
    """The package manager for the same runtime directory as node.

    On Windows npm ships as npm.cmd next to node.exe; prepending that directory
    to PATH makes npm/yarn/pnpm and every project child resolve THIS node.
    """
    directory = Path(node_path).parent
    for name in ("npm.cmd", "npm") if os.name == "nt" else ("npm",):
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return ""


def requested_is_path(value: str) -> bool:
    text = str(value).strip()
    return bool(text) and ("/" in text or "\\" in text) and Path(text).expanduser().exists()


def resolve_requested_node(
    requested: str,
    *,
    discovered: Optional[Sequence[Tuple[str, str]]] = None,
) -> NodeRuntimeResolution:
    """Resolve the requested Node value to ONE concrete installed runtime.

    Empty stays unresolved (None is returned by the caller's convention); an
    unavailable requested version is an explicit ENVIRONMENT_UNAVAILABLE result,
    never a PATH fallback. major/alias (``22``) resolves to the exact installed
    version so the saved value is precise (D2.3).
    """
    text = str(requested or "").strip()
    if not text:
        return NodeRuntimeResolution(
            requested="", effective_version="", node_path="", npm_path="",
            source="", found=False, unavailable_reason="EMPTY",
        )
    runtimes = list(discovered) if discovered is not None else discover_node_runtimes()
    if requested_is_path(text):
        version = probe_node_version(text)
        if version:
            return NodeRuntimeResolution(
                requested=text, effective_version=version,
                node_path=str(Path(text).resolve()),
                npm_path=npm_sibling(text), source="explicit-path",
            )
        return NodeRuntimeResolution(
            requested=text, effective_version="", node_path="", npm_path="",
            source="explicit-path", found=False,
            available_versions=tuple(v for _, v in runtimes),
            unavailable_reason=f"probe of {text} failed or produced no version",
        )
    exact = normalize_node_version(text)
    available = tuple(sorted({version for _, version in runtimes}))
    for executable, version in runtimes:
        if version == exact:
            return NodeRuntimeResolution(
                requested=text, effective_version=exact,
                node_path=executable, npm_path=npm_sibling(executable),
                source="installed-exact",
            )
    # major/alias: resolve to the exact installed version, never invented.
    major = exact.split(".", 1)[0] if exact else (text if re.fullmatch(r"\d+", text) else "")
    if major:
        candidates = sorted(
            ((safe_version(version), version, executable) for executable, version in runtimes
             if version.split(".", 1)[0] == major),
            key=lambda item: (item[0] is None, item[0] or _ZeroVersion(), item[1]),
        )
        if candidates:
            _sort, version, executable = candidates[-1]
            return NodeRuntimeResolution(
                requested=text, effective_version=version,
                node_path=executable, npm_path=npm_sibling(executable),
                source="major-alias",
            )
    return NodeRuntimeResolution(
        requested=text, effective_version="", node_path="", npm_path="",
        source="requested", found=False,
        available_versions=available,
        unavailable_reason=(
            f"no installed Node matches {text!r}; discovered: "
            + (", ".join(available) if available else "none")
        ),
    )


def runtime_env(
    resolution: NodeRuntimeResolution,
    base_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Child environment carrying EXACTLY the resolved runtime.

    The node directory is prepended to PATH (and mirrored into ``path`` on
    Windows) so every spawned npm/yarn/script process resolves the same node.
    """
    if not resolution.found or not resolution.node_path:
        return dict(base_env) if base_env is not None else dict(os.environ)
    node_dir = str(Path(resolution.node_path).parent)
    source = dict(base_env) if base_env is not None else dict(os.environ)
    path_key = "PATH" if "PATH" in source else "Path"
    if "PATH" in source:
        path_key = "PATH"
    elif "Path" in source:
        path_key = "Path"
    current = source.get(path_key) or ""
    entries = [entry for entry in current.split(os.pathsep) if entry]
    merged = [node_dir] + [entry for entry in entries if os.path.normcase(entry) != os.path.normcase(node_dir)]
    source[path_key] = os.pathsep.join(merged)
    if os.name == "nt":
        lower_keys = {key.lower(): key for key in source}
        path_lower = lower_keys.get("path")
        if path_lower and path_lower not in (path_key,):
            source[path_lower] = source[path_key]
    return source


def runtime_env_for_path(
    node_path: str,
    base_env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Child env with the directory of ``node_path`` prepended to PATH/path.

    Used by consumers that already persisted a resolved runtime block and must
    re-apply it without re-probing the executable (D3.3: the contract stores
    requested/effective/manager/platform, not just one path).
    """
    node_dir = str(Path(node_path).parent)
    source = dict(base_env) if base_env is not None else dict(os.environ)
    path_key = "PATH" if "PATH" in source else ("Path" if "Path" in source else "PATH")
    current = source.get(path_key) or ""
    entries = [entry for entry in current.split(os.pathsep) if entry]
    merged = [node_dir] + [entry for entry in entries if os.path.normcase(entry) != os.path.normcase(node_dir)]
    source[path_key] = os.pathsep.join(merged)
    if os.name == "nt":
        lower_keys = {key.lower(): key for key in source}
        path_lower = lower_keys.get("path")
        if path_lower and path_lower not in (path_key,):
            source[path_lower] = source[path_key]
    return source


def node_range_satisfied(range_spec: str, exact_version: str) -> bool:
    """npm-semver match of an engines.node range against an exact version.

    Handles ``||``, minimal minor/patch and prerelease semantics via
    ``semantic_version.NpmSpec``; unknown/non-semver declarations are NOT
    treated as a match (unverified compatibility, never silently green).
    """
    if NpmSpec is None or Version is None:  # pragma: no cover - import parity
        return False
    parsed = safe_version(exact_version)
    if parsed is None:
        return False
    try:
        return bool(NpmSpec(str(range_spec)).match(parsed))
    except ValueError:
        return False


def engine_mismatch_detail(
    package: str,
    version: str,
    range_spec: str,
    runtime: NodeRuntimeResolution,
) -> Dict[str, str]:
    """Structured D4.3 diagnosis: package/version/range/effective runtime/source."""
    return {
        "kind": "NODE_ENGINE_MISMATCH",
        "package": package,
        "version": version,
        "requiredNodeRange": str(range_spec or ""),
        "requestedNode": runtime.requested,
        "effectiveNode": runtime.effective_version,
        "nodeSource": runtime.source,
        "evidence": "registry metadata engines.node",
    }


def installed_runtimes_json() -> str:
    """Stable JSON for the Desktop list-node-versions IPC (no private data)."""
    return json.dumps(
        [
            {"path": executable, "version": version}
            for executable, version in discover_node_runtimes()
        ],
        ensure_ascii=False,
    )


__all__ = [
    "NodeRuntimeResolution",
    "discover_node_runtimes",
    "engine_mismatch_detail",
    "installed_runtimes_json",
    "node_range_satisfied",
    "normalize_node_version",
    "npm_sibling",
    "probe_node_version",
    "resolve_requested_node",
    "runtime_env",
    "runtime_env_for_path",
    "safe_version",
]


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv) if argv is not None else list(sys.argv[1:])
    if "--list-runtimes" in args:
        print(installed_runtimes_json())
        return 0
    print(__doc__ or "project_runtime: Node runtime discovery/resolution for DepLoom", file=sys.stderr)
    print("usage: project_runtime.py --list-runtimes", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
