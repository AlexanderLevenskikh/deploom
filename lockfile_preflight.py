"""Small isolated native lockfile probes before copying a complete trial tree."""
from __future__ import annotations
import json
from pathlib import Path
import re
import tempfile
import time

import yaml
from package_manager_profile import lockfile_preflight_args
from project_topology import resolve_project_topology
from verification_observability import emit_observability_event

_ALLOWED_CONFIG = {"nodeLinker", "yarnPath", "enableGlobalCache", "enableScripts", "npmRegistryServer",
                   "npmScopes", "npmRegistries", "unsafeHttpWhitelist", "enableImmutableInstalls",
                   "checksumBehavior", "httpTimeout", "httpRetry", "networkConcurrency", "compressionLevel"}


def probe_candidate(project, run_dir, assignment, additions, *, apply, run, source_root=None):
    """Return a native result or None when a complete small input set is unknown.

    Only registry/workspace dependencies and self-contained pinned Yarn bundles
    qualify. Local files, patches, plugins and other path-sensitive setups keep
    the existing full-source path. No bytes or proofs are adopted from this probe.
    """
    try:
        project = Path(project).resolve()
        boundary = Path(source_root or project).resolve()
        if project != boundary and boundary not in project.parents:
            return None
        # Reject unsupported managers before workspace glob expansion or Git
        # topology discovery; an npm/Classic project must pay only a few stats.
        cursor = project
        while True:
            if any((cursor / name).is_file() for name in ("yarn.lock", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml")):
                manifest = json.loads((cursor / "package.json").read_text(encoding="utf-8-sig"))
                if not re.match(r"^yarn@[34]\.", str(manifest.get("packageManager", ""))):
                    return None
                break
            if cursor == boundary:
                return None
            cursor = cursor.parent
        topology = resolve_project_topology(project, allow_discovery=False, require_supported=True)
        if not lockfile_preflight_args(topology.profile):
            return None
        root = topology.package_manager_root
        manifests = {root / "package.json", project / "package.json", *topology.workspace_member_manifests}
        if len(manifests) > 500:
            return None
        files = set(manifests) | {topology.lockfile}
        for file in manifests:
            manifest = json.loads(file.read_text(encoding="utf-8-sig"))
            for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "resolutions"):
                for spec in (manifest.get(section) or {}).values():
                    if not isinstance(spec, str) or (":" in spec and not spec.startswith(("npm:", "workspace:"))):
                        return None
        lock = topology.lockfile.read_text(encoding="utf-8-sig")
        if re.search(r"(?:file:|portal:|link:|patch:|git\+|https?://)", lock):
            return None
        # npm configuration may contain path-sensitive certificates/auth files.
        if (root / ".npmrc").exists():
            return None
        config_path = root / ".yarnrc.yml"
        if not config_path.is_file():
            return None
        if config_path.exists():
            config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
            if not isinstance(config, dict) or set(config) - _ALLOWED_CONFIG or config.get("enableGlobalCache") is not True:
                return None
            files.add(config_path)
            if config.get("yarnPath"):
                bundle = (root / config["yarnPath"]).resolve()
                bundle.relative_to(root.resolve())
                files.add(bundle)
        for name in (".env.yarn",):
            if (root / name).exists():
                return None  # Environment-file interpolation may be path-dependent.
        total = 0
        for file in files:
            file.resolve().relative_to(root.resolve())
            if file.is_symlink() or not file.is_file():
                return None
            total += file.stat().st_size
        if total > 32 * 1024 * 1024:
            return None
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, yaml.YAMLError):
        return None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="lock-plan-", dir=run_dir) as raw:
        destination = Path(raw)
        for file in files:
            relative = file.resolve().relative_to(root.resolve())
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(file.read_bytes())  # Private regular files; never hardlink sealed inputs.
        selected = destination / project.resolve().relative_to(root.resolve())
        apply(selected, assignment, additions)
        result = run(destination)  # Invoke from the explicit manager root, without Git discovery.
        emit_observability_event("planning.lockfile.probe", exitCode=result.returncode,
                                 fileCount=len(files), byteCount=total,
                                 durationMs=int((time.monotonic()-started)*1000), authority="planning-only")
        return result
