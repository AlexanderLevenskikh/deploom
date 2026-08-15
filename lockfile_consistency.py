#!/usr/bin/env python3
"""Select, validate and (when explicitly requested) refresh project lockfiles.

The roadmap must never silently read a stale or wrong lockfile.  In particular,
Yarn projects sometimes acquire an incidental root package-lock.json during a
manual npm audit.  Older generator versions preferred package-lock.json by file
name, so the dashboard could keep showing pre-migration versions even though
package.json had already changed.

This module provides a deterministic preflight used by the generator:

* select the package manager from packageManager + lockfiles;
* reject ambiguous/mixed root lockfiles;
* verify direct package.json declarations are represented by the selected lock;
* optionally refresh the selected lockfile;
* for Yarn, run a configured/discovered deduplication command when available
  and validate the result with a frozen install. Missing dedup tooling is not
  an error unless the project explicitly configures policy=required.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from semantic_version import NpmSpec, Version

DIRECT_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
INSTALL_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")
LOCKFILES = {
    "yarn": "yarn.lock",
    "npm": "package-lock.json",
    "npm-shrinkwrap": "npm-shrinkwrap.json",
    "pnpm": "pnpm-lock.yaml",
}


class LockfileConsistencyError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclasses.dataclass
class LockfileIssue:
    code: str
    package: str = ""
    section: str = ""
    requested: str = ""
    resolved: str = ""
    detail: str = ""

    def render(self) -> str:
        prefix = f"{self.section}:{self.package}" if self.package else self.code
        fields: List[str] = []
        if self.requested:
            fields.append(f"requested={self.requested}")
        if self.resolved:
            fields.append(f"resolved={self.resolved}")
        if self.detail:
            fields.append(self.detail)
        return f"{prefix}: " + "; ".join(fields)


@dataclasses.dataclass
class LockfileState:
    manager: str
    lockfile: Path
    declared_package_manager: str
    extra_lockfiles: List[Path]
    issues: List[LockfileIssue]
    mode: str
    updated: bool = False
    deduplicated: bool = False
    update_command: List[str] = dataclasses.field(default_factory=list)
    deduplicate_command: List[str] = dataclasses.field(default_factory=list)
    validation_command: List[str] = dataclasses.field(default_factory=list)
    deduplication_status: str = "not-applicable"
    warnings: List[str] = dataclasses.field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues

    def as_dict(self) -> Dict[str, Any]:
        return {
            "manager": self.manager,
            "lockfile": str(self.lockfile),
            "declaredPackageManager": self.declared_package_manager,
            "extraLockfiles": [str(x) for x in self.extra_lockfiles],
            "mode": self.mode,
            "valid": self.valid,
            "updated": self.updated,
            "deduplicated": self.deduplicated,
            "issues": [dataclasses.asdict(x) for x in self.issues],
            "packageJsonSha256": sha256_file(self.lockfile.parent / "package.json"),
            "lockfileSha256": sha256_file(self.lockfile),
            "updateCommand": self.update_command,
            "deduplicateCommand": self.deduplicate_command,
            "validationCommand": self.validation_command,
            "deduplicationStatus": self.deduplication_status,
            "warnings": self.warnings,
        }


def sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LockfileConsistencyError("PACKAGE_JSON_INVALID", f"{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LockfileConsistencyError("PACKAGE_JSON_INVALID", f"{path}: expected JSON object")
    return value


def normalize_manager(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    name = text.split("@", 1)[0]
    if name in {"yarn", "npm", "pnpm"}:
        return name
    return ""


def present_lockfiles(project: Path) -> Dict[str, Path]:
    found: Dict[str, Path] = {}
    for manager, filename in LOCKFILES.items():
        path = project / filename
        if path.exists():
            found[manager] = path
    return found


def select_lockfile(project: Path, package_json: Dict[str, Any], *, allow_extra_lockfiles: bool = False) -> Tuple[str, Path, str, List[Path]]:
    declared_text = str(package_json.get("packageManager") or "")
    declared = normalize_manager(declared_text)
    found = present_lockfiles(project)

    if declared:
        preferred_key = "npm" if declared == "npm" and "npm" in found else declared
        if declared == "npm" and "npm" not in found and "npm-shrinkwrap" in found:
            preferred_key = "npm-shrinkwrap"
        if preferred_key not in found:
            expected = LOCKFILES[preferred_key]
            raise LockfileConsistencyError(
                "LOCKFILE_MISSING",
                f"packageManager={declared_text!r} requires {expected} in {project}",
            )
        selected = found[preferred_key]
        extras = [path for key, path in found.items() if key != preferred_key]
    else:
        if not found:
            raise LockfileConsistencyError("LOCKFILE_MISSING", f"no supported lockfile found in {project}")
        # package-lock + npm-shrinkwrap are the same package-manager family; a
        # shrinkwrap intentionally wins when both are present.
        families = {"npm" if key in {"npm", "npm-shrinkwrap"} else key for key in found}
        if len(families) > 1:
            detail = ", ".join(sorted(path.name for path in found.values()))
            raise LockfileConsistencyError(
                "LOCKFILE_AMBIGUOUS",
                f"multiple package-manager lockfiles found ({detail}); set packageManager and remove the unrelated root lockfile",
            )
        if "yarn" in found:
            preferred_key = "yarn"
        elif "pnpm" in found:
            preferred_key = "pnpm"
        elif "npm-shrinkwrap" in found:
            preferred_key = "npm-shrinkwrap"
        else:
            preferred_key = "npm"
        selected = found[preferred_key]
        extras = [path for key, path in found.items() if key != preferred_key]
        declared = "npm" if preferred_key == "npm-shrinkwrap" else preferred_key

    if extras and not allow_extra_lockfiles:
        names = ", ".join(path.name for path in extras)
        raise LockfileConsistencyError(
            "LOCKFILE_CONFLICT",
            f"selected {selected.name} for {declared}, but unrelated root lockfile(s) also exist: {names}. Remove them before roadmap generation.",
        )
    manager = "npm" if selected.name in {"package-lock.json", "npm-shrinkwrap.json"} else declared
    return manager, selected, declared_text, extras


def split_yarn_keys(line: str) -> List[str]:
    text = line.strip().rstrip(":").strip()
    result: List[str] = []
    current = ""
    quoted = False
    quote = ""
    for char in text:
        if char in ('"', "'"):
            if not quoted:
                quoted, quote = True, char
            elif quote == char:
                quoted = False
            else:
                current += char
        elif char == "," and not quoted:
            if current.strip():
                result.append(current.strip().strip('"\''))
            current = ""
        else:
            current += char
    if current.strip():
        result.append(current.strip().strip('"\''))
    return result


def parse_yarn_lock_v1(path: Path) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    current_keys: List[str] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if any(line.startswith("__metadata:") for line in lines[:30]):
        raise LockfileConsistencyError(
            "YARN_LOCK_VERSION_UNSUPPORTED",
            f"{path} looks like Yarn Berry lockfile; configure command-based validation or use a supported parser",
        )
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            current_keys = split_yarn_keys(line)
            continue
        if current_keys and line.startswith("  version "):
            match = re.search(r'version\s+"?([^"\s]+)"?', line.strip())
            if match:
                for key in current_keys:
                    mapping[key] = match.group(1)
            current_keys = []
    return mapping


def is_non_registry_spec(spec: str) -> bool:
    value = str(spec or "").strip().lower()
    return value.startswith(("workspace:", "file:", "link:", "portal:", "git+", "github:", "http://", "https://"))


def _origin_tuple(url: str) -> Tuple[str, str, int]:
    parsed = urlparse(str(url or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if parsed.port is not None:
        port = parsed.port
    elif scheme == "https":
        port = 443
    elif scheme == "http":
        port = 80
    else:
        port = -1
    return scheme, host, port


def registry_artifact_url_allowed(registry: str, url: str) -> bool:
    """Require lockfile package artifacts to stay inside configured registry."""
    value = str(url or "").strip()
    if not registry or not value:
        return True
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return True
    registry_parsed = urlparse(registry)
    if _origin_tuple(registry) != _origin_tuple(value):
        return False
    prefix = registry_parsed.path.rstrip("/")
    path = parsed.path or ""
    return not prefix or path == prefix or path.startswith(prefix + "/")


def _yarn_lock_resolved_urls(lockfile: Path) -> List[str]:
    result: List[str] = []
    for line in lockfile.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r'^\s+resolved\s+["\']?([^"\'\s]+)', line)
        if match:
            result.append(match.group(1))
    return result


def _package_lock_resolved_urls(lockfile: Path) -> List[str]:
    try:
        data = json.loads(lockfile.read_text(encoding="utf-8"))
    except Exception:
        return []
    result: List[str] = []
    packages = data.get("packages") if isinstance(data, dict) else None
    if isinstance(packages, dict):
        for entry in packages.values():
            if isinstance(entry, dict) and entry.get("resolved"):
                result.append(str(entry["resolved"]))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("resolved"):
                result.append(str(node["resolved"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    if isinstance(data, dict):
        walk(data.get("dependencies"))
    return list(dict.fromkeys(result))


def validate_registry_artifact_urls(manager: str, lockfile: Path, registry: str) -> List[LockfileIssue]:
    if not registry:
        return []
    if manager == "yarn":
        urls = _yarn_lock_resolved_urls(lockfile)
    elif manager == "npm":
        urls = _package_lock_resolved_urls(lockfile)
    else:
        urls = []
    return [
        LockfileIssue(
            "FOREIGN_REGISTRY_URL",
            resolved=url,
            detail=f"package artifact URL is outside configured registry {registry}",
        )
        for url in urls
        if not registry_artifact_url_allowed(registry, url)
    ]


def yarn_selector_candidates(name: str, spec: str) -> List[str]:
    return [
        f"{name}@{spec}",
        f"{name}@npm:{spec}",
    ]


def semver_satisfies(version: str, spec: str) -> bool:
    try:
        normalized = version[1:] if version.startswith("v") else version
        return NpmSpec(spec).match(Version(normalized))
    except Exception:
        # Aliases, tags and internal protocols are validated by exact selector
        # presence rather than by pretending they are semver ranges.
        return True


def direct_declarations(package_json: Dict[str, Any], *, install_only: bool = False) -> Iterable[Tuple[str, str, str]]:
    sections = INSTALL_SECTIONS if install_only else DIRECT_SECTIONS
    for section in sections:
        values = package_json.get(section) or {}
        if not isinstance(values, dict):
            continue
        for name, spec in values.items():
            yield section, str(name), str(spec)


def validate_yarn_lock(project: Path, package_json: Dict[str, Any], lockfile: Path) -> List[LockfileIssue]:
    mapping = parse_yarn_lock_v1(lockfile)
    issues: List[LockfileIssue] = []
    for section, name, spec in direct_declarations(package_json):
        # A pure peer declaration need not be installed in this package.  If it
        # is also present in an install section, that declaration is checked.
        if section == "peerDependencies":
            installed_elsewhere = any(name in (package_json.get(s) or {}) for s in INSTALL_SECTIONS)
            if not installed_elsewhere:
                continue
        candidates = yarn_selector_candidates(name, spec)
        found = [(selector, mapping[selector]) for selector in candidates if selector in mapping]
        if not found:
            issues.append(LockfileIssue(
                "YARN_SELECTOR_MISSING",
                package=name,
                section=section,
                requested=spec,
                detail="exact package.json selector is absent from yarn.lock; the lockfile is stale",
            ))
            continue
        resolved = found[0][1]
        if not is_non_registry_spec(spec) and not semver_satisfies(resolved, spec):
            issues.append(LockfileIssue(
                "YARN_RESOLVED_OUTSIDE_SPEC",
                package=name,
                section=section,
                requested=spec,
                resolved=resolved,
                detail="resolved version does not satisfy package.json",
            ))
    return issues


def _package_lock_root(data: Dict[str, Any]) -> Dict[str, Any]:
    packages = data.get("packages") or {}
    if isinstance(packages, dict) and isinstance(packages.get(""), dict):
        return packages[""]
    return {}


def _package_lock_version(data: Dict[str, Any], name: str) -> str:
    packages = data.get("packages") or {}
    if isinstance(packages, dict):
        entry = packages.get(f"node_modules/{name}")
        if isinstance(entry, dict) and entry.get("version"):
            return str(entry["version"])
    deps = data.get("dependencies") or {}
    if isinstance(deps, dict):
        entry = deps.get(name)
        if isinstance(entry, dict) and entry.get("version"):
            return str(entry["version"])
    return ""


def validate_package_lock(project: Path, package_json: Dict[str, Any], lockfile: Path) -> List[LockfileIssue]:
    try:
        data = json.loads(lockfile.read_text(encoding="utf-8"))
    except Exception as exc:
        return [LockfileIssue("PACKAGE_LOCK_INVALID", detail=str(exc))]
    if not isinstance(data, dict):
        return [LockfileIssue("PACKAGE_LOCK_INVALID", detail="expected JSON object")]
    root = _package_lock_root(data)
    issues: List[LockfileIssue] = []
    for section, name, spec in direct_declarations(package_json):
        if section == "peerDependencies":
            installed_elsewhere = any(name in (package_json.get(s) or {}) for s in INSTALL_SECTIONS)
            if not installed_elsewhere:
                continue
        root_values = root.get(section) if isinstance(root.get(section), dict) else {}
        if root and str(root_values.get(name, "")) != spec:
            issues.append(LockfileIssue(
                "PACKAGE_LOCK_SPEC_MISMATCH",
                package=name,
                section=section,
                requested=spec,
                resolved=str(root_values.get(name, "")),
                detail="package-lock root declaration differs from package.json",
            ))
        resolved = _package_lock_version(data, name)
        if not resolved:
            issues.append(LockfileIssue(
                "PACKAGE_LOCK_ENTRY_MISSING",
                package=name,
                section=section,
                requested=spec,
                detail="direct dependency has no resolved package-lock entry",
            ))
        elif not is_non_registry_spec(spec) and not semver_satisfies(resolved, spec):
            issues.append(LockfileIssue(
                "PACKAGE_LOCK_RESOLVED_OUTSIDE_SPEC",
                package=name,
                section=section,
                requested=spec,
                resolved=resolved,
            ))
    return issues


def validate_lockfile(project: Path, package_json: Dict[str, Any], manager: str, lockfile: Path) -> List[LockfileIssue]:
    if manager == "yarn":
        return validate_yarn_lock(project, package_json, lockfile)
    if manager == "npm":
        return validate_package_lock(project, package_json, lockfile)
    if manager == "pnpm":
        # We deliberately avoid a partial YAML parser.  pnpm validation is done
        # through its own frozen-lockfile command when update/command validation
        # is requested; otherwise retain an explicit unsupported marker.
        return [LockfileIssue(
            "PNPM_STATIC_VALIDATION_UNAVAILABLE",
            detail="use lockfileSync.mode=update or provide a project validation command",
        )]
    return [LockfileIssue("LOCKFILE_MANAGER_UNSUPPORTED", detail=manager)]


def resolve_executable(name: str) -> str:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if os.name == "nt" and not name.lower().endswith((".cmd", ".exe", ".bat")):
        for suffix in (".cmd", ".exe", ".bat"):
            resolved = shutil.which(name + suffix)
            if resolved:
                return resolved
    raise LockfileConsistencyError("PACKAGE_MANAGER_NOT_FOUND", f"cannot find executable: {name}")


def normalize_command(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        # posix=False preserves Windows paths and quoted arguments more reliably
        # for settings authored on Windows.
        return shlex.split(value, posix=os.name != "nt")
    raise LockfileConsistencyError("LOCKFILE_COMMAND_INVALID", f"expected string/list, got {type(value).__name__}")


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    registry: str,
    runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if registry:
        env["npm_config_registry"] = registry
        env["YARN_REGISTRY"] = registry
    invoke = runner or subprocess.run
    return invoke(
        list(command),
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _command_failure(code: str, command: Sequence[str], result: subprocess.CompletedProcess[str]) -> LockfileConsistencyError:
    detail = (result.stderr or result.stdout or "").strip()
    if len(detail) > 1800:
        detail = detail[-1800:]
    return LockfileConsistencyError(code, f"{' '.join(command)}; exit={result.returncode}; {detail}")


def discover_yarn_deduplicate_command(package_json: Dict[str, Any], config: Dict[str, Any]) -> List[str]:
    configured = normalize_command(config.get("yarnDeduplicateCommand") or config.get("deduplicateCommand"))
    if configured:
        return configured
    scripts = package_json.get("scripts") or {}
    if isinstance(scripts, dict):
        for script in ("deduplicate", "dedupe:lockfile", "lockfile:dedupe", "deps:dedupe", "dedupe"):
            if script in scripts:
                return ["yarn", "run", script]
    all_deps: Dict[str, Any] = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        values = package_json.get(section) or {}
        if isinstance(values, dict):
            all_deps.update(values)
    if "yarn-deduplicate" in all_deps:
        return ["yarn", "run", "yarn-deduplicate", "yarn.lock"]
    return []


def yarn_deduplication_policy(config: Dict[str, Any]) -> str:
    value = config.get("yarnDeduplicate", "auto")
    if isinstance(value, bool):
        return "auto" if value else "off"
    text = str(value or "auto").strip().lower()
    aliases = {"true": "auto", "false": "off", "if-available": "auto", "optional": "auto"}
    text = aliases.get(text, text)
    if text not in {"auto", "required", "off"}:
        raise LockfileConsistencyError(
            "YARN_DEDUPLICATION_POLICY_INVALID",
            "lockfileSync.yarnDeduplicate must be auto, required or off",
        )
    return text


def update_lockfile(
    project: Path,
    package_json: Dict[str, Any],
    manager: str,
    lockfile: Path,
    registry: str,
    config: Dict[str, Any],
    *,
    runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> Tuple[List[str], List[str], List[str]]:
    if manager == "yarn":
        yarn = resolve_executable(str(config.get("yarnExecutable") or "yarn"))
        install_extra = normalize_command(config.get("yarnInstallArgs"))
        if not install_extra:
            install_extra = ["--ignore-scripts", "--non-interactive"]
        update_command = [yarn, "install", *install_extra]
        if registry and "--registry" not in update_command:
            update_command += ["--registry", registry]
        result = _run(update_command, cwd=project, registry=registry, runner=runner)
        if result.returncode != 0:
            raise _command_failure("YARN_LOCKFILE_UPDATE_FAILED", update_command, result)

        dedupe_policy = yarn_deduplication_policy(config)
        dedupe_command: List[str] = []
        if dedupe_policy != "off":
            dedupe_command = discover_yarn_deduplicate_command(package_json, config)
            if not dedupe_command and dedupe_policy == "required":
                raise LockfileConsistencyError(
                    "YARN_DEDUPLICATION_COMMAND_MISSING",
                    "Yarn deduplication is required by settings, but no command was found.",
                )
            if dedupe_command:
                if dedupe_command[0] == "yarn":
                    dedupe_command[0] = yarn
                result = _run(dedupe_command, cwd=project, registry=registry, runner=runner)
                if result.returncode != 0:
                    raise _command_failure("YARN_DEDUPLICATION_FAILED", dedupe_command, result)

        validation_command = [yarn, "install", "--frozen-lockfile", "--ignore-scripts", "--non-interactive"]
        if registry:
            validation_command += ["--registry", registry]
        result = _run(validation_command, cwd=project, registry=registry, runner=runner)
        if result.returncode != 0:
            raise _command_failure("YARN_LOCKFILE_POST_DEDUPE_INVALID", validation_command, result)
        return update_command, dedupe_command, validation_command

    if manager == "npm":
        npm = resolve_executable(str(config.get("npmExecutable") or "npm"))
        extra = normalize_command(config.get("npmInstallArgs")) or ["--package-lock-only", "--ignore-scripts", "--legacy-peer-deps", "--audit=false", "--fund=false"]
        update_command = [npm, "install", *extra]
        if registry and "--registry" not in update_command:
            update_command += ["--registry", registry]
        result = _run(update_command, cwd=project, registry=registry, runner=runner)
        if result.returncode != 0:
            raise _command_failure("NPM_LOCKFILE_UPDATE_FAILED", update_command, result)
        return update_command, [], []

    if manager == "pnpm":
        pnpm = resolve_executable(str(config.get("pnpmExecutable") or "pnpm"))
        extra = normalize_command(config.get("pnpmInstallArgs")) or ["install", "--lockfile-only", "--ignore-scripts"]
        update_command = [pnpm, *extra]
        if registry and "--registry" not in update_command:
            update_command += ["--registry", registry]
        result = _run(update_command, cwd=project, registry=registry, runner=runner)
        if result.returncode != 0:
            raise _command_failure("PNPM_LOCKFILE_UPDATE_FAILED", update_command, result)
        return update_command, [], []

    raise LockfileConsistencyError("LOCKFILE_MANAGER_UNSUPPORTED", manager)


def ensure_lockfile_consistency(
    project: Path,
    registry: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    mode: Optional[str] = None,
    allow_update: bool = True,
    runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> LockfileState:
    cfg = dict(config or {})
    effective_mode = str(mode or cfg.get("mode") or "validate").strip().lower()
    if effective_mode not in {"validate", "update", "off"}:
        raise LockfileConsistencyError("LOCKFILE_MODE_INVALID", effective_mode)
    package_json = read_json(project / "package.json")
    manager, lockfile, declared, extras = select_lockfile(
        project,
        package_json,
        allow_extra_lockfiles=bool(cfg.get("allowExtraLockfiles", False)),
    )
    state = LockfileState(manager, lockfile, declared, extras, [], effective_mode)
    if manager == "yarn":
        policy = yarn_deduplication_policy(cfg)
        state.deduplication_status = "disabled" if policy == "off" else "not-run"
    if effective_mode == "off":
        return state

    enforce_registry_artifacts = bool(cfg.get("enforceRegistryArtifacts", True))
    state.issues = validate_lockfile(project, package_json, manager, lockfile)
    registry_url_issues = (
        validate_registry_artifact_urls(manager, lockfile, registry)
        if enforce_registry_artifacts
        else []
    )
    state.issues.extend(registry_url_issues)
    if not state.issues:
        return state
    if effective_mode == "validate":
        if registry_url_issues:
            details = " | ".join(issue.render() for issue in registry_url_issues[:12])
            raise LockfileConsistencyError("FOREIGN_REGISTRY_URL", details)
        return state
    if not allow_update:
        raise LockfileConsistencyError(
            "LOCKFILE_UPDATE_NOT_ALLOWED_ON_SOURCE_CHECKOUT",
            "the verified source branch has a stale lockfile. Update/commit it separately; ordinary generation on the integration branch uses currentMode=update by default.",
        )

    update_command, dedupe_command, validation_command = update_lockfile(
        project,
        package_json,
        manager,
        lockfile,
        registry,
        cfg,
        runner=runner,
    )
    # Re-read package.json in case a project hook/script intentionally normalized
    # it (scripts are disabled by default, but custom commands may be configured).
    package_json = read_json(project / "package.json")
    state.issues = validate_lockfile(project, package_json, manager, lockfile)
    if enforce_registry_artifacts:
        state.issues.extend(validate_registry_artifact_urls(manager, lockfile, registry))
    state.updated = True
    state.deduplicated = manager == "yarn" and bool(dedupe_command)
    if manager == "yarn":
        policy = yarn_deduplication_policy(cfg)
        if dedupe_command:
            state.deduplication_status = "completed"
        elif policy == "off":
            state.deduplication_status = "disabled"
        else:
            state.deduplication_status = "not-available"
            state.warnings.append("Yarn deduplication command was not found; lockfile update continued without deduplication")
    state.update_command = update_command
    state.deduplicate_command = dedupe_command
    state.validation_command = validation_command
    if state.issues:
        details = " | ".join(issue.render() for issue in state.issues[:12])
        if any(issue.code == "FOREIGN_REGISTRY_URL" for issue in state.issues):
            raise LockfileConsistencyError("FOREIGN_REGISTRY_URL", details)
        raise LockfileConsistencyError("LOCKFILE_STILL_STALE_AFTER_UPDATE", details)
    return state


def exact_package_lock_version(lockfile: Path, package_name: str) -> Optional[str]:
    """Return the exact direct resolution from an npm lockfile.

    This is intentionally used only when the selected project package manager
    is npm. A Yarn project must never use an incidental package-lock.json as a
    dashboard source.
    """
    try:
        data = json.loads(lockfile.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = _package_lock_version(data, package_name)
    return value or None


def exact_yarn_lock_version(lockfile: Path, package_name: str, spec: str) -> Optional[str]:
    mapping = parse_yarn_lock_v1(lockfile)
    for selector in yarn_selector_candidates(package_name, spec):
        if selector in mapping:
            return mapping[selector]
    return None
