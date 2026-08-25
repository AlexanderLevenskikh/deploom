"""DepLoom Block Z package-manager capability profiles.

A package-manager name is not enough to choose authoritative install semantics.
Yarn Classic, Yarn Berry/PnP, npm and pnpm have different lockfile, install and
workspace contracts. This module makes that distinction explicit and fail-closed.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

# BLOCK_Z_PROJECT_TOPOLOGY_V1
PACKAGE_MANAGER_PROFILE_SCHEMA = "package-manager-profile-v1"


class PackageManagerProfileError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}")


@dataclasses.dataclass(frozen=True)
class PackageManagerProfile:
    manager: str
    family: str
    declared: str
    declared_version: str
    lockfile_name: str
    node_linker: str
    authoritative_supported: bool
    unsupported_code: str = ""
    unsupported_detail: str = ""

    @property
    def key(self) -> str:
        payload = {
            "schema": PACKAGE_MANAGER_PROFILE_SCHEMA,
            "manager": self.manager,
            "family": self.family,
            "declared": self.declared,
            "declaredVersion": self.declared_version,
            "lockfileName": self.lockfile_name,
            "nodeLinker": self.node_linker,
            "authoritativeSupported": self.authoritative_supported,
            "unsupportedCode": self.unsupported_code,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def require_authoritative_support(self) -> "PackageManagerProfile":
        if not self.authoritative_supported:
            raise PackageManagerProfileError(
                self.unsupported_code or "PACKAGE_MANAGER_PROFILE_UNSUPPORTED",
                self.unsupported_detail or self.family,
            )
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "profileSchema": PACKAGE_MANAGER_PROFILE_SCHEMA,
            "manager": self.manager,
            "family": self.family,
            "declared": self.declared,
            "declaredVersion": self.declared_version,
            "lockfileName": self.lockfile_name,
            "nodeLinker": self.node_linker,
            "authoritativeSupported": self.authoritative_supported,
            "unsupportedCode": self.unsupported_code,
            "unsupportedDetail": self.unsupported_detail,
            "key": self.key,
        }


def normalize_declared_package_manager(value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "", ""
    # Corepack declarations may include a hash suffix after the version.
    match = re.match(r"^([A-Za-z0-9._-]+)@([^+]+)", text)
    if match:
        return match.group(1).lower(), match.group(2)
    return text.split("@", 1)[0].lower(), ""


def _major(version: str) -> int | None:
    match = re.match(r"^[vV]?(\d+)", str(version or "").strip())
    return int(match.group(1)) if match else None


def _yarn_lock_is_berry(lockfile: Path) -> bool:
    try:
        lines = lockfile.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[:80]
    except OSError:
        return False
    return any(line.startswith("__metadata:") for line in lines)


def _berry_node_linker(root: Path) -> str:
    config = root / ".yarnrc.yml"
    if (root / ".pnp.cjs").exists() or (root / ".pnp.js").exists():
        default = "pnp"
    else:
        default = "pnp"
    if not config.is_file():
        return default
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default
    match = re.search(
        r"(?m)^\s*nodeLinker\s*:\s*[\"']?([A-Za-z0-9_-]+)",
        text,
    )
    return match.group(1).strip().lower() if match else default


def resolve_package_manager_profile(
    root: Path,
    *,
    package_json: Mapping[str, object],
    lockfile: Path,
) -> PackageManagerProfile:
    root = root.resolve()
    lockfile = lockfile.resolve()
    declared_text = str(package_json.get("packageManager") or "").strip()
    declared_manager, declared_version = normalize_declared_package_manager(
        declared_text
    )

    if lockfile.name in {"package-lock.json", "npm-shrinkwrap.json"}:
        lock_manager = "npm"
    elif lockfile.name == "yarn.lock":
        lock_manager = "yarn"
    elif lockfile.name == "pnpm-lock.yaml":
        lock_manager = "pnpm"
    else:
        raise PackageManagerProfileError(
            "PACKAGE_MANAGER_LOCKFILE_UNSUPPORTED",
            str(lockfile),
        )

    if declared_manager and declared_manager not in {"npm", "yarn", "pnpm"}:
        raise PackageManagerProfileError(
            "PACKAGE_MANAGER_DECLARATION_UNSUPPORTED",
            f"packageManager={declared_text!r}",
        )
    if declared_manager and declared_manager != lock_manager:
        raise PackageManagerProfileError(
            "PACKAGE_MANAGER_DECLARATION_LOCK_CONFLICT",
            f"packageManager={declared_text!r} conflicts with {lockfile.name}",
        )

    if lock_manager == "npm":
        return PackageManagerProfile(
            manager="npm",
            family="npm",
            declared=declared_text,
            declared_version=declared_version,
            lockfile_name=lockfile.name,
            node_linker="node-modules",
            authoritative_supported=True,
        )

    if lock_manager == "pnpm":
        return PackageManagerProfile(
            manager="pnpm",
            family="pnpm",
            declared=declared_text,
            declared_version=declared_version,
            lockfile_name=lockfile.name,
            node_linker="node-modules",
            authoritative_supported=False,
            unsupported_code="PACKAGE_MANAGER_PNPM_UNSUPPORTED",
            unsupported_detail=(
                "pnpm authoritative Baseline is disabled until importer-specific "
                "lockfile identity, fixed-source closure and store semantics are "
                "part of the proof model"
            ),
        )

    declared_major = _major(declared_version)
    berry = bool(
        (declared_major is not None and declared_major >= 2)
        or _yarn_lock_is_berry(lockfile)
        or (root / ".yarnrc.yml").is_file()
        or (root / ".pnp.cjs").exists()
        or (root / ".pnp.js").exists()
    )
    if berry:
        linker = _berry_node_linker(root)
        return PackageManagerProfile(
            manager="yarn",
            family="yarn-berry",
            declared=declared_text,
            declared_version=declared_version,
            lockfile_name=lockfile.name,
            node_linker=linker,
            authoritative_supported=False,
            unsupported_code="PACKAGE_MANAGER_YARN_BERRY_UNSUPPORTED",
            unsupported_detail=(
                f"Yarn Berry ({declared_version or '2+'}, nodeLinker={linker}) "
                "cannot use Yarn Classic --frozen-lockfile proof semantics"
            ),
        )

    return PackageManagerProfile(
        manager="yarn",
        family="yarn-classic",
        declared=declared_text,
        declared_version=declared_version or "1.x",
        lockfile_name=lockfile.name,
        node_linker="node-modules",
        authoritative_supported=True,
    )


def install_args_for_profile(
    profile: PackageManagerProfile,
    *,
    ignore_scripts: bool,
    frozen: bool = False,
) -> list[str]:
    profile.require_authoritative_support()
    if profile.family == "yarn-classic":
        args = ["install"]
        if frozen:
            args.append("--frozen-lockfile")
        if ignore_scripts:
            args.append("--ignore-scripts")
        return args
    if profile.family == "npm":
        args = ["ci", "--no-audit", "--no-fund"] if frozen else [
            "install", "--no-audit", "--no-fund"
        ]
        if ignore_scripts:
            args.append("--ignore-scripts")
        return args
    raise PackageManagerProfileError(
        "PACKAGE_MANAGER_INSTALL_SEMANTICS_UNSUPPORTED",
        profile.family,
    )
