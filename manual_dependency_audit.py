#!/usr/bin/env python3
"""Small, auditable dependency check for one npm/yarn/pnpm project.

It intentionally does only two things:
1. runs the package manager's own audit command and prints package/severity counts;
2. checks publish-date lag for every direct declaration through `npm view`.

The implementation is independent from roadmap target selection, so its output
can be used as a manual cross-check for the dashboard.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dateutil.relativedelta import relativedelta

from cli_io import configure_utf8_stdio

SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
AUDIT_ROOT_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")
SEVERITIES = ("critical", "high", "moderate", "low", "unknown")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_iso_for_fromisoformat(value: Any) -> str:
    """Normalize npm timestamps for Python 3.10 ``fromisoformat``.

    Python 3.10 accepts three or six fractional-second digits, while npm
    metadata may legally contain one, two, four or five digits (for example
    ``2022-06-14T19:46:48.37Z``).  Pad/truncate the fraction to microseconds
    before parsing so the CLI behaves identically on Python 3.10+ runtimes.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    match = re.fullmatch(
        r"(?P<prefix>\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2})"
        r"(?:\.(?P<fraction>\d+))?"
        r"(?P<timezone>[+-]\d{2}:?\d{2})?",
        text,
    )
    if not match:
        return text
    fraction = match.group("fraction")
    timezone = match.group("timezone") or ""
    if timezone and len(timezone) == 5 and ":" not in timezone:
        timezone = timezone[:3] + ":" + timezone[3:]
    if fraction is None:
        return match.group("prefix") + timezone
    microseconds = (fraction + "000000")[:6]
    return f"{match.group('prefix')}.{microseconds}{timezone}"


