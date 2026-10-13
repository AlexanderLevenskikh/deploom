"""Source-completeness obligations inside the existing verification proof model.

Commands are opaque executable programs. Workspace manifests and Git visibility
prove topology, not the complete set of filesystem inputs those programs read.
Until a complete optimized closure is proven, all non-dependency source under
the authoritative root is the conservative subject. No tool-specific allowlist.
"""
from pathlib import Path

SOURCE_CLOSURE_SCHEMA = "verification-source-closure-v1"

def require_complete_source_policy(policy):
    from source_snapshot import SourceCaptureError, DEFAULT_EXCLUDED_DIR_NAMES, DEFAULT_EXCLUDED_FILE_NAMES
    from frontend_source_policy import validate_build_output_paths, AGENT_STATE_DIRS, KNOWN_FRONTEND_CACHE_PATHS
    validate_build_output_paths(list(policy.frontend_build_output_paths))
    roots = tuple(policy.scope_roots)
    allowed_dirs = set(DEFAULT_EXCLUDED_DIR_NAMES) | (set(AGENT_STATE_DIRS) if policy.frontend_profile else set())
    allowed_paths = set(KNOWN_FRONTEND_CACHE_PATHS) if policy.frontend_profile else set()
    complete = (roots in ((), (".",)) and set(policy.excluded_relative_paths).issubset(allowed_paths)
        and set(policy.excluded_dir_names).issubset(allowed_dirs)
        and set(policy.excluded_file_names).issubset(DEFAULT_EXCLUDED_FILE_NAMES))
    if not complete:
        raise SourceCaptureError("VERIFICATION_SOURCE_CLOSURE_UNPROVEN: legacy/project-filtered source cannot prove arbitrary command inputs; recapture the complete authoritative source root")
    return {"schema": SOURCE_CLOSURE_SCHEMA, "strategy": "conservative-authoritative-root",
            "confidence": "complete-under-source-policy", "policyKey": policy.key,
            "frontendBuildOutputExclusions": list(policy.frontend_build_output_paths),
            "frontendProfile": policy.frontend_profile, "cacheExclusions": list(policy.excluded_relative_paths),
            "agentStateExclusions": [name for name in AGENT_STATE_DIRS if name in policy.excluded_dir_names]}

def execution_scope(snapshot, *, project, package_manager_root, commands, materialization):
    from source_snapshot import SourceInputPolicy
    closure = require_complete_source_policy(snapshot.input_policy or SourceInputPolicy())
    return {"selectedProject": str(snapshot.original_project_path),
            "executionCwd": str(Path(project).resolve()),
            "sourceRoot": str(snapshot.capture_root),
            "packageManagerRoot": str(Path(package_manager_root).resolve()),
            "commands": list(commands), "sourceSnapshotKey": snapshot.key,
            "materialization": str(materialization), "sourceClosure": closure}


def excluded_source_failure_evidence(snapshot, output, *, execution_root, execution_cwd):
    """Only diagnose a missing excluded input when the original path exists."""
    import re
    from source_snapshot import _excluded, SourceInputPolicy
    policy = snapshot.input_policy or SourceInputPolicy()
    if not policy.frontend_profile and not any(
        name in policy.excluded_dir_names for name in (".claude", ".codex")
    ):
        return []
    text = str(output)[-131072:]
    if not re.search(r"cannot find|can't find|enoent|no such file|module not found", text, re.I):
        return []
    found = set()
    for token in re.findall(r"['\"]([^'\"\r\n]{1,4096})['\"]", text):
        if "://" in token:
            continue
        path = Path(token)
        candidates = [path] if path.is_absolute() else [Path(execution_cwd) / path, Path(execution_root) / path]
        for candidate in candidates:
            try:
                relative = candidate.resolve().relative_to(Path(execution_root).resolve())
                original = (snapshot.capture_root / relative).resolve()
                original.relative_to(snapshot.capture_root.resolve())
                if _excluded(relative, policy) and original.is_file():
                    found.add(relative.as_posix())
            except (OSError, ValueError):
                continue
    return sorted(found)[:20]


