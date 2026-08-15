#!/usr/bin/env python3
"""
Validate a finished dependency update against dependency-roadmap JSON.

The generator produces a plan; this script is the post-merge guard. It compares
the selected target mode with the current package.json and lockfile so branch
merges cannot silently leave packages below target, on prerelease versions, or
above target without an explicit decision.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from cli_io import configure_utf8_stdio
from lockfile_consistency import (
    LockfileConsistencyError,
    registry_artifact_url_allowed,
    select_lockfile,
    validate_registry_artifact_urls,
)

DIRECT_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
NO_ACTION_VALUES = {"", "-", "—", "нет действия", "ничего не делать"}
VersionInfo = Tuple[int, int, int, str]

STATUS_RANK = {"red": 0, "yellow": 1, "green": 2}


def expected_final_status(target_mode: str, original_roadmap: Dict[str, Any], project: str) -> str:
    if target_mode in {"yellow", "green"}:
        return target_mode
    health = (original_roadmap.get("project_health") or {}).get(project) or {}
    current = str(health.get("status") or "red").lower()
    if current == "red":
        return "yellow"
    return "green"


def validate_final_status(
    original_roadmap: Dict[str, Any],
    final_roadmap: Dict[str, Any],
    project: str,
    target_mode: str,
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    findings: List[Dict[str, str]] = []
    expected = expected_final_status(target_mode, original_roadmap, project)
    final_health = (final_roadmap.get("project_health") or {}).get(project)
    if not isinstance(final_health, dict):
        findings.append({
            "severity": "error",
            "code": "final-health-missing",
            "package": "<project>",
            "target": expected,
            "actual": "missing",
            "detail": "fresh final roadmap has no project_health entry for the project",
        })
        return findings, {"expectedStatus": expected, "actualStatus": None, "remainingActions": []}

    actual = str(final_health.get("status") or "unknown").lower()
    actual_rank = STATUS_RANK.get(actual, -1)
    expected_rank = STATUS_RANK.get(expected, 99)
    if actual_rank < expected_rank:
        findings.append({
            "severity": "error",
            "code": "target-status-not-reached",
            "package": "<project>",
            "target": expected,
            "actual": actual,
            "detail": f"fresh merged-state roadmap status is {actual}; closure cycle is required",
        })

    remaining: List[str] = []
    try:
        _, rows = project_rows(final_roadmap, project)
        # `default` is relative to the current health. Once a red project reaches
        # yellow, a freshly generated default plan may legitimately point at green.
        # Validate closure against the fixed status promised by the original run.
        closure_mode = expected
        for row in action_rows(rows, closure_mode):
            name = str(row.get("name") or "")
            target = str(row.get(target_field(closure_mode)) or "")
            if not name:
                continue
            remaining.append(f"{name}->{target}")
            findings.append({
                "severity": "error",
                "code": "closure-action-remains",
                "package": name,
                "target": target,
                "actual": str(row.get("current_version") or row.get("current") or "unknown"),
                "detail": f"fresh roadmap still selects this package for {expected}; create a closure branch or document a blocker",
            })
    except ValueError as exc:
        findings.append({
            "severity": "error",
            "code": "final-roadmap-project-invalid",
            "package": "<project>",
            "target": expected,
            "actual": actual,
            "detail": str(exc),
        })

    return findings, {"expectedStatus": expected, "actualStatus": actual, "remainingActions": remaining}


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def direct_dependencies(package_json: Dict[str, Any]) -> Dict[Tuple[str, str], str]:
    """Return direct declarations keyed by (section, package).

    A package may legally be declared in more than one section. Keying only by
    name used to hide such duplicates and could validate the wrong declaration.
    """
    result: Dict[Tuple[str, str], str] = {}
    for section in DIRECT_SECTIONS:
        deps = package_json.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            result[(section, str(name))] = str(spec)
    return result


def section_for_kind(kind: str) -> str:
    return {
        "runtime": "dependencies",
        "dev": "devDependencies",
        "optional": "optionalDependencies",
        "peer": "peerDependencies",
    }.get(str(kind or ""), "dependencies")


def version_text(value: str) -> Optional[str]:
    m = re.search(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", value or "")
    return m.group(1) if m else None


def parse_version(value: str) -> Optional[VersionInfo]:
    text = version_text(value)
    if not text:
        return None
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


PrereleaseIdentifierKey = Tuple[int, int, str]
PrereleaseKey = Tuple[PrereleaseIdentifierKey, ...]


def prerelease_key(prerelease: str) -> PrereleaseKey:
    """SemVer ?11 precedence: numeric identifiers compare numerically and below text."""
    result: List[PrereleaseIdentifierKey] = []
    for identifier in prerelease.split(".") if prerelease else ():
        if identifier.isdigit():
            result.append((0, int(identifier), ""))
        else:
            result.append((1, 0, identifier))
    return tuple(result)


def version_key(version: VersionInfo) -> Tuple[int, int, int, int, PrereleaseKey]:
    major, minor, patch, prerelease = version
    # Stable semver is greater than the same version with a prerelease suffix.
    return (major, minor, patch, 0 if prerelease else 1, prerelease_key(prerelease))


def version_lt(left: VersionInfo, right: VersionInfo) -> bool:
    return version_key(left) < version_key(right)


def version_gt(left: VersionInfo, right: VersionInfo) -> bool:
    return version_key(left) > version_key(right)


def has_prerelease(version: VersionInfo) -> bool:
    return bool(version[3])


def target_is_action(value: str) -> bool:
    return (value or "").strip().lower() not in NO_ACTION_VALUES and parse_version(value) is not None


def strip_spec(spec: str) -> str:
    s = str(spec or "").strip()
    s = re.sub(r"^(npm:)?[\^~<>= ]+", "", s)
    m = re.search(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", s)
    return m.group(1) if m else s


def find_lockfile(project_dir: Path) -> Optional[Path]:
    for name in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock"):
        path = project_dir / name
        if path.exists():
            return path
    return None


def package_lock_version(lock_path: Path, package_name: str) -> Optional[str]:
    try:
        data = read_json(lock_path)
    except Exception:
        return None
    packages = data.get("packages") or {}
    key = f"node_modules/{package_name}"
    if isinstance(packages.get(key), dict) and isinstance(packages[key].get("version"), str):
        return packages[key]["version"]
    deps = data.get("dependencies") or {}
    if isinstance(deps.get(package_name), dict) and isinstance(deps[package_name].get("version"), str):
        return deps[package_name]["version"]
    return None


def split_yarn_keys(key_line: str) -> List[str]:
    s = key_line.strip().rstrip(":").strip()
    parts: List[str] = []
    cur = ""
    in_quote = False
    quote_char = ""
    for ch in s:
        if ch in ('"', "'"):
            if not in_quote:
                in_quote = True
                quote_char = ch
            elif quote_char == ch:
                in_quote = False
            else:
                cur += ch
            continue
        if ch == "," and not in_quote:
            if cur.strip():
                parts.append(cur.strip().strip('"\''))
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip().strip('"\''))
    return parts


def parse_yarn_lock_v1(lock_path: Path) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    current_keys: List[str] = []
    try:
        lines = lock_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return mapping
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            current_keys = split_yarn_keys(line)
            continue
        if current_keys and line.startswith("  version "):
            m = re.search(r'version\s+"?([^"\s]+)"?', line.strip())
            if m:
                for key in current_keys:
                    mapping[key] = m.group(1)
            current_keys = []
    return mapping


def yarn_lock_version(lock_path: Path, package_name: str, spec: str) -> Optional[str]:
    mapping = parse_yarn_lock_v1(lock_path)
    candidates = [
        f"{package_name}@{spec}",
        f"{package_name}@npm:{spec}",
        f"{package_name}@{strip_spec(spec)}",
    ]
    for candidate in candidates:
        if candidate in mapping:
            return mapping[candidate]
    prefix = f"{package_name}@"
    hits = [version for key, version in mapping.items() if key.startswith(prefix)]
    return hits[0] if len(set(hits)) == 1 else None


def resolved_current_version(project_dir: Path, package_name: str, spec: str) -> Tuple[str, str]:
    lock = find_lockfile(project_dir)
    fallback = strip_spec(spec)
    if not lock:
        return fallback, "package.json spec; lockfile not found"
    if lock.name in ("package-lock.json", "npm-shrinkwrap.json"):
        version = package_lock_version(lock, package_name)
        if version:
            return version, lock.name
    elif lock.name == "yarn.lock":
        version = yarn_lock_version(lock, package_name, spec)
        if version:
            return version, lock.name
    return fallback, f"package.json spec; exact version not found in {lock.name}"


def spec_for_target(requested_spec: str, target: str) -> str:
    version = version_text(target) or target
    spec = (requested_spec or "").strip()
    if not version or not target_is_action(version):
        return version or target
    if spec.startswith("^"):
        return "^" + version
    if spec.startswith("~"):
        return "~" + version
    if re.match(r"^(>=|>|<=|<|=)", spec):
        return "^" + version
    if re.match(r"^(workspace:|file:|link:|portal:|git\+|https?:)", spec):
        return version
    return version


def target_field(mode: str) -> str:
    if mode not in {"default", "yellow", "green"}:
        raise ValueError("--target-mode must be one of: default, yellow, green")
    return f"target_{mode}"


def project_rows(roadmap: Dict[str, Any], project: Optional[str]) -> Tuple[str, List[Dict[str, Any]]]:
    projects = roadmap.get("projects") or {}
    if not isinstance(projects, dict) or not projects:
        raise ValueError("roadmap JSON has no projects")
    if project:
        rows = projects.get(project)
        if rows is None:
            available = ", ".join(sorted(projects))
            raise ValueError(f"project not found in roadmap JSON: {project}; available: {available}")
        return project, list(rows)
    if len(projects) != 1:
        available = ", ".join(sorted(projects))
        raise ValueError(f"--project is required when roadmap has multiple projects: {available}")
    name = next(iter(projects))
    return name, list(projects[name])


def action_rows(rows: Iterable[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    field = target_field(mode)
    result = []
    for row in rows:
        target = str(row.get(field) or "")
        if target_is_action(target) and parse_version(target):
            result.append(row)
    return result


def compare_package(
    project_dir: Path,
    direct: Dict[Tuple[str, str], str],
    row: Dict[str, Any],
    mode: str,
    strict_above_target: bool,
) -> List[Dict[str, str]]:
    name = str(row.get("name") or "")
    requested_spec = str(row.get("requested_spec") or "")
    target = str(row.get(target_field(mode)) or "")
    target_version = parse_version(target)
    expected_spec = spec_for_target(requested_spec, target)
    findings: List[Dict[str, str]] = []

    expected_section = str(row.get("section") or section_for_kind(str(row.get("kind") or "")))
    key = (expected_section, name)
    action = str(row.get("action") or "update").lower()
    if action == "remove":
        if key not in direct:
            return []
        return [{
            "severity": "error",
            "code": "remove-action-not-applied",
            "package": name,
            "target": "absent",
            "actual": direct[key],
            "detail": f"scope action=remove requires deleting the direct declaration from {expected_section}",
        }]
    if key not in direct:
        other_sections = sorted(section for section, package in direct if package == name)
        if other_sections:
            return [{
                "severity": "error",
                "code": "wrong-section",
                "package": name,
                "target": target,
                "actual": ", ".join(other_sections),
                "detail": f"expected direct declaration in {expected_section}",
            }]
        return [{
            "severity": "error",
            "code": "missing-direct",
            "package": name,
            "target": target,
            "actual": "absent",
            "detail": f"package is in roadmap action rows but absent from {expected_section}",
        }]

    section, actual_spec = expected_section, direct[key]
    actual_version = parse_version(actual_spec)
    resolved, source = resolved_current_version(project_dir, name, actual_spec)
    resolved_version = parse_version(resolved)
    comparable_version = resolved_version or actual_version

    if comparable_version and target_version:
        if version_lt(comparable_version, target_version):
            findings.append({
                "severity": "error",
                "code": "below-target",
                "package": name,
                "target": str(target),
                "actual": str(resolved),
                "detail": f"{source}; expected at least {target}",
            })
        elif strict_above_target and version_gt(comparable_version, target_version):
            findings.append({
                "severity": "warning",
                "code": "above-target",
                "package": name,
                "target": str(target),
                "actual": str(resolved),
                "detail": f"{source}; expected {target} unless peer/registry reason is documented",
            })

    if actual_version and target_version and has_prerelease(actual_version) and not has_prerelease(target_version):
        findings.append({
            "severity": "error",
            "code": "prerelease-for-stable-target",
            "package": name,
            "target": str(target),
            "actual": actual_spec,
            "detail": "package.json uses prerelease while roadmap target is stable",
        })

    lock = find_lockfile(project_dir)
    if lock and expected_section != "peerDependencies" and source.startswith("package.json spec; exact version not found"):
        findings.append({
            "severity": "error",
            "code": "lock-entry-missing",
            "package": name,
            "target": str(target),
            "actual": actual_spec,
            "detail": source,
        })

    if version_text(expected_spec) and version_text(actual_spec) and actual_version and target_version:
        if version_lt(actual_version, target_version):
            findings.append({
                "severity": "error",
                "code": "manifest-below-target",
                "package": name,
                "target": expected_spec,
                "actual": actual_spec,
                "detail": f"{section} spec is below selected roadmap target",
            })

    return findings


def scope_boundary_findings(
    rows: Iterable[Dict[str, Any]],
    current_direct: Dict[Tuple[str, str], str],
) -> List[Dict[str, str]]:
    """Reject changes to rows that the exact manifest marked as deferred/excluded."""
    findings: List[Dict[str, str]] = []
    for row in rows:
        if bool(row.get("shouldUpdate")):
            continue
        name = str(row.get("name") or row.get("package") or "")
        section = str(row.get("section") or section_for_kind(str(row.get("kind") or "")))
        expected = str(row.get("requested_spec") or row.get("requestedSpec") or "")
        actual = current_direct.get((section, name))
        if not name or not expected or actual == expected:
            continue
        findings.append({
            "severity": "error",
            "code": "out-of-scope-direct-change",
            "package": name,
            "target": expected,
            "actual": actual or "absent",
            "detail": f"{section} was deferred/excluded by the exact scope manifest and must remain unchanged",
        })
    return findings


def extra_direct_changes(
    baseline_package_json: Optional[Path],
    current_direct: Dict[Tuple[str, str], str],
    scoped_keys: set[Tuple[str, str]],
) -> List[Dict[str, str]]:
    if not baseline_package_json:
        return []
    baseline = direct_dependencies(read_json(baseline_package_json))
    findings: List[Dict[str, str]] = []
    all_keys = sorted(set(baseline) | set(current_direct))
    for key in all_keys:
        if key in scoped_keys:
            continue
        before = baseline.get(key)
        after = current_direct.get(key)
        if before == after:
            continue
        section, name = key
        findings.append({
            "severity": "warning",
            "code": "out-of-scope-direct-change",
            "package": f"{section}:{name}",
            "target": "not in selected roadmap action rows",
            "actual": f"{before or 'absent'} -> {after or 'absent'}",
            "detail": "direct dependency declaration changed outside selected update scope",
        })
    return findings


def fnv1a_scope_hash(rows: Iterable[Dict[str, Any]], hash_version: int = 1) -> str:
    parts = []
    for row in rows:
        should_update = bool(row.get("shouldUpdate"))
        fields = [
            str(row.get("project") or "—"),
            str(row.get("group") or "—"),
            str(row.get("subgroup") or ""),
            str(row.get("kind") or "—"),
            str(row.get("package") or row.get("name") or "—"),
            str(row.get("current") or "—"),
            str(row.get("target") or "—"),
            "true" if should_update else "false",
        ]
        if hash_version >= 2:
            fields.extend([
                str(row.get("action") or ("update" if should_update else "deferred")),
                str(row.get("testPolicy") or row.get("test_policy") or ""),
                str(row.get("testReason") or row.get("test_reason") or ""),
            ])
        if hash_version >= 3:
            fields.extend([
                str(row.get("lagPolicyMonths") or row.get("lag_policy_months") or ""),
                str(row.get("lagPolicyTarget") or row.get("lag_policy_target") or "—"),
                str(row.get("targetReason") or row.get("target_reason") or "—"),
            ])
        if hash_version >= 4:
            fields.extend([
                str(row.get("targetArtifactStatus") or row.get("target_artifact_status") or ""),
                str(row.get("targetArtifactUrl") or row.get("target_artifact_url") or ""),
                str(row.get("targetArtifactError") or row.get("target_artifact_error") or ""),
                str(row.get("compatibilityCohort") or row.get("compatibility_cohort") or ""),
                str(row.get("compatibilityNote") or row.get("compatibility_note") or ""),
            ])
        if hash_version >= 5:
            fields.extend([
                "true" if bool(row.get("scopeExcluded") or row.get("scope_excluded")) else "false",
                str(row.get("exclusionReason") or row.get("exclusion_reason") or ""),
                str(row.get("exclusionSource") or row.get("exclusion_source") or ""),
            ])
        parts.append("|".join(fields))
    text = "\n".join(sorted(parts))
    # Match JavaScript String.charCodeAt(): hash UTF-16 code units, not Python Unicode code points.
    encoded = text.encode("utf-16-le")
    value = 2166136261
    for index in range(0, len(encoded), 2):
        code_unit = int.from_bytes(encoded[index:index + 2], "little")
        value ^= code_unit
        value = (value * 16777619) & 0xFFFFFFFF
    return f"{value:08x}"


def read_scope_manifest(path: Path, project: Optional[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    data = read_json(path)
    registry = str(data.get("registry") or "").strip()
    manifest_hash_version = int(data.get("scopeHashVersion") or 1)
    registry_policy = data.get("registryPolicy") or {}
    require_target_artifact = manifest_hash_version >= 4 or bool(
        isinstance(registry_policy, dict) and registry_policy.get("requireTargetTarball")
    )
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("scope manifest must contain rows[]")
    normalized: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str]] = set()
    manifest_projects: set[str] = set()
    columns = data.get("columns")
    if columns is not None and (
        not isinstance(columns, list)
        or not columns
        or not all(isinstance(column, str) and column for column in columns)
    ):
        raise ValueError("scope manifest columns must be a non-empty string array")
    for raw_value in rows:
        raw: Dict[str, Any]
        if isinstance(raw_value, dict):
            raw = raw_value
        elif isinstance(raw_value, list) and isinstance(columns, list):
            if len(raw_value) != len(columns):
                raise ValueError(
                    f"compact scope row length mismatch: expected {len(columns)}, actual {len(raw_value)}"
                )
            raw = dict(zip(columns, raw_value))
        else:
            raise ValueError("scope manifest rows must be objects or compact arrays with columns[]")
        row_project = str(raw.get("project") or "")
        if row_project:
            manifest_projects.add(row_project)
        if project and row_project and row_project != project:
            continue
        name = str(raw.get("package") or raw.get("name") or "")
        section = str(raw.get("section") or section_for_kind(str(raw.get("kind") or "")))
        if not name:
            raise ValueError("scope manifest row has no package/name")
        key = (row_project, section, name)
        if key in seen:
            raise ValueError(f"duplicate scope row: {row_project}:{section}:{name}")
        seen.add(key)
        item = dict(raw)
        item["name"] = name
        item["section"] = section
        item["requested_spec"] = str(raw.get("requestedSpec") or raw.get("requested_spec") or "")
        item["target_default"] = str(raw.get("target") or "")
        item["target_yellow"] = str(raw.get("target") or "")
        item["target_green"] = str(raw.get("target") or "")

        lag_policy_target = str(raw.get("lagPolicyTarget") or raw.get("lag_policy_target") or "")
        try:
            lag_policy_months = int(raw.get("lagPolicyMonths") or raw.get("lag_policy_months") or 12)
        except (TypeError, ValueError):
            lag_policy_months = 12
        current_version = parse_version(str(raw.get("current") or ""))
        policy_version = parse_version(lag_policy_target)
        scope_excluded = bool(raw.get("scopeExcluded") or raw.get("scope_excluded"))
        exclusion_reason = str(raw.get("exclusionReason") or raw.get("exclusion_reason") or "").strip()
        action = str(raw.get("action") or ("update" if bool(raw.get("shouldUpdate")) else "deferred"))
        if scope_excluded:
            if action != "excluded":
                raise ValueError(
                    f"SCOPE_MANIFEST_MISMATCH: excluded row must use action=excluded for "
                    f"{row_project}:{section}:{name}"
                )
            if bool(raw.get("shouldUpdate")):
                raise ValueError(
                    f"SCOPE_MANIFEST_MISMATCH: excluded row cannot have shouldUpdate=true for "
                    f"{row_project}:{section}:{name}"
                )
            if not exclusion_reason:
                raise ValueError(
                    f"SCOPE_MANIFEST_MISMATCH: excluded row has no exclusionReason for "
                    f"{row_project}:{section}:{name}"
                )
        if (
            lag_policy_months < 12
            and current_version is not None
            and policy_version is not None
            and version_lt(current_version, policy_version)
            and not bool(raw.get("shouldUpdate"))
            and not scope_excluded
        ):
            raise ValueError(
                "ROADMAP_TARGET_DESYNC: strict lag policy requires update "
                f"for {row_project}:{section}:{name} "
                f"({raw.get('current')} -> {lag_policy_target}), but target is deferred"
            )
        should_update = bool(raw.get("shouldUpdate"))
        action = str(raw.get("action") or ("update" if should_update else "deferred"))
        target = str(raw.get("target") or "")
        if should_update and action == "update" and require_target_artifact:
            artifact_status = str(raw.get("targetArtifactStatus") or raw.get("target_artifact_status") or "")
            artifact_url = str(raw.get("targetArtifactUrl") or raw.get("target_artifact_url") or "")
            if artifact_status not in {"available", "current-installed"}:
                raise ValueError(
                    f"REGISTRY_TARGET_UNAVAILABLE: {row_project}:{section}:{name}@{target}; "
                    f"targetArtifactStatus={artifact_status or 'missing'}"
                )
            if artifact_status == "current-installed" and target != str(raw.get("current") or ""):
                raise ValueError(
                    f"REGISTRY_TARGET_UNAVAILABLE: current-installed evidence cannot prove a new target "
                    f"for {row_project}:{section}:{name}@{target}"
                )
            if artifact_status == "available":
                if not artifact_url:
                    raise ValueError(
                        f"REGISTRY_TARGET_UNAVAILABLE: missing targetArtifactUrl for "
                        f"{row_project}:{section}:{name}@{target}"
                    )
                if registry and not registry_artifact_url_allowed(registry, artifact_url):
                    raise ValueError(
                        f"FOREIGN_REGISTRY_URL: {row_project}:{section}:{name}@{target} "
                        f"uses {artifact_url}, configured registry={registry}"
                    )
        normalized.append(item)
    expected_scope: Dict[str, Any] = data
    project_scopes = data.get("projectScopes") or {}
    if project and isinstance(project_scopes, dict) and isinstance(project_scopes.get(project), dict):
        expected_scope = project_scopes[project]
    elif project and len(manifest_projects) > 1:
        raise ValueError(f"multi-project scope manifest has no projectScopes entry for {project}")

    expected_selected = expected_scope.get("selectedRows")
    if isinstance(expected_selected, int) and expected_selected != len(normalized):
        raise ValueError(f"scope selectedRows mismatch: expected {expected_selected}, actual {len(normalized)}")
    expected_actions = expected_scope.get("actionRows")
    actual_actions = sum(1 for row in normalized if bool(row.get("shouldUpdate")))
    if isinstance(expected_actions, int) and expected_actions != actual_actions:
        raise ValueError(f"scope actionRows mismatch: expected {expected_actions}, actual {actual_actions}")
    expected_excluded = expected_scope.get("excludedRows")
    actual_excluded = sum(
        1 for row in normalized
        if bool(row.get("scopeExcluded") or row.get("scope_excluded"))
    )
    if isinstance(expected_excluded, int) and expected_excluded != actual_excluded:
        raise ValueError(
            f"scope excludedRows mismatch: expected {expected_excluded}, actual {actual_excluded}"
        )
    expected_hash = str(expected_scope.get("scopeHash") or "")
    hash_version = manifest_hash_version
    actual_hash = fnv1a_scope_hash(normalized, hash_version=hash_version)
    if expected_hash and expected_hash != actual_hash:
        raise ValueError(f"scope hash mismatch: expected {expected_hash}, actual {actual_hash}")
    return normalized, {
        "scopeHash": actual_hash,
        "scopeHashVersion": hash_version,
        "selectedRows": len(normalized),
        "actionRows": actual_actions,
        "excludedRows": actual_excluded,
        "registry": registry,
        "registryPolicy": data.get("registryPolicy") or {},
    }

def print_markdown(project: str, mode: str, findings: List[Dict[str, str]], checked: int) -> None:
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    print(f"# Dependency update validation: {project}")
    print()
    print(f"Target mode: `{mode}`")
    print(f"Checked action rows: {checked}")
    print(f"Result: {'FAIL' if errors else 'PASS'} ({errors} errors, {warnings} warnings)")
    if not findings:
        return
    print()
    print("| Severity | Code | Package | Target | Actual | Detail |")
    print("|---|---|---|---|---|---|")
    for f in findings:
        print(
            "| {severity} | `{code}` | `{package}` | `{target}` | `{actual}` | {detail} |".format(
                severity=f["severity"],
                code=f["code"],
                package=f["package"],
                target=str(f["target"]).replace("|", "\\|"),
                actual=str(f["actual"]).replace("|", "\\|"),
                detail=str(f["detail"]).replace("|", "\\|"),
            )
        )


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Validate package.json/lockfile against dependency-roadmap JSON targets.")
    parser.add_argument("--roadmap-json", required=True, help="Path to generated dependency-roadmap.json")
    parser.add_argument("--project-dir", required=True, help="Frontend project directory with package.json")
    parser.add_argument("--project", help="Project name from roadmap JSON. Required when JSON contains multiple projects.")
    parser.add_argument("--target-mode", default="yellow", choices=("default", "yellow", "green"), help="Roadmap target mode to validate")
    parser.add_argument("--baseline-package-json", help="Optional pre-update package.json to report direct changes outside selected scope")
    parser.add_argument("--scope-manifest", help="Optional exact scope manifest exported by the dashboard/prompt")
    parser.add_argument("--final-roadmap-json", help="Fresh roadmap JSON generated from the final merged checkout")
    parser.add_argument("--require-final-status", action="store_true", help="Fail unless the fresh final roadmap reaches the requested status and has no closure actions")
    parser.add_argument("--allow-above-target", action="store_true", help="Do not warn when installed version is above the selected target")
    parser.add_argument("--strict-above-target", action="store_true", help="Explicitly keep the default warning for versions above target")
    parser.add_argument("--audit-workspace", help="Project-relative tool-managed audit workspace path")
    parser.add_argument("--require-audit-workspace-removed", action="store_true", help="Fail when the audit workspace still exists in the final tree")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of Markdown")
    args = parser.parse_args()

    roadmap_path = Path(args.roadmap_json).expanduser().resolve()
    project_dir = Path(args.project_dir).expanduser().resolve()
    package_json_path = project_dir / "package.json"
    if not package_json_path.exists():
        raise SystemExit(f"package.json not found: {package_json_path}")

    roadmap = read_json(roadmap_path)
    project, roadmap_rows = project_rows(roadmap, args.project)
    scope_meta: Dict[str, Any] = {}
    if args.scope_manifest:
        try:
            rows, scope_meta = read_scope_manifest(Path(args.scope_manifest).expanduser().resolve(), project)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"invalid scope manifest: {exc}") from exc
        actions = [row for row in rows if bool(row.get("shouldUpdate")) and target_is_action(str(row.get("target") or ""))]
    else:
        rows = roadmap_rows
        actions = action_rows(rows, args.target_mode)
    direct = direct_dependencies(read_json(package_json_path))

    findings: List[Dict[str, str]] = []
    if args.scope_manifest:
        findings.extend(scope_boundary_findings(rows, direct))
    manifest_registry = str(scope_meta.get("registry") or "").strip()
    if manifest_registry:
        try:
            manager, lockfile, _, _ = select_lockfile(
                project_dir,
                read_json(package_json_path),
                allow_extra_lockfiles=False,
            )
            for issue in validate_registry_artifact_urls(manager, lockfile, manifest_registry):
                findings.append({
                    "severity": "error",
                    "code": "foreign-registry-url",
                    "package": "<lockfile>",
                    "target": manifest_registry,
                    "actual": issue.resolved,
                    "detail": issue.detail,
                })
        except LockfileConsistencyError as exc:
            findings.append({
                "severity": "error",
                "code": "lockfile-registry-validation-failed",
                "package": "<project>",
                "target": manifest_registry,
                "actual": "unverified",
                "detail": str(exc),
            })
    strict_above = args.strict_above_target or not args.allow_above_target
    for row in actions:
        findings.extend(compare_package(project_dir, direct, row, args.target_mode, strict_above))
    scoped_keys = {(str(r.get("section") or section_for_kind(str(r.get("kind") or ""))), str(r.get("name") or r.get("package") or "")) for r in actions}
    baseline = Path(args.baseline_package_json).expanduser().resolve() if args.baseline_package_json else None
    findings.extend(extra_direct_changes(baseline, direct, scoped_keys))

    if args.require_audit_workspace_removed:
        workspace_value = str(args.audit_workspace or ".dependency-roadmap-audit").replace("\\", "/")
        workspace_path = (project_dir / workspace_value).resolve()
        try:
            workspace_path.relative_to(project_dir.resolve())
        except ValueError:
            findings.append({
                "severity": "error",
                "code": "audit-workspace-invalid",
                "package": "<project>",
                "target": "project-relative absent path",
                "actual": workspace_value,
                "detail": "audit workspace must stay inside the project checkout",
            })
        else:
            if workspace_path.exists():
                findings.append({
                    "severity": "error",
                    "code": "audit-workspace-not-removed",
                    "package": "<project>",
                    "target": "absent from final tree",
                    "actual": str(workspace_path),
                    "detail": "run dependency_audit_branch.py cleanup and commit the removal before source merge",
                })

    closure_meta: Dict[str, Any] = {}
    if args.require_final_status and not args.final_roadmap_json:
        findings.append({
            "severity": "error",
            "code": "final-roadmap-required",
            "package": "<project>",
            "target": expected_final_status(args.target_mode, roadmap, project),
            "actual": "not provided",
            "detail": "--require-final-status requires --final-roadmap-json generated from merged state",
        })
    elif args.final_roadmap_json:
        final_path = Path(args.final_roadmap_json).expanduser().resolve()
        try:
            final_roadmap = read_json(final_path)
            closure_findings, closure_meta = validate_final_status(roadmap, final_roadmap, project, args.target_mode)
            findings.extend(closure_findings)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            findings.append({
                "severity": "error",
                "code": "final-roadmap-invalid",
                "package": "<project>",
                "target": expected_final_status(args.target_mode, roadmap, project),
                "actual": str(final_path),
                "detail": str(exc),
            })

    errors = sum(1 for f in findings if f["severity"] == "error")
    if args.json:
        print(json.dumps({
            "project": project,
            "targetMode": args.target_mode,
            "checkedActionRows": len(actions),
            "scope": scope_meta,
            "closure": closure_meta,
            "errors": errors,
            "warnings": sum(1 for f in findings if f["severity"] == "warning"),
            "findings": findings,
        }, ensure_ascii=False, indent=2))
    else:
        print_markdown(project, args.target_mode, findings, len(actions))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