def parse_iso(value: Any) -> Optional[dt.datetime]:
    normalized = _normalize_iso_for_fromisoformat(value)
    if not normalized:
        return None
    try:
        parsed = dt.datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def version_text(value: str) -> str:
    match = re.search(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", str(value or ""))
    return match.group(1) if match else str(value or "").strip()


def exact_semver_spec(value: str) -> bool:
    return bool(re.fullmatch(r"\s*v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\s*", str(value or "")))


def split_yarn_keys(line: str) -> List[str]:
    text = line.strip().rstrip(":")
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


def parse_yarn_lock(path: Path) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    keys: List[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith(" ") and line.rstrip().endswith(":"):
            keys = split_yarn_keys(line)
            continue
        if keys and line.startswith("  version "):
            match = re.search(r'version\s+"?([^"\s]+)"?', line.strip())
            if match:
                for key in keys:
                    mapping[key] = match.group(1)
            keys = []
    return mapping


def parse_yarn_lock_entries(path: Path) -> List[Dict[str, Any]]:
    """Parse the subset of Yarn Classic lock syntax needed for graph reachability.

    ``yarn.lock`` already contains the exact versions and dependency requests
    selected by Yarn.  The manual audit must preserve that inventory rather
    than asking npm to resolve the ranges again.  This parser intentionally
    handles only stable Yarn v1 fields: selectors, version, dependencies and
    optionalDependencies. Unknown fields are ignored.
    """
    entries: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    dependency_block = ""

    def flush() -> None:
        nonlocal current
        if current is not None and current.get("selectors"):
            entries.append(current)
        current = None

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip() or raw_line.startswith("#"):
            continue
        if not raw_line.startswith(" ") and raw_line.rstrip().endswith(":"):
            flush()
            current = {
                "selectors": split_yarn_keys(raw_line),
                "version": "",
                "dependencies": {},
                "optionalDependencies": {},
            }
            dependency_block = ""
            continue
        if current is None:
            continue

        stripped = raw_line.strip()
        if raw_line.startswith("  ") and not raw_line.startswith("    "):
            dependency_block = ""
            if stripped in {"dependencies:", "optionalDependencies:"}:
                dependency_block = stripped[:-1]
                continue
            if stripped.startswith("version "):
                try:
                    parts = shlex.split(stripped)
                except ValueError:
                    parts = stripped.split(maxsplit=1)
                if len(parts) >= 2:
                    current["version"] = parts[1]
                continue
        if dependency_block and raw_line.startswith("    "):
            try:
                parts = shlex.split(stripped)
            except ValueError:
                parts = stripped.split(maxsplit=1)
            if len(parts) >= 2:
                current[dependency_block][parts[0]] = parts[1]
    flush()
    return entries


def _yarn_selector_name_and_spec(selector: str) -> Tuple[str, str]:
    value = str(selector or "").strip().strip('"\'')
    if value.startswith("@"):
        slash = value.find("/")
        boundary = value.find("@", slash + 1) if slash >= 0 else -1
    else:
        boundary = value.find("@")
    if boundary <= 0:
        return value, ""
    return value[:boundary], value[boundary + 1:]


def _npm_alias_target(name: str, spec: str) -> str:
    """Return the package name audited for ``alias@npm:real-package@range``."""
    value = str(spec or "").strip()
    if not value.startswith("npm:"):
        return name
    target = value[4:]
    if target.startswith("@"):
        slash = target.find("/")
        boundary = target.find("@", slash + 1) if slash >= 0 else -1
    else:
        boundary = target.rfind("@")
    return target[:boundary] if boundary > 0 else target


def canonical_yarn_inventory(project: Path) -> Dict[str, Any]:
    """Build the reachable package/version inventory selected by Yarn Classic.

    The result is suitable for npm's bulk security endpoint: it contains exact
    package/version pairs but does not reinterpret semver ranges through npm's
    resolver.  Normal Yarn v1 locks resolve every edge by an exact selector.
    Any unresolved edge is reported and makes the inventory incomplete rather
    than silently claiming a complete vulnerability audit.
    """
    lock_path = project / "yarn.lock"
    entries = parse_yarn_lock_entries(lock_path)
    selector_map: Dict[str, Dict[str, Any]] = {}
    entries_by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        for selector in entry.get("selectors") or []:
            selector_map[str(selector)] = entry
            selector_name, _ = _yarn_selector_name_and_spec(str(selector))
            if selector_name and entry not in entries_by_name[selector_name]:
                entries_by_name[selector_name].append(entry)

    unresolved: List[str] = []

    def resolve(name: str, spec: str) -> Optional[Dict[str, Any]]:
        candidates = [f"{name}@{spec}"]
        if not str(spec).startswith("npm:"):
            candidates.append(f"{name}@npm:{spec}")
        for candidate in candidates:
            if candidate in selector_map:
                return selector_map[candidate]
        matching = entries_by_name.get(name) or []
        if len(matching) == 1:
            # This is safe for git/file selectors and lockfiles produced by
            # tools that normalize selector spelling, because no alternative
            # version exists for the requested package name.
            return matching[0]
        return None

    package_json = read_json(project / "package.json")
    queue: List[Tuple[str, str, Dict[str, Any], bool]] = []
    direct_names: set[str] = set()
    for section in AUDIT_ROOT_SECTIONS:
        declared = package_json.get(section) or {}
        if not isinstance(declared, dict):
            continue
        for raw_name, raw_spec in declared.items():
            name, spec = str(raw_name), str(raw_spec)
            entry = resolve(name, spec)
            if entry is None:
                unresolved.append(f"root:{section}:{name}@{spec}")
                continue
            audit_name = _npm_alias_target(name, spec)
            direct_names.add(audit_name)
            queue.append((audit_name, spec, entry, True))

    visited_entries: set[int] = set()
    pairs: set[Tuple[str, str]] = set()
    while queue:
        audit_name, requested_spec, entry, _is_direct = queue.pop()
        entry_id = id(entry)
        version = str(entry.get("version") or "").strip()
        if audit_name and version:
            pairs.add((audit_name, version))
        if entry_id in visited_entries:
            continue
        visited_entries.add(entry_id)
        for field in ("dependencies", "optionalDependencies"):
            dependencies = entry.get(field) or {}
            if not isinstance(dependencies, dict):
                continue
            for raw_name, raw_spec in dependencies.items():
                name, spec = str(raw_name), str(raw_spec)
                child = resolve(name, spec)
                if child is None:
                    parent = f"{audit_name}@{version}" if version else audit_name
                    unresolved.append(f"{parent}->{name}@{spec}")
                    continue
                queue.append((_npm_alias_target(name, spec), spec, child, False))

    # An empty project has a complete empty inventory. A non-empty declaration
    # set with zero reachable pairs is incomplete and must not produce a green
    # vulnerability result.
    declared_count = sum(
        len(values)
        for section in AUDIT_ROOT_SECTIONS
        for values in [package_json.get(section) or {}]
        if isinstance(values, dict)
    )
    complete = not unresolved and (bool(pairs) or declared_count == 0)
    return {
        "pairs": sorted(pairs),
        "directPackages": sorted(direct_names),
        "unresolvedEdges": sorted(set(unresolved)),
        "reachableEntries": len(visited_entries),
        "lockEntries": len(entries),
        "complete": complete,
    }


def package_lock_version(path: Path, name: str) -> Optional[str]:
    data = read_json(path)
    packages = data.get("packages") or {}
    entry = packages.get(f"node_modules/{name}") if isinstance(packages, dict) else None
    if isinstance(entry, dict) and entry.get("version"):
        return str(entry["version"])
    deps = data.get("dependencies") or {}
    entry = deps.get(name) if isinstance(deps, dict) else None
    if isinstance(entry, dict) and entry.get("version"):
        return str(entry["version"])
    return None


def resolved_version(project: Path, name: str, spec: str, yarn_map: Optional[Dict[str, str]]) -> Tuple[str, str]:
    for filename in ("package-lock.json", "npm-shrinkwrap.json"):
        path = project / filename
        if path.exists():
            value = package_lock_version(path, name)
            return (value, filename) if value else (version_text(spec), "package.json fallback")
    if yarn_map is not None:
        exact = [value for key, value in yarn_map.items() if key.startswith(f"{name}@")]
        unique = sorted(set(exact))
        if len(unique) == 1:
            return unique[0], "yarn.lock"
        for candidate in (f"{name}@{spec}", f"{name}@npm:{spec}"):
            if candidate in yarn_map:
                return yarn_map[candidate], "yarn.lock"
    return version_text(spec), "package.json fallback"


def direct_dependencies(project: Path) -> List[Dict[str, str]]:
    """Read direct dependency state only from package.json.

    Lockfiles are install/audit evidence, not the source of truth for what the
    project declares.  This avoids showing stale pre-migration versions when a
    lockfile has not yet been refreshed.
    """
    package_json = read_json(project / "package.json")
    result: List[Dict[str, str]] = []
    for section in SECTIONS:
        values = package_json.get(section) or {}
        if not isinstance(values, dict):
            continue
        for name, spec in sorted(values.items()):
            text = str(spec)
            current = version_text(text)
            result.append({
                "section": section,
                "name": str(name),
                "spec": text,
                "current": current,
                "source": "package.json",
                # The dashboard/audit deliberately uses the semantic version
                # declared in package.json, even when the declaration is a
                # range such as ^1.2.3.  Lockfiles are consistency evidence,
                # never the displayed direct-version source.
                "declaredExact": exact_semver_spec(text),
                "resolvedExact": bool(re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", current)),
            })
    return result


def run(command: List[str], cwd: Path, env: Optional[Dict[str, str]] = None, timeout: Optional[int] = None) -> Tuple[int, str, str]:
    if not command:
        return 127, "", "cannot execute empty command"

    executable = shutil.which(command[0])
    if executable is None:
        return 127, "", f"cannot execute {command[0]}: executable not found on PATH"

    resolved_command = [executable, *command[1:]]
    try:
        completed = subprocess.run(
            resolved_command,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            env=env,
            timeout=timeout,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = stderr.strip() or stdout.strip() or f"command timed out after {timeout}s"
        return 124, stdout, detail
    except OSError as exc:
        return 127, "", f"cannot execute {command[0]}: {exc}"


def registry_environment(registry: str) -> Dict[str, str]:
    env = os.environ.copy()
    if registry:
        env["npm_config_registry"] = registry
        env["NPM_CONFIG_REGISTRY"] = registry
    return env


def _progress(message: str) -> None:
    print(f"[audit] {message}", file=sys.stderr, flush=True)


def _sha256(path: Path) -> str:
    if not path.exists():
        return ""
    digest = __import__("hashlib").sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_registry(value: str) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _registry_from_command_output(stdout: str, fallback: str) -> str:
    """Extract a registry URL from noisy npm/Yarn command output."""
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if re.match(r"^https?://", line, flags=re.IGNORECASE):
            return line.rstrip("/")
    return str(fallback or "").strip().rstrip("/")


def _audit_input_state(project: Path, registry: str) -> Dict[str, str]:
    return {
        "schemaVersion": "1",
        "packageJsonSha256": _sha256(project / "package.json"),
        "yarnLockSha256": _sha256(project / "yarn.lock"),
        "registry": _normalized_registry(registry),
    }


def _read_optional_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_reset_workspace(workspace: Path) -> None:
    """Prepare a persistent manual-audit workspace.

    The workspace lives outside the project checkout and belongs only to the
    explicitly invoked manual audit command.  Its isolated package-lock may be
    reused only while package.json, yarn.lock and registry inputs are identical;
    changed inputs force a from-scratch bridge rebuild.  Dashboard generation
    never reads this lockfile.
    """
    workspace = workspace.resolve()
    if workspace == workspace.parent:
        raise ValueError(f"unsafe audit workspace: {workspace}")
    marker = workspace / ".dependency-roadmap-audit-workspace.json"
    if workspace.exists():
        entries = list(workspace.iterdir())
        if entries and not marker.exists():
            raise ValueError(
                f"audit workspace is not empty and is not tool-managed: {workspace}"
            )
        for entry in entries:
            if entry.name in {marker.name, "package-lock.json", "audit-input.json", "npm-metadata-cache.json"}:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    else:
        workspace.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"schemaVersion": 1, "managedBy": "dependency-roadmap-tool"}, indent=2),
        encoding="utf-8",
    )

def _copy_if_exists(source: Path, destination: Path) -> None:
    if source.exists():
        shutil.copy2(source, destination)


def _write_text(path: Path, value: str) -> None:
    path.write_text(value or "", encoding="utf-8")


def package_manager(project: Path) -> str:
    package_json = read_json(project / "package.json")
    declared = str(package_json.get("packageManager") or "").lower()
    if declared.startswith("pnpm@") or (project / "pnpm-lock.yaml").exists():
        return "pnpm"
    if declared.startswith("yarn@") or (project / "yarn.lock").exists():
        return "yarn"
    return "npm"


def severity(value: Any) -> str:
    normalized = str(value or "unknown").lower()
    return normalized if normalized in SEVERITIES else "unknown"


def parse_audit(manager: str, stdout: str) -> Tuple[Dict[str, Counter], Counter, List[str]]:
    packages: Dict[str, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    notes: List[str] = []
    if not stdout.strip():
        return packages, totals, ["audit returned no JSON output"]

    if manager == "yarn":
        for line in stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") == "auditAdvisory":
                advisory = (item.get("data") or {}).get("advisory") or {}
                name = str(advisory.get("module_name") or advisory.get("moduleName") or "unknown")
                level = severity(advisory.get("severity"))
                packages[name][level] += 1
                totals[level] += 1
            elif item.get("type") == "auditSummary":
                counts = ((item.get("data") or {}).get("vulnerabilities") or {})
                for level in SEVERITIES:
                    if isinstance(counts.get(level), int):
                        totals[level] = max(totals[level], counts[level])
        return packages, totals, notes

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return packages, totals, [f"cannot parse audit JSON: {exc}"]
    if isinstance(data, dict) and data.get("error"):
        error = data.get("error")
        notes.append(f"audit error: {json.dumps(error, ensure_ascii=False) if isinstance(error, (dict, list)) else error}")

    advisories = data.get("advisories") or {}
    if isinstance(advisories, dict) and advisories:
        for advisory in advisories.values():
            if not isinstance(advisory, dict):
                continue
            name = str(advisory.get("module_name") or advisory.get("moduleName") or "unknown")
            level = severity(advisory.get("severity"))
            packages[name][level] += 1
            totals[level] += 1
    vulnerabilities = data.get("vulnerabilities") or {}
    if isinstance(vulnerabilities, dict):
        for name, record in vulnerabilities.items():
            if not isinstance(record, dict):
                continue
            objects = [item for item in (record.get("via") or []) if isinstance(item, dict)]
            if objects:
                for item in objects:
                    level = severity(item.get("severity") or record.get("severity"))
                    packages[str(name)][level] += 1
            else:
                # npm audit v2 may list transitive package names in `via`. Those
                # strings do not expose distinct advisory records, so count the
                # vulnerability record once instead of inflating it by edge count.
                level = severity(record.get("severity"))
                packages[str(name)][level] += 1
        metadata_counts = ((data.get("metadata") or {}).get("vulnerabilities") or {})
        for level in SEVERITIES:
            if isinstance(metadata_counts.get(level), int):
                totals[level] = metadata_counts[level]
        if not any(totals.values()):
            for counts in packages.values():
                totals.update(counts)
    return packages, totals, notes


def parse_npm_audit_details(stdout: str) -> Dict[str, Any]:
    """Preserve npm audit v2 evidence that the compact summary used to lose.

    npm's ``metadata.vulnerabilities`` counts vulnerable dependency nodes, while
    the objects in ``via`` are advisory records.  Mixing the two units produced
    reports such as one package row with ``C:1`` but ``TOTAL C:2`` without any
    explanation.  Keep package severity, affected nodes/paths, ranges and unique
    advisories separately so the report is diagnosable.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "packages": {},
            "packageTotals": {level: 0 for level in SEVERITIES},
            "advisoryTotals": {level: 0 for level in SEVERITIES},
            "nodeTotals": {level: 0 for level in SEVERITIES},
        }
    vulnerabilities = data.get("vulnerabilities") or {}
    details: Dict[str, Any] = {}
    package_totals: Counter = Counter()
    advisory_totals: Counter = Counter()
    seen_advisories: set[str] = set()
    if isinstance(vulnerabilities, dict):
        for name, record in sorted(vulnerabilities.items()):
            if not isinstance(record, dict):
                continue
            record_severity = severity(record.get("severity"))
            package_totals[record_severity] += 1
            advisories: List[Dict[str, Any]] = []
            for via in record.get("via") or []:
                if not isinstance(via, dict):
                    continue
                advisory = {
                    "source": via.get("source"),
                    "name": str(via.get("name") or via.get("dependency") or name),
                    "title": str(via.get("title") or ""),
                    "url": str(via.get("url") or ""),
                    "severity": severity(via.get("severity") or record_severity),
                    "range": str(via.get("range") or ""),
                    "cwe": list(via.get("cwe") or []),
                    "cvss": via.get("cvss") if isinstance(via.get("cvss"), dict) else {},
                }
                advisories.append(advisory)
                identity = str(
                    advisory.get("source")
                    or advisory.get("url")
                    or f"{advisory['name']}|{advisory['title']}|{advisory['range']}"
                )
                if identity not in seen_advisories:
                    seen_advisories.add(identity)
                    advisory_totals[advisory["severity"]] += 1
            details[str(name)] = {
                "severity": record_severity,
                "isDirect": bool(record.get("isDirect")),
                "range": str(record.get("range") or ""),
                "nodes": [str(node) for node in (record.get("nodes") or [])],
                "effects": [str(effect) for effect in (record.get("effects") or [])],
                "fixAvailable": record.get("fixAvailable"),
                "advisories": advisories,
                "transitiveVia": [str(via) for via in (record.get("via") or []) if isinstance(via, str)],
            }
    metadata_counts = ((data.get("metadata") or {}).get("vulnerabilities") or {})
    node_totals = {
        level: int(metadata_counts.get(level) or 0)
        for level in SEVERITIES
    }
    return {
        "packages": details,
        "packageTotals": {level: package_totals[level] for level in SEVERITIES},
        "advisoryTotals": {level: advisory_totals[level] for level in SEVERITIES},
        "nodeTotals": node_totals,
    }


def _yarn_selector_package_name(selector: str) -> str:
    value = str(selector or "").strip().strip('"\'')
    if value.startswith("@"):
        slash = value.find("/")
        boundary = value.find("@", slash + 1) if slash >= 0 else -1
        return value[:boundary] if boundary > 0 else value
    boundary = value.rfind("@")
    return value[:boundary] if boundary > 0 else value


def yarn_lock_package_versions(path: Path) -> set[Tuple[str, str]]:
    result: set[Tuple[str, str]] = set()
    for selector, version in parse_yarn_lock(path).items():
        name = _yarn_selector_package_name(selector)
        if name and version:
            result.add((name, str(version)))
    return result


def _package_name_from_lock_path(path_key: str, entry: Dict[str, Any]) -> str:
    explicit = str(entry.get("name") or "").strip()
    if explicit:
        return explicit
    marker = "node_modules/"
    if marker not in path_key:
        return ""
    tail = path_key.rsplit(marker, 1)[-1]
    if tail.startswith("@"):
        parts = tail.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else tail
    return tail.split("/", 1)[0]


def package_lock_package_versions(path: Path) -> set[Tuple[str, str]]:
    data = read_json(path)
    result: set[Tuple[str, str]] = set()
    packages = data.get("packages") or {}
    if isinstance(packages, dict):
        for path_key, entry in packages.items():
            if not path_key or not isinstance(entry, dict):
                continue
            name = _package_name_from_lock_path(str(path_key), entry)
            version = str(entry.get("version") or "").strip()
            if name and version:
                result.add((name, version))
    return result


def reconcile_yarn_bridge(yarn_lock: Path, package_lock: Path) -> Dict[str, Any]:
    source = yarn_lock_package_versions(yarn_lock)
    generated = package_lock_package_versions(package_lock)
    extra = sorted(generated - source)
    missing = sorted(source - generated)
    # Extra package@version pairs prove that npm resolved something not present
    # in the canonical Yarn lock. Missing pairs can be stale/unreachable Yarn
    # entries, so they are diagnostic but not by themselves fatal.
    return {
        "sourcePairs": len(source),
        "generatedPairs": len(generated),
        "extraPairs": [f"{name}@{version}" for name, version in extra],
        "missingPairs": [f"{name}@{version}" for name, version in missing],
        "faithful": not extra,
    }


def enrich_npm_audit_node_versions(details: Dict[str, Any], package_lock: Path) -> None:
    try:
        data = read_json(package_lock)
    except Exception:
        return
    packages = data.get("packages") or {}
    if not isinstance(packages, dict):
        return
    for record in (details.get("packages") or {}).values():
        nodes = record.get("nodes") or []
        resolved = []
        for node in nodes:
            entry = packages.get(node)
            version = str(entry.get("version") or "") if isinstance(entry, dict) else ""
            resolved.append({"path": node, "version": version})
        record["nodeVersions"] = resolved


def _write_yarn_inventory_lock(
    workspace: Path,
    inventory: Dict[str, Any],
) -> Dict[str, Dict[str, str]]:
    """Write a no-resolution package-lock whose inventory mirrors yarn.lock.

    npm's modern audit endpoint receives a bulk map of package names to exact
    versions from Arborist's lockfile inventory.  The physical paths below are
    synthetic, but each entry's ``name`` and ``version`` are canonical Yarn
    facts.  No package tarballs are installed or resolved.
    """
    packages: Dict[str, Any] = {
        "": {
            "name": "dependency-roadmap-yarn-audit",
            "version": "0.0.0",
            "private": True,
            "dependencies": {},
        }
    }
    root_dependencies: Dict[str, str] = {}
    path_map: Dict[str, Dict[str, str]] = {}
    for index, pair in enumerate(inventory.get("pairs") or [], start=1):
        name, version = str(pair[0]), str(pair[1])
        alias = f"dependency-roadmap-audit-{index:06d}"
        path_key = f"node_modules/{alias}"
        root_dependencies[alias] = version
        packages[path_key] = {
            "name": name,
            "version": version,
        }
        path_map[path_key] = {"name": name, "version": version}
    packages[""]["dependencies"] = root_dependencies
    package_json = {
        "name": "dependency-roadmap-yarn-audit",
        "version": "0.0.0",
        "private": True,
        "dependencies": root_dependencies,
    }
    package_lock = {
        "name": package_json["name"],
        "version": package_json["version"],
        "lockfileVersion": 3,
        "requires": True,
        "packages": packages,
    }
    (workspace / "package.json").write_text(
        json.dumps(package_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (workspace / "package-lock.json").write_text(
        json.dumps(package_lock, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (workspace / "canonical-yarn-inventory.json").write_text(
        json.dumps({**inventory, "pathMap": path_map}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path_map


def _replace_synthetic_node_paths(
    details: Dict[str, Any],
    path_map: Dict[str, Dict[str, str]],
    direct_packages: Iterable[str],
) -> None:
    direct = set(direct_packages)
    for name, record in (details.get("packages") or {}).items():
        canonical_nodes: List[Dict[str, str]] = []
        for node in record.get("nodes") or []:
            mapped = path_map.get(str(node)) or {}
            package_name = str(mapped.get("name") or name)
            version = str(mapped.get("version") or "")
            canonical_nodes.append({
                "path": f"yarn.lock:{package_name}@{version}" if version else f"yarn.lock:{package_name}",
                "version": version,
            })
        record["nodeVersions"] = canonical_nodes
        record["isDirect"] = str(name) in direct
        # The synthetic lock deliberately does not model upgrade paths, so npm
        # cannot provide a trustworthy fixAvailable recommendation here.
        record["fixAvailable"] = "not-evaluated"


def _canonical_package_version_totals(details: Dict[str, Any]) -> Dict[str, int]:
    totals: Counter = Counter()
    for record in (details.get("packages") or {}).values():
        level = severity(record.get("severity"))
        nodes = {
            (str(item.get("path") or ""), str(item.get("version") or ""))
            for item in (record.get("nodeVersions") or [])
            if isinstance(item, dict)
        }
        totals[level] += len(nodes) or 1
    return {level: totals[level] for level in SEVERITIES}


def _yarn_inventory_audit(
    project: Path,
    registry: str,
    audit_workspace: Optional[Path] = None,
) -> Dict[str, Any]:
    """Audit exact Yarn-selected package versions through npm's security API.

    This fallback avoids both failure modes of the old npm-lock bridge: it does
    not resolve ranges with npm, and it does not download package tarballs.
    npm is used only as an authenticated client for the configured registry's
    security endpoint.
    """
    temporary: Optional[tempfile.TemporaryDirectory[str]] = None
    if audit_workspace is None:
        temporary = tempfile.TemporaryDirectory(prefix="dependency-roadmap-yarn-inventory-audit-")
        workspace = Path(temporary.name)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".dependency-roadmap-audit-workspace.json").write_text(
            json.dumps({"schemaVersion": 1, "managedBy": "dependency-roadmap-tool"}, indent=2),
            encoding="utf-8",
        )
        persisted = False
    else:
        workspace = audit_workspace.expanduser().resolve()
        _safe_reset_workspace(workspace)
        persisted = True

    started = time.monotonic()
    try:
        inventory = canonical_yarn_inventory(project)
        path_map = _write_yarn_inventory_lock(workspace, inventory)
        env = registry_environment(registry)
        probe_command = ["npm", "config", "get", "registry"]
        probe_code, probe_stdout, probe_stderr = run(probe_command, workspace, env, timeout=30)
        effective_registry = _registry_from_command_output(
            probe_stdout if probe_code == 0 else "",
            registry or "project/default npm config",
        )
        registry_mismatch = bool(registry) and _normalized_registry(effective_registry) != _normalized_registry(registry)

        command = ["npm", "audit", "--json", "--package-lock-only", "--legacy-peer-deps"]
        if registry:
            command += ["--registry", registry]
        _progress(
            "auditing exact package/version inventory from canonical yarn.lock "
            "through the effective npm security endpoint"
        )
        audit_started = time.monotonic()
        code, stdout, stderr = run(command, workspace, env, timeout=300)
        audit_seconds = time.monotonic() - audit_started
        _progress(f"canonical Yarn inventory audit finished in {audit_seconds:.1f}s with exit={code}")
        _write_text(workspace / "yarn-inventory-npm-audit.json", stdout)
        _write_text(workspace / "yarn-inventory-npm-audit.stderr.txt", stderr)

        packages, totals, notes = parse_audit("npm", stdout)
        details = parse_npm_audit_details(stdout)
        _replace_synthetic_node_paths(
            details,
            path_map,
            inventory.get("directPackages") or [],
        )
        if stderr.strip():
            notes.append(stderr.strip()[-1000:])
        if probe_stderr.strip():
            notes.append("registry probe: " + probe_stderr.strip()[-500:])
        if registry_mismatch:
            notes.append(f"effective registry mismatch: requested={registry!r}, effective={effective_registry!r}")
        unresolved = inventory.get("unresolvedEdges") or []
        if unresolved:
            preview = ", ".join(str(item) for item in unresolved[:10])
            suffix = f" (+{len(unresolved) - 10} more)" if len(unresolved) > 10 else ""
            notes.append(f"YARN_INVENTORY_INCOMPLETE: unresolved yarn.lock edges: {preview}{suffix}")

        combined_output = (stdout + "\n" + stderr).lower()
        transport_error_markers = (
            "enotfound", "eai_again", "econnrefused", "etimedout", "socket hang up",
            "unable to get local issuer certificate", "certificate has expired",
            "audit endpoint returned an error", "404 not found", "410 gone",
        )
        transport_failed = any(marker in combined_output for marker in transport_error_markers)
        parser_failed = any(
            note.startswith(("audit returned no JSON", "cannot parse audit JSON", "audit error:", "cannot execute "))
            for note in notes
        )
        raw_audit_complete = bool(stdout.strip()) and not parser_failed and not transport_failed and not registry_mismatch
        complete = raw_audit_complete and bool(inventory.get("complete"))
        result = {
            "engine": "yarn-inventory",
            "sourcePackageManager": "yarn",
            "lockSource": "yarn.lock",
            "requestedRegistry": registry,
            "effectiveRegistry": effective_registry,
            "registryProbeCommand": probe_command,
            "registryProbeExitCode": probe_code,
            "registryProbeError": probe_stderr.strip()[-500:],
            "workspace": str(workspace) if persisted else "temporary",
            "workspacePersisted": persisted,
            "lockMode": "synthetic-canonical-inventory",
            "lockExitCode": 0,
            "lockReused": False,
            "command": command,
            "exitCode": code,
            "auditDurationSeconds": round(audit_seconds, 3),
            "durationSeconds": round(time.monotonic() - started, 3),
            "complete": complete,
            "rawAuditComplete": raw_audit_complete,
            "trusted": complete,
            "packages": {name: dict(counts) for name, counts in sorted(packages.items())} if complete else {},
            "totals": {level: totals[level] for level in SEVERITIES} if complete else {level: 0 for level in SEVERITIES},
            "packageDetails": (details.get("packages") or {}) if complete else {},
            "packageTotals": (details.get("packageTotals") or {level: 0 for level in SEVERITIES}) if complete else {level: 0 for level in SEVERITIES},
            "advisoryTotals": (details.get("advisoryTotals") or {level: 0 for level in SEVERITIES}) if complete else {level: 0 for level in SEVERITIES},
            "packageVersionTotals": _canonical_package_version_totals(details) if complete else {level: 0 for level in SEVERITIES},
            "nodeTotals": (details.get("nodeTotals") or {level: 0 for level in SEVERITIES}) if complete else {level: 0 for level in SEVERITIES},
            "rawAuditEvidence": {
                "packages": {name: dict(counts) for name, counts in sorted(packages.items())},
                "packageDetails": details.get("packages") or {},
            } if not complete and raw_audit_complete else {},
            "canonicalInventory": {
                key: value
                for key, value in inventory.items()
                if key != "pairs"
            } | {"packageVersionPairs": len(inventory.get("pairs") or [])},
            "notes": notes,
        }
        (workspace / "audit-engine.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result
    finally:
        if temporary is not None:
            temporary.cleanup()


def _native_audit(project: Path, manager: str, registry: str) -> Dict[str, Any]:
    if manager == "pnpm":
        command = ["pnpm", "audit", "--json"]
    else:
        command = ["npm", "audit", "--json"]
    if registry:
        command += ["--registry", registry]
    code, stdout, stderr = run(command, project, registry_environment(registry))
    packages, totals, notes = parse_audit(manager, stdout)
    details = parse_npm_audit_details(stdout) if manager == "npm" else {
        "packages": {},
        "packageTotals": {
            level: sum(1 for counts in packages.values() if counts.get(level, 0))
            for level in SEVERITIES
        },
        "advisoryTotals": {level: totals[level] for level in SEVERITIES},
        "nodeTotals": {level: totals[level] for level in SEVERITIES},
    }
    if manager == "npm":
        for filename in ("package-lock.json", "npm-shrinkwrap.json"):
            lock_path = project / filename
            if lock_path.exists():
                enrich_npm_audit_node_versions(details, lock_path)
                break
    if stderr.strip():
        notes.append(stderr.strip()[-1000:])
    combined_output = (stdout + "\n" + stderr).lower()
    transport_error_markers = (
        "enotfound", "eai_again", "econnrefused", "etimedout", "socket hang up",
        "unable to get local issuer certificate", "certificate has expired",
    )
    transport_failed = any(marker in combined_output for marker in transport_error_markers)
    parser_failed = any(note.startswith(("audit returned no JSON", "cannot parse audit JSON", "audit error:", "cannot execute ")) for note in notes)
    complete = bool(stdout.strip()) and not parser_failed and not transport_failed
    return {
        "engine": f"{manager}-native",
        "sourcePackageManager": manager,
        "requestedRegistry": registry,
        "effectiveRegistry": registry or "project/default npm config",
        "command": command,
        "exitCode": code,
        "complete": complete,
        "packages": {name: dict(counts) for name, counts in sorted(packages.items())},
        "totals": {level: totals[level] for level in SEVERITIES},
        "packageDetails": details.get("packages") or {},
        "packageTotals": details.get("packageTotals") or {level: 0 for level in SEVERITIES},
        "advisoryTotals": details.get("advisoryTotals") or {level: 0 for level in SEVERITIES},
        "nodeTotals": details.get("nodeTotals") or {level: 0 for level in SEVERITIES},
        "notes": notes,
    }


def _yarn_classic_native_audit(
    project: Path,
    registry: str,
    audit_workspace: Optional[Path] = None,
) -> Dict[str, Any]:
    """Audit the canonical Yarn lock directly before considering a bridge.

    This is the only path that cannot accidentally reinterpret the dependency
    graph through npm's resolver. Corporate registries that no longer support
    Yarn Classic's audit endpoint may reject it; in ``auto`` mode we then fall
    back to an exact package/version inventory built from the same yarn.lock.
    """
    persisted_workspace: Optional[Path] = None
    if audit_workspace is not None:
        persisted_workspace = audit_workspace.expanduser().resolve()
        _safe_reset_workspace(persisted_workspace)

    env = registry_environment(registry)
    probe_command = ["yarn", "config", "get", "registry"]
    probe_code, probe_stdout, probe_stderr = run(probe_command, project, env, timeout=30)
    effective_registry = _registry_from_command_output(
        probe_stdout if probe_code == 0 else "",
        registry or "project/default Yarn config",
    )
    registry_mismatch = bool(registry) and _normalized_registry(effective_registry) != _normalized_registry(registry)
    command = ["yarn", "audit", "--json"]
    if registry:
        command += ["--registry", registry]
    _progress("auditing the canonical yarn.lock through Yarn Classic")
    started = time.monotonic()
    code, stdout, stderr = run(command, project, env, timeout=300)
    duration = time.monotonic() - started
    _progress(f"Yarn audit finished in {duration:.1f}s with exit={code}")
    if persisted_workspace is not None:
        _write_text(persisted_workspace / "yarn-audit.jsonl", stdout)
        _write_text(persisted_workspace / "yarn-audit.stderr.txt", stderr)
    packages, totals, notes = parse_audit("yarn", stdout)
    if stderr.strip():
        notes.append(stderr.strip()[-1000:])
    if probe_stderr.strip():
        notes.append("registry probe: " + probe_stderr.strip()[-500:])
    if registry_mismatch:
        notes.append(f"effective registry mismatch: requested={registry!r}, effective={effective_registry!r}")
    parsed_event = False
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("type") in {"auditAdvisory", "auditSummary"}:
            parsed_event = True
            break
    combined_output = (stdout + "\n" + stderr).lower()
    transport_error_markers = (
        "enotfound", "eai_again", "econnrefused", "etimedout", "socket hang up",
        "unable to get local issuer certificate", "certificate has expired",
        "audit endpoint returned an error", "410 gone", "404 not found",
    )
    transport_failed = any(marker in combined_output for marker in transport_error_markers)
    # Yarn Classic encodes vulnerability severity in the exit-code bitmask, so
    # a non-zero code is expected when findings exist.  Completeness is based
    # on parsed audit events and transport/registry integrity, not exit=0.
    complete = parsed_event and not transport_failed and not registry_mismatch
    result = {
        "engine": "yarn-native",
        "sourcePackageManager": "yarn",
        "lockSource": "yarn.lock",
        "requestedRegistry": registry,
        "effectiveRegistry": effective_registry,
        "registryProbeCommand": probe_command,
        "registryProbeExitCode": probe_code,
        "registryProbeError": probe_stderr.strip()[-500:],
        "command": command,
        "exitCode": code,
        "auditDurationSeconds": round(duration, 3),
        "complete": complete,
        "packages": {name: dict(counts) for name, counts in sorted(packages.items())},
        "totals": {level: totals[level] for level in SEVERITIES},
        "nodeTotals": {level: totals[level] for level in SEVERITIES},
        "advisoryTotals": {level: totals[level] for level in SEVERITIES},
        "packageTotals": {
            level: sum(1 for counts in packages.values() if counts.get(level, 0))
            for level in SEVERITIES
        },
        "packageDetails": {},
        "notes": notes,
    }
    if persisted_workspace is not None:
        (persisted_workspace / "audit-engine.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return result


def _yarn_npm_lock_audit(project: Path, registry: str, audit_workspace: Optional[Path]) -> Dict[str, Any]:
    """Run a reproducible npm audit from an isolated lock for a Yarn project.

    npm may resolve a slightly different transitive graph than Yarn Classic.
    Reconciliation is therefore reported as an accuracy signal, not treated as
    a transport failure. Strict canonical modes remain available explicitly.
    """
    temporary: Optional[tempfile.TemporaryDirectory[str]] = None
    input_state = _audit_input_state(project, registry)
    if audit_workspace is None:
        temporary = tempfile.TemporaryDirectory(prefix="dependency-roadmap-audit-")
        workspace = Path(temporary.name)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".dependency-roadmap-audit-workspace.json").write_text(
            json.dumps({"schemaVersion": 1, "managedBy": "dependency-roadmap-tool"}, indent=2),
            encoding="utf-8",
        )
        persisted = False
        previous_input: Dict[str, Any] = {}
    else:
        workspace = audit_workspace.expanduser().resolve()
        previous_input = _read_optional_json(workspace / "audit-input.json")
        _safe_reset_workspace(workspace)
        persisted = True

    started = time.monotonic()
    try:
        shutil.copy2(project / "package.json", workspace / "package.json")
        _copy_if_exists(project / "yarn.lock", workspace / "yarn.lock")
        (workspace / "audit-input.json").write_text(
            json.dumps(input_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        env = registry_environment(registry)

        probe_command = ["npm", "config", "get", "registry"]
        probe_code, probe_stdout, probe_stderr = run(probe_command, workspace, env, timeout=30)
        effective_registry = probe_stdout.strip().rstrip("/") if probe_code == 0 and probe_stdout.strip() else (registry.rstrip("/") if registry else "project/default npm config")
        registry_mismatch = bool(registry) and _normalized_registry(effective_registry) != _normalized_registry(registry)

        lock_command = [
            "npm", "install", "--package-lock-only", "--ignore-scripts",
            "--legacy-peer-deps", "--audit=false", "--fund=false",
        ]
        if registry:
            lock_command += ["--registry", registry]

        lock_path = workspace / "package-lock.json"
        lock_reused = bool(
            persisted
            and lock_path.exists()
            and previous_input == input_state
        )
        # A persisted npm lock must never be incrementally updated after the
        # Yarn inputs change. npm prioritizes an existing package-lock.json over
        # yarn.lock, which could otherwise audit a stale npm-shaped tree. Rebuild
        # from scratch so the manual audit bridge starts from the current
        # package.json + yarn.lock pair.
        if persisted and lock_path.exists() and not lock_reused:
            lock_path.unlink()
        lock_stdout = ""
        lock_stderr = ""
        lock_seconds = 0.0
        if lock_reused:
            lock_code = 0
            _progress("reusing isolated package-lock (package.json, yarn.lock and registry are unchanged)")
        else:
            _progress("building isolated package-lock; the first run may take several minutes")
            lock_started = time.monotonic()
            lock_code, lock_stdout, lock_stderr = run(lock_command, workspace, env, timeout=900)
            lock_seconds = time.monotonic() - lock_started
            _progress(f"package-lock step finished in {lock_seconds:.1f}s with exit={lock_code}")
        _write_text(workspace / "package-lock-generation.stdout.txt", lock_stdout)
        _write_text(workspace / "package-lock-generation.stderr.txt", lock_stderr)

        if lock_code != 0 or not lock_path.exists():
            notes = [
                f"temporary package-lock generation failed; exit={lock_code}",
                (lock_stderr.strip() or lock_stdout.strip() or "package-lock.json was not created")[-1000:],
            ]
            if registry_mismatch:
                notes.append(f"effective registry mismatch: requested={registry!r}, effective={effective_registry!r}")
            result = {
                "engine": "npm-lock-bridge",
                "sourcePackageManager": "yarn",
                "lockSource": "yarn.lock",
                "requestedRegistry": registry,
                "effectiveRegistry": effective_registry,
                "registryProbeCommand": probe_command,
                "registryProbeExitCode": probe_code,
                "registryProbeError": probe_stderr.strip()[-500:],
                "workspace": str(workspace) if persisted else "temporary",
                "workspacePersisted": persisted,
                "lockCommand": lock_command,
                "lockExitCode": lock_code,
                "lockReused": lock_reused,
                "lockDurationSeconds": round(lock_seconds, 3),
                "command": [],
                "exitCode": lock_code,
                "complete": False,
                "packages": {},
                "totals": {level: 0 for level in SEVERITIES},
                "notes": notes,
                "durationSeconds": round(time.monotonic() - started, 3),
            }
            (workspace / "audit-engine.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result

        reconciliation = reconcile_yarn_bridge(workspace / "yarn.lock", lock_path)
        (workspace / "bridge-reconciliation.json").write_text(
            json.dumps(reconciliation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        command = ["npm", "audit", "--json", "--package-lock-only", "--legacy-peer-deps"]
        if registry:
            command += ["--registry", registry]
        _write_text(
            workspace / "reproduce-npm-audit.cmd",
            "@echo off\n"
            "rem Uses the current npm registry. To override it safely, append --registry <url>.\n"
            "npm audit --json --package-lock-only --legacy-peer-deps %*\n",
        )
        _progress("querying vulnerability audit endpoint through the effective npm registry")
        audit_started = time.monotonic()
        code, stdout, stderr = run(command, workspace, env, timeout=300)
        audit_seconds = time.monotonic() - audit_started
        _progress(f"vulnerability audit finished in {audit_seconds:.1f}s with exit={code}")
        _write_text(workspace / "npm-audit.json", stdout)
        _write_text(workspace / "npm-audit.stderr.txt", stderr)
        packages, totals, notes = parse_audit("npm", stdout)
        details = parse_npm_audit_details(stdout)
        enrich_npm_audit_node_versions(details, lock_path)
        if lock_stderr.strip():
            notes.append("package-lock generation: " + lock_stderr.strip()[-1000:])
        if stderr.strip():
            notes.append(stderr.strip()[-1000:])
        if probe_stderr.strip():
            notes.append("registry probe: " + probe_stderr.strip()[-500:])
        if registry_mismatch:
            notes.append(f"effective registry mismatch: requested={registry!r}, effective={effective_registry!r}")
        if not reconciliation.get("faithful"):
            extra = reconciliation.get("extraPairs") or []
            preview = ", ".join(extra[:8])
            suffix = f" (+{len(extra) - 8} more)" if len(extra) > 8 else ""
            notes.append(
                "YARN_NPM_BRIDGE_DRIFT: generated package-lock contains package@version "
                f"pairs absent from canonical yarn.lock: {preview or 'unknown'}{suffix}"
            )
        combined_output = (stdout + "\n" + stderr).lower()
        transport_error_markers = (
            "enotfound", "eai_again", "econnrefused", "etimedout", "socket hang up",
            "unable to get local issuer certificate", "certificate has expired",
        )
        transport_failed = any(marker in combined_output for marker in transport_error_markers)
        parser_failed = any(note.startswith(("audit returned no JSON", "cannot parse audit JSON", "audit error:", "cannot execute ")) for note in notes)
        raw_audit_complete = bool(stdout.strip()) and not parser_failed and not transport_failed and not registry_mismatch
        # A graph difference affects accuracy, but a usable response from npm's
        # security endpoint is still a completed and reproducible npm audit.
        complete = raw_audit_complete
        trusted = raw_audit_complete
        raw_evidence = {
            "packages": {name: dict(counts) for name, counts in sorted(packages.items())},
            "totals": {level: totals[level] for level in SEVERITIES},
            "packageDetails": details.get("packages") or {},
            "packageTotals": details.get("packageTotals") or {level: 0 for level in SEVERITIES},
            "advisoryTotals": details.get("advisoryTotals") or {level: 0 for level in SEVERITIES},
            "nodeTotals": details.get("nodeTotals") or {level: 0 for level in SEVERITIES},
        }
        result = {
            "engine": "npm-lock-bridge",
            "sourcePackageManager": "yarn",
            "lockSource": "yarn.lock",
            "requestedRegistry": registry,
            "effectiveRegistry": effective_registry,
            "registryProbeCommand": probe_command,
            "registryProbeExitCode": probe_code,
            "registryProbeError": probe_stderr.strip()[-500:],
            "workspace": str(workspace) if persisted else "temporary",
            "workspacePersisted": persisted,
            "lockCommand": lock_command,
            "lockExitCode": lock_code,
            "lockReused": lock_reused,
            "lockDurationSeconds": round(lock_seconds, 3),
            "command": command,
            "exitCode": code,
            "auditDurationSeconds": round(audit_seconds, 3),
            "durationSeconds": round(time.monotonic() - started, 3),
            "complete": complete,
            "rawAuditComplete": raw_audit_complete,
            "trusted": trusted,
            "accuracy": "canonical-yarn-match" if reconciliation.get("faithful") else "npm-resolved-approximation",
            "reproducibleFrom": "package-lock.json",
            "packages": raw_evidence["packages"] if trusted else {},
            "totals": raw_evidence["totals"] if trusted else {level: 0 for level in SEVERITIES},
            "packageDetails": raw_evidence["packageDetails"] if trusted else {},
            "packageTotals": raw_evidence["packageTotals"] if trusted else {level: 0 for level in SEVERITIES},
            "advisoryTotals": raw_evidence["advisoryTotals"] if trusted else {level: 0 for level in SEVERITIES},
            "nodeTotals": raw_evidence["nodeTotals"] if trusted else {level: 0 for level in SEVERITIES},
            "rawAuditEvidence": raw_evidence if not trusted else {},
            "bridgeReconciliation": reconciliation,
            "notes": notes,
        }
        (workspace / "audit-engine.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        if temporary is not None:
            temporary.cleanup()

def run_audit(
    project: Path,
    manager: str,
    registry: str,
    audit_workspace: Optional[Path] = None,
    yarn_audit_engine: Optional[str] = None,
) -> Dict[str, Any]:
    if manager == "yarn":
        mode = str(
            yarn_audit_engine
            or os.environ.get("ROADMAP_YARN_AUDIT_ENGINE")
            or "auto"
        ).strip().lower()
        if mode not in {"auto", "yarn-native", "yarn-inventory", "npm-lock-bridge"}:
            return {
                "engine": mode,
                "sourcePackageManager": "yarn",
                "requestedRegistry": registry,
                "effectiveRegistry": "unknown",
                "command": [],
                "exitCode": 2,
                "complete": False,
                "packages": {},
                "totals": {level: 0 for level in SEVERITIES},
                "notes": [
                    "invalid ROADMAP_YARN_AUDIT_ENGINE; expected auto, yarn-native, "
                    "yarn-inventory or npm-lock-bridge"
                ],
            }
        if mode == "auto":
            _progress("using reproducible isolated package-lock audit for Yarn Classic")
            return _yarn_npm_lock_audit(project, registry, audit_workspace)
        if mode == "yarn-native":
            return _yarn_classic_native_audit(project, registry, audit_workspace)
        if mode == "yarn-inventory":
            return _yarn_inventory_audit(project, registry, audit_workspace)
        return _yarn_npm_lock_audit(project, registry, audit_workspace)
    return _native_audit(project, manager, registry)

def _npm_view_json(project: Path, name: str, fields: List[str], registry: str) -> Tuple[Optional[Any], str]:
    command = ["npm", "view", name, *fields, "--json"]
    if registry:
        command += ["--registry", registry]
    code, stdout, stderr = run(command, project)
    if code != 0 or not stdout.strip():
        return None, (stderr.strip() or stdout.strip() or f"npm view exit {code}")[-500:]
    try:
        return json.loads(stdout), ""
    except json.JSONDecodeError as exc:
        return None, f"invalid npm view JSON: {exc}"


def _field_object(payload: Any, field: str) -> Optional[Dict[str, Any]]:
    """Read an npm-view object from wrapped and single-field output shapes.

    npm versions do not always serialize `npm view <pkg> field --json` and
    `npm view <pkg> field1 field2 --json` identically.  Accept both the wrapped
    form (`{"time": {...}}`) and the direct single-field form (`{...}`).
    """
    if not isinstance(payload, dict):
        return None
    aliases = (field, "distTags") if field == "dist-tags" else (field,)
    for alias in aliases:
        value = payload.get(alias)
        if isinstance(value, dict):
            return value
    # A separate single-field call returns the field value itself.
    if field == "time" and any(key in payload for key in ("created", "modified")):
        return payload
    if field == "dist-tags" and any(key in payload for key in ("latest", "next", "beta")):
        return payload
    return None


def _field_scalar(payload: Any, field: str) -> Optional[Any]:
    """Read one scalar npm-view field from direct or wrapped JSON output."""
    if payload is None:
        return None
    if not isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, list):
        return payload[0] if len(payload) == 1 else None
    if field in payload and not isinstance(payload[field], (dict, list)):
        return payload[field]
    if field == "dist-tags.latest":
        tags = payload.get("dist-tags") or payload.get("distTags")
        if isinstance(tags, dict):
            return tags.get("latest")
    match = re.fullmatch(r"time\[(.+)\]", field)
    if match:
        times = payload.get("time")
        if isinstance(times, dict):
            return times.get(match.group(1))
    if len(payload) == 1:
        only = next(iter(payload.values()))
        if not isinstance(only, (dict, list)):
            return only
    return None


def npm_view_scalar(project: Path, name: str, field: str, registry: str) -> Tuple[Optional[Any], str]:
    payload, error = _npm_view_json(project, name, [field], registry)
    if error:
        return None, error
    value = _field_scalar(payload, field)
    if value is None:
        return None, f"npm view field {field} returned no scalar value"
    return value, ""


def npm_view_exact_publish_time(project: Path, name: str, version: str, registry: str) -> Tuple[Optional[dt.datetime], str]:
    """Resolve one publication timestamp without trusting a possibly partial time map."""
    normalized = version_text(version)
    if not normalized:
        return None, "empty version"

    field = f"time[{normalized}]"
    value, error = npm_view_scalar(project, name, field, registry)
    parsed = parse_iso(value)
    if parsed:
        return parsed, ""

    # Older npm/Nexus combinations can ignore bracket projections.  Query the
    # version descriptor and inspect its complete time object as a final fallback.
    payload, descriptor_error = _npm_view_json(project, f"{name}@{normalized}", ["time"], registry)
    times = _field_object(payload, "time")
    parsed = _version_time(times, normalized)
    if parsed:
        return parsed, ""

    details = [part for part in (error, descriptor_error) if part]
    return None, "; ".join(dict.fromkeys(details))[-500:] or f"publish time not found for {normalized}"


def npm_view_times(project: Path, name: str, registry: str) -> Tuple[Optional[Dict[str, Any]], str]:
    combined, combined_error = _npm_view_json(project, name, ["time", "dist-tags"], registry)
    times = _field_object(combined, "time")
    tags = _field_object(combined, "dist-tags")

    errors: List[str] = []
    if combined_error:
        errors.append(combined_error)

    # Some npm/Nexus combinations return an incomplete or differently shaped
    # multi-field projection. Retry each field separately before declaring the
    # publication metadata unknown.
    if times is None:
        time_payload, error = _npm_view_json(project, name, ["time"], registry)
        times = _field_object(time_payload, "time")
        if error:
            errors.append(f"time: {error}")
    if tags is None:
        tags_payload, error = _npm_view_json(project, name, ["dist-tags"], registry)
        tags = _field_object(tags_payload, "dist-tags")
        if error:
            errors.append(f"dist-tags: {error}")

    if times is None and tags is None:
        return None, "; ".join(dict.fromkeys(errors))[-500:] or "npm view returned no time/dist-tags objects"
    return {"time": times or {}, "dist-tags": tags or {}}, "; ".join(dict.fromkeys(errors))[-500:]


def _version_time(times: Any, version: str) -> Optional[dt.datetime]:
    if not isinstance(times, dict):
        return None
    raw = str(version or "").strip()
    candidates = [raw]
    normalized = version_text(raw)
    if normalized and normalized not in candidates:
        candidates.append(normalized)
    if raw.startswith("v") and raw[1:] not in candidates:
        candidates.append(raw[1:])
    for candidate in candidates:
        parsed = parse_iso(times.get(candidate))
        if parsed:
            return parsed
    # Be tolerant of metadata keys such as v18.2.0 while preserving exact
    # prerelease/build identifiers when they are present.
    wanted = version_text(raw)
    for key, value in times.items():
        if version_text(str(key)) == wanted:
            parsed = parse_iso(value)
            if parsed:
                return parsed
    return None


def load_lag_policies(path: Optional[Path], project_name: str) -> Dict[str, int]:
    if not path or not path.exists():
        return {}
    data = read_json(path)
    packages = ((data.get("packageOverrides") or {}).get(project_name) or {})
    result: Dict[str, int] = {}
    if isinstance(packages, dict):
        for name, override in packages.items():
            if not isinstance(override, dict):
                continue
            value = override.get("lagMonths", override.get("lagThresholdMonths"))
            try:
                months = int(value)
            except (TypeError, ValueError):
                continue
            if months in (3, 6, 9, 12):
                result[str(name)] = months
    return result


def _metadata_cache_load(path: Optional[Path], registry: str) -> Dict[str, Tuple[Optional[Dict[str, Any]], str]]:
    if not path:
        return {}
    data = _read_optional_json(path)
    if _normalized_registry(data.get("registry")) != _normalized_registry(registry):
        return {}
    try:
        generated = parse_iso(data.get("generatedAt"))
        ttl_hours = max(0.0, float(os.environ.get("ROADMAP_AUDIT_METADATA_CACHE_HOURS", "24")))
    except (TypeError, ValueError):
        generated = None
        ttl_hours = 24.0
    if not generated or (dt.datetime.now(dt.timezone.utc) - generated).total_seconds() > ttl_hours * 3600:
        return {}
    result: Dict[str, Tuple[Optional[Dict[str, Any]], str]] = {}
    entries = data.get("entries") or {}
    if isinstance(entries, dict):
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            metadata = entry.get("metadata")
            result[str(name)] = (metadata if isinstance(metadata, dict) else None, str(entry.get("error") or ""))
    return result


def _metadata_cache_write(path: Optional[Path], registry: str, values: Dict[str, Tuple[Optional[Dict[str, Any]], str]]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "registry": _normalized_registry(registry),
        "entries": {
            name: {"metadata": metadata, "error": error}
            for name, (metadata, error) in sorted(values.items())
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_package_metadata_parallel(
    project: Path,
    names: List[str],
    registry: str,
    cache_path: Optional[Path],
) -> Dict[str, Tuple[Optional[Dict[str, Any]], str]]:
    metadata_cache = _metadata_cache_load(cache_path, registry)
    missing = [name for name in names if name not in metadata_cache]
    if not missing:
        if names:
            _progress(f"reusing publish metadata cache for {len(names)} direct packages")
        return metadata_cache

    try:
        workers = int(os.environ.get("ROADMAP_AUDIT_METADATA_WORKERS", "6"))
    except ValueError:
        workers = 6
    workers = max(1, min(16, workers, len(missing)))
    _progress(f"loading publish metadata for {len(missing)} packages with {workers} workers")
    completed_count = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(npm_view_times, project, name, registry): name for name in missing}
        for future in as_completed(futures):
            name = futures[future]
            try:
                metadata_cache[name] = future.result()
            except Exception as exc:  # defensive: keep one package failure from aborting the audit
                metadata_cache[name] = (None, f"metadata query failed: {exc}")
            completed_count += 1
            if completed_count == len(missing) or completed_count % 10 == 0:
                _progress(f"publish metadata {completed_count}/{len(missing)}")
    _metadata_cache_write(cache_path, registry, metadata_cache)
    return metadata_cache


def check_lag(
    project: Path,
    deps: Iterable[Dict[str, str]],
    registry: str,
    default_months: int,
    policies: Dict[str, int],
    metadata_cache_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    dep_list = list(deps)
    names = sorted({str(dep["name"]) for dep in dep_list})
    metadata_cache = _load_package_metadata_parallel(project, names, registry, metadata_cache_path)
    kind_by_section = {
        "dependencies": "runtime",
        "devDependencies": "dev",
        "optionalDependencies": "optional",
        "peerDependencies": "peer",
    }
    for dep in dep_list:
        name, current = dep["name"], dep["current"]
        section = dep["section"]
        kind = kind_by_section.get(section, section)
        months = policies.get(f"{section}:{name}", policies.get(f"{kind}:{name}", policies.get(name, default_months)))
        metadata, error = metadata_cache[name]
        item: Dict[str, Any] = {**dep, "policyMonths": months, "status": "unknown", "latest": "", "currentPublishedAt": None, "latestPublishedAt": None, "error": error}
        if not dep.get("resolvedExact", True):
            item["error"] = "semantic version cannot be derived from package.json declaration"
            result.append(item)
            continue
        if not metadata:
            result.append(item)
            continue
        times = metadata.get("time") or {}
        tags = metadata.get("dist-tags") or metadata.get("distTags") or {}
        latest = str(tags.get("latest") or "").strip()
        recovery_errors: List[str] = []
        if not latest:
            latest_value, latest_error = npm_view_scalar(project, name, "dist-tags.latest", registry)
            latest = str(latest_value or "").strip()
            if latest_error:
                recovery_errors.append(f"latest: {latest_error}")

        current_date = _version_time(times, current)
        if not current_date:
            current_date, current_error = npm_view_exact_publish_time(project, name, current, registry)
            if current_error:
                recovery_errors.append(f"current: {current_error}")

        latest_date = _version_time(times, latest)
        if latest and not latest_date:
            latest_date, latest_error = npm_view_exact_publish_time(project, name, latest, registry)
            if latest_error:
                recovery_errors.append(f"latest-time: {latest_error}")

        item.update({"latest": latest, "currentPublishedAt": current_date.isoformat() if current_date else None, "latestPublishedAt": latest_date.isoformat() if latest_date else None})
        if current_date and latest_date:
            item["error"] = ""
        elif recovery_errors:
            item["error"] = "; ".join(dict.fromkeys(recovery_errors))[-500:]
        if current_date and latest_date:
            threshold = latest_date - relativedelta(months=months)
            item["thresholdPublishedAt"] = threshold.isoformat()
            item["status"] = "ok" if current_date >= threshold else "lagging"
            item["lagDays"] = max(0, (latest_date - current_date).days)
        else:
            missing: List[str] = []
            if not latest:
                missing.append("latest dist-tag")
            if not current_date:
                missing.append(f"current publish date ({current})")
            if latest and not latest_date:
                missing.append(f"latest publish date ({latest})")
            available_versions = sum(1 for key in times if version_text(str(key)) != str(key) or re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", str(key))) if isinstance(times, dict) else 0
            detail = ", ".join(missing) or "publication metadata"
            item["error"] = f"missing {detail}; npm time entries={available_versions}"
        result.append(item)
    return result


def format_counts(counts: Dict[str, int]) -> str:
    labels = {"critical": "C", "high": "H", "moderate": "M", "low": "L", "unknown": "U"}
    return ", ".join(f"{labels[level]}:{counts.get(level, 0)}" for level in SEVERITIES if counts.get(level, 0)) or "0"


def _severity_label(value: Any) -> str:
    return {
        "critical": "C",
        "high": "H",
        "moderate": "M",
        "low": "L",
        "unknown": "U",
    }.get(severity(value), "U")


def _fix_available_text(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False or value is None:
        return "no"
    if isinstance(value, dict):
        name = str(value.get("name") or "").strip()
        version = str(value.get("version") or "").strip()
        forced = " (force)" if value.get("isSemVerMajor") else ""
        target = "@".join(part for part in (name, version) if part)
        return (target or "yes") + forced
    return str(value)


def _node_preview(record: Dict[str, Any], limit: int = 5) -> str:
    node_versions = record.get("nodeVersions") or []
    values: List[str] = []
    if node_versions:
        for item in node_versions:
            path = str((item or {}).get("path") or "").strip()
            version = str((item or {}).get("version") or "").strip()
            if path:
                values.append(f"{path}@{version}" if version else path)
    else:
        values = [str(node) for node in (record.get("nodes") or []) if str(node).strip()]
    if not values:
        return "path unavailable"
    preview = ", ".join(f"`{value}`" for value in values[:limit])
    if len(values) > limit:
        preview += f" (+{len(values) - limit} more)"
    return preview


def markdown(report: Dict[str, Any]) -> str:
    audit_complete = bool(report.get("auditComplete", report.get("audit", {}).get("complete")))
    lag_complete = bool(
        report.get(
            "lagComplete",
            all(item.get("status") != "unknown" for item in (report.get("lag") or [])),
        )
    )
    lines = [
        "# Manual dependency audit",
        "",
        f"- Project: `{report['projectDir']}`",
        f"- Package manager: `{report['packageManager']}`",
        f"- Registry: `{report['registry'] or 'project/default npm config'}`",
        f"- Generated: `{report['generatedAt']}`",
        f"- Completeness: `{'COMPLETE' if report.get('complete') else 'INCOMPLETE'}`",
        f"- Vulnerability audit: `{'COMPLETE' if audit_complete else 'INCOMPLETE'}`",
        f"- Lag audit: `{'COMPLETE' if lag_complete else 'INCOMPLETE'}`",
        "",
        "## Vulnerabilities",
        "",
    ]
    audit = report["audit"]
    packages = audit.get("packages") or {}
    package_details = audit.get("packageDetails") or {}
    raw_package_details = ((audit.get("rawAuditEvidence") or {}).get("packageDetails") or {})
    if not audit.get("complete"):
        if audit.get("rawAuditComplete") and raw_package_details:
            if audit.get("engine") == "npm-lock-bridge":
                lines.append(
                    "- The audit endpoint returned findings, but they are **not trusted** because the generated "
                    "npm bridge graph diverged from the canonical `yarn.lock`. Counts are withheld."
                )
            else:
                lines.append(
                    "- The audit endpoint returned findings, but the canonical `yarn.lock` inventory could not "
                    "be reconstructed completely. Counts are withheld until unresolved lock edges are fixed."
                )
        else:
            lines.append("- Vulnerability audit did not complete; counts are unknown.")
    elif package_details:
        for name, record in package_details.items():
            direct = "direct" if record.get("isDirect") else "transitive"
            affected_range = str(record.get("range") or "—")
            fix_text = _fix_available_text(record.get("fixAvailable"))
            lines.append(
                f"- `{name}` — **{_severity_label(record.get('severity'))}**, {direct}; "
                f"affected range `{affected_range}`; fix available: `{fix_text}`"
            )
            lines.append(f"  - Affected nodes: {_node_preview(record)}")
            advisories = record.get("advisories") or []
            if advisories:
                advisory_bits = []
                for advisory in advisories[:4]:
                    title = str(advisory.get("title") or advisory.get("name") or "advisory").strip()
                    advisory_range = str(advisory.get("range") or "").strip()
                    bit = f"{_severity_label(advisory.get('severity'))}: {title}"
                    if advisory_range:
                        bit += f" (`{advisory_range}`)"
                    advisory_bits.append(bit)
                lines.append("  - Advisories: " + "; ".join(advisory_bits))
                if len(advisories) > 4:
                    lines.append(f"  - Additional advisories: {len(advisories) - 4}")
            transitive_via = record.get("transitiveVia") or []
            if transitive_via:
                lines.append("  - Propagated via vulnerable packages: " + ", ".join(f"`{item}`" for item in transitive_via[:8]))
    elif packages:
        for name, counts in packages.items():
            lines.append(f"- `{name}` ({format_counts(counts)})")
    else:
        lines.append("- Vulnerable packages were not found in parsed audit output.")

    totals_available = bool(audit.get("complete"))
    node_totals = audit.get("nodeTotals") or audit.get("totals") or {}
    package_version_totals = audit.get("packageVersionTotals") or {}
    package_totals = audit.get("packageTotals") or {}
    advisory_totals = audit.get("advisoryTotals") or {}
    lines += ["", "### Totals by unit", ""]
    if audit.get("engine") == "yarn-inventory":
        lines += [
            f"- Vulnerable canonical package@version records: **{format_counts(package_version_totals) if totals_available else 'UNKNOWN'}**",
            f"- Vulnerable package names: **{format_counts(package_totals) if totals_available else 'UNKNOWN'}**",
            f"- Unique advisories: **{format_counts(advisory_totals) if totals_available else 'UNKNOWN'}**",
            "",
            "The inventory engine audits exact versions reachable from `yarn.lock`; it does not claim synthetic npm paths are real installed dependency nodes.",
        ]
    else:
        lines += [
            f"- Affected dependency nodes: **{format_counts(node_totals) if totals_available else 'UNKNOWN'}**",
            f"- Vulnerable package records: **{format_counts(package_totals) if totals_available else 'UNKNOWN'}**",
            f"- Unique advisories: **{format_counts(advisory_totals) if totals_available else 'UNKNOWN'}**",
            "",
            "These totals intentionally use separate units; they must not be compared as if they were the same count.",
        ]
    lines += [
        "",
        f"Audit engine: `{audit.get('engine') or 'unknown'}`",
        f"Requested registry: `{audit.get('requestedRegistry') or report['registry'] or 'project/default npm config'}`",
        f"Effective audit registry: `{audit.get('effectiveRegistry') or 'unknown'}`",
    ]
    if audit.get("lockCommand"):
        lock_mode = "reused" if audit.get("lockReused") else "generated"
        lines.append(
            f"Package-lock: `{lock_mode}`; exit={audit.get('lockExitCode')}; "
            f"duration={audit.get('lockDurationSeconds', 0)}s"
        )
        lines.append(f"Lock command: `{' '.join(audit['lockCommand'])}`")
    lines.append(f"Audit command: `{' '.join(audit.get('command') or [])}`; exit={audit['exitCode']}")
    if audit.get("auditDurationSeconds") is not None:
        lines.append(f"Audit duration: `{audit.get('auditDurationSeconds')}s`")
    reconciliation = audit.get("bridgeReconciliation") or {}
    if reconciliation:
        lines += [
            "",
            "### Yarn → npm bridge reconciliation",
            "",
            f"- Faithful to canonical yarn.lock: `{'yes' if reconciliation.get('faithful') else 'NO'}`",
            f"- yarn.lock package@version pairs: `{reconciliation.get('sourcePairs', 0)}`",
            f"- generated package-lock pairs: `{reconciliation.get('generatedPairs', 0)}`",
        ]
        extra_pairs = reconciliation.get("extraPairs") or []
        missing_pairs = reconciliation.get("missingPairs") or []
        if extra_pairs:
            lines.append(
                "- Extra pairs introduced by npm resolver: "
                + ", ".join(f"`{item}`" for item in extra_pairs[:20])
                + (f" (+{len(extra_pairs) - 20} more)" if len(extra_pairs) > 20 else "")
            )
        if missing_pairs:
            lines.append(
                "- yarn.lock pairs absent from generated lock (diagnostic): "
                + ", ".join(f"`{item}`" for item in missing_pairs[:20])
                + (f" (+{len(missing_pairs) - 20} more)" if len(missing_pairs) > 20 else "")
            )
    canonical_inventory = audit.get("canonicalInventory") or {}
    if canonical_inventory:
        lines += [
            "",
            "### Canonical Yarn inventory",
            "",
            f"- Reconstruction complete: `{'yes' if canonical_inventory.get('complete') else 'NO'}`",
            f"- Reachable yarn.lock entries: `{canonical_inventory.get('reachableEntries', 0)}`",
            f"- Audited package@version pairs: `{canonical_inventory.get('packageVersionPairs', 0)}`",
            f"- Direct package names: `{len(canonical_inventory.get('directPackages') or [])}`",
        ]
        unresolved_edges = canonical_inventory.get("unresolvedEdges") or []
        if unresolved_edges:
            lines.append(
                "- Unresolved edges: "
                + ", ".join(f"`{item}`" for item in unresolved_edges[:20])
                + (f" (+{len(unresolved_edges) - 20} more)" if len(unresolved_edges) > 20 else "")
            )
    if report["audit"]["notes"]:
        lines += ["", "Notes:"] + [f"- {note}" for note in report["audit"]["notes"]]
    lines += ["", "## Direct dependencies outside lag policy", "", "| Section | Package | Current | Latest | Policy | Lag days | Current published | Status |", "|---|---|---|---|---:|---:|---|---|"]
    lagging = [item for item in report["lag"] if item["status"] == "lagging"]
    unknown = [item for item in report["lag"] if item["status"] == "unknown"]
    for item in lagging:
        lines.append(f"| {item['section']} | `{item['name']}` | `{item['current']}` | `{item['latest'] or '—'}` | ≤{item['policyMonths']}m | {item.get('lagDays', '—')} | {item.get('currentPublishedAt') or '—'} | LAGGING |")
    if not lagging:
        lines.append("| — | — | — | — | — | — | — | No known violations |")
    lines += ["", f"Lagging: **{len(lagging)}**; OK: **{sum(1 for item in report['lag'] if item['status'] == 'ok')}**; unknown: **{len(unknown)}**."]
    if unknown:
        lines += ["", "### Unknown lag checks"]
        for item in unknown:
            lines.append(f"- `{item['section']}:{item['name']}` `{item['current']}` — {item.get('error') or 'unknown'}")
    lines += ["", "## Reconciliation hint", "", "Compare package names, exact affected node versions, separately labelled severity totals and lagging rows with the dashboard generated from the same checkout and registry. A mismatch is a reason to investigate lockfile graph, registry/auth, dashboard state or stale artifacts; do not silently choose one result.", ""]
    return "\n".join(lines)


def build_report(
    project: Path,
    project_name: str,
    registry: str,
    lag_months: int = 12,
    dashboard_state: Optional[Path] = None,
    audit_workspace: Optional[Path] = None,
    project_dir_display: Optional[str] = None,
    yarn_audit_engine: str = "auto",
) -> Dict[str, Any]:
    manager = package_manager(project)
    deps = direct_dependencies(project)
    policies = load_lag_policies(dashboard_state, project_name)
    report = {
        "schemaVersion": 2,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "projectName": project_name,
        "projectDir": project_dir_display if project_dir_display is not None else str(project),
        "registry": registry,
        "packageManager": manager,
        "directDeclarations": len(deps),
        "audit": run_audit(
            project,
            manager,
            registry,
            audit_workspace=audit_workspace,
            yarn_audit_engine=yarn_audit_engine,
        ),
        "lag": check_lag(
            project,
            deps,
            registry,
            lag_months,
            policies,
            (audit_workspace / "npm-metadata-cache.json") if audit_workspace else None,
        ),
    }
    report["auditComplete"] = bool(report["audit"].get("complete"))
    report["lagComplete"] = all(item.get("status") != "unknown" for item in report["lag"])
    report["complete"] = report["auditComplete"] and report["lagComplete"]
    return report


def audit_command_exit_code(report: Dict[str, Any]) -> int:
    """Fail the command only when vulnerability evidence is unavailable.

    Missing publish dates are explicit lag records with status ``unknown``.
    They make the combined report incomplete, but are common for private
    registries and must not turn a usable, reproducible vulnerability audit
    into a failed desktop job.
    """
    return 0 if report.get("auditComplete", report.get("audit", {}).get("complete")) else 2


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Manual one-project audit cross-check: vulnerabilities + direct dependency publish-date lag.")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--project-name", default="")
    parser.add_argument("--registry", default="")
    parser.add_argument("--lag-months", type=int, default=12, choices=(3, 6, 9, 12))
    parser.add_argument("--dashboard-state", help="Optional dashboard-state.json with per-package lagMonths")
    parser.add_argument("--audit-workspace", help="Optional persistent workspace for raw audit evidence and guarded npm-lock fallback artifacts")
    parser.add_argument(
        "--yarn-audit-engine",
        choices=("auto", "yarn-native", "yarn-inventory", "npm-lock-bridge"),
        default="auto",
        help=(
            "Yarn vulnerability engine: auto builds an isolated package-lock and runs npm audit "
            "for a reproducible default. yarn-native and yarn-inventory remain available for "
            "strict canonical diagnostics."
        ),
    )
    parser.add_argument("--json-out")
    parser.add_argument("--md-out")
    args = parser.parse_args()

    project = Path(args.project_dir).expanduser().resolve()
    if not (project / "package.json").exists():
        raise SystemExit(f"package.json not found: {project / 'package.json'}")
    dashboard_state = Path(args.dashboard_state).expanduser().resolve() if args.dashboard_state else None
    audit_workspace = Path(args.audit_workspace).expanduser().resolve() if args.audit_workspace else None
    report = build_report(
        project,
        args.project_name,
        args.registry,
        args.lag_months,
        dashboard_state,
        audit_workspace,
        yarn_audit_engine=args.yarn_audit_engine,
    )
    md = markdown(report)
    print(md)
    if args.json_out:
        out = Path(args.json_out).expanduser().resolve(); out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.md_out:
        out = Path(args.md_out).expanduser().resolve(); out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
    if audit_workspace:
        (audit_workspace / "manual-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (audit_workspace / "manual-audit.md").write_text(md, encoding="utf-8")
    return audit_command_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
