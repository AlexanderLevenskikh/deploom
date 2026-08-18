#!/usr/bin/env python3
"""Block V-E/X verification storage and package-cache substrate.

All decisions in this module are performance-only. Storage placement, copy
worker counts, and package cache locations must never become dependency proof
authority. The real package manager and existing proof identities remain the
authority boundary.
"""
from __future__ import annotations

import ctypes
import dataclasses
import os
import shutil
import string
from pathlib import Path
from typing import Mapping, Optional

_PROOF_NEUTRAL_ENV = frozenset({
    "DEPLOOM_VERIFICATION_ROOT",
    "DEPLOOM_WORKSPACE_COPY_WORKERS",
    "DEPLOOM_NTFS_FASTPATH_MIN_COMMANDS",
    "DEPLOOM_BASELINE_RESUME",
    "DEPLOOM_COMPATIBILITY_HINTS",
    "DEPLOOM_PREDICATE_ACTIVE_SEARCH",
    "DEPLOOM_PREDICATE_REPEAT_THRESHOLD",
    "DEPLOOM_PREDICATE_PROBE_BUDGET",
    "DEPLOOM_VEX_AUTO_REFS",
    "DEPLOOM_VEX_REFS_COPY_WORKERS",
})


@dataclasses.dataclass(frozen=True)
class VerificationStorageProfile:
    root: Optional[Path]
    filesystem: str = ""
    volume_root: str = ""
    free_bytes: int = 0
    explicit: bool = False
    automatic: bool = False
    refs_same_volume_capable: bool = False

    @property
    def optimized(self) -> bool:
        return bool(self.root is not None and self.filesystem == "refs")


def semantic_verification_environment(
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    source = environment if environment is not None else os.environ
    return {
        str(key): str(value)
        for key, value in source.items()
        if str(key).upper() not in _PROOF_NEUTRAL_ENV
    }


def _windows_volume_root(path: Path) -> str:
    if os.name != "nt":
        return ""
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        buffer = ctypes.create_unicode_buffer(32768)
        probe = str(path.expanduser().absolute())
        ok = kernel32.GetVolumePathNameW(probe, buffer, len(buffer))
        if ok and buffer.value:
            return buffer.value
    except Exception:
        pass
    try:
        return path.expanduser().absolute().anchor
    except OSError:
        return ""


def windows_filesystem(path: Path) -> str:
    if os.name != "nt":
        return ""
    try:
        volume_root = _windows_volume_root(path)
        if not volume_root:
            return ""
        fs_name = ctypes.create_unicode_buffer(261)
        volume_name = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32()
        max_component = ctypes.c_uint32()
        flags = ctypes.c_uint32()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
            volume_root,
            volume_name,
            len(volume_name),
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            fs_name,
            len(fs_name),
        )
        return fs_name.value.lower() if ok else ""
    except Exception:
        return ""


def _candidate_refs_roots() -> list[tuple[int, Path]]:
    """Return ReFS volume roots ranked by free space.

    Auto selection is opt-in. We never silently place large verification data
    on an arbitrary user volume.
    """
    if os.name != "nt":
        return []
    result: list[tuple[int, Path]] = []
    try:
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())  # type: ignore[attr-defined]
    except Exception:
        mask = 0
    for index, letter in enumerate(string.ascii_uppercase):
        if mask and not (mask & (1 << index)):
            continue
        if letter == "C":
            continue
        drive = Path(f"{letter}:\\")
        if windows_filesystem(drive) != "refs":
            continue
        try:
            free = int(shutil.disk_usage(drive).free)
        except OSError:
            free = 0
        result.append((free, drive))
    result.sort(key=lambda item: (-item[0], str(item[1]).lower()))
    return result


def verification_root(
    environment: Optional[Mapping[str, str]] = None,
) -> Optional[Path]:
    env = environment if environment is not None else os.environ
    raw = str(env.get("DEPLOOM_VERIFICATION_ROOT") or "").strip()
    auto = str(env.get("DEPLOOM_VEX_AUTO_REFS") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if raw and raw.lower() not in {"auto", "refs", "devdrive"}:
        root = Path(raw).expanduser().absolute()
        root.mkdir(parents=True, exist_ok=True)
        return root
    if raw.lower() in {"auto", "refs", "devdrive"} or auto:
        candidates = _candidate_refs_roots()
        if candidates:
            root = candidates[0][1] / "DepLoom" / "verification"
            root.mkdir(parents=True, exist_ok=True)
            return root
    return None


def verification_storage_profile(
    environment: Optional[Mapping[str, str]] = None,
) -> VerificationStorageProfile:
    env = environment if environment is not None else os.environ
    raw = str(env.get("DEPLOOM_VERIFICATION_ROOT") or "").strip()
    automatic = raw.lower() in {"auto", "refs", "devdrive"} or str(
        env.get("DEPLOOM_VEX_AUTO_REFS") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    root = verification_root(env)
    if root is None:
        return VerificationStorageProfile(None)
    filesystem = windows_filesystem(root) if os.name == "nt" else ""
    volume_root = _windows_volume_root(root) if os.name == "nt" else str(root.anchor)
    try:
        free = int(shutil.disk_usage(root).free)
    except OSError:
        free = 0
    return VerificationStorageProfile(
        root=root,
        filesystem=filesystem,
        volume_root=volume_root,
        free_bytes=free,
        explicit=bool(raw and not automatic),
        automatic=automatic,
        refs_same_volume_capable=bool(os.name == "nt" and filesystem == "refs"),
    )


def package_manager_cache_environment(
    *,
    manager: str,
    proof_cache_dir: str | Path | None,
    inherited_environment: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Return performance-only package cache overrides."""
    env = inherited_environment if inherited_environment is not None else os.environ
    normalized = str(manager or "").strip().lower()
    profile = verification_storage_profile(env)

    if normalized == "yarn":
        if str(env.get("YARN_CACHE_FOLDER") or "").strip():
            return {}
        if profile.optimized and profile.root is not None:
            target = profile.root / "package-manager-artifacts" / "yarn"
            target.mkdir(parents=True, exist_ok=True)
            return {"YARN_CACHE_FOLDER": str(target)}
        return {}

    if normalized == "npm" and str(env.get("npm_config_cache") or "").strip():
        return {}
    if normalized == "pnpm" and (
        str(env.get("npm_config_store_dir") or "").strip()
        or str(env.get("PNPM_STORE_DIR") or "").strip()
    ):
        return {}

    if profile.optimized and profile.root is not None:
        base = profile.root / "package-manager-artifacts" / normalized
    elif proof_cache_dir:
        base = (
            Path(proof_cache_dir).expanduser().absolute().parent
            / "package-manager-artifacts"
            / normalized
        )
    else:
        return {}
    base.mkdir(parents=True, exist_ok=True)

    if normalized == "npm":
        return {"npm_config_cache": str(base)}
    if normalized == "pnpm":
        return {"npm_config_store_dir": str(base)}
    return {}


def storage_summary(
    environment: Optional[Mapping[str, str]] = None,
) -> str:
    profile = verification_storage_profile(environment)
    if profile.root is None:
        return "system-temp/private-copy; no dedicated verification root"
    free_gib = profile.free_bytes / (1024 ** 3) if profile.free_bytes else 0.0
    mode = (
        "ReFS/Dev-Drive-capable"
        if profile.filesystem == "refs"
        else (profile.filesystem or "unknown-filesystem")
    )
    source = "auto" if profile.automatic else "explicit"
    return f"{mode}; root={profile.root}; source={source}; freeGiB={free_gib:.1f}"