def validate_materialized_source_closure(snapshot, target, *, progress=None, progress_interval_seconds=15):
    """Verify each fresh source upper against the prepared artifact's source seal.

    Reused unchanged private trials skip materialization and this check. Never
    hash dependency payload here; lower continuity has its existing proof.
    """
    from artifact_integrity import ArtifactIntegrityError, build_artifact_tree_integrity
    expected_key = snapshot.source_closure_content_key
    if not expected_key:
        raise ArtifactIntegrityError("VERIFICATION_SOURCE_CLOSURE_RECEIPT_MISSING")
    observed = build_artifact_tree_integrity(Path(target), source_only=True,
        progress=progress, progress_label="private source closure validation",
        progress_interval_seconds=progress_interval_seconds)
    if observed.source_content_key == expected_key:
        return
    expected = {str(item["path"]): item for item in snapshot.source_closure_entries}
    actual = {str(item["path"]): item for item in observed.source_entries}
    changes = [{"path": name, "change": "missing" if name not in actual else
                "unexpected" if name not in expected else "modified"}
               for name in sorted(set(expected) | set(actual)) if expected.get(name) != actual.get(name)]
    import json
    raise ArtifactIntegrityError("VERIFICATION_SOURCE_CLOSURE_MATERIALIZED_MISMATCH: " +
        json.dumps({"expected": expected_key, "observed": observed.source_content_key,
                    "changedPaths": changes[:20], "changedPathCount": len(changes)}, sort_keys=True))


def reset_observed_trial_outputs(root, expected, changes, *, progress=None):
    """Reset only new private outputs after a complete, quiescent event stream.

    Existing source files must still match the original source seal. Dependency
    writes, aliases, unknown event types or oversized deltas reject reuse. No
    directory-name allowlist grants authority; every removed path was absent
    from the fully validated initial source tree.
    """
    import os
    import stat
    import time
    from artifact_integrity import _hash_file
    root = Path(root).resolve()
    deadline = time.monotonic() + 5
    limit = 10000
    removed = checked = 0
    touched = {}
    new_paths = set()
    deletion_plan = {}

    def plain(relative):
        if time.monotonic() >= deadline:
            raise ValueError("private trial delta budget exhausted")
        path = root
        for part in relative.parts:
            path = path / part
            try:
                info = path.lstat()
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("private trial delta contains reparse entry")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("private trial delta contains shared file")
        if os.path.normcase(str(path.resolve(strict=True))) != os.path.normcase(str(path.absolute())):
            raise ValueError("private trial delta contains a filesystem path alias")
        return info

    try:
        if not expected or len(changes) > limit:
            return False
        for change in changes:
            action, name = change.split(":", 1)
            relative = Path(name.replace("\\", "/"))
            if (int(action) not in (1, 2, 3, 4, 5) or not relative.parts
                    or relative.is_absolute() or relative.drive
                    or any(part.casefold() in ("..", "node_modules") or ":" in part
                           or part.endswith((".", " ")) for part in relative.parts)):
                return False
            key = relative.as_posix()
            touched.setdefault(key, set()).add(int(action))
        for name, actions in touched.items():
            relative = Path(name)
            info = plain(relative)
            entry = expected.get(os.path.normcase(name).replace("\\", "/"))
            if entry is not None and entry["path"] != name:
                return False  # Case-only renames/Windows aliases cannot become new outputs.
            if entry is None:
                new_paths.add(relative)
                continue
            if info is None:
                return False
            if entry["kind"] == "directory":
                if not stat.S_ISDIR(info.st_mode) or actions != {3}:
                    return False
            elif entry["kind"] == "file":
                if not stat.S_ISREG(info.st_mode):
                    return False
                digest, size, executable = _hash_file(root / relative)
                checked += 1
                if (digest, size, executable) != (entry["sha256"], entry["size"], entry["executable"]):
                    return False
            else:
                return False
        # Validate the complete deletion plan BEFORE removing anything. A new
        # output directory may not conceal baseline files or dependency roots.
        pending = list(new_paths)
        while pending:
            relative = pending.pop()
            if relative in deletion_plan:
                continue
            if (os.path.normcase(relative.as_posix()).replace("\\", "/") in expected
                    or any(part.casefold() == "node_modules" for part in relative.parts)):
                return False
            info = plain(relative)
            if info is None:
                continue
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                return False
            deletion_plan[relative] = (info.st_dev, info.st_ino, stat.S_ISDIR(info.st_mode))
            if len(deletion_plan) > limit:
                return False
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(root / relative) as children:
                    for item in children:
                        if len(pending) + len(deletion_plan) >= limit:
                            return False
                        pending.append(relative / item.name)
        for relative in sorted(deletion_plan, key=lambda path: len(path.parts), reverse=True):
            info = plain(relative)
            if info is None:
                return False
            identity = (info.st_dev, info.st_ino, stat.S_ISDIR(info.st_mode))
            if identity != deletion_plan[relative]:
                return False
            path = root / relative
            if identity[2]:
                path.rmdir()  # Nonrecursive: concurrent/unregistered children reject reuse.
            else:
                path.unlink()
            removed += 1
        if progress:
            progress(f"private trial delta validation: sourceFilesRehashed={checked}, newOutputPathsRemoved={removed}; full source hashing skipped")
        return True
    except Exception as exc:
        if progress:
            progress(f"private trial delta reuse rejected: {type(exc).__name__}: {exc}")
        return False
