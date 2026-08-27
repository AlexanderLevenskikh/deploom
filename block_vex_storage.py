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
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Optional

# BLOCK_OMEGA_VERIFICATION_SUBSTRATE_V2
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
    "DEPLOOM_DISABLE_GUARDED_LOWER",
    "DEPLOOM_OMEGA_OVERLAY_WORKERS",
    # Block Sigma global I/O governor / bounded hashing controls. These only
    # change scheduling cost and must never invalidate or distinguish proof.
    "DEPLOOM_IO_COPY_SLOTS",
    "DEPLOOM_IO_HASH_SLOTS",
    "DEPLOOM_IO_PM_SLOTS",
    "DEPLOOM_SOURCE_HASH_WORKERS",
    "DEPLOOM_ARTIFACT_HASH_WORKERS",
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
    default: bool = False
    fallback_from: str = ""

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



def _default_verification_root(
    environment: Optional[Mapping[str, str]] = None,
) -> Path:
    # Zero-configuration user cache root. Verification bytes are performance
    # substrate, not project state, so large trees stay outside the repository.
    env = environment if environment is not None else os.environ
    if os.name == "nt":
        base = str(env.get("LOCALAPPDATA") or env.get("TEMP") or "").strip()
        if base:
            return Path(base).expanduser().absolute() / "DepLoom" / "verification"
        return Path.home() / "AppData" / "Local" / "DepLoom" / "verification"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "DepLoom" / "verification"
    base = str(env.get("XDG_CACHE_HOME") or "").strip()
    if base:
        return Path(base).expanduser().absolute() / "deploom" / "verification"
    return Path.home() / ".cache" / "deploom" / "verification"


def _ensure_writable_root(root: Path) -> Optional[Path]:
    # Create and write-probe a performance root; uncertainty falls back.
    probe_path: Optional[Path] = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd, raw_probe = tempfile.mkstemp(prefix=".deploom-write-probe-", dir=str(root))
        os.close(fd)
        probe_path = Path(raw_probe)
        probe_path.unlink()
        return root
    except OSError:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass
        return None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return os.path.normcase(str(left.absolute())) == os.path.normcase(str(right.absolute()))

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
    # Resolve the verification root with a zero-config, non-invasive fallback.
    # Explicit/auto ReFS remains optional; missing/read-only roots fall back.
    env = environment if environment is not None else os.environ
    raw = str(env.get("DEPLOOM_VERIFICATION_ROOT") or "").strip()
    auto = str(env.get("DEPLOOM_VEX_AUTO_REFS") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }

    preferred: Optional[Path] = None
    if raw and raw.lower() not in {"auto", "refs", "devdrive"}:
        preferred = Path(raw).expanduser().absolute()
    elif raw.lower() in {"auto", "refs", "devdrive"} or auto:
        candidates = _candidate_refs_roots()
        if candidates:
            preferred = candidates[0][1] / "DepLoom" / "verification"

    if preferred is not None:
        usable = _ensure_writable_root(preferred)
        if usable is not None:
            return usable

    return _ensure_writable_root(_default_verification_root(env))


def verification_storage_profile(
    environment: Optional[Mapping[str, str]] = None,
) -> VerificationStorageProfile:
    env = environment if environment is not None else os.environ
    raw = str(env.get("DEPLOOM_VERIFICATION_ROOT") or "").strip()
    requested_auto = raw.lower() in {"auto", "refs", "devdrive"} or str(
        env.get("DEPLOOM_VEX_AUTO_REFS") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    explicit_raw = bool(raw and raw.lower() not in {"auto", "refs", "devdrive"})
    requested_explicit = Path(raw).expanduser().absolute() if explicit_raw else None

    root = verification_root(env)
    if root is None:
        return VerificationStorageProfile(None)

    default_root = _default_verification_root(env)
    is_default = _same_path(root, default_root)
    explicit = bool(requested_explicit is not None and _same_path(root, requested_explicit))
    automatic = bool(requested_auto and not is_default)
    fallback_from = ""
    if is_default and explicit_raw:
        fallback_from = raw
    elif is_default and requested_auto:
        fallback_from = "auto-refs"

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
        explicit=explicit,
        automatic=automatic,
        refs_same_volume_capable=bool(os.name == "nt" and filesystem == "refs"),
        default=is_default,
        fallback_from=fallback_from,
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

    # Keep package-manager artifacts isolated from the user's native cache,
    # but place them in the zero-config verification root when available.
    # This preserves the existing isolation contract without requiring ReFS
    # or writing large caches into the project workspace.
    if profile.root is not None:
        base = profile.root / "package-manager-artifacts" / normalized
    elif proof_cache_dir:
        base = (
            Path(proof_cache_dir).expanduser().absolute().parent
            / "package-manager-artifacts"
            / normalized
        )
    else:
        return {}
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}

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
    source = "auto" if profile.automatic else ("explicit" if profile.explicit else "default")
    fallback = f"; fallbackFrom={profile.fallback_from}" if profile.fallback_from else ""
    return f"{mode}; root={profile.root}; source={source}; freeGiB={free_gib:.1f}{fallback}"

# BLOCK_VG_ZERO_CONFIG_TRANSACTIONAL_UI_V1
