"""Immutable, hash-bound resolution inventory for one migration phase.

    Parser and index the canonical lockfile exactly ONCE per discovery/materialize phase and
    serves ``installed_version`` lookups from memory instead of re-reading and
    re-parsing the lockfile for every dependency probe (C1). The inventory is
    read-only and bound to the exact bytes it was built from: ``lock_hash`` is the
    SHA-256 of the raw lockfile BYTES (a UTF-8 BOM included) and any lockfile
    change invalidates the phase — a changed input can never be served stale
    resolutions.

Workspace-aware npm resolution (A1): a member package of an npm-workspaces
lockfile (lockfileVersion 3) records its direct dependencies under
``<member>/node_modules/<name>``; the member-local key is consulted first, then
the root ``node_modules/<name>``, then a ``link:``/``file:`` portal entry is
resolved through the linked package's own record. Under a legacy
``dependencies`` map (no ``packages``) a member cannot prove its own
resolution from the root map, so it abstains (None) rather than guessing.

Semantics mirror the reference helpers in ``iterative_migration``
(``_npm_lock_installed_version``, ``_yarn_lock_installed_version`` —
including yarn v1 direct-selector selection and Berry selector mapping — and
``_pnpm_lock_installed_version``); the lockfile is just parsed once up front,
and the small pure selector helpers are imported lazily from that module so
there is exactly one implementation. No registry, git or tool subprocess is
spawned here; a lockfile that fails to parse yields an effectively empty
inventory, i.e. whole-phase abstention — never an invented version.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from project_topology import ProjectTopologyError, resolve_project_topology

# Shrinkwrap-first, npm-family-first order for the local fallback (B2): when a
# project outside a resolvable topology carries several lockfiles, the npm
# family wins and, within it, the shrinkwrap is the more authoritative record.
_LOCAL_LOCKFILE_NAMES = (
    "npm-shrinkwrap.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)


def _normalize_path(project_dir: object) -> Optional[Path]:
    try:
        return Path(project_dir).expanduser().resolve()  # type: ignore[arg-type]
    except (OSError, TypeError, ValueError):
        return None


def _find_local_lockfile(project_dir: Path) -> Optional[Path]:
    for name in _LOCAL_LOCKFILE_NAMES:
        candidate = project_dir / name
        if candidate.is_file():
            return candidate
    return None


def _canonical_for_project(project_dir: Path) -> Tuple[Optional[Path], str]:
    """The canonical lockfile governing ``project_dir`` and its relative path.

    For a workspace member the canonical lockfile is the SHARED root lockfile of
    the manager package (a bare member directory rarely carries its own
    lockfile), and ``package_relative`` is the member's path inside the manager.
    Outside a resolvable topology a local, shrinkwrap-first look is the
    fallback with an empty relative path.
    """
    try:
        topology = resolve_project_topology(
            project_dir, allow_discovery=False, require_supported=False
        )
        lockfile = topology.lockfile
        if lockfile is not None and lockfile.is_file():
            return lockfile, topology.package_relative_to_manager.as_posix() or ""
    except ProjectTopologyError:
        pass
    # Without Git, the topology boundary is the directory itself, so a workspace
    # MEMBER resolves to nothing (``boundary == package_root`` never walks to
    # the manager root). Recover the shared root lockfile with a lightweight
    # owner walk over the ancestors' workspace patterns.
    shared = _shared_root_lockfile(project_dir)
    if shared is not None:
        return shared
    lock = _find_local_lockfile(project_dir)
    if lock is None:
        return None, ""
    return lock, ""


def canonical_lockfile_path(project_dir: object) -> Optional[Path]:
    """The canonical lockfile path governing a project, or ``None``.

    Single source of the canonical (workspace-root when bound) lockfile for
    hashing/identity (A1/B1): a workspace MEMBER's identity must react to the
    shared root lockfile, never to a strictly-local look that returns nothing.
    """
    resolved = _normalize_path(project_dir)
    if resolved is None:
        return None
    lock, _relative = _canonical_for_project(resolved)
    return lock


def _shared_root_lockfile(
    project_dir: Path,
) -> Optional[Tuple[Path, str]]:
    """The nearest ancestor that declares a workspace owning ``project_dir``
    and carries a lockfile (non-git member recovery).

    Mirrors ``project_topology._select_owned_lockfile``'s workspace-owner walk
    for the case topology cannot reach (a member directory outside a Git
    source root). Ownership is decided by the same ``_workspace_patterns`` /
    ``_workspace_owns`` helpers so brace/glob semantics stay identical. Returns
    ``(lockfile, package_relative_to_manager)`` or None.
    """
    from project_topology import _workspace_owns, _workspace_patterns

    resolved = _normalize_path(project_dir)
    if resolved is None:
        return None
    current: Optional[Path] = resolved
    while current is not None:
        manifest_path = current / "package.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                patterns = _workspace_patterns(manifest if isinstance(manifest, dict) else {}, current)
            except (OSError, ValueError, ProjectTopologyError):
                patterns = ()
            if patterns:
                lock = _find_local_lockfile(current)
                if lock is not None and _workspace_owns(current, resolved, patterns):
                    try:
                        relative = resolved.relative_to(current.resolve()).as_posix()
                    except ValueError:
                        relative = ""
                    return lock, relative
        parent = current.parent
        current = parent if parent != current else None
    return None


def _looks_like_berry_lock(text: str) -> bool:
    return any(line.startswith("__metadata:") for line in text.splitlines()[:80])


def _split_yarn_selectors(value: str) -> List[str]:
    """Split a yarn.lock block key (one or more selectors separated by commas,
    each optionally quoted) into individual selectors."""
    parts: List[str] = []
    buf: List[str] = []
    quote = ""
    for ch in str(value or ""):
        if ch in ('"', "'"):
            if not quote:
                quote = ch
            elif quote == ch:
                quote = ""
            else:
                buf.append(ch)
            continue
        if ch == "," and not quote:
            if "".join(buf).strip():
                parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def _scan_yarn_v1_blocks(lines: Sequence[str]) -> List[Tuple[List[str], str]]:
    """``[(selector_keys, version), ...]`` for every versioned yarn.lock v1
    block, parsed once; selector matching happens at lookup time."""
    blocks: List[Tuple[List[str], str]] = []
    keys: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not line.startswith(" "):
            # A block header: a line ending with ':' carries the selectors;
            # other top-level lines (e.g. "# yarn lockfile v1") are keyless and
            # can not record a version.
            keys = _split_yarn_selectors(stripped[:-1]) if stripped.endswith(":") else []
            continue
        match = re.match(r'\s*version\s+"([^"]+)"', line)
        if match and keys:
            blocks.append((list(keys), match.group(1)))
    return blocks


def _member_ancestor_prefixes(package_relative: str) -> List[str]:
    """All npm-lock package key prefixes that can carry a resolution for a
    member: the member itself, every intermediate directory, and the root.

    npm hoists a dependency into the nearest ancestor ``node_modules``, so for
    ``packages/team/app`` a package can resolve from
    ``packages/team/app/node_modules/<name>``, ``packages/team/node_modules/<name>``,
    ``packages/node_modules/<name>`` or the root ``node_modules/<name>``. The
    innermost (nearest) present resolution wins.
    """
    parts = [part for part in (package_relative or "").replace("\\", "/").strip("/").split("/") if part]
    return ["/".join(parts[:index]) for index in range(len(parts), 0, -1)] + [""]


def npm_lock_resolved_version(
    packages: Optional[Dict[str, Any]],
    dependencies: Optional[Dict[str, Any]],
    package_name: object,
    package_relative: str = "",
) -> Optional[str]:
    """Exact direct resolution for a package-lock.json / npm-shrinkwrap.json.

    Workspace-aware (A1): when ``package_relative`` is non-empty the resolution
    is looked up INNERMOST-FIRST over every ancestor prefix of the member — the
    member-local ``<member>/node_modules/<name>`` key, then each intermediate
    ``node_modules`` (hoisted copies), then the root ``node_modules/<name>``.
    At each prefix a ``link:``/``file:`` portal entry decides the resolution
    through the linked package's own record and never falls through to a root
    version that could shadow the member's linked local package. Under a legacy
    ``dependencies`` map (no ``packages``) a workspace member cannot prove its
    own resolution from the root map and abstains (None). For the root (empty
    ``package_relative``) the legacy map is the fallback after the ``packages``
    snapshot.
    """
    name = str(package_name)
    rel = (package_relative or "").replace("\\", "/").strip("/")
    if isinstance(packages, dict) and packages:
        for prefix in _member_ancestor_prefixes(rel):
            key = f"{prefix}/node_modules/{name}" if prefix else f"node_modules/{name}"
            entry = packages.get(key)
            if not isinstance(entry, dict):
                continue
            # A portal at the NEAREST prefix decides the member's resolution;
            # a hoisted root copy of the same name must not override the
            # locally linked package.
            if entry.get("link") and isinstance(entry.get("resolved"), str):
                linked = packages.get(entry["resolved"].replace("\\", "/").strip("/"))
                if isinstance(linked, dict):
                    version = linked.get("version")
                    if isinstance(version, str) and version:
                        return version
                # A link whose target record has no provable version: abstain
                # rather than guess a hoisted root copy.
                return None
            version = entry.get("version")
            if isinstance(version, str) and version:
                return version
        return None
    if rel:
        return None
    if isinstance(dependencies, dict):
        entry = dependencies.get(name)
        if isinstance(entry, dict):
            version = entry.get("version")
            if isinstance(version, str) and version:
                return version
    return None


class ResolutionInventory:
    """Read-only resolution facts for one canonical lockfile, parsed once.

    ``installed_version`` returns ``None`` (abstention) whenever the installed
    version cannot be proven from the parsed lockfile — never a guess. All
    reads are thread-safe: no mutable state is touched after construction.
    """

    __slots__ = (
        "_project_dir",
        "_lock_path",
        "_package_relative",
        "_family",
        "_lock_hash",
        "_packages",
        "_dependencies",
        "_berry_mapping",
        "_yarn_blocks",
        "_pnpm_lines",
    )

    def __init__(self, project_dir: Path, lock_path: Path, package_relative: str = "") -> None:
        self._project_dir = _normalize_path(project_dir) or Path(project_dir)
        self._lock_path = lock_path
        raw_relative = str(package_relative or "").replace("\\", "/").strip("/")
        self._package_relative = "" if raw_relative in ("", ".") else raw_relative
        family, data, lines, text, raw = _read_and_classify(lock_path)
        self._family = family
        self._packages = None
        self._dependencies = None
        self._berry_mapping = {}
        self._yarn_blocks = []
        self._pnpm_lines = lines if family == "pnpm" else None
        # Hash the EXACT raw bytes (a UTF-8 BOM included), so the phase identity
        # matches what Modern Yarn / the verifier hash from disk: a file that was
        # re-encoded with a different BOM is a different input, never the same
        # phase with a stale parsed version.
        self._lock_hash = hashlib.sha256(raw).hexdigest()
        try:
            self._parse(family, data, lines)
        except Exception:  # noqa: BLE001 - unreadable/ambiguous lockfile abstains
            self._family = "unparsable"

    # -- construction helpers ------------------------------------------------

    @classmethod
    def for_project(cls, project_dir: object) -> Optional["ResolutionInventory"]:
        """Build the inventory of the canonical lockfile governing a project,
        or ``None`` when no lockfile exists or it cannot be parsed."""
        resolved = _normalize_path(project_dir)
        if resolved is None:
            return None
        lock, package_relative = _canonical_for_project(resolved)
        if lock is None or not lock.is_file():
            return None
        try:
            return cls(resolved, lock, package_relative)
        except Exception:  # noqa: BLE001 - unreadable lockfile is abstention
            return None

    # -- read-only surface ---------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def lock_hash(self) -> str:
        """SHA-256 of the exact lockfile bytes this inventory was built from."""
        return self._lock_hash

    @property
    def family(self) -> str:
        return self._family

    def belongs_to(self, project_dir: object) -> bool:
        """Cheap check that this inventory covers the given project directory
        (a resolved-path comparison, no filesystem work)."""
        normalized = _normalize_path(project_dir)
        return normalized is not None and normalized == self._project_dir

    def installed_version(
        self, package_name: object, declared_spec: Optional[str] = None
    ) -> Optional[str]:
        """The exact resolved version of a direct dependency, or ``None``
        (abstention) when it cannot be proven from this lockfile."""
        try:
            if self._family == "npm":
                return npm_lock_resolved_version(
                    self._packages, self._dependencies, package_name, self._package_relative
                )
            if self._family == "berry":
                return self._berry_installed_version(package_name, declared_spec)
            if self._family == "yarn":
                return self._yarn_v1_installed_version(package_name, declared_spec)
            if self._family == "pnpm":
                return self._pnpm_installed_version(package_name)
        except Exception:  # noqa: BLE001 - any lookup failure is abstention
            return None
        return None

    # -- per-family lookups --------------------------------------------------

    def _berry_installed_version(self, package_name: object, declared_spec: Optional[str]) -> Optional[str]:
        from iterative_migration import _yarn_selector_for

        name = str(package_name)
        mapping = self._berry_mapping
        if declared_spec is not None:
            return mapping.get(f"{name}@{declared_spec}") or mapping.get(f"{name}@npm:{declared_spec}")
        versions = {version for selector, version in mapping.items() if _yarn_selector_for(selector, name)}
        return versions.pop() if len(versions) == 1 else None

    def _yarn_v1_installed_version(self, package_name: object, declared_spec: Optional[str]) -> Optional[str]:
        from iterative_migration import _yarn_direct_selectors, _yarn_selector_for

        name = str(package_name)
        selectors = _yarn_direct_selectors(name, declared_spec)
        blocks = self._yarn_blocks
        if selectors:
            direct_versions = {
                version for block_keys, version in blocks if any(key in selectors for key in block_keys)
            }
            if direct_versions:
                return direct_versions.pop() if len(direct_versions) == 1 else None
            return None
        versions = {
            version for block_keys, version in blocks if any(_yarn_selector_for(key, name) for key in block_keys)
        }
        return versions.pop() if len(versions) == 1 else None

    def _pnpm_installed_version(self, package_name: object) -> Optional[str]:
        from iterative_migration import _pnpm_importer_direct_version

        name = str(package_name)
        lines = self._pnpm_lines or []
        importer_version = _pnpm_importer_direct_version(lines, name)
        if importer_version is not None:
            return importer_version
        pattern = re.compile(rf"^\s*/?({re.escape(name)})@([^:()]+)")
        versions = set()
        for line in lines:
            match = pattern.match(line.rstrip())
            if match:
                versions.add(match.group(2))
        return versions.pop() if len(versions) == 1 else None

    # -- parsing (called exactly once from __init__) -------------------------

    def _parse(self, family: str, data: Optional[Any], lines: Optional[Sequence[str]]) -> None:
        if family == "npm":
            self._packages = (
                data.get("packages") if isinstance(data, dict) and isinstance(data.get("packages"), dict) else None
            )
            self._dependencies = (
                data.get("dependencies")
                if isinstance(data, dict) and isinstance(data.get("dependencies"), dict)
                else None
            )
        elif family == "berry":
            from yarn_berry import lock_mapping

            self._berry_mapping = lock_mapping(self._lock_path)
        elif family == "yarn":
            self._yarn_blocks = _scan_yarn_v1_blocks(list(lines or []))


def _read_and_classify(lock_path: Path) -> Tuple[str, Optional[Any], Optional[List[str]], str, bytes]:
    """Read the lockfile bytes once, classify it, and pre-parse structured data.

    Returns ``(family, data-or-text, lines, text, raw)`` where ``lines`` is the
    split text for the textual families (yarn v1 / pnpm), ``data`` is the
    parsed JSON for npm, and ``raw`` the exact original bytes (BOM preserved,
    used for the phase lock_hash). Family is ``unparsable`` when the
    classification or structure is unusable.
    """
    try:
        raw = lock_path.read_bytes()
    except OSError:
        return ("unparsable", None, None, "", b"")
    text = raw.decode("utf-8-sig", errors="replace")
    name = lock_path.name
    if name in ("package-lock.json", "npm-shrinkwrap.json"):
        try:
            data = json.loads(text)
        except ValueError:
            return ("unparsable", None, None, text, raw)
        return ("npm", data, None, text, raw)
    if name == "yarn.lock":
        if _looks_like_berry_lock(text):
            return ("berry", text, None, text, raw)
        return ("yarn", None, text.splitlines(), text, raw)
    if name == "pnpm-lock.yaml":
        return ("pnpm", None, text.splitlines(), text, raw)
    return ("unparsable", None, None, text, raw)
