"""Explicit frontend profile: MSBuild-owned outputs, never a Node bin allowlist."""
import os
from pathlib import Path

PROFILE = "frontend-msbuild-outputs-v1"
MSBUILD_SUFFIXES = {".csproj", ".fsproj", ".vbproj"}
AGENT_STATE_DIRS = (".claude", ".codex")
KNOWN_FRONTEND_CACHE_PATHS = (".nx",)


def validate_build_output_paths(paths):
    from source_snapshot import SourceCaptureError
    if not isinstance(paths, list):
        raise SourceCaptureError("SOURCE_FRONTEND_POLICY_INVALID")
    for name in paths:
        if (not isinstance(name, str) or not name or Path(name).is_absolute()
                or Path(name).drive or ".." in Path(name).parts or ":" in name
                or Path(name).name.lower() not in {"bin", "obj"}):
            raise SourceCaptureError("SOURCE_FRONTEND_BUILD_OUTPUT_PATH_INVALID")


def discover_backend_build_outputs(root):
    from source_snapshot import SourceCaptureError, DEFAULT_EXCLUDED_DIR_NAMES
    root = Path(root).resolve()
    pending = [root]
    excluded = []
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
            owns_node = any(item.name == "package.json" and item.is_file(follow_symlinks=False) for item in entries)
            owns_msbuild = not owns_node and any(Path(item.name).suffix.lower() in MSBUILD_SUFFIXES
                              and item.is_file(follow_symlinks=False) for item in entries)
            if owns_msbuild:
                # Exclude future default outputs as well as currently present ones.
                excluded.extend((directory / name).relative_to(root).as_posix() for name in ("bin", "obj"))
            for item in entries:
                if item.name in (*DEFAULT_EXCLUDED_DIR_NAMES, *AGENT_STATE_DIRS) or item.name in (".git", ".nx"):
                    continue
                path = Path(item.path)
                if item.is_symlink() or (getattr(path, "is_junction", lambda: False)()):
                    continue  # Existing reparse preflight validates these separately.
                if not item.is_dir(follow_symlinks=False):
                    continue
                if owns_msbuild and item.name.lower() in {"bin", "obj"}:
                    excluded.append(path.relative_to(root).as_posix())
                else:
                    pending.append(path)
        except OSError as exc:
            raise SourceCaptureError("SOURCE_FRONTEND_POLICY_SCAN_UNAVAILABLE: " + str(exc)) from exc
    result = sorted(set(excluded))
    validate_build_output_paths(result)
    return tuple(result)
