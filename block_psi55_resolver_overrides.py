#!/usr/bin/env python3
"""Ψ.5.5 verified resolver override candidates.

Resolver overrides/resolutions are a candidate materialization dimension, not
Solver authority.  The caller must physically verify every candidate through
the normal resolver/lifecycle/project-check pipeline before it can become part
of a ProvenDependencyEnvelope.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from semantic_version import Version


SUPPORTED_OVERRIDE_MANAGERS = frozenset({"yarn", "npm", "pnpm"})
DUPLICATE_TYPE_PREFIX = "duplicate-type-universe:"


class ResolverOverrideMaterializationError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ResolverOverrideProposal:
    package: str
    version: str
    predicate: str
    manager: str
    manifest_path: str
    reason: str = "duplicate-type-universe-unification"

    @property
    def overrides(self) -> dict[str, str]:
        return {self.package: self.version}


def normalized_resolver_overrides(
    value: Mapping[str, str] | None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_version in sorted(dict(value or {}).items()):
        name = str(raw_name or "").strip()
        version = str(raw_version or "").strip()
        if not name or not version:
            raise ResolverOverrideMaterializationError(
                "RESOLVER_OVERRIDE_INVALID: package/version must be non-empty"
            )
        try:
            Version(version)
        except ValueError as exc:
            raise ResolverOverrideMaterializationError(
                f"RESOLVER_OVERRIDE_VERSION_NOT_EXACT: {name}={version!r}"
            ) from exc
        result[name] = version
    return result


def resolver_override_fingerprint(
    overrides: Mapping[str, str] | None,
    *,
    length: int = 16,
) -> str:
    normalized = normalized_resolver_overrides(overrides)
    payload = json.dumps(
        sorted(normalized.items()),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[: max(8, length)]


def resolver_override_manifest_path(manager: str) -> str:
    normalized = str(manager or "").strip().lower()
    if normalized == "yarn":
        return "resolutions"
    if normalized == "npm":
        return "overrides"
    if normalized == "pnpm":
        return "pnpm.overrides"
    raise ResolverOverrideMaterializationError(
        f"RESOLVER_OVERRIDE_MANAGER_UNSUPPORTED: {normalized or '<missing>'}"
    )


def resolver_override_log_label(
    manager: str,
    overrides: Mapping[str, str] | None,
) -> str:
    path = resolver_override_manifest_path(manager)
    normalized = normalized_resolver_overrides(overrides)
    body = ", ".join(f"{name}={version}" for name, version in normalized.items())
    return f"{path}[{body}]"


def propose_duplicate_type_override(
    *,
    signatures: Sequence[str],
    assignment: Mapping[str, str],
    current_versions: Mapping[str, str],
    manager: str,
) -> ResolverOverrideProposal | None:
    """Propose one bounded unification candidate.

    V1 intentionally requires the duplicate-type subject to be a managed direct
    dependency.  That gives us an exact already-planned version to unify on;
    arbitrary transitive version discovery is a separate search problem and is
    not guessed here.
    """

    normalized_manager = str(manager or "").strip().lower()
    if normalized_manager not in SUPPORTED_OVERRIDE_MANAGERS:
        return None

    duplicate_packages = sorted({
        str(signature)[len(DUPLICATE_TYPE_PREFIX):].strip().lower()
        for signature in signatures
        if str(signature).startswith(DUPLICATE_TYPE_PREFIX)
        and str(signature)[len(DUPLICATE_TYPE_PREFIX):].strip()
    })
    if len(duplicate_packages) != 1:
        return None

    package = duplicate_packages[0]
    version = str(
        assignment.get(package)
        or current_versions.get(package)
        or ""
    ).strip()
    if not version:
        return None
    try:
        Version(version)
    except ValueError:
        return None

    return ResolverOverrideProposal(
        package=package,
        version=version,
        predicate=f"{DUPLICATE_TYPE_PREFIX}{package}",
        manager=normalized_manager,
        manifest_path=resolver_override_manifest_path(normalized_manager),
    )


def _override_mapping(
    manifest: dict[str, object],
    manager: str,
) -> dict[str, object]:
    if manager == "yarn":
        value = manifest.get("resolutions")
        if value is None:
            value = {}
            manifest["resolutions"] = value
        if not isinstance(value, dict):
            raise ResolverOverrideMaterializationError(
                "RESOLVER_OVERRIDE_MANIFEST_INVALID: resolutions must be an object"
            )
        return value

    if manager == "npm":
        value = manifest.get("overrides")
        if value is None:
            value = {}
            manifest["overrides"] = value
        if not isinstance(value, dict):
            raise ResolverOverrideMaterializationError(
                "RESOLVER_OVERRIDE_MANIFEST_INVALID: overrides must be an object"
            )
        return value

    if manager == "pnpm":
        pnpm = manifest.get("pnpm")
        if pnpm is None:
            pnpm = {}
            manifest["pnpm"] = pnpm
        if not isinstance(pnpm, dict):
            raise ResolverOverrideMaterializationError(
                "RESOLVER_OVERRIDE_MANIFEST_INVALID: pnpm must be an object"
            )
        value = pnpm.get("overrides")
        if value is None:
            value = {}
            pnpm["overrides"] = value
        if not isinstance(value, dict):
            raise ResolverOverrideMaterializationError(
                "RESOLVER_OVERRIDE_MANIFEST_INVALID: pnpm.overrides must be an object"
            )
        return value

    raise ResolverOverrideMaterializationError(
        f"RESOLVER_OVERRIDE_MANAGER_UNSUPPORTED: {manager or '<missing>'}"
    )


def apply_resolver_overrides(
    project_dir: Path,
    *,
    manager: str,
    overrides: Mapping[str, str] | None,
) -> tuple[str, tuple[str, ...]]:
    """Materialize exact overrides without overwriting user-authored conflicts."""

    normalized_manager = str(manager or "").strip().lower()
    normalized = normalized_resolver_overrides(overrides)
    if not normalized:
        return resolver_override_manifest_path(normalized_manager), ()

    package_json = Path(project_dir) / "package.json"
    try:
        manifest = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ResolverOverrideMaterializationError(
            f"RESOLVER_OVERRIDE_MANIFEST_UNREADABLE: {package_json}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ResolverOverrideMaterializationError(
            f"RESOLVER_OVERRIDE_MANIFEST_INVALID: {package_json}: root must be an object"
        )

    target = _override_mapping(manifest, normalized_manager)
    changed: list[str] = []
    for name, version in normalized.items():
        if name in target:
            existing = target[name]
            if existing == version:
                continue
            raise ResolverOverrideMaterializationError(
                "RESOLVER_OVERRIDE_SOURCE_CONFLICT: "
                f"{resolver_override_manifest_path(normalized_manager)}.{name} "
                f"already equals {existing!r}, candidate requested {version!r}"
            )
        target[name] = version
        changed.append(name)

    package_json.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return resolver_override_manifest_path(normalized_manager), tuple(changed)
