"""DepLoom Block Z authoritative project topology.

The selected package directory, Git/source root, workspace/package-manager root
and canonical lockfile are separate concepts. Proof and verification code must
never infer one from another by accident.
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from package_manager_profile import (
    PackageManagerProfile,
    PackageManagerProfileError,
    resolve_package_manager_profile,
)

# BLOCK_Z_PROJECT_TOPOLOGY_V1
PROJECT_TOPOLOGY_SCHEMA = "project-topology-v1"
PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH = 5
PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS = frozenset({
    ".git", "node_modules", ".dependency-roadmap", ".dependency-update-history",
    ".next", "dist", "build", "coverage", ".turbo", ".cache",
})
LOCKFILE_NAMES = (
    "yarn.lock",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
)
TOPOLOGY_CONTROL_FILES = (
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    "pnpm-workspace.yaml",
)


class ProjectTopologyError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}")


@dataclasses.dataclass(frozen=True)
class ProjectTopology:
    selected_path: Path
    source_root: Path
    git_root: Path | None
    git_layout: str
    package_root: Path
    workspace_root: Path
    package_manager_root: Path
    lockfile: Path
    package_relative_to_source: Path
    package_relative_to_manager: Path
    workspace_patterns: tuple[str, ...]
    workspace_member_manifests: tuple[Path, ...]
    profile: PackageManagerProfile
    key: str

    @property
    def is_workspace_package(self) -> bool:
        return self.package_root != self.package_manager_root

    def require_authoritative_support(self) -> "ProjectTopology":
        try:
            self.profile.require_authoritative_support()
        except PackageManagerProfileError as exc:
            raise ProjectTopologyError(exc.code, exc.detail) from exc
        return self

    def as_dict(self) -> dict[str, object]:
        def rel(path: Path, root: Path) -> str:
            try:
                return path.resolve().relative_to(root.resolve()).as_posix() or "."
            except ValueError:
                return str(path.resolve()).replace("\\", "/")

        return {
            "schemaVersion": 1,
            "topologySchema": PROJECT_TOPOLOGY_SCHEMA,
            "key": self.key,
            "selectedPath": str(self.selected_path),
            "sourceRoot": str(self.source_root),
            "gitRoot": str(self.git_root) if self.git_root else "",
            "gitLayout": self.git_layout,
            "packageRoot": str(self.package_root),
            "workspaceRoot": str(self.workspace_root),
            "packageManagerRoot": str(self.package_manager_root),
            "canonicalLockfile": str(self.lockfile),
            "packageRelativeToSource": rel(self.package_root, self.source_root),
            "packageRelativeToManager": rel(
                self.package_root, self.package_manager_root
            ),
            "workspacePatterns": list(self.workspace_patterns),
            "workspaceMemberManifests": [
                rel(path, self.package_manager_root)
                for path in self.workspace_member_manifests
            ],
            "isWorkspacePackage": self.is_workspace_package,
            "packageManagerProfile": self.profile.as_dict(),
        }


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ProjectTopologyError(
            "PROJECT_PACKAGE_JSON_INVALID",
            f"{path}: {exc}",
        ) from exc
    if not isinstance(value, dict):
        raise ProjectTopologyError(
            "PROJECT_PACKAGE_JSON_INVALID",
            f"{path}: expected JSON object",
        )
    return value


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProjectTopologyError(
            "PROJECT_TOPOLOGY_INPUT_UNREADABLE", f"{path}: {exc}"
        ) from exc


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def discover_project_package_directories(
    root: Path,
    *,
    max_depth: int = PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH,
) -> tuple[Path, ...]:
    """Find leaf-ish package roots without traversing dependency/build payloads."""
    root = root.expanduser().resolve()
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > max(0, int(max_depth)):
            return
        if directory != root and (directory / "package.json").is_file():
            found.append(directory.resolve())
            # A selected repo with a nested app should not recursively discover
            # every package below that app and turn one project into ambiguity.
            return
        try:
            children = sorted(
                directory.iterdir(), key=lambda item: item.name.lower()
            )
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name in PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS:
                continue
            walk(child, depth + 1)

    walk(root, 0)
    return tuple(sorted(set(found), key=lambda item: str(item).lower()))


def resolve_package_root(
    selected_path: Path,
    *,
    allow_discovery: bool = True,
) -> Path:
    selected = selected_path.expanduser().resolve()
    if not selected.is_dir():
        raise ProjectTopologyError(
            "PROJECT_PATH_MISSING", str(selected)
        )
    if (selected / "package.json").is_file():
        return selected
    if not allow_discovery:
        raise ProjectTopologyError(
            "PROJECT_PACKAGE_JSON_MISSING",
            f"{selected}/package.json",
        )
    candidates = discover_project_package_directories(selected)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ProjectTopologyError(
            "PROJECT_PACKAGE_JSON_MISSING",
            f"no package.json found below {selected}",
        )
    preview = ", ".join(
        candidate.relative_to(selected).as_posix()
        for candidate in candidates[:12]
    )
    if len(candidates) > 12:
        preview += f", ... (+{len(candidates) - 12})"
    raise ProjectTopologyError(
        "PROJECT_PACKAGE_ROOT_AMBIGUOUS",
        f"multiple nested package roots under {selected}: {preview}",
    )


def _git_layout(package_root: Path) -> tuple[Path | None, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(package_root), "rev-parse", "--show-toplevel"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "none"
    if result.returncode != 0 or not result.stdout.strip():
        return None, "none"
    root = Path(result.stdout.strip()).resolve()
    marker = root / ".git"
    if marker.is_file():
        return root, "linked-worktree"
    return root, "worktree"


def _workspace_patterns(manifest: Mapping[str, object], root: Path) -> tuple[str, ...]:
    raw = manifest.get("workspaces")
    values: Sequence[object]
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, dict) and isinstance(raw.get("packages"), list):
        values = raw["packages"]  # type: ignore[index]
    else:
        values = ()
    result = [
        str(value).strip().replace("\\", "/").rstrip("/")
        for value in values
        if str(value).strip()
    ]

    pnpm_workspace = root / "pnpm-workspace.yaml"
    if pnpm_workspace.is_file():
        try:
            text = pnpm_workspace.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        in_packages = False
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not raw_line[:1].isspace():
                in_packages = stripped == "packages:"
                continue
            if in_packages:
                match = re.match(r"^-\s*[\"']?(.+?)[\"']?\s*$", stripped)
                if match:
                    result.append(match.group(1).strip().replace("\\", "/").rstrip("/"))
    return tuple(sorted(set(item for item in result if item)))


def _glob_match(relative: str, pattern: str) -> bool:
    relative = relative.strip("/").replace("\\", "/")
    pattern = pattern.strip("/").replace("\\", "/")
    if not relative or not pattern or pattern.startswith("!"):
        return False
    if fnmatch.fnmatchcase(relative, pattern):
        return True
    # pathlib-style "packages/*" should own a direct member directory.
    if pattern.endswith("/*"):
        prefix = pattern[:-2].rstrip("/")
        return relative.startswith(prefix + "/") and "/" not in relative[len(prefix) + 1:]
    return False


def _workspace_owns(
    manager_root: Path,
    package_root: Path,
    patterns: Sequence[str],
) -> bool:
    try:
        relative = package_root.resolve().relative_to(manager_root.resolve()).as_posix()
    except ValueError:
        return False
    positives = [item for item in patterns if not item.startswith("!")]
    negatives = [item[1:] for item in patterns if item.startswith("!")]
    return (
        any(_glob_match(relative, item) for item in positives)
        and not any(_glob_match(relative, item) for item in negatives)
    )


def _lockfiles(directory: Path) -> tuple[Path, ...]:
    return tuple(
        (directory / name).resolve()
        for name in LOCKFILE_NAMES
        if (directory / name).is_file()
    )


def _lock_family(path: Path) -> str:
    if path.name == "yarn.lock":
        return "yarn"
    if path.name == "pnpm-lock.yaml":
        return "pnpm"
    return "npm"


def _select_owned_lockfile(
    package_root: Path,
    *,
    source_boundary: Path,
) -> tuple[Path, Path, tuple[str, ...]]:
    """Find the canonical package-manager root, never an unrelated ancestor."""
    current = package_root.resolve()
    boundary = source_boundary.resolve()

    while True:
        found = _lockfiles(current)
        if found:
            families = {_lock_family(path) for path in found}
            if len(families) > 1:
                raise ProjectTopologyError(
                    "PROJECT_LOCKFILE_AMBIGUOUS",
                    f"{current}: " + ", ".join(path.name for path in found),
                )

            manifest_path = current / "package.json"
            if not manifest_path.is_file():
                raise ProjectTopologyError(
                    "PROJECT_PACKAGE_MANAGER_ROOT_MANIFEST_MISSING",
                    str(manifest_path),
                )
            manifest = _read_json(manifest_path)
            patterns = _workspace_patterns(manifest, current)
            if current == package_root:
                # package-lock + shrinkwrap belong to the same npm family;
                # shrinkwrap wins intentionally.
                if len(found) > 1:
                    chosen = next(
                        (path for path in found if path.name == "npm-shrinkwrap.json"),
                        found[0],
                    )
                else:
                    chosen = found[0]
                return current, chosen, patterns

            if _workspace_owns(current, package_root, patterns):
                if len(found) > 1:
                    chosen = next(
                        (path for path in found if path.name == "npm-shrinkwrap.json"),
                        found[0],
                    )
                else:
                    chosen = found[0]
                return current, chosen, patterns
            # An ancestor lockfile that does not own this package as a workspace
            # is unrelated. Keep walking only to discover an even-higher owner.

        if current == boundary or current.parent == current:
            break
        if boundary not in current.parents and current != boundary:
            break
        current = current.parent

    raise ProjectTopologyError(
        "PROJECT_CANONICAL_LOCKFILE_MISSING",
        f"no owning npm/Yarn/pnpm lockfile found for {package_root} up to {boundary}",
    )


def _workspace_member_manifests(
    manager_root: Path,
    patterns: Sequence[str],
) -> tuple[Path, ...]:
    root_manifest = (manager_root / "package.json").resolve()
    result: set[Path] = {root_manifest}
    if not patterns:
        return (root_manifest,)

    # Bound discovery to package manifests and prune dependency/build payloads.
    for current, dirs, files in os.walk(manager_root):
        current_path = Path(current)
        try:
            relative_dir = current_path.resolve().relative_to(manager_root.resolve())
        except ValueError:
            dirs[:] = []
            continue
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS
        )
        if "package.json" not in files or current_path == manager_root:
            continue
        relative = relative_dir.as_posix()
        if _workspace_owns(manager_root, current_path, patterns):
            result.add((current_path / "package.json").resolve())

    return tuple(sorted(result, key=lambda item: str(item).lower()))


def _topology_key(
    *,
    source_root: Path,
    package_root: Path,
    manager_root: Path,
    lockfile: Path,
    patterns: Sequence[str],
    manifests: Sequence[Path],
    profile: PackageManagerProfile,
) -> str:
    def relative(path: Path) -> str:
        try:
            return path.resolve().relative_to(source_root.resolve()).as_posix() or "."
        except ValueError:
            return str(path.resolve()).replace("\\", "/")

    manifest_inputs = [
        {
            "path": relative(path),
            "sha256": _sha(path),
        }
        for path in manifests
    ]
    controls = []
    for name in TOPOLOGY_CONTROL_FILES:
        candidate = manager_root / name
        if candidate.is_file():
            controls.append({
                "path": relative(candidate),
                "sha256": _sha(candidate),
            })

    return _canonical_hash({
        "schema": PROJECT_TOPOLOGY_SCHEMA,
        "packageRoot": relative(package_root),
        "packageManagerRoot": relative(manager_root),
        "lockfile": {
            "path": relative(lockfile),
            "sha256": _sha(lockfile),
        },
        "workspacePatterns": list(patterns),
        "workspaceManifests": manifest_inputs,
        "controls": controls,
        "packageManagerProfileKey": profile.key,
    })


def resolve_project_topology(
    selected_path: Path,
    *,
    allow_discovery: bool = True,
    require_supported: bool = False,
) -> ProjectTopology:
    selected = selected_path.expanduser().resolve()
    package_root = resolve_package_root(
        selected,
        allow_discovery=allow_discovery,
    )
    git_root, git_layout = _git_layout(package_root)

    if git_root is not None:
        source_root = git_root
        boundary = git_root
    elif package_root != selected and allow_discovery:
        # The selected repository/directory is the explicit source boundary.
        source_root = selected
        boundary = selected
    else:
        # Without Git and without an explicit selected ancestor, never walk into
        # arbitrary parent package-manager state.
        source_root = package_root
        boundary = package_root

    try:
        package_relative_to_source = package_root.relative_to(source_root)
    except ValueError as exc:
        raise ProjectTopologyError(
            "PROJECT_PACKAGE_OUTSIDE_SOURCE_ROOT",
            f"package={package_root}, source={source_root}",
        ) from exc

    manager_root, lockfile, patterns = _select_owned_lockfile(
        package_root,
        source_boundary=boundary,
    )
    root_manifest = _read_json(manager_root / "package.json")
    target_manifest = _read_json(package_root / "package.json")
    profile = resolve_package_manager_profile(
        manager_root,
        package_json=root_manifest,
        lockfile=lockfile,
    )

    target_declared = str(target_manifest.get("packageManager") or "").strip()
    if target_declared and package_root != manager_root:
        target_manager = target_declared.split("@", 1)[0].strip().lower()
        if target_manager and target_manager != profile.manager:
            raise ProjectTopologyError(
                "PROJECT_WORKSPACE_PACKAGE_MANAGER_CONFLICT",
                f"target packageManager={target_declared!r}, root family={profile.manager}",
            )

    if package_root != manager_root and not _workspace_owns(
        manager_root, package_root, patterns
    ):
        raise ProjectTopologyError(
            "PROJECT_PACKAGE_NOT_OWNED_BY_WORKSPACE",
            f"{package_root} is below {manager_root} but not selected by workspaces={patterns!r}",
        )

    try:
        package_relative_to_manager = package_root.relative_to(manager_root)
    except ValueError as exc:
        raise ProjectTopologyError(
            "PROJECT_PACKAGE_OUTSIDE_PACKAGE_MANAGER_ROOT",
            f"package={package_root}, managerRoot={manager_root}",
        ) from exc

    manifests = _workspace_member_manifests(manager_root, patterns)
    if (package_root / "package.json").resolve() not in manifests:
        manifests = tuple(sorted(
            {*manifests, (package_root / "package.json").resolve()},
            key=lambda item: str(item).lower(),
        ))

    key = _topology_key(
        source_root=source_root,
        package_root=package_root,
        manager_root=manager_root,
        lockfile=lockfile,
        patterns=patterns,
        manifests=manifests,
        profile=profile,
    )
    topology = ProjectTopology(
        selected_path=selected,
        source_root=source_root,
        git_root=git_root,
        git_layout=git_layout,
        package_root=package_root,
        workspace_root=manager_root,
        package_manager_root=manager_root,
        lockfile=lockfile,
        package_relative_to_source=package_relative_to_source,
        package_relative_to_manager=package_relative_to_manager,
        workspace_patterns=tuple(patterns),
        workspace_member_manifests=manifests,
        profile=profile,
        key=key,
    )
    if require_supported:
        topology.require_authoritative_support()
    return topology


def topology_identity_payload(project_dir: Path) -> dict[str, object]:
    """Return topology identity without confusing identity with authority.

    Resolver/constraint identities are also built during planning and for fixed
    local-source inputs before a canonical lockfile necessarily exists. In that
    state we still need a deterministic identity, but it must be visibly
    *unbound* and can never authorize package-manager verification.

    Authoritative Baseline separately calls resolve_project_topology(...,
    require_supported=True), so missing/ambiguous/unsupported PM topology still
    fails closed before any ResolverProof can be produced.
    """
    project_dir = project_dir.expanduser().resolve()
    try:
        topology = resolve_project_topology(
            project_dir,
            allow_discovery=False,
            require_supported=True,
        )
    except ProjectTopologyError as exc:
        if exc.code != "PROJECT_CANONICAL_LOCKFILE_MISSING":
            raise

        package_root = resolve_package_root(
            project_dir,
            allow_discovery=False,
        )
        git_root, git_layout = _git_layout(package_root)
        source_root = git_root or package_root
        manifest_path = (package_root / "package.json").resolve()
        manifest = _read_json(manifest_path)
        patterns = _workspace_patterns(manifest, package_root)
        manifests = _workspace_member_manifests(package_root, patterns)
        if manifest_path not in manifests:
            manifests = tuple(sorted(
                {*manifests, manifest_path},
                key=lambda item: str(item).lower(),
            ))

        def rel(path: Path) -> str:
            try:
                return (
                    path.resolve()
                    .relative_to(source_root.resolve())
                    .as_posix()
                    or "."
                )
            except ValueError:
                return str(path.resolve()).replace("\\", "/")

        controls: list[dict[str, str]] = []
        for name in TOPOLOGY_CONTROL_FILES:
            candidate = package_root / name
            if candidate.is_file():
                controls.append({
                    "path": rel(candidate),
                    "sha256": _sha(candidate),
                })

        declared = str(manifest.get("packageManager") or "").strip()
        declared_manager = (
            declared.split("@", 1)[0].strip().lower()
            if declared
            else ""
        )
        identity_subject = {
            "schema": PROJECT_TOPOLOGY_SCHEMA,
            "authorityState": "unbound-no-canonical-lockfile",
            "sourceRootKind": "git" if git_root is not None else "package",
            "gitLayout": git_layout,
            "packageRoot": rel(package_root),
            "packageManagerRoot": "",
            "canonicalLockfile": "",
            "workspacePatterns": list(patterns),
            "workspaceManifests": [
                {
                    "path": rel(path),
                    "sha256": _sha(path),
                }
                for path in manifests
            ],
            "controls": controls,
            "declaredPackageManager": declared,
            "declaredManager": declared_manager,
        }
        key = _canonical_hash(identity_subject)
        return {
            "schema": PROJECT_TOPOLOGY_SCHEMA,
            "authorityState": "unbound-no-canonical-lockfile",
            "key": key,
            "packageRelativeToSource": rel(package_root),
            "packageManagerRoot": "",
            "packageRelativeToManager": "",
            "canonicalLockfile": "",
            "workspacePatterns": list(patterns),
            "workspaceManifests": identity_subject["workspaceManifests"],
            "profile": {
                "schemaVersion": 1,
                "profileSchema": "package-manager-profile-v1",
                "manager": declared_manager,
                "family": declared_manager or "unbound",
                "declared": declared,
                "declaredVersion": (
                    declared.split("@", 1)[1]
                    if "@" in declared
                    else ""
                ),
                "lockfileName": "",
                "nodeLinker": "",
                "authoritativeSupported": False,
                "unsupportedCode": "PROJECT_CANONICAL_LOCKFILE_MISSING",
                "unsupportedDetail": (
                    "identity-only topology: canonical package-manager "
                    "lockfile is not bound yet"
                ),
            },
        }

    root = topology.source_root

    def rel(path: Path) -> str:
        try:
            return (
                path.resolve()
                .relative_to(root.resolve())
                .as_posix()
                or "."
            )
        except ValueError:
            return str(path.resolve()).replace("\\", "/")

    return {
        "schema": PROJECT_TOPOLOGY_SCHEMA,
        "authorityState": "bound",
        "key": topology.key,
        "packageRelativeToSource": rel(topology.package_root),
        "packageManagerRoot": rel(topology.package_manager_root),
        "packageRelativeToManager": (
            topology.package_relative_to_manager.as_posix() or "."
        ),
        "canonicalLockfile": rel(topology.lockfile),
        "workspacePatterns": list(topology.workspace_patterns),
        "workspaceManifests": [
            {
                "path": rel(path),
                "sha256": _sha(path),
            }
            for path in topology.workspace_member_manifests
        ],
        "profile": topology.profile.as_dict(),
    }

def package_manager_cwd(project_dir: Path) -> Path:
    return resolve_project_topology(
        project_dir,
        allow_discovery=False,
        require_supported=True,
    ).package_manager_root


def canonical_lockfile(project_dir: Path) -> Path:
    return resolve_project_topology(
        project_dir,
        allow_discovery=False,
        require_supported=False,
    ).lockfile


def semantic_manifest_paths(project_dir: Path) -> tuple[Path, ...]:
    topology = resolve_project_topology(
        project_dir,
        allow_discovery=False,
        require_supported=False,
    )
    return topology.workspace_member_manifests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-supported", action="store_true")
    args = parser.parse_args()
    try:
        topology = resolve_project_topology(
            args.path,
            allow_discovery=True,
            require_supported=args.require_supported,
        )
    except ProjectTopologyError as exc:
        if args.json:
            print(json.dumps({
                "ok": False,
                "code": exc.code,
                "detail": exc.detail,
            }, ensure_ascii=False, sort_keys=True))
        else:
            print(str(exc))
        return 2
    payload = topology.as_dict()
    payload["ok"] = True
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"{topology.package_root} -> {topology.package_manager_root} "
            f"[{topology.profile.family}] {topology.lockfile.name}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
