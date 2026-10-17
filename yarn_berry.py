"""Modern Yarn lock and execution semantics, separate from Yarn Classic."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote


def is_berry_lock(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open(encoding="utf-8-sig") as stream:
        return any(line.startswith("__metadata:") for _, line in zip(range(80), stream))


def lock_entries(path: Path) -> list[dict]:
    import yaml
    # Reject duplicate descriptors instead of silently selecting a resolution.
    class StrictLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
        pass
    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError(f"YARN_BERRY_LOCK_DUPLICATE_KEY: {key}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result
    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    data = yaml.load(path.read_text(encoding="utf-8-sig"), Loader=StrictLoader)
    if not isinstance(data, dict) or not isinstance(data.get("__metadata"), dict):
        raise ValueError("YARN_BERRY_LOCK_INVALID_METADATA")
    result = []
    for descriptors, entry in data.items():
        if descriptors == "__metadata":
            continue
        if not isinstance(descriptors, str) or not isinstance(entry, dict) or not entry.get("version") or not entry.get("resolution"):
            raise ValueError("YARN_BERRY_LOCK_INVALID_ENTRY")
        selectors = descriptors.split(", ")
        for section in ("dependencies", "optionalDependencies"):
            if not isinstance(entry.get(section, {}), dict):
                raise ValueError("YARN_BERRY_LOCK_INVALID_DEPENDENCIES")
        result.append({**entry, "version": str(entry["version"]), "selectors": selectors})
    return result


def lock_mapping(path: Path) -> dict[str, str]:
    result = {}
    for entry in lock_entries(path):
        for selector in entry["selectors"]:
            if selector in result:
                raise ValueError(f"YARN_BERRY_LOCK_DUPLICATE_SELECTOR: {selector}")
            result[selector] = entry["version"]
    return result


def registry_identity(entry: dict, fallback: str) -> str | None:
    resolution = str(entry.get("resolution") or "")
    if not resolution:  # Classic entry
        return fallback
    # A patched npm package still needs vulnerability coverage for its base.
    resolution = unquote(resolution)
    match = re.search(r"(?:^|patch:)((?:@[^/]+/)?[^@:#]+)@npm:", resolution)
    return match.group(1) if match else None


def require_runtime_version(profile, executable: str, root: Path, environment: dict) -> str:
    from baseline_constraint_verifier import _command_prefix
    argv, _ = _command_prefix(executable, ["--version"])
    completed = subprocess.run(argv, cwd=root, env=dict(environment), capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=30)
    version = completed.stdout.strip()
    declared = profile.declared_version
    if completed.returncode or version != declared:
        raise RuntimeError(f"PACKAGE_MANAGER_RUNTIME_VERSION_MISMATCH: expected Yarn {declared}, observed {version or 'unavailable'}")
    return version


def update_berry_lockfile(project, package_json, lockfile, registry, config, *, runner=None):
    from package_manager_profile import resolve_package_manager_profile, install_args_for_profile
    from lockfile_consistency import resolve_executable, _command_failure, yarn_deduplication_policy, LockfileConsistencyError
    from baseline_constraint_verifier import _command_prefix
    profile = resolve_package_manager_profile(project, package_json=package_json, lockfile=lockfile)
    profile.require_authoritative_support()
    dedupe_policy = yarn_deduplication_policy(config)
    yarn2 = profile.declared_version.startswith("2.")
    if yarn2 and dedupe_policy == "required":
        raise LockfileConsistencyError("YARN2_DEDUPLICATION_UNSUPPORTED", "Yarn 2 dedupe cannot skip builds; use auto/off or Yarn 3/4")
    executable = resolve_executable(str(config.get("yarnExecutable") or "yarn"))
    env = {**os.environ, "YARN_ENABLE_IMMUTABLE_INSTALLS": "false", "YARN_ENABLE_SCRIPTS": "true"}
    if registry:
        env["YARN_NPM_REGISTRY_SERVER"] = registry
    if runner is None:
        require_runtime_version(profile, executable, project, env)
    def run(args, frozen=False):
        argv, _ = _command_prefix(executable, args)
        phase_env = {**env, "YARN_ENABLE_IMMUTABLE_INSTALLS": "true" if frozen else "false"}
        result = (runner or subprocess.run)(argv, cwd=str(project), env=phase_env, text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise _command_failure("YARN_BERRY_LOCKFILE_UPDATE_FAILED", argv, result)
        return argv
    extra = config.get("yarnInstallArgs")
    if extra:
        # Classic flags cannot override the modern verification contract.
        raise ValueError("YARN_BERRY_INSTALL_ARGS_UNSUPPORTED: remove Classic yarnInstallArgs")
    update = run(install_args_for_profile(profile, ignore_scripts=True))
    dedupe = []
    if dedupe_policy != "off" and not yarn2:
        dedupe = run(["dedupe", "--mode=skip-build"])
    validation = run(install_args_for_profile(profile, ignore_scripts=True, frozen=True), frozen=True)
    return update, dedupe, validation


def validate_resolver_seed(root: Path):
    """Readiness of a post skip-build seed; never verification authority."""
    from block_psi57_yarn1_resolver_seed import ResolverSeedError
    import gzip
    root = Path(root)
    marker = root / ".yarn" / "install-state.gz"
    state = root / "node_modules" / ".yarn-state.yml"
    try:
        if marker.is_symlink() or state.is_symlink() or not state.is_file():
            raise ValueError("missing canonical node-modules state")
        with gzip.open(marker, "rb") as stream:
            if not stream.read(1):
                raise ValueError("empty install state")
    except (OSError, ValueError) as exc:
        raise ResolverSeedError(f"YARN_BERRY_SEED_UNAVAILABLE: {exc}") from exc
    return ()


def prepare_resolver_seed_for_lifecycle(root: Path):
    """Reset private build state so ordinary immutable install executes builds."""
    validate_resolver_seed(root)
    marker = Path(root) / ".yarn" / "install-state.gz"
    marker.unlink()
    return marker


def owning_yarn_lock(project: Path) -> Path:
    if (project / "yarn.lock").is_file():
        return project / "yarn.lock"
    from project_topology import resolve_project_topology, ProjectTopologyError
    try:
        topology = resolve_project_topology(project, allow_discovery=False, require_supported=False)
    except ProjectTopologyError:
        return project / "yarn.lock"
    return topology.lockfile if topology.profile.manager == "yarn" else project / "yarn.lock"
