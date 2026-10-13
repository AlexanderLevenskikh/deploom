"""Source closure policy, with repository-relative layout retained."""
from pathlib import Path

MODE_ENV = "DEPLOOM_SOURCE_SCOPE_MODE"


def in_scope(name, roots):
    path = Path(name)
    if path.parts and path.parts[0] == ".git":
        return True
    for root in roots:
        base = Path(root)
        if base == Path(".") or path == base or base in path.parents:
            return True
        # Ancestor configuration and shared scripts are installation/build inputs;
        # unrelated root documents, database dumps and binaries are not.
        if path.parent in base.parents and (path.name.startswith(".") or path.suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".js", ".cjs", ".mjs", ".ts", ".cts", ".mts", ".sh", ".ps1"} or path.name in {"yarn.lock", "pnpm-lock.yaml", "Makefile"}):
            return True
    return False


def policy_payload(policy):
    if policy.frontend_profile:
        return {"mode": "frontend-source-v1", "excludedBuildOutputPaths": list(policy.frontend_build_output_paths),
                "excludedCachePaths": list(policy.excluded_relative_paths)}
    if policy.frontend_build_output_paths:
        return {"mode": "frontend-msbuild-outputs-v1", "excludedBuildOutputPaths": list(policy.frontend_build_output_paths)}
    if not policy.scope_roots and not policy.excluded_relative_paths:
        return {}
    return {"mode": "project-gitignore", "roots": list(policy.scope_roots or (".",)),
            "excludedPaths": list(policy.excluded_relative_paths)}


def policy_from_payload(raw):
    from source_snapshot import SourceInputPolicy, SourceCaptureError
    if not raw:
        return SourceInputPolicy()
    if isinstance(raw, dict) and raw.get("mode") == "frontend-source-v1":
        paths = raw.get("excludedBuildOutputPaths")
        caches = raw.get("excludedCachePaths")
        from frontend_source_policy import validate_build_output_paths, AGENT_STATE_DIRS, KNOWN_FRONTEND_CACHE_PATHS
        validate_build_output_paths(paths)
        if not isinstance(caches, list) or any(name not in KNOWN_FRONTEND_CACHE_PATHS for name in caches):
            raise SourceCaptureError("SOURCE_FRONTEND_CACHE_POLICY_INVALID")
        from source_snapshot import DEFAULT_EXCLUDED_DIR_NAMES
        return SourceInputPolicy(excluded_dir_names=tuple(dict.fromkeys((*DEFAULT_EXCLUDED_DIR_NAMES, *AGENT_STATE_DIRS))),
            excluded_relative_paths=tuple(caches), frontend_build_output_paths=tuple(paths), frontend_profile=True)
    if isinstance(raw, dict) and raw.get("mode") == "frontend-msbuild-outputs-v1":
        paths = raw.get("excludedBuildOutputPaths")
        from frontend_source_policy import validate_build_output_paths
        validate_build_output_paths(paths)
        return SourceInputPolicy(frontend_build_output_paths=tuple(paths))
    if not isinstance(raw, dict) or raw.get("mode") != "project-gitignore":
        raise SourceCaptureError("SOURCE_SCOPE_POLICY_INVALID")
    roots, excluded = raw.get("roots"), raw.get("excludedPaths")
    if not isinstance(roots, list) or not roots or not isinstance(excluded, list):
        raise SourceCaptureError("SOURCE_SCOPE_POLICY_INVALID")
    for name in roots + excluded:
        if not isinstance(name, str) or not name or Path(name).is_absolute() or Path(name).drive or ".." in Path(name).parts or ":" in name:
            raise SourceCaptureError("SOURCE_SCOPE_PATH_INVALID")
    return SourceInputPolicy(scope_roots=tuple(roots), excluded_relative_paths=tuple(excluded))


def policy_for_project(project, root):
    """Workspace membership/Git visibility cannot prove execution source closure.

    Generic JS/TS tasks can read sibling resources, ignored generated inputs or
    root-level tools. Without a complete closure certificate, retain the full
    authoritative root. Dependency payload stays under the existing separate
    SourceInputPolicy exclusions; this never expands node_modules into source.
    """
    from source_snapshot import SourceInputPolicy, SourceCaptureError
    project, root = Path(project).resolve(), Path(root).resolve()
    try:
        project.relative_to(root)
    except ValueError as exc:
        raise SourceCaptureError("SOURCE_SCOPE_PROJECT_OUTSIDE_ROOT") from exc
    import os
    if os.environ.get(MODE_ENV) == "frontend":
        from frontend_source_policy import discover_backend_build_outputs, AGENT_STATE_DIRS, KNOWN_FRONTEND_CACHE_PATHS
        from source_snapshot import DEFAULT_EXCLUDED_DIR_NAMES
        caches = KNOWN_FRONTEND_CACHE_PATHS if (root / "nx.json").is_file() else ()
        return SourceInputPolicy(excluded_dir_names=tuple(dict.fromkeys((*DEFAULT_EXCLUDED_DIR_NAMES, *AGENT_STATE_DIRS))),
            excluded_relative_paths=caches, frontend_build_output_paths=discover_backend_build_outputs(root), frontend_profile=True)
    return SourceInputPolicy()
