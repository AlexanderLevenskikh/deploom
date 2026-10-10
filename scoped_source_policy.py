"""Explicit Git-visible migration inputs, with repository-relative layout retained."""
from pathlib import Path
import json
import os
import subprocess

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
    if not policy.scope_roots:
        return {}
    return {"mode": "project-gitignore", "roots": list(policy.scope_roots),
            "excludedPaths": list(policy.excluded_relative_paths)}


def policy_from_payload(raw):
    from source_snapshot import SourceInputPolicy, SourceCaptureError
    if not raw:
        return SourceInputPolicy()
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
    from source_snapshot import SourceInputPolicy, SourceCaptureError
    if os.environ.get(MODE_ENV) != "project-gitignore":
        return SourceInputPolicy()
    project, root = Path(project).resolve(), Path(root).resolve()
    roots = {project.relative_to(root).as_posix()}
    if not (root / ".git").exists():
        # A standalone non-Git directory is already the selected source root.
        return SourceInputPolicy()
    from project_topology import _select_owned_lockfile, _workspace_owns, ProjectTopologyError
    try:
        manager, lockfile, patterns = _select_owned_lockfile(project, source_boundary=root)
        # Installation consumes declared member manifests and local workspaces.
        # Expand workspace patterns directly: never walk unrelated backend builds.
        import glob
        for pattern in patterns:
            if pattern.startswith("!"): continue
            for name in glob.glob(str(manager / pattern / "package.json"), recursive=True):
                directory = Path(name).resolve().parent
                if _workspace_owns(manager, directory, patterns):
                    roots.add(directory.relative_to(root).as_posix())
    except ProjectTopologyError as exc:
        raise SourceCaptureError(f"SOURCE_SCOPE_TOPOLOGY_UNAVAILABLE: {exc}") from exc
    # Direct file:/link:/portal: inputs remain in scope, including transitive ones.
    pending = list(roots); seen = set()
    while pending:
        name = pending.pop()
        if name in seen: continue
        seen.add(name)
        manifest = root / name / "package.json"
        if not manifest.is_file(): continue
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            for value in (raw.get(section) or {}).values():
                if isinstance(value, str) and value.startswith(("file:", "link:", "portal:")):
                    target = (manifest.parent / value.split(":", 1)[1]).resolve()
                    try: relative = target.relative_to(root).as_posix()
                    except ValueError: raise SourceCaptureError("SOURCE_SCOPE_LOCAL_DEPENDENCY_OUTSIDE_REPOSITORY")
                    if relative not in roots: roots.add(relative); pending.append(relative)
    roots = tuple(sorted(roots))
    result = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "--exclude=node_modules/", "--exclude=.dependency-roadmap/", "-z"],
        capture_output=True, encoding="utf-8", errors="strict", timeout=120, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise SourceCaptureError("SOURCE_SCOPE_GIT_INVENTORY_FAILED")
    files = {name for name in result.stdout.split("\0") if name and in_scope(name, roots)}
    # Keep the project's explicitly configured local Yarn executable, even when
    # the Yarn cache/release folder is ignored. Do not include arbitrary ignored data.
    for directory in [root, *[root / name for name in roots]]:
        config = directory / ".yarnrc.yml"
        if config.is_file():
            import re
            match = re.search(r"(?m)^\s*yarnPath:\s*['\"]?([^'\"\r\n]+)", config.read_text(encoding="utf-8"))
            if match:
                target = (directory / match.group(1).strip()).resolve()
                try: files.add(target.relative_to(root).as_posix())
                except ValueError: raise SourceCaptureError("SOURCE_SCOPE_YARN_PATH_OUTSIDE_REPOSITORY")
    dirs = {"."}
    for name in files:
        dirs.update(parent.as_posix() for parent in Path(name).parents)
    excluded = []
    stack = [root]
    while stack:
        folder = stack.pop()
        with os.scandir(folder) as entries:
            for item in entries:
                path = Path(item.path); relative = path.relative_to(root).as_posix()
                if relative == ".git": continue
                if item.is_dir(follow_symlinks=False):
                    if relative in dirs: stack.append(path)
                    else: excluded.append(relative)
                elif relative not in files:
                    excluded.append(relative)
    return SourceInputPolicy(scope_roots=roots, excluded_relative_paths=tuple(sorted(excluded)))
