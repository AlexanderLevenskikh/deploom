#!/usr/bin/env python3
"""
Live dependency roadmap generator for npm/Yarn projects.

V14 adds compact agent prompts, compact scope manifests, and checkpoint-based continuation.

Input:
  - Preferred: settings.json with projects/registry/output/history paths.
  - Legacy: a text file with project directories, one per line. Each directory must contain
    package.json and preferably package-lock.json or yarn.lock.
  - Optional JSON grouping overrides.

Output:
  - Markdown report: Project -> Group -> dependency table.
  - Optional JSON report.
  - Optional self-contained interactive HTML report.

Data sources:
  - package.json: direct runtime/dev dependency list.
  - package.json: direct dependency declarations and requested ranges.
  - the selected project-manager lockfile: exact resolved current versions
    (yarn.lock for Yarn, package-lock.json/npm-shrinkwrap.json for npm).
  - npm registry: available versions, latest dist-tag, publish dates.
  - OSV.dev: known vulnerabilities for npm package/version pairs.

Install:
  python -m pip install requests semantic_version python-dateutil

Basic usage:
  python dependency_live_roadmap_generator_v13.py \
    --projects-file projects.txt \
    --root C:/work/repo \
    --out dependency-roadmap.md \
    --json-out dependency-roadmap.json \
    --html-out dependency-roadmap.html

projects.txt format (legacy):
  # comments are allowed
  frontend/app-a
  app-b=C:/work/app-b/front
  checkout-form | ./checkout-form

Grouping is heuristic. You can override package/group/reason with --groups-config.
"""
from __future__ import annotations

import argparse
import html
import dataclasses
import datetime as dt
import hashlib
import heapq
import io
import json
import os
import re
import subprocess
import sys
import time
import tarfile
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import requests
from dateutil.relativedelta import relativedelta

from cli_io import configure_utf8_stdio
from dependency_audit_branch import AuditBranchError, recover_orphaned_managed_workspace
from git_hook_policy import GitHookPolicyError, run_git
from workspace_noise import relevant_porcelain_entries
from constraint_verify import (
    GlobalExactExclusionError,
    LocalizationTimeoutError,
    RankedComponentAlternative,
    VerificationUnit,
    assignment_matches_nogood,
    coordinate_global_exact_exclusions,
    merge_nogood_edges,
    parallel_ddmin,
)
from baseline_constraint_verifier import (
    BaselineVerifyConfig, BaselineVerifyResult, assignment_fingerprint,
    detect_package_manager, discover_baseline_project_checks, resolve_executable,
    structural_project_failure_signatures, verify_assignment,
)
from verification_proof import (
    VerificationProofStore,
    bind_resolved_state_identity,
    build_project_trial_key,
    build_resolver_context_key,
    build_resolver_trial_key,
    build_verification_proof_identity,
    is_fixed_manifest_spec,
    source_snapshot_fingerprint,
)
from source_snapshot import (
    SourceCaptureError,
    activate_source_snapshot_epoch,
    source_snapshot_provenance_head,
)
from resolved_dependency_state import load_resolved_dependency_state
from proven_dependency_state import (
    RESOLVER_PROOF_STATUS_NOT_REQUIRED_NO_OP,
    RESOLVER_PROOF_STATUS_PASSED,
    build_proven_dependency_envelope,
    write_proven_dependency_state,
)
from constraint_cache import (
    LearnedConstraintProof,
    dependency_failure_signature,
    load_verified_nogoods,
    persist_verified_nogood,
    dependency_failure_navigation_signature,
    matching_dependency_failure_signature,
)
from peer_solver_model import ForbiddenCombination, PackageVariable, PeerOptimizationModel, RequiresAny, forbidden
from peer_solver_z3 import solve_z3_exact
from peer_solver_transition import refine_transition_safe_groups
from dependency_interaction import (
    DIRECT_SHADOWING, PEER_REQUIREMENT, InteractionEdge, edge_index, graph_from_edges,
)
from dependency_compatibility_evidence import (
    CompatibilityEvidence, CompatibilityEvidenceError, load_compatibility_evidence, localize_compatibility_evidence,
)
from lockfile_consistency import (
    LockfileConsistencyError,
    ensure_lockfile_consistency,
    exact_package_lock_version,
    exact_yarn_lock_version,
    select_lockfile,
)
from semantic_version import NpmSpec, Version
from block_v_predicate_search import prioritize_probe_preference
from project_topology import (
    ProjectTopologyError,
    discover_project_package_directories,
    resolve_project_topology,
)
# BLOCK_Z_PROJECT_TOPOLOGY_V1
from verification_observability import (
    configure_observability_path,
    emit_observability_event,
)

# BLOCK_Y_FULL_OBSERVABILITY_V1

NPM_REGISTRY = "https://registry.npmjs.org"
OSV_QUERY_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{id}"

REQUEST_TIMEOUT = 30
OSV_BATCH_SIZE = 100
RATE_SLEEP_SEC = 0.05
REGISTRY_METADATA_MAX_ATTEMPTS = 3


# BLOCK_VH_BASELINE_INTENT_HUMAN_LOOP_V1
BASELINE_DECISION_MARKER = "DEPLOOM_BASELINE_DECISION_V1 "
BASELINE_INTENT_POLICIES = frozenset({"auto", "keep-current", "required"})
BASELINE_INTERACTIVE_UNARY_THRESHOLD = 3
_BASELINE_INTENT_CACHE_RAW = "<unset>"
_BASELINE_INTENT_CACHE: Dict[str, Any] = {"schemaVersion": 1, "policies": {}}


def _baseline_intent_payload() -> Dict[str, Any]:
    global _BASELINE_INTENT_CACHE_RAW, _BASELINE_INTENT_CACHE
    raw = str(os.environ.get("DEPLOOM_BASELINE_INTENT_JSON") or "").strip()
    if raw == _BASELINE_INTENT_CACHE_RAW:
        return _BASELINE_INTENT_CACHE
    _BASELINE_INTENT_CACHE_RAW = raw
    if not raw:
        _BASELINE_INTENT_CACHE = {"schemaVersion": 1, "policies": {}}
        return _BASELINE_INTENT_CACHE
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        _BASELINE_INTENT_CACHE = {"schemaVersion": 1, "policies": {}}
        return _BASELINE_INTENT_CACHE
    policies: Dict[str, str] = {}
    if isinstance(parsed, dict) and isinstance(parsed.get("policies"), dict):
        for name, policy in parsed["policies"].items():
            normalized = str(policy or "").strip().lower()
            if normalized in {"keep-current", "required"}:
                policies[str(name)] = normalized
    _BASELINE_INTENT_CACHE = {"schemaVersion": 1, "policies": policies}
    return _BASELINE_INTENT_CACHE


def _baseline_intent_policy(package: str) -> str:
    policy = str((_baseline_intent_payload().get("policies") or {}).get(package) or "auto")
    return policy if policy in BASELINE_INTENT_POLICIES else "auto"

# BLOCK_VH3_USER_SCOPE_SEMANTICS
def _apply_baseline_intent_scope(rows_by_project: Mapping[str, Sequence[DependencyRow]]) -> None:
    # "keep-current" is the persisted V-H wire value. Product semantics are
    # explicit: keep the package current in the real graph, but remove it from
    # this Baseline's update/health target. This is USER_POLICY, not evidence.
    for rows in rows_by_project.values():
        for row in rows:
            if _baseline_intent_policy(row.name) != "keep-current":
                continue
            row.scope_excluded = True
            row.exclusion_reason = "исключено пользователем из текущего Baseline"
            row.exclusion_source = "baseline-intent"


def _baseline_env_nonnegative_int(name: str) -> int:
    try:
        return max(0, int(str(os.environ.get(name) or "0").strip() or "0"))
    except (TypeError, ValueError):
        return 0


def _baseline_extra_iterations() -> int:
    return _baseline_env_nonnegative_int("DEPLOOM_BASELINE_EXTRA_ITERATIONS")


def _baseline_decision_grant_iterations() -> int:
    return _baseline_env_nonnegative_int("DEPLOOM_BASELINE_DECISION_GRANT_ITERATIONS")


def _baseline_interactive() -> bool:
    return str(os.environ.get("DEPLOOM_BASELINE_INTERACTIVE") or "").strip().lower() in {"1", "true", "yes", "on"}


def _baseline_human_decision_focus(
    learned_constraints: Sequence[Mapping[str, str]],
    current_versions: Mapping[str, str],
    *,
    min_confirmed: int = BASELINE_INTERACTIVE_UNARY_THRESHOLD,
) -> Optional[Dict[str, object]]:
    by_package: Dict[str, Set[str]] = defaultdict(set)
    for constraint in learned_constraints:
        if len(constraint) != 1:
            continue
        package, version = next(iter(constraint.items()))
        if _baseline_intent_policy(str(package)) != "auto":
            continue
        if str(version) == str(current_versions.get(str(package)) or ""):
            # If current itself is proven incompatible, KEEP_CURRENT is not a safe
            # escape hatch to suggest. The normal solver/budget decision remains.
            continue
        by_package[str(package)].add(str(version))
    candidates = [
        (len(versions), package, sorted(versions))
        for package, versions in by_package.items()
        if len(versions) >= max(1, int(min_confirmed))
    ]
    if not candidates:
        return None
    count, package, versions = sorted(candidates, key=lambda item: (-item[0], item[1]))[0]
    return {
        "package": package,
        "currentVersion": str(current_versions.get(package) or ""),
        "failedVersions": versions,
        "confirmedVersions": count,
    }


def _raise_baseline_human_decision(payload: Mapping[str, object]) -> None:
    rendered = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raise BaselineConstraintVerificationError(BASELINE_DECISION_MARKER + rendered)


class RegistryInfrastructureError(RuntimeError):
    """Registry/network uncertainty must never become dependency policy."""


def _sanitized_baseline_failure_tail(
    output: str,
    *,
    max_lines: int = 60,
    max_chars: int = 6000,
) -> str:
    """Expose package-manager diagnostics without echoing common credential forms."""
    text = str(output or "")
    substitutions = (
        (
            re.compile(
                r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)\S+"
            ),
            r"\1<redacted>",
        ),
        (
            re.compile(
                r"(?i)((?:_authToken|authToken|password|passwd|api[-_]?key)"
                r"\s*[=:]\s*)[^\s&]+"
            ),
            r"\1<redacted>",
        ),
        (
            re.compile(
                r"(?i)(https?://)[^/\s:@]+:[^@\s/]+@"
            ),
            r"\1<redacted>@",
        ),
        (
            re.compile(
                r"(?i)([?&](?:token|auth|key|password)=)[^&\s]+"
            ),
            r"\1<redacted>",
        ),
    )
    for pattern, replacement in substitutions:
        text = pattern.sub(replacement, text)
    lines = text.splitlines()[-max(1, int(max_lines)):]
    return "\n".join(lines)[-max(256, int(max_chars)):].strip()


class VulnerabilityEvidenceUnavailable(RuntimeError):
    """OSV uncertainty must never become an authoritative zero-vulnerability fact."""



# Package metadata may be served by the configured Nexus while individual
# version records still point at public npm/yarn tarballs, or at a tarball that
# has disappeared from the repository.  Metadata presence is therefore not
# enough to call a version installable.  Roadmap targets must be backed by an
# artifact that can actually be read from the configured registry.
REGISTRY_ARTIFACT_PROBE_BYTES = 1

GROUP_NAMES = {
    1: "Группа 1 — срочные и несложные security/removal миграции",
    2: "Группа 2 — несложные runtime/API/CI и важные простые обновления",
    3: "Группа 3 — runtime/API/CI изменения, не доказанные как простые",
    4: "Группа 4 — сложные, платформенные или заблокированные миграции",
    5: "Группа 5 — остальное / lag / DEV",
}


def group_display_name(group: int) -> str:
    """Human label only; arbitrary positive group ids are supported."""
    return GROUP_NAMES.get(group, f"Группа {group}")


def group_ids_for_rows(rows: Iterable[DependencyRow]) -> List[int]:
    return sorted({int(row.group) for row in rows})

GROUP_PRINCIPLES = """
## Принцип распределения по группам

Группа — **display-классификация для человека**: она помогает показать очередь/характер работы, сортировать и фильтровать зависимости. Номер, название и границы группы не участвуют в выборе executable target, peer-совместимости или разбиении compatibility components. Встроенная схема 1–5 — только дефолтное представление; override может использовать любое положительное число групп.

По смыслу дефолтная классификация показывает: что закрываем срочно, что можно обновить как контролируемое простое runtime/API/CI изменение, что уже требует отдельной runtime/API/CI проработки, что является platform/blocked задачей, а что остаётся в DEV/lag хвосте.

При классификации нельзя опираться только на `dependencies/devDependencies`, semver-major/minor/patch, `deprecated` или размер отставания. Важнее фактический план изменения:

- есть ли Critical/High и можно ли быстро убрать риск;
- влияет ли пакет на runtime/API/CI/build/release;
- можно ли доказать, что миграция мала, совместима и проверяется понятным smoke;
- требуется ли профильная регрессия, разбиение на подгруппы или отдельная задача;
- есть ли блокер, владелец upstream/backend/infra или platform migration.

| Группа | Когда относить сюда | Что делаем |
|---|---|---|
| 1 | Срочные, но несложные изменения: быстро снять Critical/High, обновить до версии без критичного риска или выпилить простое использование. | Маленький MR с быстрым security effect: remove/replace/update до минимально безопасной версии, OSV/outdated diff, короткий targeted smoke; ручной audit только по запросу пользователя. |
| 2 | Нужные и относительно простые runtime/API/CI обновления: compatible replacement, import-only fork, minor/small-major, CI/tooling update или lag/security hygiene, где diff малый и понятный. | Обычный MR или небольшой batch + OSV/outdated diff + локальные проверки/targeted smoke по зоне влияния; ручной audit только по запросу пользователя. |
| 3 | Runtime/API/CI изменение, которое не проходит критерий “несложное и относительно простое”: есть риск поведения, неясный changelog, несколько связанных пакетов или нужна профильная регрессия/подгруппы. | Отдельный MR или подгруппа + smoke/regression зоны влияния; при необходимости разбить на 3.1/3.2/3.3. |
| 4 | Сложная, платформенная или заблокированная миграция: build/lint/TS/UI/auth/shared/published widget, крупные major, latest-vulnerable/upstream/backend blocker. | Отдельная задача/подзадачи, владелец, план миграции, risk register при необходимости; blocked/выделенные “на потом” можно не учитывать в критериях текущего этапа. |
| 5 | Остальное: lag без срочности, настоящий DEV/local/test/storybook/mkcert шум, который не влияет на runtime, CI, release или published artifact. | Откладываем или квартальный batch; остаточные vuln/lag явно фиксируем. |

Важные правила:

- `deprecated` сам по себе не означает группу 1. Если есть совместимый fork/replacement и diff маленький — это обычно группа 2.
- `runtime/API/CI` сам по себе не означает группу 3. Если изменение доказуемо простое и контролируемое — это группа 2.
- Если runtime/API/CI изменение **не вписывается** в критерий “несложное и относительно простое”, оно попадает в группу 3.
- `devDependencies` не всегда означают группу 5. Если dev-зависимость участвует в CI/build/release/published artifact, она может быть группой 2, 3 или 4 в зависимости от сложности.
- `latest-vulnerable` лучше считать blocked/risk-register случаем: чаще группа 4, пока нет понятного безопасного target.

### Structural project-aware adjustments

- A published package/widget must treat build, lint, React and bundler dependencies as release-impacting even when they live in `devDependencies`.
- A shared/public package should upgrade build/release dependencies before downstream applications when compatibility requires it.
- Legacy routing (`react-router-dom@5`), Flow and multi-page setups increase migration complexity based on structure, not repository name.
- Projects already on modern React/Vite majors should not be classified as if they still need the corresponding platform migration.
- Projects that use both Vite and Webpack should treat bundler upgrades as a compatibility migration rather than an unrelated batch.
""".strip()

# Meeting-aligned defaults. Override in --groups-config if a project differs.
# Semantic package roles used by executable policy/scoring.  They are
# intentionally independent from display-group numbering.
URGENT_SIMPLE_POLICY_PACKAGES = frozenset({
    "recompose",
})

SIMPLE_RUNTIME_POLICY_PACKAGES = frozenset({
    "react-beautiful-dnd",
    "@hello-pangea/dnd",
    "vite-plugin-pwa",
})

COMPLEX_RUNTIME_POLICY_PACKAGES = frozenset({
    "react-router-dom",
    "@microsoft/signalr",
    "path-to-regexp",
    "pdfjs-dist",
    "react-dropzone",
    "recharts",
})

PLATFORM_MIGRATION_POLICY_PACKAGES = frozenset({
    "stylelint",
    "vite",
    "webpack",
    "oidc-client",
    "credentials-from-vault",
    "@vitejs/plugin-react",
    "@vitejs/plugin-react-swc",
    "react-router-dom",
    "@microsoft/signalr",
    "typescript",
    "eslint",
    "@typescript-eslint/parser",
    "@typescript-eslint/eslint-plugin",
    "typescript-eslint",
    "sass-loader",
    "css-loader",
})

DEFERABLE_DEV_POLICY_PACKAGES = frozenset({
    "vitest",
    "jest",
    "jsdom",
    "vite-plugin-mkcert",
    "storybook",
    "@storybook/react",
    "@storybook/builder-vite",
    "@storybook/react-vite",
    "@storybook/addon-essentials",
    "@storybook/addon-interactions",
    "@storybook/addon-links",
    "@storybook/addon-actions",
    "@storybook/addon-centered",
})

# Default display taxonomy. These copies are presentation/classification only;
# an override may move packages to arbitrary group ids without changing the
# semantic policy sets above or the resolved Baseline.
GROUP1_PACKAGES = set(URGENT_SIMPLE_POLICY_PACKAGES)
GROUP2_PACKAGES = set(SIMPLE_RUNTIME_POLICY_PACKAGES)
GROUP3_PACKAGES = set(COMPLEX_RUNTIME_POLICY_PACKAGES)
GROUP4_PACKAGES = set(PLATFORM_MIGRATION_POLICY_PACKAGES)
GROUP5_PACKAGES = set(DEFERABLE_DEV_POLICY_PACKAGES)

PUBLISHED_WIDGET_BUILD_PACKAGES = {
    "react", "react-dom", "vite-plugin-checker", "vite",
    "@vitejs/plugin-react", "@vitejs/plugin-react-swc",
    "stylelint", "typescript-eslint", "@typescript-eslint/parser",
    "@typescript-eslint/eslint-plugin",
}

BUILD_TOOLCHAIN_PACKAGES = {
    "vite",
    "webpack",
    "@vitejs/plugin-react",
    "@vitejs/plugin-react-swc",
    "vite-plugin-pwa",
    "vite-plugin-checker",
    "vite-plugin-svgr",
    "rollup",
    "esbuild",
    "typescript",
    "eslint",
    "stylelint",
    "@typescript-eslint/parser",
    "@typescript-eslint/eslint-plugin",
    "typescript-eslint",
    "babel-loader",
    "css-loader",
    "sass-loader",
    "style-loader",
    "@babel/core",
    "@babel/cli",
    "@babel/eslint-parser",
}

FLOW_TOOLCHAIN_PACKAGES = {
    "flow-bin",
    "flow-coverage-report",
    "eslint-plugin-flowtype",
    "eslint-plugin-ft-flow",
    "hermes-eslint",
    "prettier-plugin-hermes-parser",
    "babel-plugin-syntax-hermes-parser",
}

LOCAL_DEV_ONLY_PACKAGES = {
    "vitest",
    "jest",
    "jsdom",
    "vite-plugin-mkcert",
}


RISK_REGISTER_PACKAGES = {
    "eslint-config-react-app",
    "unplugin-detect-duplicated-deps",
    "flow-coverage-report",
    "eslint-plugin-flowtype",
    "react-color",
    "react-text-mask",
}

REACT_UI_PACKAGES = {"react", "react-dom"}

# Internal scopes are user configuration, never a baked-in organization default.
DEFAULT_INTERNAL_SCOPES: tuple[str, ...] = ()
INTERNAL_SCOPES = list(DEFAULT_INTERNAL_SCOPES)

SEVERITY_LABELS = {4: "C", 3: "H", 2: "M", 1: "L", 0: "U"}

FIXED_GIT_HOOK_POLICY = {
    "intermediateCommits": "skip",
    "intermediateMerges": "skip",
    "intermediatePushes": "skip",
    "releaseCommit": "run",
    "releasePush": "run",
}


def normalize_git_hook_policy(value: Optional[Dict[str, Any]]) -> Dict[str, str]:
    configured = dict(value or {})
    conflicts = [
        f"{key}={configured[key]!r} (required {expected!r})"
        for key, expected in FIXED_GIT_HOOK_POLICY.items()
        if key in configured and configured[key] != expected
    ]
    if conflicts:
        raise ValueError("GIT_HOOK_POLICY_UNSAFE: " + "; ".join(conflicts))
    return dict(FIXED_GIT_HOOK_POLICY)


def normalize_release_policy(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    configured = dict(value or {})
    if configured.get("strategy", "squash") != "squash":
        raise ValueError("RELEASE_STRATEGY_UNSUPPORTED: only squash is allowed")
    if configured.get("cleanupAuditWorkspace", True) is not True:
        raise ValueError("RELEASE_AUDIT_CLEANUP_REQUIRED: cleanupAuditWorkspace must be true")
    configured["strategy"] = "squash"
    configured["cleanupAuditWorkspace"] = True
    configured.setdefault("commitMessage", "chore(deps): update dependencies")
    commands = configured.get("finalGateCommands", [])
    if not isinstance(commands, list) or any(not isinstance(item, str) or not item.strip() for item in commands):
        raise ValueError("RELEASE_FINAL_GATES_INVALID: finalGateCommands must be an array of non-empty strings")
    configured["finalGateCommands"] = commands
    return configured


@dataclasses.dataclass
class ProjectSpec:
    name: str
    path: Path
    source_branch: str = ""
    base_branch: str = "libs"
    branch_prefix: str = ""
    merged_branch: str = ""
    release_branch: str = ""
    git_push: bool = False
    git_remote: str = "origin"
    source_checkout_guard: Optional[bool] = None
    source_checkout: Dict[str, Any] = dataclasses.field(default_factory=dict)
    audit_bootstrap_config: Dict[str, Any] = dataclasses.field(default_factory=dict)
    git_hooks: Dict[str, Any] = dataclasses.field(default_factory=dict)
    release_config: Dict[str, Any] = dataclasses.field(default_factory=dict)
    migration_config: Dict[str, Any] = dataclasses.field(default_factory=dict)
    constraint_verify_config: Dict[str, Any] = dataclasses.field(default_factory=dict)
    lockfile_sync_config: Dict[str, Any] = dataclasses.field(default_factory=dict)
    lockfile_state: Dict[str, Any] = dataclasses.field(default_factory=dict)
    current_audit: Dict[str, Any] = dataclasses.field(default_factory=dict)
    constraint_cache_path: Optional[Path] = None

    def resolved_branch_prefix(self) -> str:
        return self.branch_prefix or self.base_branch

    def resolved_merged_branch(self) -> str:
        return self.merged_branch or f"{self.base_branch}-merged"

    def resolved_release_branch(self) -> str:
        return self.release_branch or f"{self.resolved_branch_prefix()}-release"


@dataclasses.dataclass
class DependencyRow:
    project: str
    package_dir: str
    name: str
    kind: str  # runtime/dev
    requested_spec: str
    current_version: str
    current_source: str
    latest_version: str
    current_vulns: str
    min_no_critical: str
    min_no_high: str
    min_no_vuln: str
    min_lag_12m: str
    min_lag_9m: str
    min_lag_6m: str
    min_lag_3m: str
    group: int
    reason: str
    notes: str
    subgroup: str = ""
    lag_threshold_months: int = 12
    release_target: str = "—"
    release_status: str = "not-checked"
    release_summary: str = "Release notes не проверялись"
    release_coverage: str = ""
    breaking_changes: List[str] = dataclasses.field(default_factory=list)
    migration_notes: List[str] = dataclasses.field(default_factory=list)
    deprecations: List[str] = dataclasses.field(default_factory=list)
    release_requirements: List[str] = dataclasses.field(default_factory=list)
    release_sources: List[Dict[str, str]] = dataclasses.field(default_factory=list)
    release_by_target: Dict[str, "ReleaseIntelligence"] = dataclasses.field(default_factory=dict)
    target_default: str = "—"
    target_yellow: str = "—"
    target_green: str = "—"
    target_default_reason: str = "—"
    target_yellow_reason: str = "—"
    target_green_reason: str = "—"
    # Compatibility resolution is intentionally separated from display grouping.
    # desired_target_* captures the policy/planner intent before peer/registry
    # solving; target_* remains the backwards-compatible resolved target that is
    # exported to the Dashboard, prompts and Baseline artifacts.
    desired_target_default: str = ""
    desired_target_yellow: str = ""
    desired_target_green: str = ""
    resolution_reason_default: str = ""
    resolution_reason_yellow: str = ""
    resolution_reason_green: str = ""
    target_default_non_lag: str = "—"
    target_yellow_non_lag: str = "—"
    target_green_non_lag: str = "—"
    target_default_non_lag_reason: str = "—"
    target_yellow_non_lag_reason: str = "—"
    target_green_non_lag_reason: str = "—"
    target_default_has_lag: bool = False
    target_yellow_has_lag: bool = False
    target_green_has_lag: bool = False
    target_default_dynamic_locked: bool = False
    target_yellow_dynamic_locked: bool = False
    target_green_dynamic_locked: bool = False
    planning_min_lag_12m: str = ""
    planning_min_lag_9m: str = ""
    planning_min_lag_6m: str = ""
    planning_min_lag_3m: str = ""
    lag_planning_source: str = "live"
    registry_artifacts: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)
    compatibility_cohort: str = ""
    compatibility_note: str = ""
    constraint_preflight: Dict[str, Any] = dataclasses.field(default_factory=dict)
    scope_excluded: bool = False
    exclusion_reason: str = ""
    exclusion_source: str = ""
    planner_deferred: bool = False
    planner_deferred_reason: str = ""
    planner_target_default: str = ""
    planner_target_yellow: str = ""
    planner_target_green: str = ""
    # Executable action is planned before Dashboard/Executor export.  In
    # particular @types/* stub removals must not be invented later in browser
    # JavaScript after solver/transition-safety has already finished.
    planned_action_default: str = ""
    planned_action_yellow: str = ""
    planned_action_green: str = ""


@dataclasses.dataclass
class AnalysisInfo:
    metadata_available: bool = False
    non_registry: bool = False
    latest_version: str = "—"
    current_vulns: str = "unknown"
    min_no_critical: str = "неизвестно"
    min_no_high: str = "неизвестно"
    min_no_vuln: str = "неизвестно"
    min_lag_12m: str = "неизвестно"
    min_lag_9m: str = "неизвестно"
    min_lag_6m: str = "неизвестно"
    min_lag_3m: str = "неизвестно"


@dataclasses.dataclass
class ProjectProfile:
    name: str
    package_name: str = ""
    path: str = ""
    private: Optional[bool] = None
    scripts: Dict[str, str] = dataclasses.field(default_factory=dict)
    files: List[str] = dataclasses.field(default_factory=list)
    exports_repr: str = ""
    main: str = ""
    module: str = ""
    dependencies: Dict[str, str] = dataclasses.field(default_factory=dict)
    dev_dependencies: Dict[str, str] = dataclasses.field(default_factory=dict)
    peer_dependencies: Dict[str, str] = dataclasses.field(default_factory=dict)
    optional_dependencies: Dict[str, str] = dataclasses.field(default_factory=dict)
    react_major: Optional[int] = None
    vite_major: Optional[int] = None
    router_major: Optional[int] = None
    has_vite: bool = False
    has_webpack: bool = False
    has_flow: bool = False
    has_published_artifact: bool = False
    is_published_widget: bool = False
    is_shared_package: bool = False
    is_legacy_frontend: bool = False
    is_modern_stack: bool = False
    has_dual_bundlers: bool = False

    def has_dependency(self, package_name: str) -> bool:
        return package_name in self.dependencies or package_name in self.dev_dependencies or package_name in self.peer_dependencies or package_name in self.optional_dependencies

    def dependency_spec(self, package_name: str) -> str:
        for bag in (self.dependencies, self.dev_dependencies, self.peer_dependencies, self.optional_dependencies):
            if package_name in bag:
                return bag[package_name]
        return ""


@dataclasses.dataclass
class ProjectHealth:
    project: str
    status: str
    status_rank: int
    total: int
    lag_ok_12m: int
    lag_bad_12m: int
    lag_ok_pct: float
    critical: int
    high: int
    moderate: int
    low: int
    unknown: int
    reason: str
    lag_policy_summary: str = "default 12m"
    excluded: int = 0
    lag_unknown: int = 0
    removed: int = 0
    # Every dependency that fails its own lag policy right now, with the facts
    # needed to explain *why* a level is not reached: what is installed, what
    # the live policy boundary demands, and whether the current plan even has
    # a target for it. Without this the UI can only say "77.8%" and leave the
    # user guessing which packages that number is about.
    lag_blockers: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    # How many more dependencies must become lag-compliant to reach the 80%
    # yellow threshold (0 when already there).
    lag_needed_for_yellow: int = 0
    # Installed health stays anchored to the hard 80% gate. Planning is
    # projected separately after compatibility/registry narrowing.
    yellow_plan_required: int = 0
    yellow_projected_lag_ok: int = 0
    yellow_projected_lag_pct: float = 0.0
    yellow_plan_shortfall: int = 0


@dataclasses.dataclass
class WorkSuggestion:
    project: str
    group: int
    family: str
    title: str
    suggested_branch: str
    packages: List[str]
    rationale: str
    checks: str
    risk_note: str
    confidence: str = "medium"


@dataclasses.dataclass
class ReleaseIntelligence:
    target: str
    status: str
    summary: str
    coverage: str = ""
    breaking_changes: List[str] = dataclasses.field(default_factory=list)
    migration_notes: List[str] = dataclasses.field(default_factory=list)
    deprecations: List[str] = dataclasses.field(default_factory=list)
    requirements: List[str] = dataclasses.field(default_factory=list)
    sources: List[Dict[str, str]] = dataclasses.field(default_factory=list)



def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def strip_spec(spec: str) -> str:
    s = str(spec).strip()
    m = re.search(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", s)
    return m.group(1) if m else s


def safe_version(v: str) -> Optional[Version]:
    try:
        return Version.coerce(str(v), partial=False)
    except Exception:
        return None


def version_sort_key(v: str) -> Version:
    return safe_version(v) or Version("0.0.0")


def compare_semver(a: str, b: str) -> Optional[int]:
    """Return -1/0/1, or None when either side isn't parseable semver.

    Callers must treat None as "unknown" rather than "equal" — silently
    falling back to 0 previously let "not older than current" guards pass
    for rows whose current_version wasn't valid semver (e.g. a workspace/git
    version string), effectively disabling the guard for that row.
    """
    av = safe_version(a)
    bv = safe_version(b)
    if av is None or bv is None:
        return None
    return (av > bv) - (av < bv)


def is_prerelease(v: str) -> bool:
    sv = safe_version(v)
    return bool(sv and sv.prerelease)


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_dependency_knowledge(path: Optional[Path]) -> List[Dict[str, Any]]:
    """Load active entries from an append-only, revisioned knowledge document."""
    if path is None:
        return []
    if not path.exists():
        raise FileNotFoundError(f"dependency knowledge log not found: {path}")
    document = read_json(path)
    if document.get("schemaVersion") != 1 or document.get("type") != "dependency-roadmap-knowledge-log":
        raise ValueError(f"invalid dependency knowledge header: {path}")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"dependency knowledge entries must be an array: {path}")
    entries: List[Dict[str, Any]] = []
    ids = set()
    superseded = set()
    required_text = ("id", "recordedAt", "title", "symptom", "cause", "guidance")
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError(f"dependency knowledge entry {index} must be an object: {path}")
        entry = dict(raw)
        missing = [key for key in required_text if not isinstance(entry.get(key), str) or not entry[key].strip()]
        packages = entry.get("packages")
        verification = entry.get("verification")
        if missing or not isinstance(packages, list) or not packages or not all(isinstance(x, str) and x for x in packages):
            raise ValueError(f"invalid dependency knowledge entry {index}: missing {missing or 'packages'}")
        if not isinstance(verification, list) or not all(isinstance(x, str) and x for x in verification):
            raise ValueError(f"invalid dependency knowledge verification in entry {entry['id']}")
        if entry["id"] in ids:
            raise ValueError(f"duplicate dependency knowledge id {entry['id']}: {path}")
        ids.add(entry["id"])
        supersedes = entry.get("supersedes", [])
        if not isinstance(supersedes, list) or not all(isinstance(x, str) and x for x in supersedes):
            raise ValueError(f"invalid supersedes list in dependency knowledge entry {entry['id']}")
        superseded.update(supersedes)
        entries.append(entry)
    return [entry for entry in entries if entry.get("status", "active") == "active" and entry["id"] not in superseded]


def normalize_config_path_value(value: Any) -> Optional[str]:
    r"""Normalize path-like values read from settings/CLI.

    JSON users on Windows often paste paths as C:\\work\\repo or even raw
    C:\work\repo after the settings fallback has repaired the JSON parse. Python
    on Windows accepts forward slashes just fine, so we normalize backslashes to
    forward slashes before Path(...) to make config values portable and predictable.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.replace("\\", "/")


# BLOCK_W_P0_P1_TYPES_NESTED_FIX_V1
PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH = 5
PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS = frozenset({
    ".git", "node_modules", ".dependency-roadmap", ".dependency-update-history",
    ".next", "dist", "build", "coverage", ".turbo", ".cache",
})


def _nested_package_manifest_candidates(root: Path) -> List[Path]:
    """Canonical Block Z package discovery; Desktop discovery is UX-only."""
    return list(discover_project_package_directories(
        root,
        max_depth=PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH,
    ))

def resolve_project_package_path(path: Path) -> Path:
    """Resolve a repository selection to exactly one npm package root."""
    resolved = path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        return resolved
    if (resolved / "package.json").is_file():
        return resolved
    candidates = _nested_package_manifest_candidates(resolved)
    if len(candidates) == 1:
        chosen = candidates[0]
        eprint(
            f"[info] nested npm project auto-resolved: repository={resolved}, packageRoot={chosen}"
        )
        return chosen
    if len(candidates) > 1:
        preview = ", ".join(
            candidate.relative_to(resolved).as_posix() for candidate in candidates[:8]
        )
        if len(candidates) > 8:
            preview += f", ... (+{len(candidates) - 8})"
        raise ValueError(
            "PROJECT_PACKAGE_ROOT_AMBIGUOUS: selected directory contains multiple "
            f"nested package.json files ({preview}). Configure projects[].path to "
            "the exact npm package directory."
        )
    return resolved


def parse_projects_file(path: Path, root: Optional[Path]) -> List[ProjectSpec]:
    projects: List[ProjectSpec] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name: Optional[str] = None
        value = line
        if "=" in line:
            left, right = line.split("=", 1)
            name, value = left.strip(), right.strip()
        elif "|" in line:
            left, right = line.split("|", 1)
            # Support both "name | path" and "path | name"; prefer existing path side.
            a, b = left.strip(), right.strip()
            pa = (root / a if root and not Path(a).is_absolute() else Path(a)).expanduser()
            pb = (root / b if root and not Path(b).is_absolute() else Path(b)).expanduser()
            if pa.exists():
                value, name = a, b
            elif pb.exists():
                value, name = b, a
            else:
                value, name = a, b
        normalized_value = normalize_config_path_value(value) or value
        p = Path(normalized_value).expanduser()
        if root and not p.is_absolute():
            p = root / p
        p = resolve_project_package_path(p.resolve())
        if not p.exists():
            eprint(f"[warn] projects-file:{line_no}: path does not exist: {p}")
        project_name = name or p.name
        projects.append(ProjectSpec(project_name, p))
    return projects



def settings_get(settings: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in settings and settings[key] not in (None, ""):
            return settings[key]
    return default


def resolve_config_path(value: Optional[str], base: Path, root: Optional[Path] = None) -> Optional[Path]:
    normalized = normalize_config_path_value(value)
    if not normalized:
        return None
    p = Path(normalized).expanduser()
    if p.is_absolute():
        return p.resolve()
    if root is not None:
        return (root / p).resolve()
    return (base / p).resolve()


def _loads_settings_json(text: str, source: Path) -> Dict[str, Any]:
    """Load settings.json with recovery for raw Windows paths.

    Strict JSON requires either forward slashes or escaped backslashes in paths.
    Preferred: C:/work/repo
    Also valid in JSON: C:\\work\\repo

    If the file contains raw Windows-style backslashes, json.loads can fail with
    "Invalid \\escape". For settings only, we retry after escaping raw
    backslashes and print a warning. Prefer forward slashes in settings.json.
    """
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        if "Invalid \\escape" not in str(exc):
            raise ValueError(
                f"Cannot parse {source}: {exc.msg} at line {exc.lineno}, column {exc.colno}. "
                "settings.json must be valid JSON."
            ) from exc
        repaired = text.replace("\\", "\\\\")
        try:
            loaded = json.loads(repaired)
        except json.JSONDecodeError as repaired_exc:
            raise ValueError(
                f"Cannot parse {source}: {exc.msg} at line {exc.lineno}, column {exc.colno}. "
                "If you use Windows paths in JSON, write them with forward slashes "
                "like C:/Users/name/repo or escape backslashes like C:\\\\Users\\\\name."
            ) from repaired_exc
        eprint(
            f"[warn] {source}: settings contained unescaped Windows-style backslashes; "
            "parsed after escaping them. Prefer forward slashes in settings.json."
        )
    if not isinstance(loaded, dict):
        raise ValueError(f"settings file must contain a JSON object: {source}")
    return loaded


def _warn_control_chars_in_settings(value: Any, path: str = "settings") -> None:
    """Warn about paths accidentally decoded through JSON escapes."""
    if isinstance(value, dict):
        for k, v in value.items():
            _warn_control_chars_in_settings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _warn_control_chars_in_settings(v, f"{path}[{i}]")
    elif isinstance(value, str) and any(ch in value for ch in ("\n", "\r", "\t")):
        eprint(
            f"[warn] {path}: value contains a control character. "
            "If this is a Windows path, use forward slashes: C:/path/to/project."
        )


def read_settings(path: Optional[Path]) -> Tuple[Dict[str, Any], Optional[Path]]:
    if path is None:
        candidate = Path("settings.json")
        if candidate.exists():
            path = candidate
    if path is None:
        return {}, None
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"settings file not found: {resolved}")
    loaded = _loads_settings_json(resolved.read_text(encoding="utf-8"), resolved)
    _warn_control_chars_in_settings(loaded)
    return loaded, resolved


def merge_settings(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow/deep merge settings dictionaries. Lists and scalars are replaced.

    This is intentional: local settings should be able to replace projects/root/out,
    while nested dictionaries can be extended later without losing sibling keys.
    """
    result: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_settings(result[key], value)
        else:
            result[key] = value
    return result


def auto_settings_paths() -> Tuple[Optional[Path], Optional[Path]]:
    """Find project/local settings in the recommended workspace layout.

    Priority for project settings:
    1) .dependency-roadmap/settings.project.json
    2) settings.project.json
    3) settings.json (legacy)

    Priority for local settings:
    1) .dependency-roadmap/settings.local.json
    2) settings.local.json
    """
    project_candidates = [
        Path(".dependency-roadmap/settings.project.json"),
        Path("settings.project.json"),
        Path("settings.json"),
    ]
    local_candidates = [
        Path(".dependency-roadmap/settings.local.json"),
        Path("settings.local.json"),
    ]
    project = next((p for p in project_candidates if p.exists()), None)
    local = next((p for p in local_candidates if p.exists()), None)
    return project, local


def read_merged_settings(
    settings_path: Optional[Path],
    project_settings_path: Optional[Path],
    local_settings_path: Optional[Path],
) -> Tuple[Dict[str, Any], Optional[Path], List[Path]]:
    """Read settings with layered precedence.

    Merge order:
    - legacy --settings, if passed, is treated as the base config;
    - otherwise auto/explicit project settings are used as base;
    - local settings override project settings;
    - CLI args are applied later in main().

    The returned base path is the project/base settings file directory when possible,
    so relative paths from settings.project.json stay predictable.
    """
    sources: List[Path] = []
    settings: Dict[str, Any] = {}
    base_path: Optional[Path] = None

    if settings_path is not None:
        loaded, resolved = read_settings(settings_path)
        settings = merge_settings(settings, loaded)
        if resolved is not None:
            sources.append(resolved)
            base_path = resolved
    else:
        auto_project, auto_local = auto_settings_paths()
        project_path = project_settings_path or auto_project
        local_path = local_settings_path or auto_local

        if project_path is not None:
            loaded, resolved = read_settings(project_path)
            settings = merge_settings(settings, loaded)
            if resolved is not None:
                sources.append(resolved)
                base_path = resolved

        if local_path is not None:
            loaded, resolved = read_settings(local_path)
            settings = merge_settings(settings, loaded)
            if resolved is not None:
                sources.append(resolved)
                if base_path is None:
                    base_path = resolved

    return settings, base_path, sources




def settings_workspace_base(settings_path: Optional[Path]) -> Path:
    """Return the workspace root for resolving paths from config.

    Recommended layout stores project settings in:
      <workspace>/.dependency-roadmap/settings.project.json

    In that case values like `.dependency-roadmap/artifacts` must be resolved
    from <workspace>, not from <workspace>/.dependency-roadmap; otherwise we get
    duplicated paths like `.dependency-roadmap/.dependency-roadmap/artifacts`.

    Legacy settings files in the repository root keep the old behavior: paths are
    resolved from the settings file directory.
    """
    if settings_path is None:
        return Path.cwd().resolve()
    settings_dir = settings_path.parent.resolve()
    if settings_dir.name == ".dependency-roadmap":
        return settings_dir.parent.resolve()
    return settings_dir

def parse_project_entries(
    entries: Iterable[Any],
    base: Path,
    root: Optional[Path],
    global_git_hooks: Optional[Dict[str, Any]] = None,
    global_release: Optional[Dict[str, Any]] = None,
    global_migration: Optional[Dict[str, Any]] = None,
    global_lockfile_sync: Optional[Dict[str, Any]] = None,
    global_constraint_verify: Optional[Dict[str, Any]] = None,
) -> List[ProjectSpec]:
    projects: List[ProjectSpec] = []
    effective_root = root or base
    for idx, entry in enumerate(entries, start=1):
        if isinstance(entry, str):
            line = entry.strip()
            if not line or line.startswith("#"):
                continue
            # Reuse the legacy parser semantics by writing the same split logic inline.
            name: Optional[str] = None
            value = line
            if "=" in line:
                left, right = line.split("=", 1)
                name, value = left.strip(), right.strip()
            elif "|" in line:
                left, right = line.split("|", 1)
                a, b = left.strip(), right.strip()
                pa = resolve_config_path(a, base, effective_root)
                pb = resolve_config_path(b, base, effective_root)
                if pa and pa.exists():
                    value, name = a, b
                elif pb and pb.exists():
                    value, name = b, a
                else:
                    value, name = a, b
            p = resolve_config_path(value, base, effective_root)
            assert p is not None
            p = resolve_project_package_path(p)
            projects.append(ProjectSpec(name or p.name, p))
            continue
        if isinstance(entry, dict):
            if entry.get("enabled") is False:
                continue
            value = entry.get("path") or entry.get("dir") or entry.get("directory")
            if not value:
                eprint(f"[warn] settings.projects[{idx}]: missing path")
                continue
            p = resolve_config_path(str(value), base, effective_root)
            assert p is not None
            p = resolve_project_package_path(p)
            name = entry.get("name") or entry.get("project") or p.name
            git_cfg = entry.get("git") if isinstance(entry.get("git"), dict) else {}
            source_branch = str(entry.get("sourceBranch") or git_cfg.get("sourceBranch") or "")
            base_branch = str(entry.get("baseBranch") or git_cfg.get("baseBranch") or "libs")
            branch_prefix = str(entry.get("branchPrefix") or git_cfg.get("branchPrefix") or "")
            merged_branch = str(entry.get("mergedBranch") or git_cfg.get("mergedBranch") or "")
            release_branch = str(entry.get("releaseBranch") or git_cfg.get("releaseBranch") or "")
            git_push = as_bool(entry.get("gitPush") if "gitPush" in entry else git_cfg.get("push"), False)
            git_remote = str(entry.get("gitRemote") or git_cfg.get("remote") or "origin")
            guard_raw = entry.get("sourceCheckoutGuard") if "sourceCheckoutGuard" in entry else git_cfg.get("sourceCheckoutGuard")
            source_checkout_guard = None if guard_raw is None else as_bool(guard_raw, True)
            audit_cfg_raw = entry.get("auditBootstrap") if "auditBootstrap" in entry else git_cfg.get("auditBootstrap")
            audit_cfg = dict(audit_cfg_raw) if isinstance(audit_cfg_raw, dict) else {}
            hooks_cfg = dict(global_git_hooks or {})
            project_hooks_raw = entry.get("gitHooks") if "gitHooks" in entry else git_cfg.get("hooks")
            if isinstance(project_hooks_raw, dict):
                hooks_cfg.update(project_hooks_raw)
            release_cfg = dict(global_release or {})
            project_release_raw = entry.get("release") if "release" in entry else git_cfg.get("release")
            if isinstance(project_release_raw, dict):
                release_cfg.update(project_release_raw)
            migration_cfg = dict(global_migration or {})
            project_migration_raw = entry.get("migration")
            if isinstance(project_migration_raw, dict):
                migration_cfg.update(project_migration_raw)
            lockfile_cfg = dict(global_lockfile_sync or {})
            constraint_verify_cfg = dict(global_constraint_verify or {})
            project_lockfile_raw = entry.get("lockfileSync") if "lockfileSync" in entry else git_cfg.get("lockfileSync")
            if isinstance(project_lockfile_raw, dict):
                lockfile_cfg.update(project_lockfile_raw)
            project_constraint_verify_raw = entry.get("constraintVerification") if "constraintVerification" in entry else git_cfg.get("constraintVerification")
            if isinstance(project_constraint_verify_raw, dict):
                constraint_verify_cfg.update(project_constraint_verify_raw)
            projects.append(ProjectSpec(
                name=str(name),
                path=p,
                source_branch=source_branch,
                base_branch=base_branch,
                branch_prefix=branch_prefix,
                merged_branch=merged_branch,
                release_branch=release_branch,
                git_push=git_push,
                git_remote=git_remote,
                source_checkout_guard=source_checkout_guard,
                audit_bootstrap_config=audit_cfg,
                git_hooks=hooks_cfg,
                release_config=release_cfg,
                migration_config=migration_cfg,
                constraint_verify_config=constraint_verify_cfg,
                lockfile_sync_config=lockfile_cfg,
            ))
            continue
        eprint(f"[warn] settings.projects[{idx}]: unsupported entry: {entry!r}")
    return projects



class SourceCheckoutGuardError(RuntimeError):
    def __init__(self, code: str, project: ProjectSpec, detail: str):
        self.code = code
        self.project = project
        self.detail = detail
        super().__init__(f"{code}: {project.name}: {detail}")


def _git_command(project: ProjectSpec, args: List[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Source checkout/fetch/fast-forward is tool-managed lifecycle work too.
    # Use the same command-local empty hooksPath as every other intermediate
    # Git operation so post-checkout/post-merge hooks do not run before the
    # final release commit.
    try:
        result = run_git(
            project.path,
            args,
            skip_hooks=True,
            check=False,
            capture_output=True,
        )
    except (FileNotFoundError, OSError, GitHookPolicyError) as exc:
        raise SourceCheckoutGuardError("GIT_NOT_FOUND", project, str(exc)) from exc
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "git command failed").strip()
        raise SourceCheckoutGuardError(
            "GIT_COMMAND_FAILED",
            project,
            f"{' '.join(args)}: {message[-1000:]}",
        )
    return result


def _git_value(project: ProjectSpec, args: List[str]) -> str:
    return _git_command(project, args).stdout.strip()


def _dirty_checkout_details(project: ProjectSpec) -> str:
    result = _git_command(
        project,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    if not result.stdout:
        return ""
    entries = relevant_porcelain_entries(result.stdout, nul=True)
    preview = "; ".join(entries[:12])
    if len(entries) > 12:
        preview += f"; ... (+{len(entries) - 12} more)"
    return preview


def ensure_source_checkout(project: ProjectSpec, *, allow_checkpoint_resume: bool = False) -> Dict[str, Any]:
    """Move a clean checkout to the exact fetched source branch commit.

    The guard never resets, stashes, rebases or discards local work.  A dirty
    tree fails before fetch/checkout.  A local source branch may be fast-forwarded
    but must end at exactly ``remote/sourceBranch``; ahead/diverged branches fail.
    """
    if not project.path.exists():
        raise SourceCheckoutGuardError("SOURCE_PROJECT_NOT_FOUND", project, str(project.path))
    inside = _git_command(project, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
        raise SourceCheckoutGuardError("SOURCE_NOT_GIT_REPOSITORY", project, str(project.path))

    dirty = _dirty_checkout_details(project)
    if dirty:
        workspace = str(project.audit_bootstrap_config.get("workspace") or ".dependency-roadmap-audit")
        try:
            recovered = recover_orphaned_managed_workspace(project.path, workspace)
        except AuditBranchError as exc:
            raise SourceCheckoutGuardError(exc.code, project, exc.detail) from exc
        if recovered:
            eprint(
                f"[warn] recovered orphaned tool-managed audit workspace before source sync: "
                f"{project.name} {workspace}"
            )
            dirty = _dirty_checkout_details(project)
        if dirty:
            raise SourceCheckoutGuardError(
                "SOURCE_CHECKOUT_DIRTY",
                project,
                f"commit/stash/remove changes before generation: {dirty}",
            )

    branch = project.source_branch.strip()
    remote = project.git_remote.strip() or "origin"
    if not branch:
        raise SourceCheckoutGuardError(
            "SOURCE_BRANCH_NOT_CONFIGURED",
            project,
            "configure projects[].git.sourceBranch or disable the guard explicitly",
        )

    remote_check = _git_command(project, ["remote", "get-url", remote], check=False)
    if remote_check.returncode != 0:
        raise SourceCheckoutGuardError(
            "SOURCE_REMOTE_NOT_FOUND",
            project,
            f"remote {remote!r} is not configured",
        )

    remote_ref = f"refs/remotes/{remote}/{branch}"
    fetch_refspec = f"+refs/heads/{branch}:{remote_ref}"
    fetch = _git_command(project, ["fetch", "--prune", remote, fetch_refspec], check=False)
    if fetch.returncode != 0:
        message = (fetch.stderr or fetch.stdout or "git fetch failed").strip()
        if allow_checkpoint_resume:
            # A resumable Baseline is already pinned to the source snapshot that
            # created its checkpoint. A temporary remote outage must not discard
            # that work. We lease only the locally cached remote snapshot; the
            # exact localization identity is still checked later.
            cached_remote_probe = _git_command(project, ["rev-parse", remote_ref], check=False)
            local_head_probe = _git_command(project, ["rev-parse", "HEAD"], check=False)
            branch_probe = _git_command(project, ["branch", "--show-current"], check=False)
            cached_remote = (cached_remote_probe.stdout or "").strip() if cached_remote_probe.returncode == 0 else ""
            local_head = (local_head_probe.stdout or "").strip() if local_head_probe.returncode == 0 else ""
            local_branch = (branch_probe.stdout or "").strip() if branch_probe.returncode == 0 else ""
            dirty_resume = _dirty_checkout_details(project)
            if cached_remote and local_head and cached_remote == local_head and local_branch == branch and not dirty_resume:
                metadata = {
                    "verified": True,
                    "remote": remote,
                    "sourceBranch": branch,
                    "sourceCommit": local_head,
                    "remoteCommit": cached_remote,
                    "verifiedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "remoteFresh": False,
                    "resumedFromCheckpoint": True,
                }
                project.source_checkout = metadata
                eprint(
                    f"[warn] {project.name}: source fetch failed, but a Baseline checkpoint exists; "
                    f"resuming cached source snapshot {local_head[:12]} because HEAD == {remote_ref}. "
                    "Checkpoint identity will still be validated before evidence reuse."
                )
                return metadata
        raise SourceCheckoutGuardError(
            "SOURCE_FETCH_FAILED",
            project,
            f"cannot fetch {remote}/{branch}: {message[-1000:]}",
        )

    verify_remote = _git_command(project, ["show-ref", "--verify", "--quiet", remote_ref], check=False)
    if verify_remote.returncode != 0:
        raise SourceCheckoutGuardError(
            "SOURCE_BRANCH_NOT_FOUND",
            project,
            f"fetched reference {remote}/{branch} does not exist",
        )

    local_ref = f"refs/heads/{branch}"
    local_exists = _git_command(project, ["show-ref", "--verify", "--quiet", local_ref], check=False).returncode == 0
    current = _git_command(project, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False).stdout.strip()
    if current != branch:
        if local_exists:
            checkout = _git_command(project, ["checkout", branch], check=False)
        else:
            checkout = _git_command(project, ["checkout", "-b", branch, "--track", f"{remote}/{branch}"], check=False)
        if checkout.returncode != 0:
            message = (checkout.stderr or checkout.stdout or "git checkout failed").strip()
            raise SourceCheckoutGuardError(
                "SOURCE_CHECKOUT_FAILED",
                project,
                f"cannot switch to {branch}: {message[-1000:]}",
            )

    ff = _git_command(project, ["merge", "--ff-only", f"{remote}/{branch}"], check=False)
    if ff.returncode != 0:
        message = (ff.stderr or ff.stdout or "fast-forward failed").strip()
        raise SourceCheckoutGuardError(
            "SOURCE_BRANCH_DIVERGED",
            project,
            f"local {branch} cannot fast-forward to {remote}/{branch}: {message[-1000:]}",
        )

    source_commit = _git_value(project, ["rev-parse", "HEAD"])
    remote_commit = _git_value(project, ["rev-parse", remote_ref])
    if source_commit != remote_commit:
        raise SourceCheckoutGuardError(
            "SOURCE_BRANCH_DIVERGED",
            project,
            f"local {branch}={source_commit} differs from {remote}/{branch}={remote_commit}; no reset was performed",
        )

    dirty_after = _dirty_checkout_details(project)
    if dirty_after:
        raise SourceCheckoutGuardError(
            "SOURCE_CHECKOUT_DIRTY_AFTER_SYNC",
            project,
            f"checkout became dirty during synchronization: {dirty_after}",
        )

    metadata = {
        "verified": True,
        "remote": remote,
        "sourceBranch": branch,
        "sourceCommit": source_commit,
        "remoteCommit": remote_commit,
        "verifiedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    project.source_checkout = metadata
    return metadata


def ensure_output_parent(path: Optional[Path]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)


def write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def ensure_history_layout(events_log: Optional[Path], runs_dir: Optional[Path], index_file: Optional[Path]) -> None:
    if events_log:
        events_log.parent.mkdir(parents=True, exist_ok=True)
        if not events_log.exists():
            events_log.write_text("", encoding="utf-8")
        write_if_missing(events_log.parent / ".gitkeep", "")
    if runs_dir:
        runs_dir.mkdir(parents=True, exist_ok=True)
        write_if_missing(runs_dir / ".gitkeep", "")
    if index_file:
        index_file.parent.mkdir(parents=True, exist_ok=True)
        if not index_file.exists():
            index_file.write_text("# Dependency update history\n\nAppend-only index of dependency migration runs.\n\n", encoding="utf-8")


def normalize_internal_scopes(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        raw = [value]
    else:
        raw = list(value)
    return [str(scope) if str(scope).endswith("/") else str(scope) + "/" for scope in raw]


def as_int(value: Any, default: int) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def discover_projects(root: Path) -> List[ProjectSpec]:
    package_jsons = [p for p in root.rglob("package.json") if "node_modules" not in p.parts]
    result: List[ProjectSpec] = []
    for pkg in package_jsons:
        try:
            data = read_json(pkg)
            name = data.get("name") or pkg.parent.name
            result.append(ProjectSpec(name, pkg.parent))
        except Exception:
            continue
    return result


def find_lockfile(project_dir: Path) -> Optional[Path]:
    """Return the package-manager lockfile selected from package.json.

    Mixed root lockfiles are deliberately not guessed.  Callers that need an
    actionable diagnostic use the lockfile preflight before analysis; this
    helper returns None on ambiguity rather than silently preferring an old
    package-lock.json over yarn.lock.
    """
    pkg_path = project_dir / "package.json"
    if not pkg_path.exists():
        return None
    try:
        pkg_json = read_json(pkg_path)
        _manager, lockfile, _declared, _extras = select_lockfile(project_dir, pkg_json)
        return lockfile
    except LockfileConsistencyError:
        return None


def package_lock_version(lock_path: Path, package_name: str) -> Optional[str]:
    try:
        data = read_json(lock_path)
    except Exception:
        return None
    packages = data.get("packages") or {}
    key = f"node_modules/{package_name}"
    if key in packages and isinstance(packages[key], dict):
        v = packages[key].get("version")
        if isinstance(v, str):
            return v
    deps = data.get("dependencies") or {}
    if package_name in deps and isinstance(deps[package_name], dict):
        v = deps[package_name].get("version")
        if isinstance(v, str):
            return v
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
                version = m.group(1)
                for k in current_keys:
                    mapping[k] = version
            current_keys = []
    return mapping


def yarn_lock_version(lock_path: Path, package_name: str, spec: str) -> Optional[str]:
    # Never use the old "single entry for this package" fallback.  That fallback
    # made a stale yarn.lock look valid after package.json changed its range.
    return exact_yarn_lock_version(lock_path, package_name, spec)


def resolved_current_version(
    project_dir: Path,
    package_name: str,
    spec: str,
    kind: str,
    selected_lockfile: Optional[Path] = None,
) -> Tuple[str, str]:
    """Resolve a direct dependency from the selected canonical lockfile.

    Identity/discovery is intentionally weaker than package-manager authority:
    a missing canonical lockfile is represented as a package.json fallback.
    For npm *workspace* lockfiles, topology is required only when the selected
    lockfile lives above the target package so the correct workspace record can
    be addressed.
    """
    fallback = strip_spec(spec)
    if selected_lockfile is None:
        return fallback, "package.json; canonical lockfile unavailable"

    selected_lockfile = selected_lockfile.resolve()
    project_dir = project_dir.resolve()

    if selected_lockfile.name == "yarn.lock":
        version = exact_yarn_lock_version(
            selected_lockfile,
            package_name,
            spec,
        )
        if version:
            return version, "yarn.lock"

    elif selected_lockfile.name in {
        "package-lock.json",
        "npm-shrinkwrap.json",
    }:
        package_relative_to_manager = ""
        if selected_lockfile.parent != project_dir:
            try:
                topology = resolve_project_topology(
                    project_dir,
                    allow_discovery=False,
                    require_supported=False,
                )
            except ProjectTopologyError as exc:
                raise ValueError(
                    f"PROJECT_TOPOLOGY_UNAVAILABLE: {exc}"
                ) from exc

            try:
                same_lockfile = topology.lockfile.samefile(
                    selected_lockfile
                )
            except OSError:
                same_lockfile = (
                    topology.lockfile.resolve()
                    == selected_lockfile
                )
            if not same_lockfile:
                raise ValueError(
                    "PROJECT_TOPOLOGY_LOCKFILE_MISMATCH: "
                    f"selected={selected_lockfile}; "
                    f"canonical={topology.lockfile}"
                )
            package_relative_to_manager = (
                topology.package_relative_to_manager.as_posix()
                if topology.is_workspace_package
                else ""
            )

        version = exact_package_lock_version(
            selected_lockfile,
            package_name,
            package_relative=package_relative_to_manager,
        )
        if version:
            return version, selected_lockfile.name

    if kind == "peer":
        return (
            fallback,
            "package.json peer declaration; not locally resolved",
        )
    return (
        fallback,
        "package.json fallback; exact entry missing from "
        f"{selected_lockfile.name}",
    )


class LiveDataClient:
    def __init__(self, registry: str, timeout: int, batch_size: int, sleep_sec: float, use_system_proxy: bool = False):
        self.registry = registry.rstrip("/")
        self.timeout = timeout
        self.batch_size = batch_size
        self.sleep_sec = sleep_sec
        self.use_system_proxy = use_system_proxy
        self.session = requests.Session()
        # Important for Windows/corporate setups: requests inherits proxy
        # settings from environment variables and WinINet/registry through
        # urllib.getproxies() when trust_env=True. That can make the tool try
        # to reach Nexus via a stale local proxy like 127.0.0.1:10809 even
        # when the browser opens the same URL directly. By default, do not
        # inherit system proxy settings; opt in with --use-system-proxy or
        # useSystemProxy=true when the environment actually requires it.
        self.session.trust_env = use_system_proxy
        # Keep cache attributes unannotated: some IDE/type-checker setups used by
        # contributors report false-positive "Invalid type argument" diagnostics
        # on nested typing aliases here. The runtime contract is still documented
        # by method names and local variables; behavior is unchanged.
        self.npm_cache = {}
        self.vuln_detail_cache = {}
        self.osv_cache = {}
        self.text_cache = {}
        self.json_cache = {}
        self.bytes_cache = {}
        self.release_intelligence_cache = {}
        self.registry_artifact_cache = {}
        self.registry_types_cache = {}
        self.registry_runtime_entrypoint_cache = {}
        self.registry_self_types_cache = {}

    @staticmethod
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

    def registry_artifact_url_allowed(self, url: str) -> bool:
        """Allow package artifacts only from the configured registry path.

        Release notes may still come from GitHub, but package-manager metadata,
        tarballs and lockfile `resolved` URLs must stay inside the configured
        registry.  This prevents a Nexus-backed roadmap from silently selecting
        a public npm/yarn artifact.
        """
        value = str(url or "").strip()
        if not value:
            return False
        registry_parsed = urlparse(self.registry)
        artifact_parsed = urlparse(value)
        if self._origin_tuple(self.registry) != self._origin_tuple(value):
            return False
        registry_path = registry_parsed.path.rstrip("/")
        artifact_path = artifact_parsed.path or ""
        if registry_path and not (
            artifact_path == registry_path or artifact_path.startswith(registry_path + "/")
        ):
            return False
        return artifact_parsed.scheme in {"http", "https"} and bool(artifact_parsed.hostname)

    def registry_tarball_url(self, meta: Dict[str, Any], version: str) -> str:
        version_meta = (meta.get("versions") or {}).get(version)
        if not isinstance(version_meta, dict):
            return ""
        dist = version_meta.get("dist") or {}
        if not isinstance(dist, dict):
            return ""
        return str(dist.get("tarball") or "").strip()

    def registry_version_artifact(self, pkg: str, meta: Dict[str, Any], version: str) -> Dict[str, Any]:
        """Return cached proof that a version tarball is readable from Nexus.

        A tiny ranged GET is used instead of HEAD because some Nexus/proxy
        combinations expose metadata to HEAD but fail when Yarn performs the
        actual GET.  Redirects are accepted only when the final URL remains
        inside the configured registry path.
        """
        key = (pkg, version)
        if key in self.registry_artifact_cache:
            return dict(self.registry_artifact_cache[key])

        tarball_url = self.registry_tarball_url(meta, version)
        evidence: Dict[str, Any] = {
            "package": pkg,
            "version": version,
            "registry": self.registry,
            "tarballUrl": tarball_url,
            "status": "unknown",
            "httpStatus": None,
            "finalUrl": "",
            "error": "",
        }
        if not tarball_url:
            evidence["status"] = "missing-tarball-url"
            evidence["error"] = "version metadata has no dist.tarball"
            self.registry_artifact_cache[key] = evidence
            return dict(evidence)
        if not self.registry_artifact_url_allowed(tarball_url):
            evidence["status"] = "foreign-registry-url"
            evidence["error"] = "dist.tarball is outside the configured registry"
            self.registry_artifact_cache[key] = evidence
            return dict(evidence)

        response = None
        try:
            response = self.session.get(
                tarball_url,
                timeout=self.timeout,
                stream=True,
                headers={"Range": "bytes=0-0", "Accept": "application/octet-stream"},
                allow_redirects=True,
            )
            evidence["httpStatus"] = int(response.status_code)
            evidence["finalUrl"] = str(response.url or tarball_url)
            if not self.registry_artifact_url_allowed(evidence["finalUrl"]):
                evidence["status"] = "foreign-registry-redirect"
                evidence["error"] = "tarball redirected outside the configured registry"
            else:
                response.raise_for_status()
                first = next(response.iter_content(chunk_size=REGISTRY_ARTIFACT_PROBE_BYTES), b"")
                if first:
                    evidence["status"] = "available"
                else:
                    evidence["status"] = "empty-artifact"
                    evidence["error"] = "registry returned an empty artifact response"
        except requests.HTTPError as exc:
            status = int(getattr(exc.response, "status_code", 0) or 0)
            if status in {404, 410}:
                evidence["status"] = "missing-artifact"
                evidence["error"] = f"registry returned HTTP {status} for exact tarball"
            else:
                raise RegistryInfrastructureError(
                    f"REGISTRY_ARTIFACT_UNAVAILABLE: {pkg}@{version}: "
                    f"HTTP {status or 'unknown'}: {str(exc)[-500:]}"
                ) from exc
        except requests.RequestException as exc:
            raise RegistryInfrastructureError(
                f"REGISTRY_ARTIFACT_UNAVAILABLE: {pkg}@{version}: {str(exc)[-500:]}"
            ) from exc
        except Exception as exc:
            raise RegistryInfrastructureError(
                f"REGISTRY_ARTIFACT_INVALID_RESPONSE: {pkg}@{version}: {str(exc)[-500:]}"
            ) from exc
        finally:
            if response is not None:
                response.close()

        self.registry_artifact_cache[key] = evidence
        time.sleep(self.sleep_sec)
        return dict(evidence)

    def registry_version_is_installable(self, pkg: str, meta: Dict[str, Any], version: str) -> bool:
        return self.registry_version_artifact(pkg, meta, version).get("status") == "available"

    def registry_structural_candidates(self, meta: Dict[str, Any], versions: Iterable[str]) -> Tuple[List[str], List[str]]:
        """Remove metadata-only versions that point outside the configured registry.

        Network probing remains lazy.  This cheap pass blocks public registry
        URLs before OSV/target planning and records why versions were omitted.
        """
        accepted: List[str] = []
        notes: List[str] = []
        for version in versions:
            url = self.registry_tarball_url(meta, version)
            if not url:
                notes.append(f"{version}: metadata has no dist.tarball")
                continue
            if not self.registry_artifact_url_allowed(url):
                notes.append(f"{version}: dist.tarball outside configured registry")
                continue
            accepted.append(version)
        return accepted, notes

    def first_installable_version(
        self,
        pkg: str,
        meta: Dict[str, Any],
        versions: Iterable[str],
        *,
        current: str = "",
    ) -> str:
        for version in versions:
            if current and version == current:
                return version
            if self.registry_version_is_installable(pkg, meta, version):
                return version
        return ""

    def latest_installable_version(
        self,
        pkg: str,
        meta: Dict[str, Any],
        include_prerelease: bool = False,
        *,
        current: str = "",
    ) -> str:
        versions = published_versions(meta, include_prerelease=include_prerelease)
        structural, _ = self.registry_structural_candidates(meta, versions)
        found = self.first_installable_version(pkg, meta, reversed(structural), current=current)
        return found or "не найден installable tarball в configured registry"

    def fetch_registry_tarball(self, pkg: str, meta: Dict[str, Any], version: str) -> Optional[bytes]:
        evidence = self.registry_version_artifact(pkg, meta, version)
        if evidence.get("status") != "available":
            return None
        url = str(evidence.get("finalUrl") or evidence.get("tarballUrl") or "")
        if not self.registry_artifact_url_allowed(url):
            return None
        return self.fetch_bytes(url)

    def registry_version_type_declarations_ok(self, pkg: str, meta: Dict[str, Any], version: str) -> Optional[str]:
        """Return a blocker when promised declarations are absent from tarball."""
        key = (pkg, version)
        if key in self.registry_types_cache:
            return self.registry_types_cache[key]
        data = self.fetch_registry_tarball(pkg, meta, version)
        reason = registry_tarball_missing_declared_types(data) if data else None
        self.registry_types_cache[key] = reason
        return reason

    def registry_version_runtime_entrypoint_ok(self, pkg: str, meta: Dict[str, Any], version: str) -> Optional[str]:
        """Return a blocker for a reachable but structurally broken publish."""
        key = (pkg, version)
        if key in self.registry_runtime_entrypoint_cache:
            return self.registry_runtime_entrypoint_cache[key]
        data = self.fetch_registry_tarball(pkg, meta, version)
        reason = registry_tarball_missing_declared_runtime_entrypoint(data) if data else None
        self.registry_runtime_entrypoint_cache[key] = reason
        return reason

    def registry_version_provides_own_types(self, pkg: str, meta: Dict[str, Any], version: str) -> bool:
        """Prove that an exact runtime version really ships usable declarations."""
        key = (pkg, version)
        if key in self.registry_self_types_cache:
            return bool(self.registry_self_types_cache[key])
        data = self.fetch_registry_tarball(pkg, meta, version)
        value = bool(data and registry_tarball_provides_own_types(data))
        self.registry_self_types_cache[key] = value
        return value

    def fetch_npm_metadata(self, pkg: str):
        if pkg in self.npm_cache:
            return self.npm_cache[pkg]
        encoded = pkg.replace("/", "%2F")
        url = f"{self.registry}/{encoded}"
        last_error = ""
        for attempt in range(1, REGISTRY_METADATA_MAX_ATTEMPTS + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
                status = int(getattr(response, "status_code", 0) or 0)
                if status == 404:
                    # A real 404 is a deterministic registry fact. Unlike a
                    # timeout/5xx it is safe to memoize as package absence.
                    self.npm_cache[pkg] = None
                    return None
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("registry metadata response is not a JSON object")
                self.npm_cache[pkg] = data
                time.sleep(self.sleep_sec)
                return data
            except requests.HTTPError as exc:
                status = int(getattr(exc.response, "status_code", 0) or 0)
                if status == 404:
                    self.npm_cache[pkg] = None
                    return None
                last_error = f"HTTP {status or 'unknown'}: {exc}"
                if 400 <= status < 500 and status != 429:
                    break
            except requests.RequestException as exc:
                last_error = str(exc)
            except (ValueError, TypeError) as exc:
                last_error = f"invalid registry response: {exc}"
                break
            except Exception as exc:
                last_error = str(exc)
                break

            if attempt < REGISTRY_METADATA_MAX_ATTEMPTS:
                delay = min(0.25 * (2 ** (attempt - 1)), 2.0)
                eprint(
                    f"[warn] npm metadata transient failure for {pkg}; "
                    f"retry {attempt}/{REGISTRY_METADATA_MAX_ATTEMPTS}: {last_error}"
                )
                time.sleep(delay)

        raise RegistryInfrastructureError(
            f"REGISTRY_METADATA_UNAVAILABLE: {pkg}: "
            f"{last_error or 'registry request failed without a stable response'}"
        )

    def query_osv_versions(
        self,
        pkg: str,
        versions: List[str],
        progress_label: str = "",
    ) -> Dict[str, List[Dict[str, Any]]]:
        result: Dict[str, List[Dict[str, Any]]] = {}
        missing = [v for v in versions if (pkg, v) not in self.osv_cache]
        batch_total = (len(missing) + self.batch_size - 1) // self.batch_size if missing else 0
        for i in range(0, len(missing), self.batch_size):
            batch = missing[i:i + self.batch_size]
            if progress_label and batch_total > 1:
                batch_index = (i // self.batch_size) + 1
                eprint(
                    f"[info] {progress_label}: OSV batch {batch_index}/{batch_total} "
                    f"({len(batch)} versions)"
                )
            payload = {"queries": [
                {"package": {"name": pkg, "ecosystem": "npm"}, "version": v}
                for v in batch
            ]}
            try:
                r = self.session.post(OSV_QUERY_BATCH, json=payload, timeout=self.timeout)
                r.raise_for_status()
                data = r.json().get("results", [])
                for v, item in zip(batch, data):
                    details: List[Dict[str, Any]] = []
                    for ref in item.get("vulns", []) or []:
                        vuln_id = ref.get("id")
                        if vuln_id:
                            details.append(self.fetch_osv_vuln(vuln_id))
                    self.osv_cache[(pkg, v)] = details
                time.sleep(self.sleep_sec)
            except VulnerabilityEvidenceUnavailable:
                raise
            except Exception as e:
                # UNKNOWN must not be cached as []: [] means a successful query
                # with zero known vulnerabilities.
                raise VulnerabilityEvidenceUnavailable(
                    f"OSV_QUERY_UNAVAILABLE: {pkg}: batch={i // self.batch_size + 1}/{max(1, batch_total)}: {str(e)[-500:]}"
                ) from e
        for v in versions:
            key = (pkg, v)
            if key not in self.osv_cache:
                raise VulnerabilityEvidenceUnavailable(
                    f"OSV_QUERY_INCOMPLETE: {pkg}@{v}: query returned no authoritative result"
                )
            result[v] = self.osv_cache[key]
        return result

    def fetch_osv_vuln(self, vuln_id: str) -> Dict[str, Any]:
        if vuln_id in self.vuln_detail_cache:
            return self.vuln_detail_cache[vuln_id]
        try:
            r = self.session.get(OSV_VULN.format(id=vuln_id), timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError("OSV vulnerability detail is not an object")
            self.vuln_detail_cache[vuln_id] = data
            time.sleep(self.sleep_sec)
            return data
        except Exception as e:
            raise VulnerabilityEvidenceUnavailable(
                f"OSV_VULN_DETAIL_UNAVAILABLE: {vuln_id}: {str(e)[-500:]}"
            ) from e


    def fetch_text(self, url: str, quiet: bool = True) -> Optional[str]:
        if url in self.text_cache:
            return self.text_cache[url]
        try:
            headers = {"Accept": "text/plain, text/markdown, application/json"}
            token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
            if token and "github" in url:
                headers["Authorization"] = f"Bearer {token}"
            response = self.session.get(url, timeout=self.timeout, headers=headers)
            response.raise_for_status()
            text = response.text
            self.text_cache[url] = text
            time.sleep(self.sleep_sec)
            return text
        except Exception as exc:
            if not quiet:
                eprint(f"[warn] text unavailable {url}: {exc}")
            self.text_cache[url] = None
            return None

    def fetch_json_url(self, url: str, quiet: bool = True) -> Optional[Any]:
        if url in self.json_cache:
            return self.json_cache[url]
        try:
            headers = {"Accept": "application/vnd.github+json, application/json"}
            token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
            if token and "api.github.com" in url:
                headers["Authorization"] = f"Bearer {token}"
            response = self.session.get(url, timeout=self.timeout, headers=headers)
            response.raise_for_status()
            data = response.json()
            self.json_cache[url] = data
            time.sleep(self.sleep_sec)
            return data
        except Exception as exc:
            if not quiet:
                eprint(f"[warn] json unavailable {url}: {exc}")
            self.json_cache[url] = None
            return None


    def fetch_bytes(self, url: str, quiet: bool = True, max_bytes: int = 8 * 1024 * 1024) -> Optional[bytes]:
        if url in self.bytes_cache:
            return self.bytes_cache[url]
        try:
            response = self.session.get(url, timeout=self.timeout, stream=True)
            response.raise_for_status()
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length and content_length > max_bytes:
                raise ValueError(f"response too large: {content_length} bytes")
            chunks: List[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"response exceeded {max_bytes} bytes")
                chunks.append(chunk)
            data = b"".join(chunks)
            self.bytes_cache[url] = data
            time.sleep(self.sleep_sec)
            return data
        except Exception as exc:
            if not quiet:
                eprint(f"[warn] bytes unavailable {url}: {exc}")
            self.bytes_cache[url] = None
            return None

    def fetch_release_intelligence(self, package: str, meta: Dict[str, Any], current: str, target: str) -> ReleaseIntelligence:
        cache_key = (package, current, target)
        if cache_key in self.release_intelligence_cache:
            return self.release_intelligence_cache[cache_key]
        result = build_release_intelligence(self, package, meta, current, target)
        self.release_intelligence_cache[cache_key] = result
        return result

def repository_url_from_metadata(meta: Dict[str, Any], target: str = "") -> str:
    candidates: List[Any] = []
    if target:
        version_meta = (meta.get("versions") or {}).get(target)
        if isinstance(version_meta, dict):
            candidates.extend([version_meta.get("repository"), version_meta.get("homepage")])
    candidates.extend([meta.get("repository"), meta.get("homepage"), meta.get("bugs")])
    for raw in candidates:
        if isinstance(raw, dict):
            raw = raw.get("url")
        if not raw:
            continue
        value = str(raw).strip()
        value = re.sub(r"^(git\+|git://)", "https://", value)
        value = re.sub(r"^github:", "https://github.com/", value)
        value = value.removesuffix(".git")
        if "github.com/" in value:
            return value
    return ""


def github_repo_parts(url: str) -> Optional[Tuple[str, str]]:
    if not url:
        return None
    match = re.search(r"github\.com[/:]([^/]+)/([^/#?]+)", url)
    if not match:
        return None
    return match.group(1), match.group(2).removesuffix(".git")


def version_in_window(value: str, current: str, target: str) -> bool:
    version = semver_value(value)
    current_v = semver_value(current)
    target_v = semver_value(target)
    return bool(version and current_v and target_v and version > current_v and version <= target_v)


def extract_relevant_markdown_details(text: str, current: str, target: str, limit: int = 30000) -> Tuple[str, List[str], bool]:
    """Return version sections while preserving nested headings.

    A common changelog shape is ``## 2.0.0`` followed by ``### Breaking`` and
    ``### Migration``. Splitting at every heading loses those nested details,
    so a selected version section extends until the next heading of the same or
    higher level.
    """
    if not text:
        return "", [], False
    lines = text.splitlines()
    headings: List[Tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            headings.append((index, len(heading.group(1)), line))

    selected_ranges: List[Tuple[int, int]] = []
    matched_versions: List[str] = []
    for position, (start, level, header) in enumerate(headings):
        match = re.search(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", header)
        if not match or not version_in_window(match.group(0), current, target):
            continue
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1:]:
            if next_level <= level:
                end = next_start
                break
        selected_ranges.append((start, end))
        matched_versions.append(match.group(0))

    if selected_ranges:
        selected: List[str] = []
        last_end = -1
        for start, end in sorted(selected_ranges):
            # Avoid duplicate content when unusual nested version headings overlap.
            start = max(start, last_end)
            if start < end:
                selected.extend(lines[start:end])
                last_end = end
            if sum(len(line) + 1 for line in selected) >= limit:
                break
        return "\n".join(selected)[:limit], sorted(set(matched_versions), key=version_sort_key), False
    # Unknown changelog layout: inspect only the beginning and explicitly mark
    # coverage as fallback/partial so absence of evidence is not overstated.
    return text[:limit], [], True


def extract_relevant_markdown(text: str, current: str, target: str, limit: int = 30000) -> str:
    return extract_relevant_markdown_details(text, current, target, limit)[0]

def compact_evidence_lines(text: str, patterns: Iterable[str], max_items: int = 6) -> List[str]:
    result: List[str] = []
    regexes = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw.strip(" #-*\t"))
        if len(line) < 4 or len(line) > 320:
            continue
        if any(regex.search(line) for regex in regexes) and line not in result:
            result.append(line)
            if len(result) >= max_items:
                break
    return result


def _collect_nested_types_conditions(node: Any, out: List[str]) -> None:
    """Collect every `types` condition value found anywhere under `node`.

    Conditional exports nest `types` under `import`/`require`/`node`/etc
    (e.g. `{"import": {"types": "./index.d.ts", ...}, "require": {...}}`),
    the standard shape for a dual ESM/CJS package -- a plain top-level
    `.get("types")` misses it entirely.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "types":
                if isinstance(value, str) and value.strip():
                    out.append(value.strip())
                else:
                    _collect_nested_types_conditions(value, out)
            else:
                _collect_nested_types_conditions(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_nested_types_conditions(item, out)


def declared_type_declaration_paths(package_json: Dict[str, Any]) -> List[str]:
    """Return the TypeScript declaration path(s) a package.json promises.

    Checks `types`/`typings` and every `types` condition reachable under the
    root `exports["."]` entry, including ones nested inside `import`/
    `require`/other conditions -- the conventions npm packages use to point
    at their own `.d.ts` entry point. An empty result means the package
    makes no such promise, which is the common case (plain JS, or types
    shipped separately via `@types/*`) and is not itself a problem.
    """
    candidates: List[str] = []
    for key in ("types", "typings"):
        value = package_json.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    exports = package_json.get("exports")
    root = exports.get(".") if isinstance(exports, dict) else None
    if isinstance(root, str) and root.endswith((".d.ts", ".d.cts", ".d.mts")):
        candidates.append(root)
    elif isinstance(root, (dict, list)):
        _collect_nested_types_conditions(root, candidates)
    return candidates


def _registry_tarball_manifest(data: bytes) -> Optional[Tuple[Dict[str, Any], Set[str], str]]:
    if not data:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            names = {member.name.rstrip("/") for member in members}
            manifest_member = next(
                (m for m in members if Path(m.name).name == "package.json" and m.name.count("/") <= 1),
                None,
            )
            if manifest_member is None:
                return None
            extracted = archive.extractfile(manifest_member)
            if extracted is None:
                return None
            package_json = json.loads(extracted.read().decode("utf-8", errors="replace"))
            if not isinstance(package_json, dict):
                return None
            prefix = manifest_member.name.rsplit("/", 1)[0] if "/" in manifest_member.name else ""
            return package_json, names, prefix
    except (tarfile.TarError, OSError, ValueError, json.JSONDecodeError):
        return None


def _tarball_path_exists(names: Set[str], prefix: str, declared: str, *, node_resolution: bool = False) -> bool:
    normalized = str(declared or "").strip().lstrip("./").rstrip("/")
    if not normalized:
        return False
    base = f"{prefix}/{normalized}" if prefix else normalized
    candidates = {base}
    if node_resolution and not Path(normalized).suffix:
        candidates.update({base + ext for ext in (".js", ".cjs", ".mjs", ".json", ".node")})
        candidates.update({f"{base}/index{ext}" for ext in (".js", ".cjs", ".mjs", ".json", ".node")})
    return any(candidate in names for candidate in candidates)


def registry_tarball_missing_declared_types(data: bytes) -> Optional[str]:
    """Return a blocker when package.json promises declarations absent from tarball."""
    parsed = _registry_tarball_manifest(data)
    if parsed is None:
        return None
    package_json, names, prefix = parsed
    declared = declared_type_declaration_paths(package_json)
    if not declared:
        return None
    if any(_tarball_path_exists(names, prefix, path) for path in declared):
        return None
    return f"package.json declares TypeScript types at {', '.join(declared)}, none present in the published tarball"


def _collect_runtime_export_paths(node: Any, out: List[str]) -> None:
    """Collect root runtime entrypoints while ignoring explicit type conditions."""
    if isinstance(node, str):
        value = node.strip()
        if value:
            out.append(value)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key) == "types":
                continue
            _collect_runtime_export_paths(value, out)
        return
    if isinstance(node, list):
        for item in node:
            _collect_runtime_export_paths(item, out)


def inferred_type_declaration_paths(package_json: Dict[str, Any]) -> List[str]:
    """Return declaration paths TypeScript can infer from runtime entrypoints."""
    runtime_paths: List[str] = []
    for key in ("main", "module"):
        value = package_json.get(key)
        if isinstance(value, str) and value.strip():
            runtime_paths.append(value.strip())

    exports = package_json.get("exports")
    root_export: Any = exports
    if isinstance(exports, dict) and "." in exports:
        root_export = exports.get(".")
    if isinstance(root_export, (str, dict, list)):
        _collect_runtime_export_paths(root_export, runtime_paths)

    candidates: List[str] = ["index.d.ts"]
    for raw in runtime_paths:
        normalized = str(raw).strip().split("?", 1)[0].split("#", 1)[0]
        if not normalized:
            continue
        lower = normalized.lower()
        if lower.endswith(".mjs"):
            stem = normalized[:-4]
            candidates.extend([stem + ".d.mts", stem + ".d.ts"])
        elif lower.endswith(".cjs"):
            stem = normalized[:-4]
            candidates.extend([stem + ".d.cts", stem + ".d.ts"])
        elif lower.endswith(".js"):
            candidates.append(normalized[:-3] + ".d.ts")
        elif lower.endswith(".jsx"):
            candidates.append(normalized[:-4] + ".d.ts")
        elif not Path(normalized).suffix:
            candidates.extend([normalized + ".d.ts", normalized.rstrip("/") + "/index.d.ts"])

    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def registry_tarball_provides_own_types(data: bytes) -> bool:
    """Positive proof that the runtime package ships resolvable declarations."""
    parsed = _registry_tarball_manifest(data)
    if parsed is None:
        return False
    package_json, names, prefix = parsed
    declared = declared_type_declaration_paths(package_json)
    if declared and any(_tarball_path_exists(names, prefix, path) for path in declared):
        return True
    inferred = inferred_type_declaration_paths(package_json)
    return any(_tarball_path_exists(names, prefix, path) for path in inferred)

def registry_tarball_missing_declared_runtime_entrypoint(data: bytes) -> Optional[str]:
    """Reject obviously broken publishes before package-manager/Executor work.

    npm/yarn can install a tarball whose package.json points at a file that was
    never published.  The package then fails only when a lint/build/plugin loader
    tries to require it.  Validate conservative hard entrypoints: `main` and
    executable `bin` files.  We deliberately do not reject `module`/conditional
    exports here because bundlers may choose different conditions.
    """
    parsed = _registry_tarball_manifest(data)
    if parsed is None:
        return None
    package_json, names, prefix = parsed
    main = package_json.get("main")
    if isinstance(main, str) and main.strip() and not _tarball_path_exists(names, prefix, main, node_resolution=True):
        return f"package.json main={main!r}, but the declared runtime entrypoint is absent from the published tarball"
    bin_value = package_json.get("bin")
    bin_paths: List[str] = []
    if isinstance(bin_value, str):
        bin_paths = [bin_value]
    elif isinstance(bin_value, dict):
        bin_paths = [str(value) for value in bin_value.values() if isinstance(value, str)]
    missing_bins = [path for path in bin_paths if path.strip() and not _tarball_path_exists(names, prefix, path)]
    if missing_bins:
        return f"package.json declares bin entrypoint(s) {', '.join(missing_bins)}, absent from the published tarball"
    return None


def extract_release_docs_from_tarball(data: bytes, current: str, target: str, max_total_chars: int = 50000) -> Tuple[str, List[str]]:
    """Extract changelog/migration documents shipped in an npm package tarball."""
    if not data:
        return "", []
    accepted = {"changelog.md", "changes.md", "history.md", "migration.md", "migrations.md", "upgrading.md", "upgrade.md"}
    texts: List[str] = []
    names: List[str] = []
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile() or member.size <= 0 or member.size > 1024 * 1024:
                    continue
                basename = Path(member.name).name.lower()
                if basename not in accepted:
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                raw = extracted.read().decode("utf-8", errors="replace")
                relevant = extract_relevant_markdown(raw, current, target, limit=max_total_chars - total)
                if not relevant:
                    continue
                texts.append(relevant)
                names.append(member.name)
                total += len(relevant)
                if total >= max_total_chars:
                    break
    except (tarfile.TarError, OSError, ValueError):
        return "", []
    return "\n\n".join(texts), names


def analyze_release_text(text: str, current: str, target: str, source_count: int, coverage_note: str = "", coverage_complete: bool = False) -> ReleaseIntelligence:
    major, _, _ = semver_delta(current, target)
    breaking = compact_evidence_lines(text, [
        r"\bbreaking(?: change| changes)?\b",
        r"\bbreaks?\b.*\bapi\b",
        r"\bremoved?\b.*\b(?:api|method|option|property|prop|component|export|support)\b",
        r"\b(?:api|method|option|property|prop|component|export|support)\b.*\bremoved?\b",
        r"\bno longer supports?\b",
        r"\bdropped support\b",
        r"\bincompatible\b",
    ])
    breaking = [line for line in breaking if not re.search(
        r"(?:\b(?:no|not|without)\b.{0,24}\bbreaking\b|\bnon[- ]breaking\b|\bbreaking changes?\b.{0,12}\bnone\b)",
        line,
        re.IGNORECASE,
    )]
    migration = compact_evidence_lines(text, [
        r"\bmigrat(?:e|ion|ing)\b",
        r"\bupgrade guide\b",
        r"\bcodemod\b",
        r"\bconfiguration(?: change)?\b",
        r"\brename[sd]?\b",
    ])
    deprecations = compact_evidence_lines(text, [r"\bdeprecat(?:e|ed|ion)\b"])
    requirements = compact_evidence_lines(text, [
        r"\brequires? (?:node|npm|yarn|pnpm)\b",
        r"\bminimum (?:node|npm|browser)\b",
        r"\bpeer dependenc(?:y|ies)\b",
        r"\bengines?\b.*\bnode\b",
        r"\bdropped support\b",
        r"\bbrowser support\b",
    ])

    if breaking:
        status = "breaking-confirmed"
        summary = f"Найдены явные признаки breaking changes: {len(breaking)}"
    elif source_count == 0 or not text.strip():
        status = "unavailable"
        summary = (
            "Источники найдены, но читаемый текст release notes отсутствует; breaking changes не проверены"
            if source_count else
            "Не удалось получить changelog/release notes; breaking changes не проверены"
        )
    elif major > 0:
        status = "breaking-likely"
        summary = "Major-переход: явный BREAKING-маркер не найден, но совместимость требует ручной проверки"
    elif not coverage_complete:
        status = "coverage-incomplete"
        summary = "В прочитанной части release notes breaking changes не найдены, но диапазон покрыт не полностью"
    else:
        status = "no-breaking-found"
        summary = "Во всех найденных release notes диапазона явные breaking changes не найдены"
    if migration:
        summary += f"; migration notes: {len(migration)}"
    if deprecations:
        summary += f"; deprecations: {len(deprecations)}"
    if requirements:
        summary += f"; requirements: {len(requirements)}"
    if coverage_note:
        summary += f"; {coverage_note}"
    return ReleaseIntelligence(
        target=target,
        status=status,
        summary=summary,
        coverage=coverage_note,
        breaking_changes=breaking,
        migration_notes=migration,
        deprecations=deprecations,
        requirements=requirements,
    )

def build_release_intelligence(client: LiveDataClient, package: str, meta: Dict[str, Any], current: str, target: str) -> ReleaseIntelligence:
    if not target_is_action(target):
        return ReleaseIntelligence(target=target, status="not-applicable", summary="Для строки нет target-обновления")
    repo_url = repository_url_from_metadata(meta, target)
    repo = github_repo_parts(repo_url)
    sources: List[Dict[str, str]] = []
    texts: List[str] = []
    coverage_parts: List[str] = []
    target_semver = semver_value(target)
    expected_versions = set()
    for version in (meta.get("versions") or {}):
        parsed = semver_value(str(version))
        if not parsed or not version_in_window(str(version), current, target):
            continue
        # A stable migration does not need every historical alpha/beta/RC note
        # to claim coverage of the stable release path.  Prereleases are kept
        # only when the selected target itself is a prerelease.
        if bool(parsed.prerelease) and target_semver and not bool(target_semver.prerelease):
            continue
        expected_versions.add(str(version))
    covered_versions: set[str] = set()
    fallback_sources = 0

    target_meta = (meta.get("versions") or {}).get(target)
    if isinstance(target_meta, dict) and target_meta.get("deprecated"):
        texts.append(f"Deprecated target package metadata: {target_meta.get('deprecated')}")
        sources.append({"kind": "npm-metadata", "url": f"{client.registry}/{package.replace('/', '%2F')}", "title": f"npm metadata {target}"})

    if repo:
        owner, repository = repo
        releases_url = f"https://api.github.com/repos/{owner}/{repository}/releases?per_page=100"
        releases = client.fetch_json_url(releases_url)
        release_hits = 0
        if isinstance(releases, list):
            for release in releases:
                if not isinstance(release, dict):
                    continue
                tag = str(release.get("tag_name") or release.get("name") or "")
                if not version_in_window(tag, current, target):
                    continue
                tag_match = re.search(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", tag)
                if tag_match:
                    covered_versions.add(tag_match.group(0))
                body = str(release.get("body") or "")
                if body:
                    texts.append(body)
                url = str(release.get("html_url") or releases_url)
                sources.append({"kind": "github-release", "url": url, "title": tag or "release"})
                release_hits += 1
        if release_hits:
            coverage_parts.append(f"GitHub releases в диапазоне: {release_hits}")

        changelog_found = False
        for branch in ("main", "master"):
            for filename in ("CHANGELOG.md", "Changelog.md", "HISTORY.md", "MIGRATION.md", "UPGRADING.md"):
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repository}/{branch}/{filename}"
                text = client.fetch_text(raw_url)
                if not text:
                    continue
                relevant, matched_versions, used_fallback = extract_relevant_markdown_details(text, current, target)
                if relevant:
                    texts.append(relevant)
                    covered_versions.update(matched_versions)
                    if used_fallback:
                        fallback_sources += 1
                    sources.append({"kind": "changelog", "url": f"https://github.com/{owner}/{repository}/blob/{branch}/{filename}", "title": filename})
                    changelog_found = True
                    break
            if changelog_found:
                break
        if changelog_found:
            coverage_parts.append("changelog найден")

    target_version_meta = (meta.get("versions") or {}).get(target)
    tarball_url = ""
    if isinstance(target_version_meta, dict):
        dist = target_version_meta.get("dist") or {}
        if isinstance(dist, dict):
            tarball_url = str(dist.get("tarball") or "")
    if tarball_url:
        if not client.registry_artifact_url_allowed(tarball_url):
            coverage_parts.append("target package tarball blocked: URL outside configured registry")
            tarball_data = None
        else:
            tarball_data = client.fetch_registry_tarball(package, meta, target)
            if tarball_data is None:
                coverage_parts.append("target package tarball unavailable in configured registry")
        tarball_text, tarball_docs = extract_release_docs_from_tarball(tarball_data or b"", current, target)
        if tarball_text:
            texts.append(tarball_text)
            tarball_versions = {
                match.group(0) for match in re.finditer(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", tarball_text)
                if version_in_window(match.group(0), current, target)
            }
            covered_versions.update(tarball_versions)
            if not tarball_versions:
                fallback_sources += 1
            sources.append({"kind": "npm-tarball-docs", "url": tarball_url, "title": ", ".join(tarball_docs[:4])})
            coverage_parts.append(f"docs из package tarball: {len(tarball_docs)}")

    coverage_complete = bool(expected_versions) and expected_versions.issubset(covered_versions)
    if expected_versions:
        coverage_parts.append(f"версии покрыты: {len(expected_versions & covered_versions)}/{len(expected_versions)}")
    else:
        coverage_parts.append("список промежуточных версий недоступен")
    if fallback_sources:
        coverage_parts.append(f"источников без version-section: {fallback_sources}")
    combined = "\n\n".join(texts)
    result = analyze_release_text(
        combined,
        current,
        target,
        len(sources),
        ", ".join(coverage_parts),
        coverage_complete=coverage_complete,
    )
    result.sources = sources[:12]
    return result


def enrich_release_intelligence(rows_by_project: Dict[str, List[DependencyRow]], client: LiveDataClient, enabled: bool = True, max_packages: int = 0) -> None:
    release_started = time.perf_counter()
    if not enabled:
        for rows in rows_by_project.values():
            for row in rows:
                row.release_status = "disabled"
                row.release_summary = "Release intelligence отключён настройкой"
        return
    checked = 0
    actionable = [
        row
        for rows in rows_by_project.values()
        for row in rows
        if any(target_is_action(v) for v in (row.target_default, row.target_yellow, row.target_green))
    ]
    eprint(
        f"[info] release intelligence started; actionable rows={len(actionable)}; "
        f"limit={max_packages if max_packages > 0 else 'none'}"
    )
    release_index = 0
    for rows in rows_by_project.values():
        for row in rows:
            targets = sorted(
                {v for v in (row.target_default, row.target_yellow, row.target_green) if target_is_action(v)},
                key=version_sort_key,
            )
            target = target_version_max(targets)
            if not target_is_action(target):
                row.release_status = "not-applicable"
                row.release_summary = "Для строки нет target-обновления"
                continue
            release_index += 1
            release_label = f"[release {release_index}/{len(actionable)}] {row.project}:{row.name}"
            release_item_started = time.perf_counter()
            eprint(
                f"[info] {release_label}: loading changelog/release evidence; "
                f"current={row.current_version}; targets={','.join(targets)}"
            )
            if max_packages > 0 and checked >= max_packages:
                row.release_status = "not-checked-limit"
                row.release_summary = f"Не проверено: достигнут лимит releaseIntelMaxPackages={max_packages}"
                eprint(f"[info] {release_label}: skipped by limit")
                continue
            meta = client.fetch_npm_metadata(row.name)
            if not meta:
                row.release_status = "unavailable"
                row.release_summary = "Registry metadata недоступны; changelog не проверен"
                eprint(
                    f"[info] {release_label}: unavailable after "
                    f"{time.perf_counter() - release_item_started:.1f}s"
                )
                continue
            row.release_by_target = {}
            for candidate_target in targets:
                row.release_by_target[candidate_target] = client.fetch_release_intelligence(
                    row.name, meta, row.current_version, candidate_target
                )
            intel = row.release_by_target[target]
            # Legacy summary fields intentionally describe the maximum planned target.
            # HTML/prompt exports select release intelligence for the active target mode.
            row.release_target = target
            row.release_status = intel.status
            row.release_summary = intel.summary
            row.release_coverage = intel.coverage
            row.breaking_changes = intel.breaking_changes
            row.migration_notes = intel.migration_notes
            row.deprecations = intel.deprecations
            row.release_requirements = intel.requirements
            row.release_sources = intel.sources
            checked += 1
            eprint(
                f"[info] {release_label}: done in {time.perf_counter() - release_item_started:.1f}s; "
                f"status={intel.status}; sources={len(intel.sources)}"
            )
    eprint(
        f"[info] release intelligence completed; checked={checked}; "
        f"elapsed={time.perf_counter() - release_started:.1f}s"
    )


def published_versions(meta: Dict[str, Any], include_prerelease: bool = False) -> List[str]:
    versions = list((meta.get("versions") or {}).keys())
    if not include_prerelease:
        versions = [v for v in versions if not is_prerelease(v)]
    return sorted(versions, key=version_sort_key)


def latest_version(meta: Dict[str, Any]) -> str:
    return (meta.get("dist-tags") or {}).get("latest", "—")


def version_date(meta: Dict[str, Any], version: str) -> Optional[dt.datetime]:
    raw = (meta.get("time") or {}).get(version)
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def versions_from_current(meta: Dict[str, Any], current: str, include_prerelease: bool, max_candidates: int) -> Tuple[List[str], str]:
    all_versions = published_versions(meta, include_prerelease=include_prerelease)
    cur = safe_version(current)
    if cur is None:
        candidates = all_versions
    else:
        candidates = [v for v in all_versions if (safe_version(v) and safe_version(v) >= cur)]
    note = ""
    if max_candidates > 0 and len(candidates) > max_candidates:
        note = f"candidate list capped: {len(candidates)} -> {max_candidates}; minimal versions may be approximate"
        candidates = candidates[:max_candidates]
    return candidates, note


def severity_rank(vuln: Dict[str, Any]) -> int:
    dbs = vuln.get("database_specific") if isinstance(vuln.get("database_specific"), dict) else {}
    sev = str(dbs.get("severity") or "").lower()
    if sev == "critical":
        return 4
    if sev == "high":
        return 3
    if sev in ("moderate", "medium"):
        return 2
    if sev == "low":
        return 1

    # GitHub advisories commonly provide database_specific.severity, but other OSV sources may provide CVSS.
    for item in vuln.get("severity") or []:
        score = str(item.get("score") if isinstance(item, dict) else "")
        m = re.search(r"(?:^|\s)(\d+(?:\.\d+)?)", score)
        if m:
            x = float(m.group(1))
            if x >= 9.0:
                return 4
            if x >= 7.0:
                return 3
            if x >= 4.0:
                return 2
            return 1
    return 0


def vuln_summary(vulns: List[Dict[str, Any]]) -> str:
    if not vulns:
        return "0"
    counts = {"C": 0, "H": 0, "M": 0, "L": 0, "U": 0}
    for v in vulns:
        counts[SEVERITY_LABELS.get(severity_rank(v), "U")] += 1
    return ", ".join(f"{k}:{v}" for k, v in counts.items() if v)


def min_by_vuln(
    candidates: List[str],
    vulns_by_version: Dict[str, List[Dict[str, Any]]],
    mode: str,
    is_available: Optional[Callable[[str], bool]] = None,
) -> str:
    for v in candidates:
        vulns = vulns_by_version.get(v, [])
        ranks = [severity_rank(x) for x in vulns]
        matches = False
        if mode == "no-critical" and not any(r >= 4 for r in ranks):
            matches = True
        if mode == "no-high" and not any(r >= 3 for r in ranks):
            matches = True
        if mode == "no-vuln" and not vulns:
            matches = True
        if matches and (is_available is None or is_available(v)):
            return v
    if is_available is not None:
        return "не найден installable target в configured registry; metadata/tarball нужно проверить"
    return "не найдено / latest тоже уязвим; для независимой сверки можно запустить ручной Nexus/npm audit"


def min_by_lag(
    meta: Dict[str, Any],
    candidates: List[str],
    months: int,
    *,
    latest_override: str = "",
    is_available: Optional[Callable[[str], bool]] = None,
) -> str:
    latest = latest_override or latest_version(meta)
    latest_dt = version_date(meta, latest)
    if not latest_dt:
        return "неизвестно"
    threshold = latest_dt - relativedelta(months=months)
    for v in candidates:
        d = version_date(meta, v)
        if d and d >= threshold and (is_available is None or is_available(v)):
            return v
    if is_available is not None:
        return f"не найден installable target в configured registry; нужна дата >= {threshold.date()}"
    return f"не найдено; нужна дата >= {threshold.date()}"


def _put_override(result: Dict[str, Dict[str, Any]], key: str, value: Any, inherited: Optional[Dict[str, Any]] = None) -> None:
    if not isinstance(value, dict):
        return
    merged = dict(inherited or {})
    merged.update(value)
    result[str(key)] = merged



def normalize_override_document(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Flatten global, project and subgroup overrides into lookup keys.

    Supported forms are intentionally simple and human-editable:

    - packages.<name>
    - projects.<project>.packages.<name>
    - subgroups.<name> = {packages: [...], group, lagMonths, reason}
    - projects.<project>.subgroups.<name> = same structure

    Project entries are flattened as ``Project:package``.  Older versions only
    read the top-level ``packages`` object, which silently ignored the project
    overrides shipped in the template.
    """
    result: Dict[str, Dict[str, Any]] = {}
    packages = data.get("packages") if isinstance(data, dict) else None
    if isinstance(packages, dict):
        for name, value in packages.items():
            _put_override(result, str(name), value)

    def add_subgroups(container: Any, project: Optional[str] = None) -> None:
        if not isinstance(container, dict):
            return
        for subgroup, raw in container.items():
            if not isinstance(raw, dict):
                continue
            inherited = {k: v for k, v in raw.items() if k != "packages"}
            inherited.setdefault("subgroup", str(subgroup))
            members = raw.get("packages") or []
            if isinstance(members, str):
                members = [members]
            for package_name in members:
                key = f"{project}:{package_name}" if project else str(package_name)
                _put_override(result, key, {}, inherited)

    add_subgroups(data.get("subgroups") if isinstance(data, dict) else None)
    projects = data.get("projects") if isinstance(data, dict) else None
    if isinstance(projects, dict):
        for project, project_data in projects.items():
            if not isinstance(project_data, dict):
                continue
            project_packages = project_data.get("packages") or {}
            if isinstance(project_packages, dict):
                for name, value in project_packages.items():
                    _put_override(result, f"{project}:{name}", value)
            add_subgroups(project_data.get("subgroups"), str(project))

    # Backward compatibility for a document that is itself a package map.
    if not result and isinstance(data, dict):
        for key, value in data.items():
            _put_override(result, str(key), value)
    return result


def load_group_overrides(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if not path:
        return {}
    if not path.exists():
        eprint(f"[warn] groups override file not found, continuing without overrides: {path}")
        return {}
    return normalize_override_document(read_json(path))


def load_dashboard_state(path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    if not path or not path.exists():
        return {}
    try:
        data = read_json(path)
    except Exception as exc:
        eprint(f"[warn] cannot read dashboard state {path}: {exc}")
        return {}
    package_overrides = data.get("packageOverrides") or data.get("package_overrides") or {}
    normalized: Dict[str, Any] = {"projects": {}}
    if isinstance(package_overrides, dict):
        for project, packages in package_overrides.items():
            if isinstance(packages, dict):
                normalized["projects"][str(project)] = {"packages": packages}
    return normalize_override_document(normalized)


def merge_override_maps(*maps: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for mapping in maps:
        for key, value in mapping.items():
            if key in result:
                merged = dict(result[key])
                merged.update(value)
                result[key] = merged
            else:
                result[key] = dict(value)
    return result


def parse_cli_exclusions(values: Optional[Iterable[str]]) -> Dict[str, Dict[str, Any]]:
    """Convert repeatable ``--exclude-dependency`` values into package overrides.

    Accepted forms use ``|`` so scoped package names remain unambiguous:

    - ``package|reason``
    - ``project|package|reason``
    - ``project|kind|package|reason`` where kind is runtime/dev/optional/peer

    The exclusion is deliberately explicit and reasoned. It removes the row
    from target planning, branch plans, lag coverage and vulnerability/health
    statistics, while the row remains visible and reviewable in the dashboard.
    """
    result: Dict[str, Dict[str, Any]] = {}
    valid_kinds = {"runtime", "dev", "optional", "peer"}
    for raw in values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split("|")]
        if len(parts) == 2:
            package, reason = parts
            key = package
        elif len(parts) == 3:
            project, package, reason = parts
            key = f"{project}:{package}"
        elif len(parts) >= 4:
            project, kind, package = parts[:3]
            reason = "|".join(parts[3:]).strip()
            if kind not in valid_kinds:
                raise ValueError(
                    f"invalid --exclude-dependency kind {kind!r}; "
                    "use runtime/dev/optional/peer"
                )
            key = f"{project}:{kind}:{package}"
        else:
            raise ValueError(
                "invalid --exclude-dependency value; use "
                "package|reason, project|package|reason, or project|kind|package|reason"
            )
        if not package:
            raise ValueError("--exclude-dependency package must not be empty")
        if not reason:
            raise ValueError(f"--exclude-dependency requires a reason for {package}")
        result[key] = {
            "excluded": True,
            "excludeFromScope": True,
            "exclusionReason": reason,
            "exclusionSource": "cli",
        }
    return result


def override_for_package(overrides: Dict[str, Dict[str, Any]], profile: Optional[ProjectProfile], package_name: str, kind: str = "") -> Optional[Dict[str, Any]]:
    for key in project_override_keys(profile, package_name, kind):
        if key in overrides:
            return overrides[key]
    return None


def is_internal_package(name: str) -> bool:
    return any(name.startswith(scope) for scope in INTERNAL_SCOPES)


def has_severity(summary: str, code: str) -> bool:
    return bool(re.search(rf"(?:^|,\s*){re.escape(code)}:\d+", summary or ""))


def has_critical_or_high(summary: str) -> bool:
    return has_severity(summary, "C") or has_severity(summary, "H")


def is_unknown_analysis(value: str) -> bool:
    v = (value or "").lower()
    return (
        not v
        or v in {"unknown", "неизвестно", "—"}
        or "registry unavailable" in v
        or "internal/non-registry" in v
    )


def has_safe_target(value: str) -> bool:
    v = (value or "").lower()
    return not (
        not v
        or v in {"unknown", "неизвестно", "—"}
        or v.startswith("не найдено")
        or "latest тоже уязвим" in v
        or "нужен nexus" in v
        or "npm audit" in v
    )


def has_quick_ch_target(analysis: AnalysisInfo) -> bool:
    """True when Critical/High can be removed by a concrete target version."""
    return has_safe_target(analysis.min_no_high) or has_safe_target(analysis.min_no_critical)


def is_build_or_platform_package(name: str) -> bool:
    return (
        name in BUILD_TOOLCHAIN_PACKAGES
        or name in REACT_UI_PACKAGES
        or name in PLATFORM_MIGRATION_POLICY_PACKAGES
        or name.startswith("@vitejs/")
        or name.startswith("@typescript-eslint/")
        or name.startswith("eslint")
        or name.startswith("stylelint")
        or name.endswith("-loader")
    )


def coerce_dict(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def coerce_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)]


def package_major(spec: str) -> Optional[int]:
    v = safe_version(strip_spec(spec))
    return int(v.major) if v else None


def package_json_repr(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def build_project_profile(project: ProjectSpec, pkg_json: Dict[str, Any]) -> ProjectProfile:
    scripts = coerce_dict(pkg_json.get("scripts"))
    deps = coerce_dict(pkg_json.get("dependencies"))
    dev_deps = coerce_dict(pkg_json.get("devDependencies"))
    peer_deps = coerce_dict(pkg_json.get("peerDependencies"))
    optional_deps = coerce_dict(pkg_json.get("optionalDependencies"))
    files = coerce_list(pkg_json.get("files"))
    exports_repr = package_json_repr(pkg_json.get("exports"))
    main = str(pkg_json.get("main") or "")
    module = str(pkg_json.get("module") or "")
    package_name = str(pkg_json.get("name") or project.name)
    build_script = " ".join(str(v) for k, v in scripts.items() if "build" in k.lower())
    all_text = " ".join([
        project.name,
        str(project.path),
        package_name,
        exports_repr,
        main,
        module,
        " ".join(files),
        build_script,
    ]).lower()
    profile = ProjectProfile(
        name=project.name,
        package_name=package_name,
        path=str(project.path),
        private=pkg_json.get("private") if isinstance(pkg_json.get("private"), bool) else None,
        scripts=scripts,
        files=files,
        exports_repr=exports_repr,
        main=main,
        module=module,
        dependencies=deps,
        dev_dependencies=dev_deps,
        peer_dependencies=peer_deps,
        optional_dependencies=optional_deps,
    )
    profile.react_major = package_major(profile.dependency_spec("react"))
    profile.vite_major = package_major(profile.dependency_spec("vite"))
    profile.router_major = package_major(profile.dependency_spec("react-router-dom"))
    profile.has_vite = profile.has_dependency("vite") or "vite" in build_script.lower()
    profile.has_webpack = profile.has_dependency("webpack") or "webpack" in build_script.lower()
    profile.has_flow = profile.has_dependency("flow-bin") or "flow" in scripts.get("flow", "").lower() or "flow" in all_text
    profile.has_published_artifact = (
        bool(main or module or exports_repr or files)
        and pkg_json.get("private") is not True
    ) or ".artifacts" in all_text or "npm-loader" in all_text or "build-loader" in all_text
    profile.is_published_widget = profile.has_published_artifact and (
        "npm-loader" in all_text or "build-loader" in all_text or bool(exports_repr)
    )
    profile.is_shared_package = pkg_json.get("private") is not True and bool(files or exports_repr or main or module)
    profile.is_legacy_frontend = (profile.router_major is not None and profile.router_major <= 5) or profile.has_flow
    profile.is_modern_stack = (profile.react_major or 0) >= 19 or (profile.vite_major or 0) >= 7
    profile.has_dual_bundlers = profile.has_vite and profile.has_webpack
    return profile


def section_for_dependency_kind(kind: str) -> str:
    return {
        "runtime": "dependencies",
        "dev": "devDependencies",
        "optional": "optionalDependencies",
        "peer": "peerDependencies",
    }.get(kind, kind)


def project_override_keys(profile: Optional[ProjectProfile], package_name: str, kind: str = "") -> List[str]:
    keys: List[str] = []
    section = section_for_dependency_kind(kind) if kind else ""
    if profile:
        project_aliases = {profile.name, profile.package_name, Path(profile.path).name}
        for alias in sorted(a for a in project_aliases if a):
            # Accept both dashboard-facing section keys (dependencies:react) and
            # the compact model kind (runtime:react).  This also disambiguates a
            # package declared in peerDependencies and devDependencies.
            if section:
                keys.append(f"{alias}:{section}:{package_name}")
            if kind and kind != section:
                keys.append(f"{alias}:{kind}:{package_name}")
            keys.append(f"{alias}:{package_name}")
            keys.append(f"{alias}/{package_name}")
    if section:
        keys.append(f"{section}:{package_name}")
    if kind and kind != section:
        keys.append(f"{kind}:{package_name}")
    keys.append(package_name)
    return keys


def published_widget_package(name: str) -> bool:
    return name in PUBLISHED_WIDGET_BUILD_PACKAGES or name.startswith("vite-plugin-")



def classify(name: str, kind: str, current_vulns: str, overrides: Dict[str, Dict[str, Any]], analysis: Optional[AnalysisInfo] = None, profile: Optional[ProjectProfile] = None) -> Tuple[int, str]:
    """Classify dependency into meeting-aligned groups with project-aware corrections.

    Important: package name and dependency section are not enough. Some devDependencies
    participate in published artifacts/build/release pipelines, and some projects already
    passed major platform steps (for example Logan with React 19/Vite 7).
    """
    analysis = analysis or AnalysisInfo(current_vulns=current_vulns)
    current_vulns = analysis.current_vulns or current_vulns

    item = override_for_package(overrides, profile, name, kind)
    if item is not None and item.get("group") is not None:
        group = int(item.get("group", 5))
        reason = str(item.get("reason", f"override from groups/state for {name}"))
        return group, reason

    # Structural project-aware corrections. Repository identity never affects policy.
    if profile:
        if profile.is_published_widget and published_widget_package(name):
            if name in {"react", "react-dom"}:
                return 4, "published UI runtime affects the shipped artifact; keep it in a dedicated UI/runtime migration"
            if name in {"stylelint", "typescript-eslint", "@typescript-eslint/parser", "@typescript-eslint/eslint-plugin"}:
                return 4, "published build/lint pipeline dependency; do not treat it as local DEV noise"
            return 4, "published loader/build dependency; use a dedicated build/framework migration"

        if profile.is_shared_package and kind == "dev" and name in DEFERABLE_DEV_POLICY_PACKAGES:
            return 4, "shared/package release context: devDependency may affect build/release; verify before deferring"

        if profile.is_shared_package and kind == "dev" and name in BUILD_TOOLCHAIN_PACKAGES and has_critical_or_high(current_vulns):
            return 4, "shared/package build/release dependency with C/H; update before downstream consumers"

        if profile.is_legacy_frontend and name in {"react-router-dom", "path-to-regexp"}:
            return 4, "legacy routing setup: router migration is a separate compatibility change"

        if profile.has_flow and name in FLOW_TOOLCHAIN_PACKAGES:
            return 4, "Flow/legacy toolchain requires a dedicated lint/type migration"

        if profile.is_modern_stack and name in {"react", "react-dom", "vite", "@vitejs/plugin-react", "@vitejs/plugin-react-swc"}:
            modern_for_package = (name in {"react", "react-dom"} and (profile.react_major or 0) >= 19) or (name not in {"react", "react-dom"} and (profile.vite_major or 0) >= 7)
            if modern_for_package:
                if has_critical_or_high(current_vulns):
                    return 1, "project already passed the platform major; C/H can be handled as a focused security update"
                return 5, "project already passed this platform major; low priority without C/H"

        if profile.has_dual_bundlers and name in {"vite", "webpack", "@vitejs/plugin-react", "@vitejs/plugin-react-swc"}:
            return 4, "project uses Vite and Webpack together; treat build-toolchain changes as one compatibility migration"

    # Explicit product decisions from the meeting/config.
    if name in GROUP1_PACKAGES:
        if profile and (profile.is_legacy_frontend or profile.is_shared_package):
            return 1, "recompose in a legacy/shared context: remove or replace it in a focused migration"
        return 1, "срочная маленькая миграция: убрать/заменить зависимость; локально контролируемая работа"

    if name in GROUP2_PACKAGES:
        if name == "vite-plugin-pwa" and has_critical_or_high(current_vulns):
            return 3, "PWA/service worker влияет на production update/offline flow; переносить в group 3, если update не доказан как простой runtime/CI diff"
        if has_critical_or_high(current_vulns) and has_quick_ch_target(analysis):
            return 1, "важная простая миграция с C/H: можно быстро снять Critical/High, поэтому поднимаем в срочную group 1"
        return 2, "несложное runtime/API/CI или важное простое обновление: compatible replacement/малый diff, понятный smoke"

    if name in GROUP3_PACKAGES:
        return 3, "runtime/API/CI изменение не доказано как простое; нужен отдельный MR/подгруппа и smoke зоны влияния"

    if name in GROUP4_PACKAGES:
        return 4, "сложная/platform/blocked миграция; отдельная задача, владелец или risk register"

    if name in GROUP5_PACKAGES or name.startswith("@storybook/"):
        if profile and (profile.has_published_artifact or profile.is_shared_package) and has_critical_or_high(current_vulns):
            return 4, "обычно DEV/local, но проект публикует/релизит артефакт и есть C/H; нужен impact-анализ перед откладыванием"
        return 5, "остальное/локальная dev-test-storybook зависимость; отложить, если не участвует в CI/CD/exposed artifact"

    # React/UI packages are heavy migrations regardless of internal/external scope.
    if name in REACT_UI_PACKAGES:
        return 4, "React/UI миграция; не делать ради lag без отдельного UI-регресса"

    # Known latest-vulnerable/deprecated packages.
    if name in RISK_REGISTER_PACKAGES:
        return 4, "latest-vulnerable/deprecated/upstream; нужен risk register или замена"

    # Corporate/internal packages: analyze actual data, do not auto-risk-register.
    if is_internal_package(name):
        if analysis.non_registry or is_unknown_analysis(analysis.latest_version):
            return 4, "внутренний пакет: нет registry/advisory metadata; нужен владелец или доступ к registry"

        if current_vulns in ("unknown", "неизвестно"):
            return 4, "внутренний пакет: не удалось проверить уязвимости; нужен владелец/advisory или повторный прогон"

        if has_critical_or_high(current_vulns):
            # min_no_high means no Critical and no High, see min_by_vuln(mode='no-high').
            if has_safe_target(analysis.min_no_high):
                if kind == "runtime":
                    return 3, "внутренний runtime/API пакет с C/H; если не доказано как простой quick win, вести group 3 отдельным MR со smoke"
                if profile and (profile.has_published_artifact or profile.is_shared_package):
                    return 4, "внутренний build/release пакет с C/H; safe target найден, но нужен supply-chain impact-анализ"
                return 1, "внутренний пакет с C/H и safe target: срочный небольшой security MR, проверить audit diff"
            if has_safe_target(analysis.min_no_critical):
                return 4, "внутренний пакет: Critical можно снять, но High остаётся; нужен владелец/реестр остаточного риска"
            return 4, "внутренний пакет: safe target не найден или latest всё ещё уязвим; нужен владелец upstream/risk register"

        if kind == "runtime":
            return 2, "внутренний runtime/API пакет без C/H; group 2, если diff простой и smoke понятен"
        if profile and (profile.has_published_artifact or profile.is_shared_package) and name in BUILD_TOOLCHAIN_PACKAGES:
            return 4, "внутренний dev-пакет участвует в build/release артефакте; не считать локальным DEV"
        return 5, "внутренний DEV/local lag без C/H; квартальный batch, если не участвует в CI/CD/exposed artifact"

    # Generic fallback.
    has_ch = has_critical_or_high(current_vulns)
    if has_ch:
        if profile and (profile.has_published_artifact or profile.is_shared_package) and kind == "dev":
            return 4, "devDependency с C/H участвует в build/release/published artifact; не считать локальным DEV"
        if has_quick_ch_target(analysis) and not is_build_or_platform_package(name):
            return 1, "C/H можно быстро закрыть конкретным target; срочный, но небольшой security MR"
        if kind == "runtime":
            return 3, "runtime/API/CI C/H не доказан как простой quick win; нужен product-impact анализ и smoke"
        return 4, "C/H есть, но безопасный простой target не найден или пакет похож на platform/build; нужен отдельный разбор/risk register"
    if kind == "runtime":
        return 2, "runtime/API hygiene без C/H; group 2, если update простой и проверяется targeted smoke"
    return 5, "остальное: DEV/local/lag без runtime/CI/release влияния; квартальный batch после групп 1–4"


def collect_direct_dependencies(pkg_json: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """Return every direct declaration that can affect install or consumers.

    ``peerDependencies`` and ``optionalDependencies`` used to be absent from the
    roadmap entirely.  That made scope counts look complete while the prompt was
    missing real direct declarations.  Keep repeated package names when a library
    is intentionally declared in more than one section (for example peer + dev).
    """
    deps: List[Tuple[str, str, str]] = []
    sections = (
        ("dependencies", "runtime"),
        ("devDependencies", "dev"),
        ("optionalDependencies", "optional"),
        ("peerDependencies", "peer"),
    )
    for section, kind in sections:
        values = pkg_json.get(section) or {}
        if not isinstance(values, dict):
            continue
        for name, spec in values.items():
            deps.append((str(name), kind, str(spec)))
    return sorted(deps, key=lambda x: (x[0].lower(), x[1]))


def is_non_registry_spec(spec: str) -> bool:
    return is_fixed_manifest_spec(spec)


def _is_fixed_dependency_input(row: DependencyRow) -> bool:
    # External/fixed source, not a registry version decision.
    return is_non_registry_spec(row.requested_spec)


def _partition_solver_inputs(
    rows_by_name: Mapping[str, DependencyRow],
) -> Tuple[Dict[str, DependencyRow], Dict[str, DependencyRow]]:
    # Fixed inputs stay in package.json/lockfile and therefore in real
    # package-manager verification/proof identity. They are deliberately absent
    # from the exact finite-domain model.
    solver_rows: Dict[str, DependencyRow] = {}
    fixed_rows: Dict[str, DependencyRow] = {}
    for name, row in rows_by_name.items():
        if _is_fixed_dependency_input(row):
            fixed_rows[str(name)] = row
        else:
            solver_rows[str(name)] = row
    return solver_rows, fixed_rows


def _project_constraints_over_fixed_inputs(
    constraints: Sequence[Mapping[str, str]],
    fixed_rows_by_name: Mapping[str, DependencyRow],
    *,
    project: str,
    mode: str,
    source: str,
) -> List[Dict[str, str]]:
    # Partially evaluate authoritative clauses against immutable fixed inputs.
    # A matching fixed literal is a constant True and is removed. A mismatching
    # literal makes the clause unreachable. An all-fixed matching clause means
    # the fixed environment itself is incompatible and must fail closed.
    projected_constraints: List[Dict[str, str]] = []
    for raw in constraints:
        if not raw:
            continue
        projected: Dict[str, str] = {}
        reachable = True
        matched_fixed: List[str] = []
        for raw_name, raw_version in sorted(raw.items()):
            name = str(raw_name)
            version = str(raw_version)
            fixed = fixed_rows_by_name.get(name)
            if fixed is None:
                projected[name] = version
                continue
            if version != str(fixed.current_version):
                reachable = False
                break
            matched_fixed.append(name)

        if not reachable:
            continue
        if not projected:
            fixed_detail = ", ".join(
                f"{name}@{fixed_rows_by_name[name].current_version}"
                for name in matched_fixed
            )
            raise BaselineConstraintVerificationError(
                f"FIXED_INPUT_CONSTRAINT_CONFLICT: {project}/{mode}: {source} constraint "
                f"matches only immutable non-registry resolver input(s): {fixed_detail}. "
                "Fixed-source incompatibility cannot be turned into an empty Solver clause."
            )
        if projected not in projected_constraints:
            projected_constraints.append(projected)
    return projected_constraints


def normalized_lag_months(value: Any, default: int = 12) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed in (3, 6, 9, 12) else default


REGISTRY_PREFETCH_PARALLELISM = 8


def _prefetch_registry_metadata(
    client: LiveDataClient,
    dependencies: Sequence[Tuple[str, str, str]],
    *,
    progress_label: str = "",
    max_workers: int = REGISTRY_PREFETCH_PARALLELISM,
) -> None:
    """Fetch independent packuments concurrently, then reduce deterministically."""
    names = sorted({
        str(name)
        for name, _kind, spec in dependencies
        if not is_non_registry_spec(spec) and str(name) not in client.npm_cache
    })
    if not names:
        return
    workers = max(1, min(int(max_workers), 16, len(names)))
    if workers <= 1:
        for name in names:
            client.npm_cache[name] = client.fetch_npm_metadata(name)
        return

    started = time.perf_counter()
    local = threading.local()

    def fetch_one(name: str) -> Any:
        worker = getattr(local, "client", None)
        if worker is None:
            worker = LiveDataClient(
                client.registry, timeout=client.timeout,
                batch_size=client.batch_size, sleep_sec=client.sleep_sec,
                use_system_proxy=client.use_system_proxy,
            )
            local.client = worker
        return worker.fetch_npm_metadata(name)

    results: Dict[str, Any] = {}
    errors: Dict[str, BaseException] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, name): name for name in names}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except BaseException as exc:
                errors[name] = exc

    if errors:
        first_name = sorted(errors)[0]
        first_error = errors[first_name]
        raise RegistryInfrastructureError(
            f"REGISTRY_METADATA_PREFETCH_FAILED: {first_name}: {first_error}"
        ) from first_error

    for name in names:
        client.npm_cache[name] = results[name]

    eprint(
        f"[info] {progress_label}: registry metadata prefetch completed; "
        f"packages={len(names)}; parallelism={workers}; "
        f"elapsed={time.perf_counter() - started:.1f}s"
    )


OSV_PREFETCH_PARALLELISM = 4


def _prefetch_osv_evidence(
    client: LiveDataClient,
    project: ProjectSpec,
    dependencies: Sequence[Tuple[str, str, str]],
    *,
    lock: Optional[Path],
    include_prerelease: bool,
    max_candidates: int,
    progress_label: str = "",
    max_workers: int = OSV_PREFETCH_PARALLELISM,
) -> None:
    """Warm fresh OSV evidence concurrently without changing authority or freshness.

    Every package/version query still goes to OSV in this invocation. Worker
    sessions are isolated; results are merged only after all workers complete,
    in deterministic package/key order. UNKNOWN never becomes an empty result.
    """
    jobs: Dict[str, Set[str]] = defaultdict(set)
    metadata_by_name: Dict[str, Dict[str, Any]] = {}
    for name, kind, spec in dependencies:
        if is_non_registry_spec(spec):
            continue
        meta = client.npm_cache.get(name)
        if not isinstance(meta, dict):
            continue
        try:
            current, _source = resolved_current_version(
                project.path, name, spec, kind, lock
            )
            candidates, _cap_note = versions_from_current(
                meta, current, include_prerelease, max_candidates
            )
            structural, _notes = client.registry_structural_candidates(
                meta, candidates
            )
            if current not in structural:
                structural = [current, *structural]
            jobs[name].update(str(version) for version in structural if str(version))
            metadata_by_name[name] = meta
        except (TypeError, ValueError):
            # Prefetch is an optimization only. The canonical sequential path
            # below will surface malformed package/version input normally.
            continue

    names = sorted(name for name, versions in jobs.items() if versions)
    if not names:
        return
    workers = max(1, min(int(max_workers), 16, len(names)))
    started = time.perf_counter()
    def fetch_one(name: str) -> Tuple[Dict[Tuple[str, str], Any], Dict[str, Any]]:
        worker = LiveDataClient(
            client.registry,
            timeout=client.timeout,
            batch_size=client.batch_size,
            sleep_sec=client.sleep_sec,
            use_system_proxy=client.use_system_proxy,
        )
        try:
            worker.npm_cache[name] = metadata_by_name[name]
            worker.query_osv_versions(name, sorted(jobs[name], key=version_sort_key))
            return dict(worker.osv_cache), dict(worker.vuln_detail_cache)
        finally:
            worker.session.close()

    results: Dict[str, Tuple[Dict[Tuple[str, str], Any], Dict[str, Any]]] = {}
    errors: Dict[str, BaseException] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, name): name for name in names}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except BaseException as exc:
                errors[name] = exc

    if errors:
        first_name = sorted(errors)[0]
        first_error = errors[first_name]
        # Parallel prefetch is never a proof boundary. Do not make a healthy
        # sequential OSV path fail merely because the optimization hit rate
        # limiting/contention; merge nothing and let the canonical loop query
        # every package normally.
        eprint(
            f"[warn] {progress_label}: parallel OSV prefetch unavailable for {first_name}; "
            f"falling back to sequential fresh queries: {first_error}"
        )
        return

    def same_json(left: object, right: object) -> bool:
        return json.dumps(
            left, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) == json.dumps(
            right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    for name in names:
        osv_cache, detail_cache = results[name]
        for key in sorted(osv_cache):
            value = osv_cache[key]
            if key in client.osv_cache and not same_json(client.osv_cache[key], value):
                raise VulnerabilityEvidenceUnavailable(
                    f"OSV_PREFETCH_INCONSISTENT: {key[0]}@{key[1]} changed within one invocation"
                )
            client.osv_cache[key] = value
        for vuln_id in sorted(detail_cache):
            value = detail_cache[vuln_id]
            if (
                vuln_id in client.vuln_detail_cache
                and not same_json(client.vuln_detail_cache[vuln_id], value)
            ):
                raise VulnerabilityEvidenceUnavailable(
                    f"OSV_PREFETCH_INCONSISTENT_DETAIL: {vuln_id} changed within one invocation"
                )
            client.vuln_detail_cache[vuln_id] = value

    eprint(
        f"[info] {progress_label}: OSV evidence prefetch completed; "
        f"packages={len(names)}; parallelism={workers}; "
        f"elapsed={time.perf_counter() - started:.1f}s"
    )


def analyze_project(
    project: ProjectSpec,
    client: LiveDataClient,
    overrides: Dict[str, Dict[str, Any]],
    include_prerelease: bool,
    max_candidates: int,
    progress_prefix: str = "",
) -> List[DependencyRow]:
    project_started = time.perf_counter()
    pkg_path = project.path / "package.json"
    if not pkg_path.exists():
        eprint(f"[warn] {project.name}: package.json not found in {project.path}")
        return []
    selected_lock = str(project.lockfile_state.get("lockfile") or "")
    lock = Path(selected_lock) if selected_lock else None
    pkg_json = read_json(pkg_path)
    profile = build_project_profile(project, pkg_json)
    rows: List[DependencyRow] = []
    dependencies = collect_direct_dependencies(pkg_json)
    manager = str(project.lockfile_state.get("manager") or "unknown")
    lock_name = lock.name if lock else "none"
    label = f"{progress_prefix} {project.name}".strip()
    eprint(
        f"[info] {label}: dependency analysis started; direct={len(dependencies)}; "
        f"manager={manager}; lockfile={lock_name}"
    )
    _prefetch_registry_metadata(
        client,
        dependencies,
        progress_label=label,
    )
    _prefetch_osv_evidence(
        client,
        project,
        dependencies,
        lock=lock,
        include_prerelease=include_prerelease,
        max_candidates=max_candidates,
        progress_label=label,
    )

    for dependency_index, (name, kind, spec) in enumerate(dependencies, start=1):
        dependency_started = time.perf_counter()
        dependency_label = f"{progress_prefix} [dependency {dependency_index}/{len(dependencies)}] {name}".strip()
        intent_policy = _baseline_intent_policy(name)
        if intent_policy == "keep-current":
            eprint(f"[info] {dependency_label}: excluded from this Baseline update/health scope by USER_POLICY; metadata analysis may still run because the package remains in the real manifest/package-manager graph")
        elif intent_policy == "required":
            eprint(f"[info] {dependency_label}: REQUIRED update in this Baseline by USER_POLICY")
        eprint(f"[info] {dependency_label}: resolving declared and locked versions")
        current, current_source = resolved_current_version(project.path, name, spec, kind, lock)
        notes: List[str] = []
        override = override_for_package(overrides, profile, name, kind) or {}
        subgroup = str(override.get("subgroup") or "").strip()
        dashboard_note = str(override.get("note") or "").strip()
        scope_excluded = as_bool(
            override.get("excluded", override.get("excludeFromScope", False)),
            False,
        )
        exclusion_reason = str(
            override.get("exclusionReason")
            or override.get("excludeReason")
            or (dashboard_note if scope_excluded else "")
            or ""
        ).strip()
        exclusion_source = str(override.get("exclusionSource") or "dashboard-state").strip()
        planner_deferred = as_bool(override.get("plannerDefer", False), False)
        planner_deferred_reason = str(override.get("plannerDeferReason") or "").strip()
        planner_target_default = str(override.get("plannerTargetDefault") or "").strip()
        planner_target_yellow = str(override.get("plannerTargetYellow") or "").strip()
        planner_target_green = str(override.get("plannerTargetGreen") or "").strip()
        if scope_excluded and not exclusion_reason:
            exclusion_reason = "исключено из текущего scope без указанной причины"
        lag_months = normalized_lag_months(override.get("lagMonths", override.get("lagThresholdMonths", 12)))
        if lag_months != 12:
            notes.append(f"ручная lag-policy: ≤{lag_months} месяцев")
        if subgroup:
            notes.append(f"подгруппа: {subgroup}")
        if dashboard_note:
            notes.append(f"комментарий команды: {dashboard_note}")
        if scope_excluded:
            notes.append(f"исключено из текущего scope: {exclusion_reason}")
        if planner_deferred:
            notes.append(f"временно отложено Supervisor: {planner_deferred_reason or 'текущий target не закрывается безопасно в этом плане'}")
        if profile.is_published_widget and profile.has_published_artifact and kind == "dev":
            notes.append("project publishes loader/artifact; devDependency may be supply-chain relevant")
        if profile.is_shared_package:
            notes.append("shared/public-ish package; consider consumers and release pipeline")

        base_kwargs = dict(
            project=project.name,
            package_dir=str(project.path),
            name=name,
            kind=kind,
            requested_spec=spec,
            current_version=current,
            current_source=current_source,
            subgroup=subgroup,
            lag_threshold_months=lag_months,
            scope_excluded=scope_excluded,
            exclusion_reason=exclusion_reason,
            exclusion_source=exclusion_source if scope_excluded else "",
            planner_deferred=planner_deferred,
            planner_deferred_reason=planner_deferred_reason,
            planner_target_default=planner_target_default,
            planner_target_yellow=planner_target_yellow,
            planner_target_green=planner_target_green,
        )

        if is_non_registry_spec(spec):
            analysis = AnalysisInfo(metadata_available=False, non_registry=True, latest_version="internal/non-registry", current_vulns="unknown")
            group, reason = classify(name, kind, "unknown", overrides, analysis, profile)
            rows.append(DependencyRow(
                **base_kwargs,
                latest_version="internal/non-registry",
                current_vulns="unknown",
                min_no_critical="—", min_no_high="—", min_no_vuln="—",
                min_lag_12m="—", min_lag_9m="—", min_lag_6m="—", min_lag_3m="—",
                group=group, reason=reason,
                notes="; ".join(notes + ["non-registry spec; проверить внутренний registry/advisory"]),
            ))
            eprint(
                f"[info] {dependency_label}: done in {time.perf_counter() - dependency_started:.1f}s; "
                "source=non-registry"
            )
            continue

        eprint(
            f"[info] {dependency_label}: current={current} ({current_source}); "
            "loading registry metadata"
        )
        meta = client.fetch_npm_metadata(name)
        if not meta:
            analysis = AnalysisInfo(metadata_available=False, latest_version="registry unavailable", current_vulns="unknown")
            group, reason = classify(name, kind, "unknown", overrides, analysis, profile)
            rows.append(DependencyRow(
                **base_kwargs,
                latest_version="registry unavailable",
                current_vulns="unknown",
                min_no_critical="неизвестно", min_no_high="неизвестно", min_no_vuln="неизвестно",
                min_lag_12m="неизвестно", min_lag_9m="неизвестно", min_lag_6m="неизвестно", min_lag_3m="неизвестно",
                group=group, reason=reason,
                notes="; ".join(notes + ["package unavailable in configured npm registry"]),
            ))
            eprint(
                f"[info] {dependency_label}: done in {time.perf_counter() - dependency_started:.1f}s; "
                "registry metadata unavailable"
            )
            continue

        metadata_latest = latest_version(meta)
        candidates, cap_note = versions_from_current(meta, current, include_prerelease, max_candidates)
        if cap_note:
            notes.append(cap_note)
        structural_candidates, registry_candidate_notes = client.registry_structural_candidates(meta, candidates)
        if registry_candidate_notes:
            notes.append(
                "registry artifact policy excluded metadata-only/foreign versions: "
                + "; ".join(registry_candidate_notes[:6])
                + (f"; +{len(registry_candidate_notes) - 6} more" if len(registry_candidate_notes) > 6 else "")
            )
        candidates = structural_candidates
        if current not in candidates:
            # The currently installed version remains valid baseline evidence
            # even when Nexus no longer exposes its tarball. It must not be used
            # as proof that a *new* target can be installed.
            candidates = [current] + candidates

        latest = client.latest_installable_version(
            name,
            meta,
            include_prerelease=include_prerelease,
            current=current,
        )
        if latest != metadata_latest:
            notes.append(
                f"registry latest adjusted by tarball availability: metadata={metadata_latest}, installable={latest}"
            )

        # min_by_lag/min_by_vuln below already walk `candidates` from `current`
        # upward and return the first version satisfying both the policy and
        # this predicate -- so rejecting a version here for missing types
        # makes the *existing* minimum-satisfying-version search skip past it
        # to the next candidate automatically, with no separate retry loop
        # needed. `current` is exempt: an already-installed version's own
        # type-declaration state is a pre-existing condition, not something
        # this migration introduces.
        type_declaration_notes: Set[str] = set()

        def target_available(version: str) -> bool:
            if version == current:
                return True
            if not client.registry_version_is_installable(name, meta, version):
                return False
            types_issue = client.registry_version_type_declarations_ok(name, meta, version)
            if types_issue:
                if version not in type_declaration_notes:
                    type_declaration_notes.add(version)
                    notes.append(f"{version} skipped as a target: {types_issue}")
                return False
            entrypoint_issue = client.registry_version_runtime_entrypoint_ok(name, meta, version)
            if entrypoint_issue:
                if version not in type_declaration_notes:
                    type_declaration_notes.add(version)
                    notes.append(f"{version} skipped as a target: {entrypoint_issue}")
                return False
            return True

        eprint(
            f"[info] {dependency_label}: registry metadata ready; latest={latest}; "
            f"OSV candidates={len(candidates)}"
        )
        vulns = client.query_osv_versions(name, candidates, progress_label=dependency_label)
        current_summary = vuln_summary(vulns.get(current, []))
        min_nc = min_by_vuln(candidates, vulns, "no-critical", target_available)
        min_nh = min_by_vuln(candidates, vulns, "no-high", target_available)
        min_nv = min_by_vuln(candidates, vulns, "no-vuln", target_available)
        min_12 = min_by_lag(meta, candidates, 12, latest_override=latest, is_available=target_available)
        min_9 = min_by_lag(meta, candidates, 9, latest_override=latest, is_available=target_available)
        min_6 = min_by_lag(meta, candidates, 6, latest_override=latest, is_available=target_available)
        min_3 = min_by_lag(meta, candidates, 3, latest_override=latest, is_available=target_available)
        registry_artifacts: Dict[str, Dict[str, Any]] = {}
        for candidate_target in {latest, min_nc, min_nh, min_nv, min_12, min_9, min_6, min_3}:
            if not target_is_action(candidate_target):
                continue
            if candidate_target == current:
                registry_artifacts[candidate_target] = {
                    "package": name,
                    "version": candidate_target,
                    "registry": client.registry,
                    "status": "current-installed",
                    "tarballUrl": client.registry_tarball_url(meta, candidate_target),
                    "error": "",
                }
            else:
                registry_artifacts[candidate_target] = client.registry_version_artifact(
                    name, meta, candidate_target
                )
        analysis = AnalysisInfo(
            metadata_available=True,
            non_registry=False,
            latest_version=latest,
            current_vulns=current_summary,
            min_no_critical=min_nc,
            min_no_high=min_nh,
            min_no_vuln=min_nv,
            min_lag_12m=min_12,
            min_lag_9m=min_9,
            min_lag_6m=min_6,
            min_lag_3m=min_3,
        )
        group, reason = classify(name, kind, current_summary, overrides, analysis, profile)

        if current_summary not in ("0", "unknown", "неизвестно", "—"):
            notes.append(f"текущий dependency risk: {current_summary}")
        rows.append(DependencyRow(
            **base_kwargs,
            latest_version=latest,
            current_vulns=current_summary,
            min_no_critical=min_nc,
            min_no_high=min_nh,
            min_no_vuln=min_nv,
            min_lag_12m=min_12,
            min_lag_9m=min_9,
            min_lag_6m=min_6,
            min_lag_3m=min_3,
            group=group,
            reason=reason,
            notes="; ".join(notes),
            registry_artifacts=registry_artifacts,
        ))
        eprint(
            f"[info] {dependency_label}: done in {time.perf_counter() - dependency_started:.1f}s; "
            f"current={current}; latest={latest}; vulnerabilities={current_summary}; group={group}"
        )
    eprint(
        f"[info] {label}: dependency analysis completed; rows={len(rows)}; "
        f"elapsed={time.perf_counter() - project_started:.1f}s"
    )
    return rows


def slugify(value: str) -> str:
    s = re.sub(r"[^0-9a-zA-Zа-яА-Я]+", "-", value.strip().lower(), flags=re.IGNORECASE).strip("-")
    return s or "project"


def parse_vuln_counts(summary: str) -> Dict[str, int]:
    result = {"C": 0, "H": 0, "M": 0, "L": 0, "U": 0}
    for code, value in re.findall(r"(C|H|M|L|U):(\d+)", summary or ""):
        result[code] += int(value)
    return result


def vuln_counts_text(summary: str) -> str:
    counts = parse_vuln_counts(summary)
    parts = [f"{k}:{v}" for k, v in counts.items() if v]
    return ", ".join(parts) if parts else "0"


def vulnerability_work_note(row: DependencyRow) -> str:
    """Human note for reports/prompts: what security risk this work removes or leaves."""
    counts = parse_vuln_counts(row.current_vulns)
    has_any = any(counts.values())
    has_ch = counts.get("C", 0) or counts.get("H", 0)
    target = row.target_default
    target_action = target_is_action(target)

    if row.current_vulns in ("unknown", "неизвестно"):
        return "vuln неизвестны: нужен доступ к registry; ручной Nexus/npm audit — отдельная пользовательская сверка"
    if row.current_vulns == "—":
        return "vuln не оценивались для этого источника; проверить корпоративный audit"

    if not has_any:
        if target_action:
            return "известных vuln по OSV нет; работа снижает lag/API/platform риск"
        return "известных vuln по OSV нет; security-риск не является причиной работы"

    current = vuln_counts_text(row.current_vulns)
    if not target_action:
        return f"обновление не запланировано: текущие vuln остаются ({current})"

    if has_ch:
        if has_safe_target(row.min_no_high) and target_version_max([target, row.min_no_high]) == target:
            return f"цель — снять Critical/High ({current}); после MR подтвердить audit diff"
        if has_safe_target(row.min_no_critical) and target_version_max([target, row.min_no_critical]) == target:
            return f"цель — снять Critical ({current}); High/прочие остатки явно проверить audit diff"
        return f"есть C/H ({current}); target выбран по roadmap, остаточный риск проверить audit diff"

    if has_safe_target(row.min_no_vuln) and target_version_max([target, row.min_no_vuln]) == target:
        return f"цель — снять все известные OSV vuln ({current})"
    return f"текущие vuln: {current}; target снижает часть риска или lag, остаток проверить audit diff"


def aggregate_vuln_note(rows: List[DependencyRow]) -> str:
    totals = {"C": 0, "H": 0, "M": 0, "L": 0, "U": 0}
    risky: List[str] = []
    for row in rows:
        counts = parse_vuln_counts(row.current_vulns)
        for k, v in counts.items():
            totals[k] += v
        if row.current_vulns not in ("0", "unknown", "неизвестно", "—"):
            risky.append(f"{row.name}: {row.current_vulns}")
    total_str = ", ".join(f"{k}:{v}" for k, v in totals.items() if v)
    if not total_str:
        return "нет известных C/H по OSV; ручной Nexus/npm audit доступен как независимая сверка"
    sample = "; ".join(risky[:5])
    tail = "…" if len(risky) > 5 else ""
    return f"остаточные/текущие vuln: {total_str}; {sample}{tail}"


def suggestion_family(row: DependencyRow) -> str:
    name = row.name
    if name == "recompose":
        return "recompose-small"
    if name in {"react-router-dom", "path-to-regexp"}:
        return "router-runtime"
    if name == "@microsoft/signalr":
        return "signalr-runtime"
    if name in {"react-beautiful-dnd", "@hello-pangea/dnd", "react-dnd", "react-dnd-html5-backend", "dnd-core"} or "dnd" in name.lower():
        return "dnd-runtime"
    if name in {"pdfjs-dist", "react-dropzone", "file-saver", "content-disposition"}:
        return "file-runtime"
    if name in {"oidc-client", "oidc-client-ts", "credentials-from-vault"} or "credential" in name.lower() or "oidc" in name.lower():
        return "auth-oidc-vault"
    if name.startswith("@storybook/") or name == "storybook":
        return "storybook"
    if name in LOCAL_DEV_ONLY_PACKAGES or name in {"jest", "ts-jest", "@testing-library/react", "@testing-library/jest-dom"}:
        return "dev-test-local"
    if name in FLOW_TOOLCHAIN_PACKAGES or name.startswith("eslint") or name.startswith("stylelint") or name.startswith("@typescript-eslint/") or name in {"typescript-eslint", "typescript", "prettier"}:
        return "lint-types-style"
    if name in BUILD_TOOLCHAIN_PACKAGES or name.startswith("@vitejs/") or name.startswith("vite-plugin") or name.endswith("-loader") or name in {"rollup", "esbuild", "sass", "less"}:
        return "build-toolchain"
    if name in REACT_UI_PACKAGES:
        return "ui-react"
    if is_internal_package(name):
        return "internal-packages"
    if row.kind in {"runtime", "optional", "peer"}:
        return "safe-deps"
    if row.kind == "dev":
        return "quarterly-dev"
    return "dependency-misc"

SUGGESTION_TEMPLATES: Dict[str, Dict[str, str]] = {
    "recompose-small": {
        "title": "Удаление recompose в точечных местах",
        "branch": "g1-remove-recompose-small",
        "rationale": "Срочная маленькая миграция: пакет лучше убрать/заменить, usage выглядит локальным; хорошо подходит для первой ветки.",
        "checks": "typecheck/build + smoke экранов, где были HOC/compose.",
    },
        "safe-deps": {
        "title": "Important safe dependency batch",
        "branch": "g2-important-safe-deps",
        "rationale": "Несложные runtime/API/CI и важные простые обновления: заметный lag/security hygiene/deprecated replacement с малым diff и понятным smoke.",
        "checks": "install + OSV/outdated diff + lint/test/build; manual audit only if requested.",
    },
    "router-runtime": {
        "title": "Runtime router update",
        "branch": "g3-router-runtime",
        "rationale": "Routing влияет на пользовательские сценарии; если не доказано, что это простой diff, вести как group 3 с отдельным smoke.",
        "checks": "smoke навигации, deep links, redirects, permissions, browser back/forward.",
    },
    "router-heavy": {
        "title": "Legacy router migration",
        "branch": "g4-router-legacy-migration",
        "rationale": "Legacy routing/multi-page/React Router v5 — отдельная тяжёлая миграция, не общий deps MR.",
        "checks": "карта роутов, smoke всех entrypoints, redirects, query params, guards.",
    },
    "signalr-runtime": {
        "title": "SignalR runtime contract update",
        "branch": "g3-signalr-runtime",
        "rationale": "Realtime/API-контракты могут ломаться не на компиляции, а в reconnect/auth/transport сценариях.",
        "checks": "connect/reconnect, auth expiration, server events, fallback transports, error handling.",
    },
    "dnd-runtime": {
        "title": "DnD compatible replacement/update",
        "branch": "g2-dnd-compatible-replacement",
        "rationale": "Если переход на @hello-pangea/dnd сводится к import-only/совместимому API — это важное и простое обновление группы 2; при поведенческих ломках поднять в group 3 override.",
        "checks": "smoke drag/drop зон, reorder, empty state, touch/mouse, визуальный diff.",
    },
    "file-runtime": {
        "title": "File/PDF/upload runtime update",
        "branch": "g3-file-pdf-upload",
        "rationale": "PDF/upload/download зоны требуют отдельного product-smoke и тестовых файлов.",
        "checks": "upload/download/PDF preview, large file, invalid file, browser console.",
    },
    "ui-react": {
        "title": "React/UI migration",
        "branch": "g4-ui-react",
        "rationale": "UI-kit/React/icons/logos лучше обновлять согласованно, с визуальным регрессом и не смешивать с build/lint.",
        "checks": "build + visual/smoke ключевых страниц, формы, модалки, тултипы, таблицы.",
    },
    "build-toolchain": {
        "title": "Build toolchain migration",
        "branch": "g4-build-toolchain",
        "rationale": "Vite/Webpack/Rollup/loaders/plugins меняют сборку и артефакты; нужна отдельная ветка.",
        "checks": "clean install, build, preview/smoke, bundle artifacts, env vars, asset paths, CI job.",
    },
    "lint-types-style": {
        "title": "Lint/TypeScript/Stylelint migration",
        "branch": "g4-lint-types-style",
        "rationale": "Линтеры и TS часто тянут конфиг-миграцию и массовые правки; лучше отдельный batch.",
        "checks": "lint/stylelint/typecheck, форматтеры, changed rules summary, no autofix surprise.",
    },
    "auth-oidc-vault": {
        "title": "Auth/OIDC/Vault blocked track",
        "branch": "g4-auth-oidc-vault",
        "rationale": "Auth/credentials зависят от backend/infra/владельцев и требуют отдельного risk register/согласования.",
        "checks": "login/logout/refresh, callback route, token renewal, стендовая проверка с backend.",
    },
    "widget-framework-runtime": {
        "title": "Published widget/framework migration",
        "branch": "g4-widget-framework-runtime",
        "rationale": "Для виджета devDependencies могут попадать в публикуемый loader/build; нельзя считать их локальным шумом.",
        "checks": "build-loader, npm pack/exports, consumer smoke, artifact diff.",
    },
    "internal-packages": {
        "title": "Internal packages update track",
        "branch": "g2-g4-internal-packages",
        "rationale": "Корпоративные пакеты анализируются как обычные, но требуют registry/advisory/audit diff и иногда владельца upstream.",
        "checks": "OSV/outdated diff, changelog/internal owner, smoke зоны влияния; manual Nexus audit only if requested.",
    },
    "storybook": {
        "title": "Storybook/dev preview track",
        "branch": "g5-storybook-quarterly",
        "rationale": "Если Storybook не используется/не опубликован — квартальный DEV batch; если опубликован — поднять в group 4.",
        "checks": "storybook build/start, published/static exposure check, визуальные stories по необходимости.",
    },
    "dev-test-local": {
        "title": "Local test/dev tooling batch",
        "branch": "g5-local-test-dev",
        "rationale": "Vitest/Jest/jsdom/mkcert можно отложить, если они реально локальные и не участвуют в CI/release.",
        "checks": "проверить scripts/CI; если запускается в CI — поднять в group 3/4.",
    },
    "quarterly-dev": {
        "title": "Quarterly DEV cleanup",
        "branch": "g5-quarterly-dev",
        "rationale": "Низкоприоритетный DEV/local хвост, который полезно закрывать отдельным квартальным batch.",
        "checks": "install + lint/test/build only where relevant; зафиксировать остаточные vuln.",
    },
}


def suggestion_template(family: str) -> Dict[str, str]:
    return SUGGESTION_TEMPLATES.get(family, {
        "title": f"Dependency batch: {family}",
        "branch": family,
        "rationale": "Скрипт нашёл зависимости с похожим типом риска/работы; проверить вручную перед MR.",
        "checks": "install + OSV/outdated diff + профильные smoke checks; manual audit only if requested.",
    })


def group_for_suggestion(rows: List[DependencyRow]) -> int:
    """Presentation-only group label for a suggestion; supports arbitrary ids."""
    groups = sorted({int(r.group) for r in rows})
    return groups[0] if groups else 0


def build_project_suggestions(project: str, rows: List[DependencyRow]) -> List[WorkSuggestion]:
    buckets: Dict[str, List[DependencyRow]] = defaultdict(list)
    for row in rows:
        buckets[suggestion_family(row)].append(row)

    suggestions: List[WorkSuggestion] = []
    for family, items in sorted(buckets.items(), key=lambda kv: (group_for_suggestion(kv[1]), kv[0])):
        if not items:
            continue
        template = suggestion_template(family)
        group = group_for_suggestion(items)
        packages = sorted({r.name for r in items})
        branch_prefix = template.get("branch", family)
        branch = f"{branch_prefix}-{slugify(project)}"
        confidence = "high" if family in SUGGESTION_TEMPLATES else "low"
        # Avoid pretending that giant catch-all misc buckets are strong recommendations.
        if family.endswith("-misc") and len(packages) < 3:
            confidence = "low"
        suggestions.append(WorkSuggestion(
            project=project,
            group=group,
            family=family,
            title=template.get("title", family),
            suggested_branch=branch,
            packages=packages,
            rationale=template.get("rationale", "Похожие зависимости по типу риска/работы."),
            checks=template.get("checks", "install + OSV/outdated diff + smoke; manual audit only if requested."),
            risk_note=aggregate_vuln_note(items),
            confidence=confidence,
        ))
    return suggestions


def build_global_suggestions(project_suggestions: Dict[str, List[WorkSuggestion]]) -> List[WorkSuggestion]:
    buckets: Dict[str, List[WorkSuggestion]] = defaultdict(list)
    for suggestions in project_suggestions.values():
        for item in suggestions:
            if item.family in {"safe-deps", "quarterly-dev", "group-2-misc", "group-5-misc"}:
                continue
            buckets[item.family].append(item)

    result: List[WorkSuggestion] = []
    for family, items in sorted(buckets.items(), key=lambda kv: (group_for_suggestion_rows_from_suggestions(kv[1]), kv[0])):
        if len(items) < 2:
            continue
        template = suggestion_template(family)
        projects = sorted({x.project for x in items})
        packages = sorted({pkg for x in items for pkg in x.packages})
        group = group_for_suggestion_rows_from_suggestions(items)
        result.append(WorkSuggestion(
            project="несколько проектов",
            group=group,
            family=family,
            title=f"Cross-project: {template.get('title', family)}",
            suggested_branch=f"{template.get('branch', family)}-cross-project-plan",
            packages=packages,
            rationale=f"Одинаковый тип работы найден в проектах: {', '.join(projects)}. Можно сделать общий план и затем MR per repo.",
            checks=template.get("checks", "install + OSV/outdated diff + профильные smoke checks; manual audit only if requested."),
            risk_note="; ".join(f"{x.project}: {x.risk_note}" for x in items[:5]) + ("…" if len(items) > 5 else ""),
            confidence="medium",
        ))
    return result


def group_for_suggestion_rows_from_suggestions(items: List[WorkSuggestion]) -> int:
    groups = {i.group for i in items}
    if 4 in groups:
        return 4
    if 3 in groups:
        return 3
    if 1 in groups:
        return 1
    if 2 in groups:
        return 2
    return 5


def build_all_suggestions(rows_by_project: Dict[str, List[DependencyRow]]) -> Tuple[Dict[str, List[WorkSuggestion]], List[WorkSuggestion]]:
    by_project = {project: build_project_suggestions(project, rows) for project, rows in rows_by_project.items()}
    global_suggestions = build_global_suggestions(by_project)
    return by_project, global_suggestions



STATUS_NAMES = {"red": "Красный", "yellow": "Жёлтый", "green": "Зелёный"}
STATUS_RANK = {"red": 0, "yellow": 1, "green": 2}
TARGET_RANK = {"yellow": 1, "green": 2}
NO_ACTION = "—"
LAG_TARGET_BUFFER_MONTHS = 3
YELLOW_HEALTH_RATIO = (8, 10)
# The executable plan keeps a five-point reserve above the release gate.
YELLOW_PLANNING_RATIO = (17, 20)


def required_ratio_count(total: int, ratio: Tuple[int, int]) -> int:
    numerator, denominator = ratio
    return -(-total * numerator // denominator) if total else 0


def semver_value(value: str) -> Optional[Version]:
    if not value or str(value).strip() == NO_ACTION:
        return None
    m = re.search(r"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", str(value))
    if not m:
        return None
    return safe_version(m.group(1))


def target_is_action(value: str) -> bool:
    """An update action must contain a concrete semver target.

    Older code treated blocker text such as ``нет safe target`` as an action.
    The dashboard then exported it, while the post-merge validator silently
    discarded it because it was not semver.  Requiring a concrete version keeps
    plan, prompt and validator scopes identical.
    """
    return semver_value(value) is not None


def value_is_version(value: str) -> bool:
    return semver_value(value) is not None


def current_meets_target(current: str, target: str) -> bool:
    cv = semver_value(current)
    tv = semver_value(target)
    if cv and tv:
        return cv >= tv
    return False


def lag_compliance_target_for_row(row: DependencyRow, months: Optional[int] = None) -> str:
    """Return the *live* boundary used to decide whether a row is healthy now.

    Baselines intentionally freeze remediation targets so the +3 month buffer is
    applied once per migration cycle. They must not freeze the health criterion
    itself: the dashboard's red/yellow/green status is a statement about the
    current registry window. Keeping those two concepts separate prevents an old
    baseline from making a dependency look stale after it already satisfies the
    live lag policy.
    """
    effective = normalized_lag_months(months if months is not None else row.lag_threshold_months)
    return {
        3: row.min_lag_3m,
        6: row.min_lag_6m,
        9: row.min_lag_9m,
        12: row.min_lag_12m,
    }[effective]


def lag_target_for_row(row: DependencyRow, months: Optional[int] = None) -> str:
    """Return the baseline-anchored planning boundary.

    Kept as the public planning helper for backward compatibility. Health and
    compliance checks use :func:`lag_compliance_target_for_row`; remediation
    targets continue to use this frozen baseline so repeated verification does
    not move the goalposts or apply the buffer twice.
    """
    effective = normalized_lag_months(months if months is not None else row.lag_threshold_months)
    return {
        3: row.planning_min_lag_3m or row.min_lag_3m,
        6: row.planning_min_lag_6m or row.min_lag_6m,
        9: row.planning_min_lag_9m or row.min_lag_9m,
        12: row.planning_min_lag_12m or row.min_lag_12m,
    }[effective]


def lag_update_target_for_row(row: DependencyRow, months: Optional[int] = None) -> str:
    """Return the one-time buffered target for a lag-policy violation.

    The compliance boundary and the update target are intentionally separate.
    A 12-month policy is checked against the current live 12-month boundary,
    while remediation targets the captured baseline's 9-month boundary. Because
    only the remediation target is anchored, ordinary regeneration reflects
    current health without applying the three-month buffer twice.
    """
    effective = normalized_lag_months(months if months is not None else row.lag_threshold_months)
    buffered = max(3, effective - LAG_TARGET_BUFFER_MONTHS)
    planned = lag_target_for_row(row, buffered)
    # The buffered target comes from the frozen baseline, the compliance
    # boundary from the live registry window. Normally the buffered boundary
    # is the newer of the two, so this changes nothing. But once a baseline
    # is old enough, the live boundary overtakes it -- and planning the
    # baseline value would send the agent to install a version that still
    # fails the policy, so the work lands and the percentage does not move.
    # Never plan an update that provably leaves the row non-compliant.
    compliance = lag_compliance_target_for_row(row, effective)
    if not has_safe_target(compliance):
        return planned
    return target_version_max([planned, compliance])


def _baseline_row_key(row: Any) -> Tuple[str, str]:
    if isinstance(row, DependencyRow):
        return row.kind, row.name
    if isinstance(row, dict):
        return str(row.get("kind") or ""), str(row.get("name") or "")
    return "", ""


def apply_lag_planning_baseline(
    rows: List[DependencyRow],
    baseline: Optional[Dict[str, Any]],
) -> None:
    """Anchor lag boundaries to the captured baseline for this migration cycle."""
    baseline_rows = baseline.get("rows") if isinstance(baseline, dict) else None
    indexed = {
        _baseline_row_key(item): item
        for item in (baseline_rows or [])
        if isinstance(item, dict) and all(_baseline_row_key(item))
    }
    for row in rows:
        source = indexed.get(_baseline_row_key(row))
        for months in (12, 9, 6, 3):
            field = f"planning_min_lag_{months}m"
            live_field = f"min_lag_{months}m"
            if source:
                value = source.get(field)
                if value in (None, ""):
                    # Compatibility with baselines captured before the explicit
                    # planning fields were introduced.
                    value = source.get(live_field)
                setattr(row, field, str(value) if value not in (None, "") else getattr(row, live_field))
            else:
                setattr(row, field, getattr(row, live_field))
        row.lag_planning_source = "baseline" if source else "live"


def dependency_is_lag_ok(row: DependencyRow) -> bool:
    if row.scope_excluded:
        return True
    target = lag_compliance_target_for_row(row)
    if not has_safe_target(target):
        return False
    return current_meets_target(row.current_version, target)


def dependency_needs_lag_update(row: DependencyRow) -> bool:
    if row.scope_excluded:
        return False
    target = lag_compliance_target_for_row(row)
    if not has_safe_target(target):
        return False
    return not current_meets_target(row.current_version, target)


def dependency_is_lag_ok_12m(row: DependencyRow) -> bool:
    # Backward-compatible name; now respects per-package policy.
    return dependency_is_lag_ok(row)


def dependency_needs_lag_update_12m(row: DependencyRow) -> bool:
    return dependency_needs_lag_update(row)


def dependency_needs_lag_update_9m(row: DependencyRow) -> bool:
    target = lag_compliance_target_for_row(row, 9)
    return has_safe_target(target) and not current_meets_target(row.current_version, target)


def dependency_needs_lag_update_6m(row: DependencyRow) -> bool:
    target = lag_compliance_target_for_row(row, 6)
    return has_safe_target(target) and not current_meets_target(row.current_version, target)


def dependency_needs_lag_update_3m(row: DependencyRow) -> bool:
    target = lag_compliance_target_for_row(row, 3)
    return has_safe_target(target) and not current_meets_target(row.current_version, target)


def dependency_is_lag_ok_after_planned_target(row: DependencyRow, mode: str) -> bool:
    """Return whether current + already planned work satisfies the row policy.

    Yellow planning is incremental: security work and explicit package policies
    may already move a dependency far enough before the 80% greedy pass runs.
    Counting the projected state avoids selecting redundant extra updates.
    """
    if row.scope_excluded:
        # Excluded rows are outside the health universe. They must never be
        # counted as projected successes when a planner compares its result
        # with ProjectHealth.total, which already excludes them.
        return False
    target = lag_compliance_target_for_row(row)
    if not has_safe_target(target):
        return False
    if current_meets_target(row.current_version, target):
        return True
    planned = getattr(row, f"target_{mode}", NO_ACTION)
    return target_is_action(planned) and current_meets_target(planned, target)


def target_version_max(values: List[str]) -> str:
    valid = [(semver_value(v), v) for v in values if target_is_action(v)]
    versioned = [(sv, v) for sv, v in valid if sv is not None]
    if versioned:
        return max(versioned, key=lambda x: x[0])[1]
    non_empty = [v for _, v in valid if v]
    return non_empty[0] if non_empty else NO_ACTION


def target_reason_join(values: List[str]) -> str:
    cleaned = []
    for v in values:
        v = (v or "").strip()
        if not v or v == NO_ACTION:
            continue
        # Callers often pass an already accumulated reason string as the first
        # value and one new atomic reason as the second. Comparing only whole
        # strings made the same AUTO_PEER_CLOSURE/SUPERVISOR reason grow on
        # every deterministic replan. Treat an exact semicolon-delimited item
        # already present in an accumulated value as a duplicate as well.
        already_present = any(
            existing == v
            or existing.startswith(v + "; ")
            or existing.endswith("; " + v)
            or ("; " + v + "; ") in existing
            for existing in cleaned
        )
        if not already_present:
            cleaned.append(v)
    return "; ".join(cleaned) if cleaned else NO_ACTION


BREAKING_RISK_PACKAGE_HINTS = {
    "react",
    "react-dom",
    "vite",
    "webpack",
    "stylelint",
    "eslint",
    "typescript",
    "typescript-eslint",
    "@vitejs/plugin-react",
    "@vitejs/plugin-react-swc",
    "@typescript-eslint/parser",
    "@typescript-eslint/eslint-plugin",
    "react-router-dom",
    "@microsoft/signalr",
    "oidc-client",
}


def semver_delta(current: str, target: str) -> Tuple[int, int, int]:
    cv = semver_value(current)
    tv = semver_value(target)
    if not cv or not tv:
        return (0, 0, 0)
    return (
        max(0, int(tv.major) - int(cv.major)),
        max(0, int(tv.minor) - int(cv.minor)) if int(tv.major) == int(cv.major) else int(tv.minor),
        max(0, int(tv.patch) - int(cv.patch)) if int(tv.major) == int(cv.major) and int(tv.minor) == int(cv.minor) else int(tv.patch),
    )


def row_security_priority(row: DependencyRow) -> Tuple[int, int]:
    """Lower bucket means the row is more security-important."""
    counts = parse_vuln_counts(row.current_vulns)
    ch = counts.get("C", 0) + counts.get("H", 0)
    if counts.get("C", 0):
        return (0, -(counts.get("C", 0) * 1000 + counts.get("H", 0) * 250 + counts.get("M", 0) * 30 + counts.get("L", 0) * 5))
    if counts.get("H", 0):
        return (1, -(counts.get("H", 0) * 250 + counts.get("M", 0) * 30 + counts.get("L", 0) * 5))
    if counts.get("M", 0):
        return (2, -(counts.get("M", 0) * 30 + counts.get("L", 0) * 5))
    if counts.get("L", 0):
        return (3, -counts.get("L", 0) * 5)
    return (4, 0)


def row_update_effort_score(row: DependencyRow, target: Optional[str] = None) -> int:
    """Approximate time/risk score. Lower means safer/cheaper.

    This is intentionally conservative: platform/UI/build majors and unclear
    breaking-change windows are much more expensive than local DEV/test lag.
    C/H priority is handled separately by row_security_priority().
    """
    target = target or row.target_default or lag_update_target_for_row(row, 12)
    major, minor, patch = semver_delta(row.current_version, target)
    counts = parse_vuln_counts(row.current_vulns)

    # Effort is a policy/scoring input and therefore MUST NOT depend on the
    # display group.  Derive it from package semantics instead; changing group
    # ids/names/boundaries must not change the executable Baseline.
    name = row.name
    if name in URGENT_SIMPLE_POLICY_PACKAGES:
        score = 6
    elif name in SIMPLE_RUNTIME_POLICY_PACKAGES:
        score = 16
    elif (
        name in PLATFORM_MIGRATION_POLICY_PACKAGES
        or name in REACT_UI_PACKAGES
        or name in BUILD_TOOLCHAIN_PACKAGES
        or name in RISK_REGISTER_PACKAGES
        or name.startswith("@storybook/")
    ):
        score = 95
    elif name in COMPLEX_RUNTIME_POLICY_PACKAGES or row.kind in {"runtime", "optional", "peer"}:
        score = 48
    else:
        score = 22

    if row.kind == "runtime":
        score += 14
    elif row.kind == "dev":
        score += 3

    score += major * 70 + minor * 6 + min(patch, 10)

    name = row.name
    if major > 0:
        if name in {"react", "react-dom"}:
            score += 430
        elif name in REACT_UI_PACKAGES:
            score += 340
        elif name in {"vite", "webpack"}:
            score += 300
        elif name in {"stylelint", "eslint", "typescript", "typescript-eslint"} or name.startswith("@typescript-eslint/"):
            score += 230
        elif name in {"react-router-dom", "@microsoft/signalr", "oidc-client"}:
            score += 150
        elif row.kind == "dev" and name in (LOCAL_DEV_ONLY_PACKAGES | set(DEFERABLE_DEV_POLICY_PACKAGES)):
            score += 8
        else:
            score += 45

    # Platform/build work is an expensive filler candidate for Yellow even if
    # somebody changes its display group. Security priority is handled above.
    platform_or_build = (
        name in PLATFORM_MIGRATION_POLICY_PACKAGES
        or name in REACT_UI_PACKAGES
        or name in BUILD_TOOLCHAIN_PACKAGES
        or name in RISK_REGISTER_PACKAGES
        or is_build_or_platform_package(name)
    )
    if platform_or_build and not (counts.get("C", 0) or counts.get("H", 0)):
        score += 130

    if row.name in BREAKING_RISK_PACKAGE_HINTS:
        score += 35

    if is_unknown_analysis(row.latest_version) or "неизвест" in (row.min_lag_12m or "").lower():
        score += 60

    return score


def row_breaking_risk_note(row: DependencyRow, target: Optional[str] = None) -> str:
    target = target or row.target_default
    major, minor, patch = semver_delta(row.current_version, target)
    notes: List[str] = []
    if major > 0:
        notes.append(f"major +{major}: обязательно проверить changelog/release notes и breaking changes")
    elif minor > 0 and (row.kind in {"runtime", "optional", "peer"} or is_build_or_platform_package(row.name) or row.name in BREAKING_RISK_PACKAGE_HINTS):
        notes.append("minor/runtime/platform: проверить changelog на behavioral changes")
    if row.name in BREAKING_RISK_PACKAGE_HINTS or is_build_or_platform_package(row.name):
        notes.append("повышенный API/CI/platform риск")
    if row.kind == "dev":
        notes.append("DEV-кандидат: проверить, что не участвует в CI/build/release")
    return "; ".join(dict.fromkeys(notes)) if notes else "низкий semver/API риск по эвристике"


def row_update_simplicity_score(row: DependencyRow, target: Optional[str] = None) -> Tuple[int, int, int, int, str]:
    security_bucket, security_weight = row_security_priority(row)
    effort = row_update_effort_score(row, target)
    major, minor, patch = semver_delta(row.current_version, target or lag_update_target_for_row(row, 12))
    return (security_bucket, effort, security_weight, major * 10000 + minor * 100 + patch, row.name.lower())


def greedy_target_reason(base: str, row: DependencyRow, target: str) -> str:
    return (
        f"{base}; выбрано maximal-greedy-safe по C/H→effort score={row_update_effort_score(row, target)}; "
        f"{row_breaking_risk_note(row, target)}"
    )


def baseline_removed_dependency_count(
    rows: List[DependencyRow],
    baseline: Optional[Dict[str, Any]],
) -> int:
    """Count baseline dependencies removed from the current project.

    The health universe is anchored to the union of baseline and current direct
    dependencies. A removed baseline dependency counts as closed, while a newly
    added dependency still participates in health.
    """
    if not baseline:
        return 0
    baseline_dependencies = baseline.get("directDependencies")
    if not isinstance(baseline_dependencies, dict):
        return 0
    current_keys = {
        f"{section_for_dependency_kind(row.kind)}:{row.name}"
        for row in rows
    }
    return len(set(baseline_dependencies) - current_keys)


def dependency_has_lag_policy_target(row: DependencyRow) -> bool:
    return not row.scope_excluded and has_safe_target(lag_compliance_target_for_row(row))


def compute_project_health(
    rows: List[DependencyRow],
    project: str,
    baseline: Optional[Dict[str, Any]] = None,
) -> ProjectHealth:
    active_rows = [row for row in rows if not row.scope_excluded]
    excluded_rows = [row for row in rows if row.scope_excluded]
    lag_known_rows = [row for row in active_rows if dependency_has_lag_policy_target(row)]
    lag_unknown = len(active_rows) - len(lag_known_rows)
    removed_closed = baseline_removed_dependency_count(rows, baseline)
    total = len(lag_known_rows) + removed_closed
    lag_ok = sum(1 for r in lag_known_rows if dependency_is_lag_ok(r)) + removed_closed
    lag_bad = total - lag_ok
    lag_pct = (lag_ok / total * 100.0) if total else 100.0
    # -(-a // b) is integer ceil: the smallest lag_ok that still satisfies
    # lag_ok / total >= 0.80 without float rounding surprises at the boundary.
    yellow_required = required_ratio_count(total, YELLOW_HEALTH_RATIO)
    yellow_plan_required = required_ratio_count(total, YELLOW_PLANNING_RATIO)
    lag_needed_for_yellow = max(0, yellow_required - lag_ok)
    yellow_projected_lag_ok = (
        sum(1 for row in lag_known_rows if dependency_is_lag_ok_after_planned_target(row, "yellow"))
        + removed_closed
    )
    yellow_projected_lag_pct = (yellow_projected_lag_ok / total * 100.0) if total else 100.0
    yellow_plan_shortfall = max(0, yellow_plan_required - yellow_projected_lag_ok)
    lag_blockers = [
        {
            "package": row.name,
            "kind": row.kind,
            "group": row.group,
            "current": row.current_version,
            "required": lag_compliance_target_for_row(row),
            "lagPolicyMonths": row.lag_threshold_months,
            # All three modes are emitted because the consumer decides which
            # goal is being pursued. Reporting only the default-mode target
            # would mislabel a row as "the plan cannot close this" whenever the
            # yellow/green planner picked a different target than default.
            "plannedTarget": row.target_default if target_is_action(row.target_default) else "",
            "plannedTargetYellow": row.target_yellow if target_is_action(row.target_yellow) else "",
            "plannedTargetGreen": row.target_green if target_is_action(row.target_green) else "",
            "note": (row.compatibility_note or row.target_default_reason or "").strip(),
        }
        for row in lag_known_rows
        if not dependency_is_lag_ok(row)
    ]
    totals = {"C": 0, "H": 0, "M": 0, "L": 0, "U": 0}
    for r in active_rows:
        counts = parse_vuln_counts(r.current_vulns)
        for k in totals:
            totals[k] += counts.get(k, 0)

    critical = totals["C"]
    high = totals["H"]
    moderate = totals["M"]
    low = totals["L"]
    unknown = totals["U"]

    if critical > 0:
        status = "red"
        reason = f"есть Critical: {critical}"
        if excluded_rows:
            reason += f"; полностью исключено из расчёта: {len(excluded_rows)}"
    elif total == 0:
        status = "yellow" if lag_unknown else "green"
        reason = f"lag-policy target неизвестен для {lag_unknown} зависимостей" if lag_unknown else "нет зависимостей в активном scope"
    elif lag_pct < 80.0:
        status = "red"
        reason = f"только {lag_pct:.1f}% библиотек соблюдают свою lag-policy (<80%)"
    elif lag_bad == 0 and lag_unknown == 0 and critical == 0 and high == 0 and (moderate + low) <= 20:
        status = "green"
        reason = "0 нарушений lag-policy, 0 C/H, Low+Moderate ≤20"
    else:
        status = "yellow"
        parts = [f"{lag_pct:.1f}% библиотек соблюдают lag-policy", "0 Critical"]
        if lag_unknown:
            parts.append(f"lag-policy target неизвестен: {lag_unknown}")
        if high:
            parts.append(f"High остаются: {high}")
        if moderate or low:
            parts.append(f"M/L: {moderate + low}")
        status = "yellow"
        reason = "; ".join(parts)

    return ProjectHealth(
        project=project,
        status=status,
        status_rank=STATUS_RANK[status],
        total=total,
        lag_ok_12m=lag_ok,
        lag_bad_12m=lag_bad,
        lag_ok_pct=lag_pct,
        critical=critical,
        high=high,
        moderate=moderate,
        low=low,
        unknown=unknown,
        reason=reason,
        lag_policy_summary=", ".join(f"≤{months}м: {sum(1 for r in active_rows if r.lag_threshold_months == months)}" for months in (3, 6, 9, 12) if any(r.lag_threshold_months == months for r in active_rows)) or "default 12m",
        excluded=len(excluded_rows),
        lag_unknown=lag_unknown,
        removed=removed_closed,
        lag_blockers=lag_blockers,
        lag_needed_for_yellow=lag_needed_for_yellow,
        yellow_plan_required=yellow_plan_required,
        yellow_projected_lag_ok=yellow_projected_lag_ok,
        yellow_projected_lag_pct=yellow_projected_lag_pct,
        yellow_plan_shortfall=yellow_plan_shortfall,
    )


def next_target_for_status(status: str) -> str:
    if status == "red":
        return "yellow"
    if status == "yellow":
        return "green"
    return "none"


def set_row_target(row: DependencyRow, mode: str, target_values: List[str], reasons: List[str]) -> None:
    value = target_version_max(target_values)
    reason = target_reason_join(reasons)
    if mode == "yellow":
        row.target_yellow = value
        row.target_yellow_reason = reason
    elif mode == "green":
        row.target_green = value
        row.target_green_reason = reason
    elif mode == "default":
        row.target_default = value
        row.target_default_reason = reason


def merge_row_target(
    row: DependencyRow,
    mode: str,
    target: str,
    reason: str,
    component: str = "non-lag",
) -> None:
    old_value = getattr(row, f"target_{mode}")
    old_reason = getattr(row, f"target_{mode}_reason")
    if not target_is_action(target):
        # Preserve the blocker explanation, but do not turn it into an update.
        setattr(row, f"target_{mode}_reason", target_reason_join([old_reason, reason]))
        return
    set_row_target(
        row,
        mode,
        [v for v in [old_value, target] if target_is_action(v)],
        [v for v in [old_reason, reason] if v and v != NO_ACTION],
    )
    if component == "lag":
        setattr(row, f"target_{mode}_has_lag", True)
        return
    non_lag_value = getattr(row, f"target_{mode}_non_lag")
    non_lag_reason = getattr(row, f"target_{mode}_non_lag_reason")
    setattr(
        row,
        f"target_{mode}_non_lag",
        target_version_max([v for v in [non_lag_value, target] if target_is_action(v)]),
    )
    setattr(
        row,
        f"target_{mode}_non_lag_reason",
        target_reason_join([non_lag_reason, reason]),
    )


def plan_target_yellow(rows: List[DependencyRow], health: ProjectHealth, mode: str = "yellow") -> None:
    if health.status_rank >= TARGET_RANK["yellow"]:
        return

    # 1. Yellow requires 0 Critical.
    for r in rows:
        if r.scope_excluded:
            continue
        if parse_vuln_counts(r.current_vulns).get("C", 0) > 0:
            target = r.min_no_critical if has_safe_target(r.min_no_critical) else "нет safe target без Critical / risk register"
            merge_row_target(r, mode, target, greedy_target_reason("для жёлтого нужно убрать Critical", r, target) if target_is_action(target) else "для жёлтого нужно убрать Critical; safe target не найден, нужен risk register")

    # 2. A stricter per-package policy is an explicit team decision, not one of
    # the 20% rows that the project-level yellow threshold may leave behind.
    # Otherwise a costly 3m/6m/9m package can be silently skipped by the greedy
    # selector even though the dashboard says that this exact policy is active.
    for r in rows:
        if r.scope_excluded:
            continue
        if r.lag_threshold_months >= 12 or not dependency_needs_lag_update(r):
            continue
        lag_target = lag_update_target_for_row(r)
        merge_row_target(
            r,
            mode,
            lag_target,
            greedy_target_reason(
                f"явная package lag-policy ≤{r.lag_threshold_months}м обязательна и не исключается правилом 80%; target зафиксирован от baseline с запасом {LAG_TARGET_BUFFER_MONTHS}м",
                r,
                lag_target,
            ),
            component="lag",
        )

    # 3. Build the complete lag candidate pool first. Peer/cohort/registry
    # passes decide what is actually executable; greedy minimization happens
    # only afterwards, over that proven-compatible set.
    removed_closed = health.removed
    projected_lag_ok = (
        sum(
            1
            for r in rows
            if not r.scope_excluded and dependency_is_lag_ok_after_planned_target(r, mode)
        )
        + removed_closed
    )
    if projected_lag_ok >= health.total:
        return
    candidates = [
        r for r in rows
        if not r.scope_excluded
        and not dependency_is_lag_ok_after_planned_target(r, mode)
        and has_safe_target(lag_update_target_for_row(r))
    ]
    candidates.sort(key=lambda r: row_update_simplicity_score(r, lag_update_target_for_row(r)))
    for r in candidates:
        lag_target = lag_update_target_for_row(r)
        merge_row_target(
            r,
            mode,
            lag_target,
            greedy_target_reason(f"кандидат полного pre-compatibility lag-плана ≤{r.lag_threshold_months}м; post-compatibility greedy оставит безопасный запас выше 80%; target зафиксирован от baseline с запасом {LAG_TARGET_BUFFER_MONTHS}м", r, lag_target),
            component="lag",
        )


def plan_target_green(rows: List[DependencyRow], health: ProjectHealth, mode: str = "green") -> None:
    if health.status_rank >= TARGET_RANK["green"]:
        return

    # 1. Green requires 0 Critical and 0 High.
    for r in rows:
        if r.scope_excluded:
            continue
        counts = parse_vuln_counts(r.current_vulns)
        if counts.get("C", 0) or counts.get("H", 0):
            target = r.min_no_high if has_safe_target(r.min_no_high) else "нет safe target без C/H / risk register"
            merge_row_target(r, mode, target, greedy_target_reason("для зелёного нужно убрать Critical/High", r, target) if target_is_action(target) else "для зелёного нужно убрать Critical/High; safe target не найден, нужен risk register")

    # 2. Green requires every dependency to satisfy its configured lag policy.
    for r in rows:
        if r.scope_excluded:
            continue
        if dependency_needs_lag_update(r):
            lag_target = lag_update_target_for_row(r)
            merge_row_target(
                r,
                mode,
                lag_target,
                greedy_target_reason(f"для зелёного нужно выполнить lag-policy ≤{r.lag_threshold_months}м; target зафиксирован от baseline с запасом {LAG_TARGET_BUFFER_MONTHS}м", r, lag_target),
                component="lag",
            )

    # 3. Prefer removing Moderate. Green allows some Low/Moderate, but moderate is not worth keeping if a safe target exists.
    for r in sorted((row for row in rows if not row.scope_excluded), key=lambda x: (-(parse_vuln_counts(x.current_vulns).get("M", 0)), row_update_simplicity_score(x, x.min_no_vuln))):
        counts = parse_vuln_counts(r.current_vulns)
        if counts.get("M", 0) > 0 and has_safe_target(r.min_no_vuln):
            merge_row_target(r, mode, r.min_no_vuln, greedy_target_reason("для зелёного по максимуму закрываем Moderate", r, r.min_no_vuln))

    # 4. If Low+Moderate is still above 20, remove Low-heavy rows until the cap is plausible.
    remaining_ml = health.moderate + health.low
    already_no_vuln = {r.name for r in rows if target_is_action(r.target_green) and r.target_green == r.min_no_vuln and has_safe_target(r.min_no_vuln)}
    for r in rows:
        if r.name in already_no_vuln:
            c = parse_vuln_counts(r.current_vulns)
            remaining_ml -= c.get("M", 0) + c.get("L", 0)
    if remaining_ml > 20:
        candidates = []
        for r in rows:
            if r.scope_excluded:
                continue
            if r.name in already_no_vuln or not has_safe_target(r.min_no_vuln):
                continue
            c = parse_vuln_counts(r.current_vulns)
            if c.get("L", 0) > 0:
                candidates.append(r)
        candidates.sort(key=lambda r: (row_update_simplicity_score(r, r.min_no_vuln), -parse_vuln_counts(r.current_vulns).get("L", 0)))
        for r in candidates:
            if remaining_ml <= 20:
                break
            c = parse_vuln_counts(r.current_vulns)
            merge_row_target(r, mode, r.min_no_vuln, greedy_target_reason("для зелёного снижаем Low/Moderate до допустимого остатка ≤20", r, r.min_no_vuln))
            remaining_ml -= c.get("L", 0) + c.get("M", 0)


def enrich_project_targets(
    rows_by_project: Dict[str, List[DependencyRow]],
    baselines_by_project: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, ProjectHealth]:
    baselines_by_project = baselines_by_project or {}
    for project, rows in rows_by_project.items():
        apply_lag_planning_baseline(rows, baselines_by_project.get(project))
    health_by_project = {
        project: compute_project_health(rows, project, baselines_by_project.get(project))
        for project, rows in rows_by_project.items()
    }
    for project, rows in rows_by_project.items():
        health = health_by_project[project]
        # Reset targets in case function is called more than once.
        for r in rows:
            r.target_default = r.target_yellow = r.target_green = NO_ACTION
            r.target_default_non_lag = r.target_yellow_non_lag = r.target_green_non_lag = NO_ACTION
            r.target_default_non_lag_reason = r.target_yellow_non_lag_reason = r.target_green_non_lag_reason = NO_ACTION
            r.target_default_has_lag = r.target_yellow_has_lag = r.target_green_has_lag = False
            r.target_default_dynamic_locked = r.target_yellow_dynamic_locked = r.target_green_dynamic_locked = False
            if r.scope_excluded:
                excluded_reason = f"исключено из текущего scope: {r.exclusion_reason}"
                r.target_default_reason = r.target_yellow_reason = r.target_green_reason = excluded_reason
            else:
                r.target_default_reason = r.target_yellow_reason = r.target_green_reason = NO_ACTION
        plan_target_yellow(rows, health, "yellow")
        plan_target_green(rows, health, "green")
        default_goal = next_target_for_status(health.status)
        if default_goal == "yellow":
            for r in rows:
                r.target_default = r.target_yellow
                r.target_default_reason = r.target_yellow_reason
                r.target_default_non_lag = r.target_yellow_non_lag
                r.target_default_non_lag_reason = r.target_yellow_non_lag_reason
                r.target_default_has_lag = r.target_yellow_has_lag
        elif default_goal == "green":
            for r in rows:
                r.target_default = r.target_green
                r.target_default_reason = r.target_green_reason
                r.target_default_non_lag = r.target_green_non_lag
                r.target_default_non_lag_reason = r.target_green_non_lag_reason
                r.target_default_has_lag = r.target_green_has_lag
        else:
            for r in rows:
                r.target_default = NO_ACTION
                if not r.scope_excluded:
                    r.target_default_reason = "проект уже в зелёном состоянии по эвристике скрипта"
    return health_by_project


STORYBOOK_ESLINT_PACKAGE = "eslint-plugin-storybook"


def is_storybook_core_package(name: str) -> bool:
    return name == "storybook" or name.startswith("@storybook/")


def _target_attr(mode: str) -> str:
    return f"target_{mode}"


def _target_reason_attr(mode: str) -> str:
    return f"target_{mode}_reason"


def _set_mode_target(row: DependencyRow, mode: str, target: str, reason: str) -> None:
    setattr(row, _target_attr(mode), target)
    setattr(row, _target_reason_attr(mode), target_reason_join([
        getattr(row, _target_reason_attr(mode), ""),
        reason,
    ]))
    setattr(row, f"target_{mode}_dynamic_locked", True)


def _storybook_common_installable_version(
    rows: List[DependencyRow],
    client: LiveDataClient,
    desired_major: int,
    required_floor: str,
) -> str:
    version_sets: List[Set[str]] = []
    for row in rows:
        meta = client.npm_cache.get(row.name)
        if not isinstance(meta, dict):
            return ""
        versions = published_versions(meta, include_prerelease=False)
        structural, _ = client.registry_structural_candidates(meta, versions)
        eligible = {
            version
            for version in structural
            if (parsed := safe_version(version)) is not None
            and int(parsed.major) == desired_major
            and (cmp_current := compare_semver(version, row.current_version)) is not None
            and cmp_current >= 0
            and (cmp_floor := compare_semver(version, required_floor)) is not None
            and cmp_floor >= 0
        }
        if not eligible:
            return ""
        version_sets.append(eligible)
    common = set.intersection(*version_sets) if version_sets else set()
    for version in sorted(common, key=version_sort_key):
        if all(
            client.registry_version_is_installable(row.name, client.npm_cache[row.name], version)
            for row in rows
        ):
            return version
    return ""


def _storybook_plugin_target(
    row: DependencyRow,
    client: LiveDataClient,
    desired_major: int,
    preferred: str,
) -> str:
    meta = client.npm_cache.get(row.name)
    if not isinstance(meta, dict):
        return ""
    versions = published_versions(meta, include_prerelease=False)
    structural, _ = client.registry_structural_candidates(meta, versions)
    same_major = [
        version
        for version in structural
        if (parsed := safe_version(version)) is not None
        and int(parsed.major) == desired_major
        and (cmp_current := compare_semver(version, row.current_version)) is not None
        and cmp_current >= 0
    ]
    if preferred in same_major and client.registry_version_is_installable(row.name, meta, preferred):
        return preferred
    # The eslint plugin is part of the Storybook release train, but an exact
    # patch may not exist for every core package. Pick the newest installable
    # version in the same major; never jump to a different Storybook major.
    for version in sorted(same_major, key=version_sort_key, reverse=True):
        if client.registry_version_is_installable(row.name, meta, version):
            return version
    return ""


def enforce_storybook_cohort(rows_by_project: Dict[str, List[DependencyRow]], client: LiveDataClient) -> None:
    """Keep Storybook packages on one installable Nexus release train.

    Independent lag planning can otherwise produce combinations such as core
    Storybook 7 + eslint-plugin-storybook 9, or select a metadata-only builder
    version whose tarball is absent.  A cohort is either planned coherently or
    deferred as an explicit blocker; agents must not repair the manifest ad hoc.
    """
    for project, rows in rows_by_project.items():
        emitted_warnings: set[str] = set()
        core_rows = [
            row for row in rows
            if is_storybook_core_package(row.name) and not row.scope_excluded
        ]
        plugin_rows = [
            row for row in rows
            if row.name == STORYBOOK_ESLINT_PACKAGE and not row.scope_excluded
        ]
        if not core_rows:
            continue
        for row in core_rows + plugin_rows:
            row.compatibility_cohort = "storybook"

        for mode in ("yellow", "green", "default"):
            actionable_core = [
                row for row in core_rows
                if target_is_action(getattr(row, _target_attr(mode)))
            ]
            if not actionable_core:
                continue
            target_versions = [getattr(row, _target_attr(mode)) for row in actionable_core]
            parsed_targets = [safe_version(value) for value in target_versions]
            majors = [int(value.major) for value in parsed_targets if value is not None]
            if not majors:
                continue
            desired_major = max(set(majors), key=lambda major: (majors.count(major), major))
            required_floor = target_version_max([
                value for value in target_versions
                if (parsed := safe_version(value)) is not None and int(parsed.major) == desired_major
            ])
            family_rows = [
                row for row in core_rows
                if (current := safe_version(row.current_version)) is not None
                and int(current.major) <= desired_major
            ]
            common = _storybook_common_installable_version(
                family_rows,
                client,
                desired_major,
                required_floor,
            )
            if not common:
                blocker = (
                    f"STORYBOOK_COHORT_BLOCKED: нет общей installable Storybook {desired_major}.x версии "
                    f">= {required_floor} во configured registry для {', '.join(row.name for row in family_rows)}"
                )
                for row in family_rows + plugin_rows:
                    _set_mode_target(row, mode, NO_ACTION, blocker)
                    row.compatibility_note = blocker
                if blocker not in emitted_warnings:
                    eprint(f"[warn] {project}: {blocker}")
                    emitted_warnings.add(blocker)
                continue

            cohort_reason = (
                f"storybook cohort: все core-пакеты выровнены на общую installable Nexus версию {common}"
            )
            for row in family_rows:
                _set_mode_target(row, mode, common, cohort_reason)
                row.compatibility_note = cohort_reason

            for plugin in plugin_rows:
                plugin_target = _storybook_plugin_target(
                    plugin,
                    client,
                    desired_major,
                    common,
                )
                if plugin_target:
                    plugin_reason = (
                        f"storybook cohort: eslint plugin ограничен совместимой линией {desired_major}.x; "
                        f"выбрана installable Nexus версия {plugin_target}"
                    )
                    _set_mode_target(plugin, mode, plugin_target, plugin_reason)
                    plugin.compatibility_note = plugin_reason
                else:
                    blocker = (
                        f"STORYBOOK_COHORT_BLOCKED: для {plugin.name} нет installable версии {desired_major}.x "
                        "во configured registry; не заменять на другой major и не откатывать manifest вручную"
                    )
                    _set_mode_target(plugin, mode, NO_ACTION, blocker)
                    plugin.compatibility_note = blocker
                    if blocker not in emitted_warnings:
                        eprint(f"[warn] {project}: {blocker}")
                        emitted_warnings.add(blocker)



def _npm_peer_satisfied(spec: str, version: str) -> bool:
    parsed = safe_version(version)
    if parsed is None:
        return False
    try:
        return NpmSpec(str(spec)).match(parsed)
    except ValueError:
        # Unknown/non-semver peer declarations are not safe to reject based on
        # a parser guess; project verification remains the final authority.
        return True


def apply_supervisor_scope_expansions(rows_by_project: Dict[str, List[DependencyRow]]) -> None:
    """Apply bounded supervisor strategy through the normal verified pipeline.

    Targets arrive only after the desktop kernel has proven that the package is
    an existing, unambiguous, non-excluded direct dependency. This pass does
    not bypass registry evidence, peer closure, group verification or merged
    verification; it only lets the independent supervisor choose more work.
    """
    for project, rows in rows_by_project.items():
        for row in rows:
            if row.scope_excluded:
                continue
            for mode in ("default", "yellow", "green"):
                target = getattr(row, f"planner_target_{mode}", "").strip()
                if not target:
                    continue
                reason = (
                    f"SUPERVISOR_SCOPE_EXPANSION: independent supervisor activated existing direct "
                    f"dependency {row.name}@{target}; registry and verification gates remain mandatory"
                )
                _set_mode_target(row, mode, target, reason)
                row.compatibility_note = target_reason_join([row.compatibility_note, reason])
                eprint(f"[info] {project}: {reason}")


def apply_planner_deferrals(rows_by_project: Dict[str, List[DependencyRow]]) -> None:
    """Remove only executable targets while keeping dependencies in health.

    Supervisor deferral is deliberately different from ``scopeExcluded``: the
    package remains in the denominator and final blocker report.  Compatibility
    cohorts stay atomic: deferring one member defers the whole already-known
    cohort so Executor never receives half of a coupled migration.
    """
    for project, rows in rows_by_project.items():
        deferred_cohorts = {
            row.compatibility_cohort
            for row in rows
            if row.planner_deferred and not row.scope_excluded and row.compatibility_cohort
        }
        cohort_reason = next((
            row.planner_deferred_reason
            for row in rows
            if row.compatibility_cohort in deferred_cohorts and row.planner_deferred_reason
        ), "")
        for row in rows:
            inherited = bool(row.compatibility_cohort and row.compatibility_cohort in deferred_cohorts)
            if not (row.planner_deferred or inherited) or row.scope_excluded:
                continue
            if inherited and not row.planner_deferred:
                row.planner_deferred = True
                row.planner_deferred_reason = cohort_reason or "companion of a Supervisor-deferred compatibility cohort"
            reason = (
                "PLANNER_DEFERRED: "
                + (row.planner_deferred_reason or "target temporarily deferred by autonomous Supervisor")
            )
            for mode in ("yellow", "green", "default"):
                if target_is_action(getattr(row, _target_attr(mode))):
                    _set_mode_target(row, mode, NO_ACTION, reason)
            row.compatibility_note = target_reason_join([row.compatibility_note, reason])
            eprint(f"[warn] {project}: {row.name}: {reason}")


def _desired_target_attr(mode: str) -> str:
    return f"desired_target_{mode}"


def _resolution_reason_attr(mode: str) -> str:
    return f"resolution_reason_{mode}"


def _planned_action_attr(mode: str) -> str:
    return f"planned_action_{mode}"


_TYPES_STUB_MARKERS = (
    "stub types definition",
    "stub type definition",
    "provides its own type definitions",
    "provides its own types",
    "do not need this installed",
    "no longer need to install",
)


def types_runtime_package_name(types_package: str) -> str:
    """Map DefinitelyTyped package names back to their runtime package."""
    name = str(types_package or "")
    if not name.startswith("@types/"):
        return ""
    suffix = name[len("@types/"):]
    if "__" in suffix:
        scope, package = suffix.split("__", 1)
        if scope and package:
            return f"@{scope}/{package}"
    return suffix


def metadata_marks_types_stub(meta: Optional[Dict[str, Any]], version: str) -> bool:
    if not isinstance(meta, dict) or not version:
        return False
    version_meta = (meta.get("versions") or {}).get(version)
    texts: List[str] = []
    if isinstance(version_meta, dict) and version_meta.get("deprecated"):
        texts.append(str(version_meta.get("deprecated")))
    # Some private registries flatten deprecation text at package level.  Only
    # use it when the selected version is the latest dist-tag, avoiding a stale
    # package-level warning turning every historical @types version into remove.
    latest = str((meta.get("dist-tags") or {}).get("latest") or "")
    if latest == version and meta.get("deprecated"):
        texts.append(str(meta.get("deprecated")))
    text = " ".join(texts).lower()
    return any(marker in text for marker in _TYPES_STUB_MARKERS)


def _row_resolved_version(row: DependencyRow, mode: str, assignment: Optional[Dict[str, str]] = None) -> str:
    if assignment and row.name in assignment:
        return str(assignment[row.name])
    target = str(getattr(row, _target_attr(mode), "") or "")
    return target if target_is_action(target) else row.current_version


def _types_stub_removals_for_assignment(
    rows_by_name: Dict[str, DependencyRow],
    assignment: Dict[str, str],
    mode: str,
    client: LiveDataClient,
) -> Set[str]:
    """Return removals already proven safe by the runtime target assignment."""
    removals: Set[str] = set()
    for name, row in rows_by_name.items():
        if not name.startswith("@types/"):
            continue
        selected = str(assignment.get(name) or row.current_version)
        meta = client.npm_cache.get(name)
        if not metadata_marks_types_stub(meta, selected):
            continue
        runtime_name = types_runtime_package_name(name)
        runtime_row = rows_by_name.get(runtime_name)
        runtime_meta = client.npm_cache.get(runtime_name)
        if runtime_row is None or not isinstance(runtime_meta, dict):
            continue
        runtime_version = _row_resolved_version(runtime_row, mode, assignment)
        if client.registry_version_provides_own_types(runtime_name, runtime_meta, runtime_version):
            removals.add(name)
    return removals


def _merge_atomic_type_cohorts(rows: List[DependencyRow], pairs: List[Tuple[str, str]]) -> None:
    if not pairs:
        return
    rows_by_name = {row.name: row for row in rows}
    parent = {name: name for name in rows_by_name}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        if left not in parent or right not in parent:
            return
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    by_existing: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        if row.compatibility_cohort:
            by_existing[row.compatibility_cohort].append(row.name)
    for names in by_existing.values():
        for name in names[1:]:
            union(names[0], name)
    for left, right in pairs:
        union(left, right)

    atomic_roots = {find(left) for left, right in pairs if left in parent and right in parent}
    components: Dict[str, List[str]] = defaultdict(list)
    for name in parent:
        components[find(name)].append(name)
    for root in atomic_roots:
        names = sorted(components[root])
        digest = hashlib.sha256("\0".join(names).encode("utf-8")).hexdigest()[:10]
        runtime_hint = next((types_runtime_package_name(name) for name in names if name.startswith("@types/")), "types")
        cohort = f"atomic-types-{slugify(runtime_hint or 'types')}-{digest}"
        for name in names:
            rows_by_name[name].compatibility_cohort = cohort


def plan_executable_actions(
    rows_by_project: Dict[str, List[DependencyRow]],
    client: LiveDataClient,
    *,
    modes: Tuple[str, ...] = ("yellow", "green", "default"),
    immutable_targets: bool = False,
) -> None:
    """Freeze update/remove semantics before Branch-plan generation.

    A deprecated @types/* stub is removable only when the exact resolved runtime
    version is proven (from its registry tarball) to ship its own declarations.
    If current runtime does not provide them, runtime upgrade + removal become one
    atomic compatibility cohort so no intermediate branch can make tsc red.
    """
    for project, rows in rows_by_project.items():
        rows_by_name = {row.name: row for row in rows}
        atomic_pairs: List[Tuple[str, str]] = []
        for row in rows:
            for mode in modes:
                action_attr = _planned_action_attr(mode)
                target = str(getattr(row, _target_attr(mode), "") or "")
                if row.scope_excluded:
                    setattr(row, action_attr, "excluded")
                    continue
                if not target_is_action(target):
                    setattr(row, action_attr, "deferred")
                    continue
                action = "update"
                if row.name.startswith("@types/") and metadata_marks_types_stub(client.npm_cache.get(row.name), target):
                    runtime_name = types_runtime_package_name(row.name)
                    runtime_row = rows_by_name.get(runtime_name)
                    runtime_meta = client.npm_cache.get(runtime_name)
                    runtime_version = _row_resolved_version(runtime_row, mode) if runtime_row else ""
                    proved = bool(
                        runtime_row is not None
                        and isinstance(runtime_meta, dict)
                        and client.registry_version_provides_own_types(runtime_name, runtime_meta, runtime_version)
                    )
                    if proved:
                        action = "remove"
                        note = (
                            f"TYPE_STUB_REMOVE_PROVED: {row.name} removal is coupled to "
                            f"{runtime_name}@{runtime_version}, whose registry tarball ships resolvable TypeScript declarations"
                        )
                        row.compatibility_note = target_reason_join([row.compatibility_note, note])
                        current_provides = client.registry_version_provides_own_types(
                            runtime_name, runtime_meta, runtime_row.current_version
                        )
                        if runtime_version != runtime_row.current_version and not current_provides:
                            atomic_pairs.append((row.name, runtime_name))
                    else:
                        # Updating to a deprecated empty/stub @types package is not
                        # useful, and removing it is not yet safe. Keep it out of
                        # executable scope until a self-typed runtime assignment exists.
                        reason = (
                            f"TYPE_STUB_REMOVE_DEFERRED: {row.name}@{target} is a deprecated types stub, "
                            f"but resolved runtime {runtime_name or 'package'}@{runtime_version or 'unknown'} "
                            "was not proven to ship its own declarations"
                        )
                        if immutable_targets:
                            raise BaselineConstraintVerificationError(
                                f"FINAL_PROVEN_ASSIGNMENT_ACTION_INVALID: {project}/{mode}: {reason}. "
                                "A post-proof action planner may not change dependency versions."
                            )
                        _set_mode_target(row, mode, NO_ACTION, reason)
                        row.compatibility_note = target_reason_join([row.compatibility_note, reason])
                        action = "deferred"
                setattr(row, action_attr, action)
        _merge_atomic_type_cohorts(rows, atomic_pairs)
        if atomic_pairs:
            rendered = ", ".join(f"{types}+{runtime}" for types, runtime in sorted(set(atomic_pairs)))
            eprint(f"[info] {project}: atomic type-provider cohort(s): {rendered}")


def capture_desired_targets(rows_by_project: Dict[str, List[DependencyRow]]) -> None:
    """Freeze policy/planner intent before registry/peer compatibility mutates targets.

    ``target_*`` is a public, long-lived field consumed by old Baseline files,
    Dashboard code and the desktop app.  We therefore keep it as the resolved
    target and store the pre-solver intent separately instead of changing the
    artifact schema contract in one release.
    """
    for rows in rows_by_project.values():
        for row in rows:
            for mode in ("yellow", "green", "default"):
                setattr(row, _desired_target_attr(mode), getattr(row, _target_attr(mode)))
                setattr(row, _resolution_reason_attr(mode), "")


def _aggregate_vulnerability_summary(rows: List[DependencyRow]) -> str:
    """Combine duplicate declarations without counting the same finding twice."""
    totals = {"C": 0, "H": 0, "M": 0, "L": 0, "U": 0}
    known = False
    for row in rows:
        if row.current_vulns not in ("unknown", "неизвестно", "—"):
            known = True
        for code, value in parse_vuln_counts(row.current_vulns).items():
            totals[code] = max(totals[code], value)
    parts = [f"{code}:{value}" for code, value in totals.items() if value]
    return " ".join(parts) if parts else ("0" if known else "unknown")



def _assert_duplicate_dependency_source_contract(rows: Sequence[DependencyRow]) -> None:
    if len(rows) < 2:
        return
    fixed = [row for row in rows if is_non_registry_spec(row.requested_spec)]
    managed = [row for row in rows if not is_non_registry_spec(row.requested_spec)]
    name = rows[0].name
    if fixed and managed:
        detail = ", ".join(
            f"{row.kind}={row.requested_spec}"
            for row in sorted(rows, key=lambda item: (item.kind, item.requested_spec))
        )
        raise BaselineConstraintVerificationError(
            f"HETEROGENEOUS_DIRECT_DEPENDENCY_DECLARATION: {name}: "
            f"registry-managed and fixed source declarations cannot share one solver identity; {detail}"
        )
    fixed_specs = sorted({row.requested_spec.strip() for row in fixed})
    if len(fixed_specs) > 1:
        raise BaselineConstraintVerificationError(
            f"FIXED_DEPENDENCY_DECLARATION_MISMATCH: {name}: "
            f"fixed declarations disagree: {fixed_specs!r}"
        )

def _aggregate_duplicate_package_row(rows: List[DependencyRow]) -> DependencyRow:
    """Build one solver identity without discarding stricter duplicate intents."""
    _assert_duplicate_dependency_source_contract(rows)
    representative = sorted(rows, key=lambda row: (row.kind, row.requested_spec))[0]
    if len(rows) == 1:
        return representative
    aggregate = dataclasses.replace(
        representative,
        registry_artifacts={
            version: evidence
            for row in rows
            for version, evidence in row.registry_artifacts.items()
        },
    )
    for mode in ("yellow", "green", "default"):
        desired = target_version_max([_desired_target_for_mode(row, mode) for row in rows])
        setattr(aggregate, _desired_target_attr(mode), desired)
        setattr(aggregate, _target_attr(mode), desired)
        setattr(aggregate, _target_reason_attr(mode), target_reason_join([
            getattr(row, _target_reason_attr(mode), "") for row in rows
        ]))
    aggregate.min_no_critical = target_version_max([row.min_no_critical for row in rows])
    aggregate.min_no_high = target_version_max([row.min_no_high for row in rows])
    aggregate.current_vulns = _aggregate_vulnerability_summary(rows)
    aggregate.scope_excluded = all(row.scope_excluded for row in rows)
    aggregate.planner_deferred = all(row.planner_deferred for row in rows)
    return aggregate

def _desired_target_for_mode(row: DependencyRow, mode: str) -> str:
    saved = str(getattr(row, _desired_target_attr(mode), "") or "").strip()
    return saved if saved else getattr(row, _target_attr(mode))


def _package_version_metadata(
    row: DependencyRow,
    version: str,
    client: LiveDataClient,
) -> Optional[Dict[str, Any]]:
    meta = client.npm_cache.get(row.name)
    if isinstance(meta, dict):
        target_meta = (meta.get("versions") or {}).get(version)
        if isinstance(target_meta, dict):
            return target_meta
    if version != row.current_version:
        return None
    try:
        manifest_path = Path(row.package_dir) / "node_modules" / Path(*row.name.split("/")) / "package.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(manifest, dict) and str(manifest.get("version") or "") == version:
            return manifest
    except (OSError, ValueError, TypeError):
        pass
    return None


def _peer_entries(row: DependencyRow, version: str, client: LiveDataClient) -> List[Tuple[str, str, bool]]:
    metadata = _package_version_metadata(row, version, client)
    if not isinstance(metadata, dict):
        return []
    peers = metadata.get("peerDependencies")
    if not isinstance(peers, dict):
        return []
    peers_meta = metadata.get("peerDependenciesMeta")
    result: List[Tuple[str, str, bool]] = []
    for raw_name, raw_spec in peers.items():
        name = str(raw_name)
        spec = str(raw_spec).strip() if isinstance(raw_spec, str) else ""
        if not spec:
            continue
        optional = bool(
            isinstance(peers_meta, dict)
            and isinstance(peers_meta.get(name), dict)
            and peers_meta[name].get("optional") is True
        )
        result.append((name, spec, optional))
    return result


def _ordinary_dependency_entries(row: DependencyRow, version: str, client: LiveDataClient) -> List[Tuple[str, str, str]]:
    """Return installed non-peer dependency relations for one published version.

    These relations are verification evidence, not hard equality/peer constraints:
    npm/yarn/pnpm may legally install a nested copy.  When the dependency is also
    direct in the project, however, two toolchain/type universes can interact at
    project level, so the verifier must keep the packages in the same diagnostic
    neighborhood.
    """
    metadata = _package_version_metadata(row, version, client)
    if not isinstance(metadata, dict):
        return []
    result: List[Tuple[str, str, str]] = []
    for section in ("dependencies", "optionalDependencies"):
        values = metadata.get(section)
        if not isinstance(values, dict):
            continue
        for raw_name, raw_spec in values.items():
            name = str(raw_name).strip()
            spec = str(raw_spec).strip() if isinstance(raw_spec, str) else ""
            if name and spec:
                result.append((name, spec, section))
    return result


def _potential_interaction_edges(
    rows_by_name: Dict[str, DependencyRow],
    domains: Dict[str, List[str]],
    client: LiveDataClient,
) -> List[InteractionEdge]:
    """Discover verification-coupling edges without inventing hard constraints."""
    direct_names = set(rows_by_name)
    edges: Set[InteractionEdge] = set()
    for name, row in rows_by_name.items():
        for version in domains.get(name, [row.current_version]):
            for peer_name, spec, _optional in _peer_entries(row, version, client):
                if peer_name in direct_names and peer_name != name:
                    edges.add(InteractionEdge.create(
                        name, peer_name, kind=PEER_REQUIREMENT, provenance="peerDependencies",
                        detail=f"{name}@{version} -> {peer_name}@{spec}",
                    ))
            for dependency_name, spec, section in _ordinary_dependency_entries(row, version, client):
                if dependency_name in direct_names and dependency_name != name:
                    edges.add(InteractionEdge.create(
                        name, dependency_name, kind=DIRECT_SHADOWING, provenance=section,
                        detail=f"{name}@{version} -> {dependency_name}@{spec}",
                    ))
    return sorted(edges)


_PROJECT_NODE_VERSION_CACHE: Dict[str, List[Tuple[str, str]]] = {}


def _normalized_node_version(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(?:^|[^0-9])(\d+\.\d+\.\d+)(?:[^0-9]|$)", text)
    return match.group(1) if match else ""


def _project_node_versions(project_dir: str) -> List[Tuple[str, str]]:
    """Return only exact Node versions explicitly pinned by the project.

    The runtime executing DepLoom is tool infrastructure, not project intent.
    Feeding the planner host runtime into Z3 makes one source commit produce
    different assignments on different machines. Only repository pins are
    authoritative here; an unpinned project must not inherit the host.
    """
    key = str(Path(project_dir).resolve())
    cached = _PROJECT_NODE_VERSION_CACHE.get(key)
    if cached is not None:
        return cached
    root = Path(key)
    found: List[Tuple[str, str]] = []
    try:
        package_json = json.loads((root / "package.json").read_text(encoding="utf-8"))
        volta = package_json.get("volta") if isinstance(package_json, dict) else None
        if isinstance(volta, dict):
            version = _normalized_node_version(volta.get("node"))
            if version:
                found.append((version, "package.json#volta.node"))
    except (OSError, ValueError, TypeError):
        pass
    for filename in (".nvmrc", ".node-version"):
        try:
            version = _normalized_node_version((root / filename).read_text(encoding="utf-8"))
        except OSError:
            version = ""
        if version:
            found.append((version, filename))
    dedup: Dict[str, str] = {}
    for version, source in found:
        dedup.setdefault(version, source)
    result = sorted(dedup.items(), key=lambda item: version_sort_key(item[0]))
    _PROJECT_NODE_VERSION_CACHE[key] = result
    return result


def _project_environment_constraint_issue(
    row: DependencyRow,
    version: str,
    client: LiveDataClient,
) -> str:
    """Reject a candidate that cannot run on an exact project Node runtime.

    This is deliberately metadata-only and deterministic.  Source/API/tsconfig
    migration remains the executor's job, but a target whose own ``engines``
    excludes a pinned/current project runtime is a plan-level contradiction and
    should never reach a work branch.
    """
    if version == row.current_version:
        return ""
    metadata = _package_version_metadata(row, version, client)
    if not isinstance(metadata, dict):
        return ""
    engines = metadata.get("engines")
    if not isinstance(engines, dict):
        return ""
    node_spec = str(engines.get("node") or "").strip()
    if not node_spec:
        return ""
    incompatible = [
        (node_version, source)
        for node_version, source in _project_node_versions(row.package_dir)
        if not _npm_peer_satisfied(node_spec, node_version)
    ]
    if not incompatible:
        return ""
    facts = ", ".join(f"node {node_version} ({source})" for node_version, source in incompatible)
    return (
        f"PROJECT_NODE_ENGINE_CONFLICT: {row.name}@{version} requires node@{node_spec}, "
        f"but project uses {facts}"
    )


def _candidate_registry_installable(
    row: DependencyRow,
    version: str,
    client: LiveDataClient,
    *,
    trusted_target: str = "",
) -> bool:
    """Return registry proof for a candidate, grandfathering only proven/current versions.

    ``trusted_target`` is the target that survived the pipeline's preceding
    ``enrich_registry_target_evidence`` pass. It also keeps the compatibility
    helper independently testable with legacy fixtures that predate explicit
    tarball evidence. Alternative/fallback candidates are never trusted this way.
    """
    if version == row.current_version:
        return True
    if trusted_target == version and target_is_action(trusted_target):
        return True
    evidence = row.registry_artifacts.get(version)
    if isinstance(evidence, dict):
        status = str(evidence.get("status") or "")
        if status == "available":
            return True
        if status and status not in {"not-proven", "metadata-unavailable"}:
            return False
    cached = client.registry_artifact_cache.get((row.name, version))
    if isinstance(cached, dict):
        status = str(cached.get("status") or "")
        if status == "available":
            return True
        if status:
            return False
    meta = client.npm_cache.get(row.name)
    if not isinstance(meta, dict) or not isinstance((meta.get("versions") or {}).get(version), dict):
        return False
    tarball = client.registry_tarball_url(meta, version)
    if not tarball or not client.registry_artifact_url_allowed(tarball):
        return False
    ok = client.registry_version_is_installable(row.name, meta, version)
    if ok:
        row.registry_artifacts[version] = client.registry_version_artifact(row.name, meta, version)
    return ok


PEER_SOLVER_MAX_DOMAIN = 32
PEER_SOLVER_MAX_VISITS = 50_000
PEER_SOLVER_EXACT_COMPONENT_SIZE = 8
PEER_SOLVER_LARGE_DOMAIN = 16
PEER_SOLVER_BEAM_WIDTH = 8
PEER_SOLVER_LARGE_MAX_VISITS = 100_000


def _bounded_candidate_versions(
    ordered: List[str],
    eligible: List[str],
    anchors: List[str],
) -> List[str]:
    """Keep policy anchors and semver boundaries without scanning registry history."""
    if len(ordered) <= PEER_SOLVER_MAX_DOMAIN:
        return ordered
    selected: Set[str] = {version for version in anchors if version in ordered}
    by_major: Dict[int, List[str]] = defaultdict(list)
    for version in eligible:
        parsed = safe_version(version)
        if parsed is not None:
            by_major[int(parsed.major)].append(version)
    for versions in by_major.values():
        versions.sort(key=version_sort_key)
        for boundary in (versions[0], versions[-1]):
            if len(selected) >= PEER_SOLVER_MAX_DOMAIN:
                break
            selected.add(boundary)
        if len(selected) >= PEER_SOLVER_MAX_DOMAIN:
            break
    for version in ordered:
        if len(selected) >= PEER_SOLVER_MAX_DOMAIN:
            break
        selected.add(version)
    # Preserve the solver's preference order (desired/fallback/raise/current).
    return [version for version in ordered if version in selected]


def _candidate_domain(row: DependencyRow, mode: str, client: LiveDataClient) -> List[str]:
    """Build the complete deterministic registry domain without display groups.

    Correctness must not depend on a heuristic version cap.  Large-component
    search may still *prioritize* a compact seed domain, but every eligible
    registry candidate remains reachable when a conflict asks that package to
    move.  This preserves the distinction between UNKNOWN_BUDGET and UNSAT.
    """
    intent_policy = _baseline_intent_policy(row.name)
    if intent_policy == "keep-current":
        return [row.current_version]
    if row.scope_excluded or row.planner_deferred:
        return [row.current_version]
    desired = _desired_target_for_mode(row, mode)
    meta = client.npm_cache.get(row.name)
    if not isinstance(meta, dict):
        return [row.current_version]
    versions = published_versions(meta, include_prerelease=False)
    structural, _ = client.registry_structural_candidates(meta, versions)
    current_v = safe_version(row.current_version)
    eligible: List[str] = []
    for version in structural:
        parsed = safe_version(version)
        if current_v is not None and parsed is not None and parsed < current_v:
            continue
        eligible.append(version)
    if row.current_version not in eligible:
        eligible.append(row.current_version)
    eligible = sorted(set(eligible), key=version_sort_key)
    desired_v = safe_version(desired) if target_is_action(desired) else None
    if desired_v is None:
        above = [version for version in eligible if version != row.current_version]
        ordered = [row.current_version] + above
        if intent_policy == "required":
            alternatives = [version for version in ordered if version != row.current_version]
            return list(dict.fromkeys(alternatives or [row.current_version]))
        return list(dict.fromkeys(ordered))

    exact = [desired] if desired in eligible else []
    below = [
        version for version in eligible
        if version != row.current_version
        and (parsed := safe_version(version)) is not None
        and parsed <= desired_v
        and version != desired
    ]
    above = [
        version for version in eligible
        if (parsed := safe_version(version)) is not None and parsed > desired_v
    ]
    ordered = (
        exact
        + sorted(below, key=version_sort_key, reverse=True)
        + sorted(above, key=version_sort_key)
        + [row.current_version]
    )
    if intent_policy == "required":
        alternatives = [version for version in ordered if version != row.current_version]
        return list(dict.fromkeys(alternatives or [row.current_version]))
    return list(dict.fromkeys(ordered))

def _known_peer_range_satisfaction(spec: str, version: str) -> Optional[bool]:
    """Return exact semver peer satisfaction, or None when static proof is unavailable.

    Fixed inputs are immutable constants, but metadata that cannot be parsed is
    never converted into a hard exclusion. The real package manager remains the
    authority for every skipped/unknown relation.
    """
    parsed = safe_version(version)
    if parsed is None:
        return None
    try:
        return bool(NpmSpec(str(spec)).match(parsed))
    except Exception:
        return None


def _apply_fixed_peer_constant_constraints(
    solver_rows_by_name: Mapping[str, DependencyRow],
    fixed_rows_by_name: Mapping[str, DependencyRow],
    domains: Mapping[str, List[str]],
    client: LiveDataClient,
) -> Tuple[Dict[str, List[str]], Dict[str, int]]:
    """Project immutable fixed peers into managed finite domains.

    Fixed packages never become Solver variables. Instead, any peer relation
    that can be proven from exact local/registry metadata becomes a unary hard
    domain restriction on the managed endpoint. Existing current/current debt is
    grandfathered exactly like the ordinary peer model: only a moved managed
    candidate can be excluded. Missing/unparseable metadata stays UNKNOWN and is
    left to fresh package-manager verification.
    """
    pruned: Dict[str, List[str]] = {
        str(name): list(values)
        for name, values in domains.items()
    }
    evaluated = 0
    unknown = 0
    excluded: Set[Tuple[str, str]] = set()

    # managed candidate -> fixed provider
    for managed_name, managed_row in sorted(solver_rows_by_name.items()):
        kept: List[str] = []
        for version in pruned.get(managed_name, [managed_row.current_version]):
            if version == managed_row.current_version:
                kept.append(version)
                continue
            blocked = False
            for peer_name, spec, _optional in _peer_entries(managed_row, version, client):
                fixed_row = fixed_rows_by_name.get(peer_name)
                if fixed_row is None:
                    continue
                satisfied = _known_peer_range_satisfaction(spec, fixed_row.current_version)
                if satisfied is None:
                    unknown += 1
                    continue
                evaluated += 1
                if not satisfied:
                    blocked = True
                    excluded.add((managed_name, version))
                    break
            if not blocked:
                kept.append(version)
        if managed_row.current_version not in kept:
            kept.append(managed_row.current_version)
        pruned[managed_name] = list(dict.fromkeys(kept))

    # fixed source -> managed provider
    for _fixed_name, fixed_row in sorted(fixed_rows_by_name.items()):
        for peer_name, spec, _optional in _peer_entries(
            fixed_row, fixed_row.current_version, client
        ):
            managed_row = solver_rows_by_name.get(peer_name)
            if managed_row is None:
                continue
            kept: List[str] = []
            for version in pruned.get(peer_name, [managed_row.current_version]):
                if version == managed_row.current_version:
                    kept.append(version)
                    continue
                satisfied = _known_peer_range_satisfaction(spec, version)
                if satisfied is None:
                    unknown += 1
                    kept.append(version)
                    continue
                evaluated += 1
                if satisfied:
                    kept.append(version)
                else:
                    excluded.add((peer_name, version))
            if managed_row.current_version not in kept:
                kept.append(managed_row.current_version)
            pruned[peer_name] = list(dict.fromkeys(kept))

    return pruned, {
        "evaluated": evaluated,
        "excluded": len(excluded),
        "unknown": unknown,
    }


def _potential_peer_graph(
    rows_by_name: Dict[str, DependencyRow],
    domains: Dict[str, List[str]],
    client: LiveDataClient,
) -> Dict[str, Set[str]]:
    graph: Dict[str, Set[str]] = {name: set() for name in rows_by_name}
    direct_names = set(rows_by_name)
    for name, row in rows_by_name.items():
        for version in domains.get(name, [row.current_version]):
            for peer_name, _spec, _optional in _peer_entries(row, version, client):
                if peer_name in direct_names and peer_name != name:
                    graph[name].add(peer_name)
                    graph[peer_name].add(name)
    return graph


def _graph_components(graph: Dict[str, Set[str]]) -> List[List[str]]:
    remaining = set(graph)
    components: List[List[str]] = []
    while remaining:
        start = min(remaining)
        stack = [start]
        seen: Set[str] = set()
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            stack.extend(sorted(graph.get(name, set()) - seen, reverse=True))
        remaining -= seen
        components.append(sorted(seen))
    return sorted(components, key=lambda names: names[0] if names else "")


def _assignment_constraint_issue(
    names: Set[str],
    assignment: Dict[str, str],
    rows_by_name: Dict[str, DependencyRow],
    client: LiveDataClient,
    *,
    partial: bool,
    domains: Optional[Dict[str, List[str]]] = None,
    learned_nogoods: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Validate active peer edges for a partial or complete assignment.

    Existing current/current metadata inconsistencies are grandfathered. A peer
    constraint becomes part of this Baseline proof when either endpoint moves.
    This prevents an unrelated pre-existing peer warning from making every
    future Baseline impossible while still proving that the plan introduces no
    new contradiction.
    """
    for nogood in learned_nogoods or []:
        if assignment_matches_nogood(assignment, nogood):
            detail = ", ".join(f"{name}@{version}" for name, version in sorted(nogood.items()))
            return f"LEARNED_CONSTRAINT: verified incompatible combination: {detail}"

    for intent_name, intent_version in sorted(assignment.items()):
        intent_row = rows_by_name.get(intent_name)
        if intent_row is None:
            continue
        intent_policy = _baseline_intent_policy(intent_name)
        if intent_policy == "keep-current" and intent_version != intent_row.current_version:
            return f"USER_BASELINE_KEEP_CURRENT: {intent_name} must remain {intent_row.current_version} in this Baseline"
        if intent_policy == "required" and intent_version == intent_row.current_version:
            return f"USER_BASELINE_REQUIRED_UPDATE: {intent_name} must move away from current {intent_row.current_version} in this Baseline"

    for source_name in sorted(assignment):
        if source_name not in names:
            continue
        source = rows_by_name[source_name]
        source_version = assignment[source_name]
        source_moved = source_version != source.current_version
        if source_moved:
            environment_issue = _project_environment_constraint_issue(source, source_version, client)
            if environment_issue:
                return environment_issue
        for peer_name, spec, optional in _peer_entries(source, source_version, client):
            peer = rows_by_name.get(peer_name)
            if peer is None:
                if source_moved and not optional:
                    return (
                        f"MISSING_REQUIRED_PEER: {source.name}@{source_version} requires {peer_name}@{spec}; "
                        "automatic addition of new package names is not enabled by current project policy"
                    )
                continue
            if peer_name not in names:
                continue
            if peer_name in assignment:
                peer_version = assignment[peer_name]
                peer_moved = peer_version != peer.current_version
                if not (source_moved or peer_moved):
                    continue
                if not _npm_peer_satisfied(spec, peer_version):
                    return (
                        f"PEER_CONFLICT: {source.name}@{source_version} requires {peer.name}@{spec}, "
                        f"resolved {peer_version}"
                    )
            elif partial and source_moved and domains is not None:
                # Cheap forward propagation: if no structural candidate can ever
                # satisfy this range, do not descend into this branch. Registry
                # reachability remains lazy and is checked when that candidate is
                # actually assigned.
                possible = any(_npm_peer_satisfied(spec, version) for version in domains.get(peer_name, []))
                if not possible:
                    return (
                        f"PEER_DOMAIN_EMPTY: {source.name}@{source_version} requires {peer.name}@{spec}; "
                        "no candidate version satisfies the range"
                    )
    return ""


def _new_assignment_constraint_issue(
    name: str,
    names: Set[str],
    assignment: Dict[str, str],
    rows_by_name: Dict[str, DependencyRow],
    client: LiveDataClient,
    domains: Dict[str, List[str]],
    learned_nogoods: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Validate only edges activated by the newest partial assignment."""
    for nogood in learned_nogoods or []:
        if assignment_matches_nogood(assignment, nogood):
            detail = ", ".join(f"{package}@{version}" for package, version in sorted(nogood.items()))
            return f"LEARNED_CONSTRAINT: verified incompatible combination: {detail}"

    source = rows_by_name[name]
    source_version = assignment[name]
    intent_policy = _baseline_intent_policy(name)
    if intent_policy == "keep-current" and source_version != source.current_version:
        return f"USER_BASELINE_KEEP_CURRENT: {name} must remain {source.current_version} in this Baseline"
    if intent_policy == "required" and source_version == source.current_version:
        return f"USER_BASELINE_REQUIRED_UPDATE: {name} must move away from current {source.current_version} in this Baseline"
    source_moved = source_version != source.current_version
    if source_moved:
        environment_issue = _project_environment_constraint_issue(source, source_version, client)
        if environment_issue:
            return environment_issue
    for peer_name, spec, optional in _peer_entries(source, source_version, client):
        peer = rows_by_name.get(peer_name)
        if peer is None:
            if source_moved and not optional:
                return (
                    f"MISSING_REQUIRED_PEER: {source.name}@{source_version} requires {peer_name}@{spec}; "
                    "automatic addition of new package names is not enabled by current project policy"
                )
            continue
        if peer_name not in names:
            continue
        if peer_name in assignment:
            peer_version = assignment[peer_name]
            if (source_moved or peer_version != peer.current_version) and not _npm_peer_satisfied(spec, peer_version):
                return f"PEER_CONFLICT: {source.name}@{source_version} requires {peer.name}@{spec}, resolved {peer_version}"
        elif source_moved and not any(_npm_peer_satisfied(spec, version) for version in domains.get(peer_name, [])):
            return (
                f"PEER_DOMAIN_EMPTY: {source.name}@{source_version} requires {peer.name}@{spec}; "
                "no candidate version satisfies the range"
            )

    for other_name, other_version in assignment.items():
        if other_name == name:
            continue
        other = rows_by_name[other_name]
        for peer_name, spec, _optional in _peer_entries(other, other_version, client):
            if peer_name != name:
                continue
            if (other_version != other.current_version or source_moved) and not _npm_peer_satisfied(spec, source_version):
                return f"PEER_CONFLICT: {other.name}@{other_version} requires {source.name}@{spec}, resolved {source_version}"
    return ""

def _security_priority_for_row(row: DependencyRow) -> Tuple[int, str]:
    counts = parse_vuln_counts(row.current_vulns)
    if counts.get("C", 0) > 0 and has_safe_target(row.min_no_critical):
        return 2, row.min_no_critical
    if counts.get("H", 0) > 0 and has_safe_target(row.min_no_high):
        return 1, row.min_no_high
    return 0, ""


def _score_version_contribution(
    row: DependencyRow,
    version: str,
    mode: str,
    rank: int,
    stability_target: str = "",
) -> Tuple[int, int, int, int, int, int, int, int]:
    """Per-package lexicographic contribution.

    During a residual replan, a previously approved *pending* target is a
    stability preference. Security remediation still outranks it, while the
    stability tier outranks fresh policy/freshness movement so a replan does not
    churn targets merely because newer registry metadata appeared. Packages
    whose approved target is already the current merged version are hard-fixed
    by the residual domain builder and never reach this soft tier.
    """
    desired = _desired_target_for_mode(row, mode)
    desired_action = target_is_action(desired)
    security_priority, floor = _security_priority_for_row(row)
    critical = 1 if security_priority == 2 and current_meets_target(version, floor) else 0
    high = 1 if security_priority == 1 and current_meets_target(version, floor) else 0
    stable = 1 if stability_target and version == stability_target and version != row.current_version else 0
    preserved = 1 if desired_action and version != row.current_version else 0
    exact = 1 if desired_action and version == desired else 0
    companions = 1 if not desired_action and version != row.current_version else 0
    movement = 0
    cur = safe_version(row.current_version)
    resolved = safe_version(version)
    if cur is not None and resolved is not None:
        movement += abs(int(resolved.major) - int(cur.major)) * 1_000_000
        movement += abs(int(resolved.minor) - int(cur.minor)) * 1_000
        movement += abs(int(resolved.patch) - int(cur.patch))
    elif version != row.current_version:
        movement = 1_000_000_000
    return critical, high, stable, preserved, exact, -rank, -companions, -movement


def _assignment_score(
    component: List[str],
    assignment: Dict[str, str],
    rows_by_name: Dict[str, DependencyRow],
    mode: str,
    domain_rank: Dict[str, Dict[str, int]],
    stability_targets: Optional[Dict[str, str]] = None,
) -> Tuple[int, int, int, int, int, int, int, int]:
    total = [0, 0, 0, 0, 0, 0, 0, 0]
    stability_targets = stability_targets or {}
    for name in component:
        row = rows_by_name[name]
        version = assignment[name]
        contribution = _score_version_contribution(
            row, version, mode, domain_rank[name].get(version, len(domain_rank[name]) + 1000),
            stability_targets.get(name, ""),
        )
        for index, value in enumerate(contribution):
            total[index] += value
    return tuple(total)  # type: ignore[return-value]




def _build_peer_optimization_model(
    component: List[str],
    rows_by_name: Dict[str, DependencyRow],
    domains: Dict[str, List[str]],
    client: LiveDataClient,
    mode: str,
    learned_nogoods: Optional[List[Dict[str, str]]] = None,
    stability_targets: Optional[Dict[str, str]] = None,
) -> PeerOptimizationModel:
    """Extract one peer component into a solver-neutral finite-domain IR.

    The IR keeps peer ranges as compact implication clauses and stores unary
    environment/missing-peer exclusions plus arbitrary n-way learned nogoods as
    forbidden exact conjunctions.  Registry artifact
    reachability remains a lazy oracle and is refined by the shadow runner so
    model extraction itself does not probe every historical candidate.
    """
    component_set = set(component)
    domain_rank = {
        name: {version: index for index, version in enumerate(domains[name])}
        for name in component
    }
    packages: List[PackageVariable] = []
    for name in sorted(component):
        row = rows_by_name[name]
        scores = tuple(
            (
                version,
                _score_version_contribution(
                    row,
                    version,
                    mode,
                    domain_rank[name].get(version, len(domain_rank[name]) + 1000),
                    (stability_targets or {}).get(name, ""),
                ),
            )
            for version in domains[name]
        )
        packages.append(
            PackageVariable(
                name=name,
                current_version=row.current_version,
                domain=tuple(domains[name]),
                scores=scores,
            )
        )

    constraints: List[ForbiddenCombination] = []
    requirements: List[RequiresAny] = []
    seen_constraints: Set[Tuple[Tuple[Tuple[str, str], ...], str, str]] = set()
    seen_requirements: Set[Tuple[Tuple[str, str], str, Tuple[str, ...], str, str]] = set()

    def add_constraint(
        literals: Iterable[Tuple[str, str]],
        *,
        reason: str,
        provenance: str,
    ) -> None:
        constraint = forbidden(literals, reason=reason, provenance=provenance)
        key = (constraint.literals, constraint.reason, constraint.provenance)
        if key in seen_constraints:
            return
        seen_constraints.add(key)
        constraints.append(constraint)

    def add_requirement(
        trigger: Tuple[str, str],
        provider: str,
        allowed_versions: Iterable[str],
        *,
        reason: str,
        provenance: str,
    ) -> None:
        allowed = tuple(dict.fromkeys(str(version) for version in allowed_versions))
        requirement = RequiresAny(
            trigger=(str(trigger[0]), str(trigger[1])),
            provider=str(provider),
            allowed_versions=allowed,
            reason=reason,
            provenance=provenance,
        )
        key = (requirement.trigger, requirement.provider, requirement.allowed_versions, reason, provenance)
        if key in seen_requirements:
            return
        seen_requirements.add(key)
        requirements.append(requirement)

    for intent_name in sorted(component):
        intent_row = rows_by_name[intent_name]
        intent_policy = _baseline_intent_policy(intent_name)
        if intent_policy == "required" and intent_row.current_version in domains.get(intent_name, []):
            add_constraint(
                [(intent_name, intent_row.current_version)],
                reason=f"USER_BASELINE_REQUIRED_UPDATE: {intent_name} must move away from current {intent_row.current_version}",
                provenance="user-baseline-intent",
            )
        elif intent_policy == "keep-current":
            for intent_version in domains.get(intent_name, []):
                if intent_version == intent_row.current_version:
                    continue
                add_constraint(
                    [(intent_name, intent_version)],
                    reason=f"USER_BASELINE_KEEP_CURRENT: {intent_name} must remain {intent_row.current_version}",
                    provenance="user-baseline-intent",
                )

    for nogood in learned_nogoods or []:
        if not nogood or not set(nogood).issubset(component_set):
            continue
        literals = []
        reachable = True
        for name, version in sorted(nogood.items()):
            if version not in domains.get(name, []):
                reachable = False
                break
            literals.append((name, version))
        if reachable:
            detail = ", ".join(f"{name}@{version}" for name, version in literals)
            add_constraint(
                literals,
                reason=f"LEARNED_CONSTRAINT: verified incompatible combination: {detail}",
                provenance="learned-nogood",
            )

    for source_name in sorted(component):
        source = rows_by_name[source_name]
        for source_version in domains[source_name]:
            source_moved = source_version != source.current_version
            if source_moved:
                environment_issue = _project_environment_constraint_issue(source, source_version, client)
                if environment_issue:
                    add_constraint(
                        [(source_name, source_version)],
                        reason=environment_issue,
                        provenance="project-environment",
                    )

            for peer_name, spec, optional in _peer_entries(source, source_version, client):
                peer = rows_by_name.get(peer_name)
                if peer is None:
                    if source_moved and not optional:
                        add_constraint(
                            [(source_name, source_version)],
                            reason=(
                                f"MISSING_REQUIRED_PEER: {source.name}@{source_version} requires {peer_name}@{spec}; "
                                "automatic addition of new package names is not enabled by current project policy"
                            ),
                            provenance="required-peer",
                        )
                    continue
                if peer_name not in component_set:
                    continue
                allowed_versions = [
                    peer_version
                    for peer_version in domains[peer_name]
                    if _npm_peer_satisfied(spec, peer_version)
                ]
                if source_moved:
                    add_requirement(
                        (source_name, source_version),
                        peer_name,
                        allowed_versions,
                        reason=(
                            f"PEER_CONFLICT: {source.name}@{source_version} requires "
                            f"{peer.name}@{spec}"
                        ),
                        provenance="peer-range",
                    )
                    continue

                # Existing current/current mismatches are grandfathered, but if
                # the peer itself moves then the current source's peer range
                # becomes active.  This special case remains a compact binary
                # nogood because the source may also move away from current.
                for peer_version in domains[peer_name]:
                    if peer_version == peer.current_version or _npm_peer_satisfied(spec, peer_version):
                        continue
                    add_constraint(
                        [(source_name, source_version), (peer_name, peer_version)],
                        reason=(
                            f"PEER_CONFLICT: {source.name}@{source_version} requires "
                            f"{peer.name}@{spec}, resolved {peer_version}"
                        ),
                        provenance="peer-range-current-source",
                    )

    return PeerOptimizationModel(
        packages=tuple(packages),
        constraints=tuple(constraints),
        requirements=tuple(requirements),
        objective_width=8,
    )


def _peer_solver_backend_options(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize custom/exact backend and diagnostic comparison settings."""
    config = dict(raw or {})
    requested_authoritative = str(
        config.get("solverBackend")
        or config.get("authoritativeSolver")
        or config.get("authoritative_solver")
        or "z3"
    ).strip().lower()
    if requested_authoritative in {"", "legacy", "heuristic"}:
        requested_authoritative = "custom"
    reference_only = as_bool(config.get("referenceOnly"), False)
    authority_override = ""
    if requested_authoritative == "custom" and not reference_only:
        # Production dependency decisions are exact-Z3 only. Keep the custom
        # solver available for explicit micrograph/reference tests, but never
        # let a legacy workspace setting silently restore heuristic authority.
        authoritative = "z3"
        authority_override = "custom->z3"
    elif requested_authoritative in {"custom", "z3"}:
        authoritative = requested_authoritative
    else:
        authoritative = "invalid"

    shadow = str(config.get("shadowSolver") or "off").strip().lower()
    if shadow in {"", "false", "none", "disabled"}:
        shadow = "off"

    timeout_ms = int(config.get("timeoutMs") or config.get("shadowSolverTimeoutMs") or 30_000)
    max_refinements = int(config.get("maxRefinements") or config.get("shadowSolverMaxRefinements") or 32)
    min_component_size = int(config.get("minComponentSize") or config.get("shadowSolverMinComponentSize") or 1)
    compare_legacy = as_bool(config.get("compareLegacySolver"), False)
    legacy_max_component_size = as_int(config.get("legacyComparatorMaxComponentSize"), 8)
    persistent_learning = as_bool(config.get("persistentLearning"), True)
    reproduce_count = as_int(config.get("learningReproductions"), 2)
    return {
        "authoritative": authoritative,
        "authorityOverride": authority_override,
        "referenceOnly": bool(reference_only),
        "shadow": shadow,
        "timeoutMs": max(100, min(timeout_ms, 600_000)),
        "maxRefinements": max(0, min(max_refinements, 512)),
        "minComponentSize": max(1, min(min_component_size, 10_000)),
        "compareLegacySolver": bool(compare_legacy),
        "legacyComparatorMaxComponentSize": max(1, min(legacy_max_component_size, 10_000)),
        "persistentLearning": bool(persistent_learning),
        "learningReproductions": max(2, min(reproduce_count, 5)),
    }


def _shadow_solver_options(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Backward-compatible shadow-only view used by older tests/callers."""
    options = _peer_solver_backend_options(raw)
    return {
        "backend": options["shadow"],
        "timeoutMs": options["timeoutMs"],
        "maxRefinements": options["maxRefinements"],
        "minComponentSize": options["minComponentSize"],
    }


def _run_z3_peer_component(
    component: List[str],
    rows_by_name: Dict[str, DependencyRow],
    domains: Dict[str, List[str]],
    client: LiveDataClient,
    mode: str,
    learned_nogoods: Optional[List[Dict[str, str]]],
    raw_config: Optional[Dict[str, Any]],
    stability_targets: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Solve one component exactly and refine registry-unavailable candidates.

    The returned assignment is authoritative only when the caller explicitly
    configured ``solverBackend=z3``.  No fallback to the heuristic solver is
    performed here: exact ``unknown`` is a distinct planner state.
    """
    options = _peer_solver_backend_options(raw_config)
    model = _build_peer_optimization_model(
        component,
        rows_by_name,
        domains,
        client,
        mode,
        learned_nogoods,
        stability_targets,
    )
    refinements = 0
    total_elapsed_ms = 0
    result = solve_z3_exact(model, timeout_ms=options["timeoutMs"])
    total_elapsed_ms += int(result.elapsed_ms)

    while result.status == "optimal" and result.assignment is not None:
        unavailable: List[ForbiddenCombination] = []
        for name in sorted(component):
            row = rows_by_name[name]
            version = result.assignment[name]
            if version == row.current_version:
                continue
            if _candidate_registry_installable(
                row,
                version,
                client,
                trusted_target=getattr(row, _target_attr(mode)),
            ):
                continue
            unavailable.append(
                forbidden(
                    [(name, version)],
                    reason=f"REGISTRY_UNAVAILABLE: {name}@{version}",
                    provenance="registry-oracle",
                )
            )
        if not unavailable:
            break
        if refinements >= options["maxRefinements"]:
            return {
                "backend": "z3",
                "status": "unknown_refinement_budget",
                "detail": f"registry refinement budget {options['maxRefinements']} exhausted",
                "refinements": refinements,
                "elapsedMs": total_elapsed_ms,
                "variables": len(model.packages),
                "candidates": model.candidate_count(),
                "hardConstraints": len(model.constraints) + len(model.requirements),
                "stateUpperBound": str(model.state_count_upper_bound()),
            }
        model = model.with_constraints(unavailable)
        refinements += 1
        result = solve_z3_exact(model, timeout_ms=options["timeoutMs"])
        total_elapsed_ms += int(result.elapsed_ms)

    report: Dict[str, Any] = {
        "backend": "z3",
        "status": result.status,
        "detail": result.detail,
        "refinements": refinements,
        "elapsedMs": total_elapsed_ms,
        "variables": len(model.packages),
        "candidates": model.candidate_count(),
        "hardConstraints": len(model.constraints) + len(model.requirements),
        "stateUpperBound": str(model.state_count_upper_bound()),
    }
    if result.assignment is not None:
        report.update(
            assignment=dict(result.assignment),
            score=list(model.assignment_score(result.assignment)),
            changed=sum(
                result.assignment[name] != rows_by_name[name].current_version
                for name in component
            ),
        )
    return report


def _run_shadow_peer_component(
    component: List[str],
    production_assignment: Optional[Dict[str, str]],
    rows_by_name: Dict[str, DependencyRow],
    domains: Dict[str, List[str]],
    client: LiveDataClient,
    mode: str,
    learned_nogoods: Optional[List[Dict[str, str]]],
    raw_config: Optional[Dict[str, Any]],
    stability_targets: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run exact comparison without ever changing the production assignment."""
    options = _shadow_solver_options(raw_config)
    backend = options["backend"]
    if backend == "off":
        return {"backend": "off", "status": "disabled"}
    if len(component) < options["minComponentSize"]:
        return {"backend": backend, "status": "skipped-small-component"}
    if backend != "z3":
        return {
            "backend": backend,
            "status": "error",
            "detail": f"unsupported shadow solver backend: {backend}",
        }

    report = _run_z3_peer_component(
        component,
        rows_by_name,
        domains,
        client,
        mode,
        learned_nogoods,
        raw_config,
        stability_targets,
    )
    if report.get("status") != "optimal" or not isinstance(report.get("assignment"), dict):
        return report

    shadow_assignment = dict(report["assignment"])
    shadow_changed = int(report.get("changed") or 0)
    # Running shadow before the legacy solver is deliberate: a pathological
    # heuristic search must not hide the exact answer from diagnostics.
    if production_assignment is None:
        report.update(
            shadowChanged=shadow_changed,
            sameAssignment=None,
            objectiveRelation="pending-production",
        )
        return report

    model = _build_peer_optimization_model(
        component,
        rows_by_name,
        domains,
        client,
        mode,
        learned_nogoods,
        stability_targets,
    )
    production_score = model.assignment_score(production_assignment)
    shadow_score = model.assignment_score(shadow_assignment)
    relation = "equal"
    if shadow_score > production_score:
        relation = "better"
    elif shadow_score < production_score:
        relation = "worse"
    production_changed = sum(
        production_assignment[name] != rows_by_name[name].current_version
        for name in component
    )
    report.update(
        productionScore=list(production_score),
        objectiveRelation=relation,
        sameAssignment=shadow_assignment == production_assignment,
        productionChanged=production_changed,
        shadowChanged=shadow_changed,
    )
    return report

def _complete_assignment_constraint_detail(
    component: List[str],
    assignment: Dict[str, str],
    rows_by_name: Dict[str, DependencyRow],
    client: LiveDataClient,
    learned_nogoods: Optional[List[Dict[str, str]]] = None,
) -> Tuple[str, Set[str]]:
    """Return the first hard constraint violation and the packages that can repair it.

    This helper is intentionally complete-assignment only. It powers the large
    component solver with conflict-directed search: every invalid state names the
    smallest set of packages whose version choice can make progress.
    """
    component_set = set(component)
    for nogood in learned_nogoods or []:
        if assignment_matches_nogood(assignment, nogood):
            involved = set(nogood) & component_set
            detail = ", ".join(f"{name}@{version}" for name, version in sorted(nogood.items()))
            return f"LEARNED_CONSTRAINT: verified incompatible combination: {detail}", involved

    for source_name in sorted(component):
        source = rows_by_name[source_name]
        source_version = assignment[source_name]
        source_moved = source_version != source.current_version
        if source_moved:
            environment_issue = _project_environment_constraint_issue(source, source_version, client)
            if environment_issue:
                return environment_issue, {source_name}
        for peer_name, spec, optional in _peer_entries(source, source_version, client):
            peer = rows_by_name.get(peer_name)
            if peer is None:
                if source_moved and not optional:
                    return (
                        f"MISSING_REQUIRED_PEER: {source.name}@{source_version} requires {peer_name}@{spec}; "
                        "automatic addition of new package names is not enabled by current project policy",
                        {source_name},
                    )
                continue
            if peer_name not in component_set:
                continue
            peer_version = assignment[peer_name]
            peer_moved = peer_version != peer.current_version
            if not (source_moved or peer_moved):
                continue
            if not _npm_peer_satisfied(spec, peer_version):
                return (
                    f"PEER_CONFLICT: {source.name}@{source_version} requires {peer.name}@{spec}, "
                    f"resolved {peer_version}",
                    {source_name, peer_name},
                )
    return "", set()


def _solve_large_peer_component(
    component: List[str],
    rows_by_name: Dict[str, DependencyRow],
    domains: Dict[str, List[str]],
    graph: Dict[str, Set[str]],
    client: LiveDataClient,
    mode: str,
    learned_nogoods: Optional[List[Dict[str, str]]] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
    stability_targets: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Conflict-directed best-first solver for large peer components.

    The previous implementation built the assignment left-to-right and retained
    only a tiny beam. Coordinated upgrades can look locally worse until *all*
    members of an ecosystem move, so the beam routinely discarded the only good
    path and fell back to current versions. Here we start from the independently
    best complete assignment and repair only packages participating in a proven
    conflict. Because every valid assignment must change at least one package in
    each encountered conflict set, this explores useful states before unrelated
    combinations and makes large toolchain upgrades tractable.
    """
    search_domains: Dict[str, List[str]] = {}
    for name in component:
        row = rows_by_name[name]
        domain = domains[name]
        anchors = [
            _desired_target_for_mode(row, mode),
            row.min_no_critical,
            row.min_no_high,
            lag_compliance_target_for_row(row),
            row.current_version,
        ]
        selected = [version for version in anchors if version in domain]
        selected.extend(version for version in domain if version not in selected)
        compact = list(dict.fromkeys(selected))[:PEER_SOLVER_LARGE_DOMAIN]
        if row.current_version not in compact:
            if compact:
                compact[-1] = row.current_version
            else:
                compact = [row.current_version]
        search_domains[name] = list(dict.fromkeys(compact))

    # Score against the complete domain.  A version that is outside the compact
    # seed set must not become artificially unattractive merely because it was
    # not selected for the first wave of search.
    domain_rank = {
        name: {version: index for index, version in enumerate(domains[name])}
        for name in component
    }
    installable_cache: Dict[Tuple[str, str], bool] = {}

    def installable(name: str, version: str) -> bool:
        key = (name, version)
        if key not in installable_cache:
            row = rows_by_name[name]
            installable_cache[key] = (
                version == row.current_version
                or _candidate_registry_installable(
                    row, version, client, trusted_target=getattr(row, _target_attr(mode))
                )
            )
        return installable_cache[key]

    def assignment_key(assignment: Dict[str, str]) -> Tuple[int, int, int, int, int, int, int, int]:
        return _assignment_score(component, assignment, rows_by_name, mode, domain_rank, stability_targets)

    # This is the theoretical package-local optimum before peer/environment
    # constraints. Starting here is crucial: coordinated migrations are repaired
    # downward instead of being constructed through locally-invalid prefixes.
    seed: Dict[str, str] = {}
    for name in component:
        row = rows_by_name[name]
        seed[name] = max(
            search_domains[name],
            key=lambda version: _score_version_contribution(
                row, version, mode, domain_rank[name].get(version, 10_000),
                (stability_targets or {}).get(name, ""),
            ),
        )

    current_assignment = {name: rows_by_name[name].current_version for name in component}
    # Heap is max-by-score via negated tuple; deterministic assignment tuple is a
    # secondary key. A monotonically increasing counter avoids dict comparisons.
    heap: List[Tuple[Tuple[int, ...], Tuple[Tuple[str, str], ...], int, Dict[str, str]]] = []
    queued: Set[Tuple[Tuple[str, str], ...]] = set()
    visited: Set[Tuple[Tuple[str, str], ...]] = set()
    counter = 0

    def push(candidate: Dict[str, str]) -> None:
        nonlocal counter
        frozen = tuple((name, candidate[name]) for name in sorted(component))
        if frozen in queued or frozen in visited:
            return
        score = assignment_key(candidate)
        priority = tuple(-value for value in score)
        counter += 1
        heapq.heappush(heap, (priority, frozen, counter, candidate))
        queued.add(frozen)

    push(seed)
    # Keep the current state directly reachable even if the search budget is
    # exhausted. Existing current/current peer inconsistencies are grandfathered.
    fallback = dict(current_assignment)
    visits = 0

    while heap and visits < PEER_SOLVER_LARGE_MAX_VISITS:
        _priority, frozen, _counter, assignment = heapq.heappop(heap)
        queued.discard(frozen)
        if frozen in visited:
            continue
        visited.add(frozen)
        visits += 1

        # Registry reachability is a unary hard constraint. Probe lazily only for
        # versions actually reached by the conflict-directed search.
        unavailable_name = next(
            (name for name in component if not installable(name, assignment[name])),
            None,
        )
        if unavailable_name is not None:
            issue = f"REGISTRY_UNAVAILABLE: {unavailable_name}@{assignment[unavailable_name]}"
            involved = {unavailable_name}
        else:
            issue, involved = _complete_assignment_constraint_detail(
                component, assignment, rows_by_name, client, learned_nogoods
            )

        if not issue:
            if diagnostics is not None:
                diagnostics.update(status="optimal", states=visits, probes=len(installable_cache))
            eprint(
                f"[info] peer solver {mode} large component solved: "
                f"packages={len(component)}, states={visits}, probes={len(installable_cache)}"
            )
            return assignment

        # Any valid descendant must alter at least one member of the conflict.
        # Generate *all* eligible values for conflict members, not merely the
        # compact seed set.  The old PEER_SOLVER_LARGE_DOMAIN cap could remove
        # the only compatible interior version and silently turn SAT into a
        # false deferred/current result.
        for name in sorted(involved):
            current_value = assignment[name]
            for version in domains[name]:
                if version == current_value:
                    continue
                candidate = dict(assignment)
                candidate[name] = version
                push(candidate)

        if visits % 2_000 == 0:
            eprint(
                f"[info] peer solver {mode} large component progress: "
                f"states={visits}, queued={len(heap)}, probes={len(installable_cache)}, "
                f"last={issue[:180]}"
            )

    if visits >= PEER_SOLVER_LARGE_MAX_VISITS:
        if diagnostics is not None:
            diagnostics.update(status="unknown_budget", states=visits, probes=len(installable_cache))
        eprint(
            f"[warn] peer solver {mode} large component UNKNOWN_BUDGET after "
            f"{PEER_SOLVER_LARGE_MAX_VISITS} conflict-directed states; using current assignment without UNSAT proof"
        )
    else:
        if diagnostics is not None:
            diagnostics.update(status="unsat", states=visits, probes=len(installable_cache))
        eprint(f"[warn] peer solver {mode} large component has no compatible candidate; using current assignment")
    return fallback

def _solve_peer_component(
    component: List[str],
    rows_by_name: Dict[str, DependencyRow],
    domains: Dict[str, List[str]],
    graph: Dict[str, Set[str]],
    client: LiveDataClient,
    mode: str,
    learned_nogoods: Optional[List[Dict[str, str]]] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
    stability_targets: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    if len(component) > PEER_SOLVER_EXACT_COMPONENT_SIZE:
        return _solve_large_peer_component(
            component, rows_by_name, domains, graph, client, mode, learned_nogoods, diagnostics, stability_targets
        )
    component_set = set(component)
    order = sorted(
        component,
        key=lambda name: (
            0 if target_is_action(_desired_target_for_mode(rows_by_name[name], mode)) else 1,
            -len(graph.get(name, set())),
            name,
        ),
    )
    domain_rank = {name: {version: index for index, version in enumerate(domains[name])} for name in component}
    max_contribution: Dict[str, Tuple[int, int, int, int, int, int, int, int]] = {}
    for name in component:
        row = rows_by_name[name]
        contributions = [
            _score_version_contribution(
                row, version, mode, domain_rank[name][version],
                (stability_targets or {}).get(name, ""),
            )
            for version in domains[name]
        ]
        max_contribution[name] = tuple(
            max(item[index] for item in contributions) for index in range(8)
        )  # type: ignore[assignment]

    assignment: Dict[str, str] = {}
    current_assignment = {name: rows_by_name[name].current_version for name in component}
    best = dict(current_assignment)
    best_score = _assignment_score(component, current_assignment, rows_by_name, mode, domain_rank, stability_targets)
    best_key = tuple(sorted(current_assignment.items()))
    visits = 0
    budget_exhausted = False

    def optimistic_bound() -> Tuple[int, int, int, int, int, int, int, int]:
        total = [0, 0, 0, 0, 0, 0, 0, 0]
        for candidate_name in component:
            if candidate_name in assignment:
                candidate_row = rows_by_name[candidate_name]
                candidate_version = assignment[candidate_name]
                contribution = _score_version_contribution(
                    candidate_row,
                    candidate_version,
                    mode,
                    domain_rank[candidate_name][candidate_version],
                    (stability_targets or {}).get(candidate_name, ""),
                )
            else:
                contribution = max_contribution[candidate_name]
            for score_index, value in enumerate(contribution):
                total[score_index] += value
        return tuple(total)  # type: ignore[return-value]

    def visit(index: int) -> None:
        nonlocal best, best_score, best_key, visits, budget_exhausted
        if budget_exhausted:
            return
        visits += 1
        if visits > PEER_SOLVER_MAX_VISITS:
            budget_exhausted = True
            return
        if optimistic_bound() < best_score:
            return
        if index >= len(order):
            issue = _assignment_constraint_issue(
                component_set, assignment, rows_by_name, client, partial=False, domains=domains, learned_nogoods=learned_nogoods
            )
            if issue:
                return
            score = _assignment_score(component, assignment, rows_by_name, mode, domain_rank, stability_targets)
            key = tuple((name, assignment[name]) for name in sorted(component))
            if score > best_score or (score == best_score and key < best_key):
                best = dict(assignment)
                best_score = score
                best_key = key
            return

        name = order[index]
        row = rows_by_name[name]
        for version in domains[name]:
            if budget_exhausted:
                break
            assignment[name] = version
            if optimistic_bound() < best_score:
                assignment.pop(name, None)
                continue
            trusted_target = getattr(row, _target_attr(mode))
            if version != row.current_version and not _candidate_registry_installable(
                row, version, client, trusted_target=trusted_target
            ):
                assignment.pop(name, None)
                continue
            issue = _assignment_constraint_issue(
                component_set, assignment, rows_by_name, client, partial=True, domains=domains, learned_nogoods=learned_nogoods
            )
            if not issue:
                visit(index + 1)
            assignment.pop(name, None)

    visit(0)
    if budget_exhausted:
        if diagnostics is not None:
            diagnostics.update(status="sat_unproven", states=visits)
        eprint(
            f"[warn] peer solver budget reached for {mode} component "
            f"{', '.join(component)}; using best valid assignment after {PEER_SOLVER_MAX_VISITS} states"
        )
    elif diagnostics is not None:
        diagnostics.update(status="optimal", states=visits)
    return best

def _actual_peer_component_graph(
    rows_by_name: Dict[str, DependencyRow],
    assignment: Dict[str, str],
    client: LiveDataClient,
) -> Dict[str, Set[str]]:
    graph: Dict[str, Set[str]] = {name: set() for name in rows_by_name}
    for source_name, source_version in assignment.items():
        source = rows_by_name[source_name]
        source_moved = source_version != source.current_version
        for peer_name, _spec, _optional in _peer_entries(source, source_version, client):
            peer = rows_by_name.get(peer_name)
            if peer is None:
                continue
            peer_version = assignment.get(peer_name, peer.current_version)
            peer_moved = peer_version != peer.current_version
            if not (source_moved or peer_moved):
                continue
            graph[source_name].add(peer_name)
            graph[peer_name].add(source_name)
    return graph


def _cohort_name_for_component(names: List[str], rows_by_name: Dict[str, DependencyRow]) -> str:
    existing = sorted({rows_by_name[name].compatibility_cohort for name in names if rows_by_name[name].compatibility_cohort})
    if len(existing) == 1:
        return existing[0]
    payload = "\n".join(sorted(names))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    readable = re.sub(r"[^a-zA-Z0-9._-]+", "-", "-".join(sorted(names)[:3])).strip("-").lower()
    return f"peer-{readable[:48]}-{digest}" if readable else f"peer-{digest}"


def _resolution_change_reason(
    row: DependencyRow,
    desired: str,
    resolved: str,
    assignment: Dict[str, str],
    rows_by_name: Dict[str, DependencyRow],
    client: LiveDataClient,
    mode: str,
    solver_status: str = "optimal",
) -> str:
    # Resolution diagnostics describe a deviation from policy intent. Keeping
    # the exact desired version is a successful resolution, not a fallback.
    intent_policy = _baseline_intent_policy(row.name)
    if intent_policy == "keep-current" and resolved == row.current_version:
        return f"USER_BASELINE_KEEP_CURRENT: kept {row.name}@{row.current_version} by explicit Baseline intent"
    if intent_policy == "required" and resolved == row.current_version:
        return f"USER_BASELINE_REQUIRED_UPDATE_UNSATISFIED: {row.name} remained at current {row.current_version}"
    if target_is_action(desired) and resolved == desired:
        return ""
    if target_is_action(desired) and desired != row.current_version:
        if solver_status == "unknown_budget":
            return (
                f"PEER_SOLVER_UNKNOWN_BUDGET: desired={desired}; resolved={resolved}; "
                "search budget was exhausted, so incompatibility was not proven"
            )
        if solver_status == "sat_unproven":
            return (
                f"PEER_SOLVER_SAT_UNPROVEN: desired={desired}; resolved={resolved}; "
                "a valid assignment was found, but optimality was not proven within the search budget"
            )
        if solver_status.startswith("exact_") and solver_status.endswith("_deferred"):
            exact_reason = solver_status[len("exact_"):-len("_deferred")].upper()
            return (
                f"PEER_SOLVER_EXACT_DEFERRED: desired={desired}; resolved=current {resolved}; "
                f"exact solver status={exact_reason}; this component was locally deferred without heuristic fallback"
            )
        trusted_target = getattr(row, _target_attr(mode))
        if desired != row.current_version and not _candidate_registry_installable(
            row, desired, client, trusted_target=trusted_target
        ):
            base = f"desired {desired} is unavailable in configured registry"
        else:
            desired_meta = _package_version_metadata(row, desired, client)
            peers = _peer_entries(row, desired, client) if isinstance(desired_meta, dict) else []
            base = _project_environment_constraint_issue(row, desired, client)
            for peer_name, spec, optional in peers:
                if base:
                    break
                peer = rows_by_name.get(peer_name)
                if peer is None and not optional:
                    base = f"{row.name}@{desired} requires missing peer {peer_name}@{spec}; new package-name additions are disabled"
                    break
                if peer is not None:
                    peer_version = assignment.get(peer_name, peer.current_version)
                    if not _npm_peer_satisfied(spec, peer_version):
                        base = f"{row.name}@{desired} requires {peer_name}@{spec}, but compatible assignment uses {peer_version}"
                        break
            if not base:
                reverse_conflicts: List[str] = []
                for source_name, source in rows_by_name.items():
                    if source_name == row.name:
                        continue
                    source_version = assignment.get(source_name, source.current_version)
                    for peer_name, spec, optional in _peer_entries(source, source_version, client):
                        if peer_name != row.name:
                            continue
                        if optional and not target_is_action(desired) and desired == row.current_version:
                            continue
                        if not _npm_peer_satisfied(spec, desired):
                            reverse_conflicts.append(
                                f"{source.name}@{source_version} requires {row.name}@{spec}, incompatible with desired {desired}"
                            )
                base = "; ".join(sorted(set(reverse_conflicts))) if reverse_conflicts else (
                    "no compatible assignment exists for the desired version under final peer/registry constraints"
                )
        if resolved == row.current_version:
            return f"PEER_RESOLUTION_DEFERRED: desired={desired}; resolved=current {resolved}; {base}"
        return f"PEER_RESOLUTION_FALLBACK: desired={desired}; resolved={resolved}; {base}"

    if resolved != row.current_version:
        requiring: List[str] = []
        for source_name, source in rows_by_name.items():
            source_version = assignment.get(source_name, source.current_version)
            for peer_name, spec, _optional in _peer_entries(source, source_version, client):
                if peer_name != row.name:
                    continue
                if _npm_peer_satisfied(spec, resolved) and not _npm_peer_satisfied(spec, row.current_version):
                    requiring.append(f"{source.name}@{source_version} requires {row.name}@{spec}")
        detail = "; ".join(sorted(set(requiring))) or "required by peer compatibility component"
        return f"PEER_COMPANION: desired=current {row.current_version}; resolved={resolved}; {detail}"
    return ""




def _coordinate_solver_global_exclusions(
    components: List[List[str]],
    initial_assignment: Dict[str, str],
    exact_exclusions: List[Dict[str, str]],
    rows_by_name: Dict[str, DependencyRow],
    domains: Dict[str, List[str]],
    client: LiveDataClient,
    mode: str,
    learned_nogoods: List[Dict[str, str]],
    solver_config: Optional[Dict[str, Any]],
    stability_targets: Optional[Dict[str, str]],
) -> Dict[str, str]:
    """Apply full-assignment exclusions without changing component topology."""
    if not exact_exclusions:
        return initial_assignment
    if not any(
        assignment_matches_nogood(initial_assignment, exclusion)
        for exclusion in exact_exclusions
    ):
        return initial_assignment

    ranked_initial: List[RankedComponentAlternative] = []
    models = []
    for component in components:
        model = _build_peer_optimization_model(
            component,
            rows_by_name,
            domains,
            client,
            mode,
            learned_nogoods,
            stability_targets,
        )
        models.append(model)
        local_assignment = {
            name: initial_assignment[name]
            for name in component
        }
        ranked_initial.append(
            RankedComponentAlternative(
                assignment=local_assignment,
                score=tuple(model.assignment_score(local_assignment)),
            )
        )

    def next_alternative(
        component_index: int,
        existing: Tuple[Mapping[str, str], ...],
    ) -> Optional[RankedComponentAlternative]:
        component = components[component_index]
        # These exact local exclusions exist only inside the ranked alternative
        # generator. They are navigation state, never learned solver authority.
        temporary_nogoods = list(learned_nogoods)
        temporary_nogoods.extend(dict(item) for item in existing)
        report = _run_z3_peer_component(
            component,
            rows_by_name,
            domains,
            client,
            mode,
            temporary_nogoods,
            solver_config,
            stability_targets,
        )
        status = str(report.get("status") or "")
        candidate = report.get("assignment")
        if status == "optimal" and isinstance(candidate, dict):
            candidate_assignment = {
                name: str(candidate[name])
                for name in component
            }
            return RankedComponentAlternative(
                assignment=candidate_assignment,
                score=tuple(models[component_index].assignment_score(candidate_assignment)),
            )
        if status == "unsat":
            return None
        reason = (
            "solver-unavailable"
            if status == "unavailable"
            else (
                "budget-exhausted"
                if status == "unknown_refinement_budget"
                else "solver-unknown"
            )
        )
        raise GlobalExactExclusionError(
            f"component {component_index} alternative solve returned {status or 'error'}: "
            f"{str(report.get('detail') or '')[:240]}",
            reason=reason,
        )

    def coordinator_progress(event: str, details: Mapping[str, object]) -> None:
        if event in {"rejected-global-exact", "component-alternative", "accepted"}:
            detail = ", ".join(
                f"{key}={value}" for key, value in sorted(details.items())
            )
            eprint(
                f"[info] global exact coordinator {mode} {event}; {detail}"
            )

    assignment, explored = coordinate_global_exact_exclusions(
        ranked_initial,
        exact_exclusions,
        next_alternative,
        max_states=4096,
        progress=coordinator_progress,
    )
    eprint(
        f"[info] global exact coordinator {mode} resolved "
        f"{len(exact_exclusions)} exclusion(s) across {len(components)} "
        f"independent component(s); exploredStates={explored}"
    )
    return assignment


def resolve_peer_compatibility(
    rows_by_project: Dict[str, List[DependencyRow]],
    client: LiveDataClient,
    *,
    modes: Tuple[str, ...] = ("yellow", "green", "default"),
    learned_nogoods_by_project_mode: Optional[Dict[str, Dict[str, List[Dict[str, str]]]]] = None,
    global_exact_exclusions_by_project_mode: Optional[Dict[str, Dict[str, List[Dict[str, str]]]]] = None,
    apply_results: bool = True,
    solver_statuses_out: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
    shadow_solver_config_by_project: Optional[Dict[str, Dict[str, Any]]] = None,
    shadow_reports_out: Optional[Dict[str, Dict[str, List[Dict[str, Any]]]]] = None,
    residual_targets_by_project: Optional[Dict[str, Dict[str, str]]] = None,
    diagnostic_preferences_by_project_mode: Optional[Dict[str, Dict[str, Dict[str, str]]]] = None,
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Resolve peer-connected package versions as constraints, never by display group.

    Candidate metadata is version-specific, components may cross any number of
    display groups/subgroups, and every mode is solved from the frozen desired
    target to a deterministic registry-backed assignment.  ``target_*`` becomes
    the resolved target; group/subgroup are never read or mutated here.
    """
    assignments_by_project: Dict[str, Dict[str, Dict[str, str]]] = {}
    for project, rows in rows_by_project.items():
        assignments_by_project[project] = {}
        # Direct package names are the compatibility identity. If a manifest
        # declares the same package in multiple sections, solve one installed
        # version and mirror it to every row afterwards.
        rows_for_name: Dict[str, List[DependencyRow]] = defaultdict(list)
        for row in rows:
            rows_for_name[row.name].append(row)
        rows_by_name: Dict[str, DependencyRow] = {
            name: _aggregate_duplicate_package_row(items)
            for name, items in rows_for_name.items()
        }
        solver_rows_by_name, fixed_rows_by_name = _partition_solver_inputs(rows_by_name)

        residual_targets = dict((residual_targets_by_project or {}).get(project, {}))
        for mode in modes:
            raw_learned_nogoods = ((learned_nogoods_by_project_mode or {}).get(project, {}).get(mode, []))
            raw_global_exact_exclusions = (
                (global_exact_exclusions_by_project_mode or {})
                .get(project, {})
                .get(mode, [])
            )
            learned_nogoods = _project_constraints_over_fixed_inputs(
                raw_learned_nogoods,
                fixed_rows_by_name,
                project=project,
                mode=mode,
                source="learned-nogood",
            )
            global_exact_exclusions = _project_constraints_over_fixed_inputs(
                raw_global_exact_exclusions,
                fixed_rows_by_name,
                project=project,
                mode=mode,
                source="global-exact-exclusion",
            )
            domains: Dict[str, List[str]] = {}
            for name, row in solver_rows_by_name.items():
                previous_target = residual_targets.get(name, "")
                # If a previously approved target is already the version in the
                # cumulative merged checkout, that package is completed work.
                # Residual solving must never move it again.
                if previous_target and previous_target == row.current_version:
                    domains[name] = [row.current_version]
                else:
                    domains[name] = _candidate_domain(row, mode, client)

            # BLOCK_VF_ACTIVE_PREDICATE_SEARCH_V1
            # Predicate probes may reorder fallback *cost* but never remove
            # a version. Explicit residual/approved targets keep precedence.
            diagnostic_preferences = dict(
                ((diagnostic_preferences_by_project_mode or {})
                 .get(project, {})
                 .get(mode, {}))
            )
            for preferred_name, preferred_version in sorted(diagnostic_preferences.items()):
                if preferred_name in residual_targets:
                    continue
                preferred_row = solver_rows_by_name.get(preferred_name)
                preferred_domain = domains.get(preferred_name)
                if preferred_row is None or not preferred_domain:
                    continue
                reordered = prioritize_probe_preference(
                    preferred_domain,
                    preferred_version=preferred_version,
                    current_version=preferred_row.current_version,
                )
                if reordered == preferred_domain:
                    continue
                domains[preferred_name] = reordered
                eprint(
                    f"[info] {project}: predicate-guided solver preference {mode}; "
                    f"package={preferred_name}, preferred={preferred_version}, "
                    "authority=DIAGNOSTIC_HINT; domain remains complete"
                )

            domains, fixed_peer_stats = _apply_fixed_peer_constant_constraints(
                solver_rows_by_name, fixed_rows_by_name, domains, client,
            )
            graph = _potential_peer_graph(solver_rows_by_name, domains, client)
            merge_nogood_edges(graph, learned_nogoods)
            components = _graph_components(graph)
            eprint(
                f"[info] {project}: peer solver {mode}; packages={len(solver_rows_by_name)}, "
                f"fixedInputs={len(fixed_rows_by_name)}, direct={len(rows_by_name)}, "
                f"components={len(components)}, candidates={sum(len(domain) for domain in domains.values())}, "
                f"fixedPeerChecks={fixed_peer_stats['evaluated']}, "
                f"fixedPeerExcluded={fixed_peer_stats['excluded']}, "
                f"fixedPeerUnknown={fixed_peer_stats['unknown']}"
            )
            assignment: Dict[str, str] = {}
            status_by_name: Dict[str, str] = {}
            solver_config = (shadow_solver_config_by_project or {}).get(project)
            backend_options = _peer_solver_backend_options(solver_config)
            authoritative_backend = backend_options["authoritative"]
            if authoritative_backend == "invalid":
                raise BaselineConstraintVerificationError(
                    f"EXACT_SOLVER_CONFIG_INVALID: {project}/{mode}: solverBackend must be z3; custom is reference-only"
                )
            if backend_options.get("authorityOverride"):
                eprint(
                    f"[warn] {project}: legacy solverBackend=custom ignored for production authority; "
                    "exact Z3 remains authoritative. Use referenceOnly=true only in tests/reference tooling."
                )
            shadow_active = authoritative_backend == "custom" and backend_options["shadow"] != "off"
            mode_shadow_reports: List[Dict[str, Any]] = []
            mode_exact_reports: List[Dict[str, Any]] = []
            for component in components:
                diagnostics: Dict[str, Any] = {}
                pre_shadow_report: Optional[Dict[str, Any]] = None

                # Exact diagnostics run *before* the heuristic solver.  This is
                # deliberate: a 100k-state legacy search must not hide the exact
                # answer or make users wait before seeing whether Z3 solved it.
                if shadow_active:
                    pre_shadow_report = _run_shadow_peer_component(
                        component,
                        None,
                        rows_by_name,
                        domains,
                        client,
                        mode,
                        learned_nogoods,
                        solver_config,
                        residual_targets,
                    )
                    shadow_status = str(pre_shadow_report.get("status") or "")
                    if shadow_status == "unavailable":
                        eprint(
                            f"[warn] {project}: shadow solver z3 unavailable; "
                            "install optional z3-solver to enable exact comparison"
                        )
                        shadow_active = False
                    elif shadow_status == "optimal":
                        eprint(
                            f"[info] {project}: shadow z3 {mode} READY; packages={len(component)}, "
                            f"shadowChanged={pre_shadow_report.get('shadowChanged', 0)}, "
                            f"constraints={pre_shadow_report.get('hardConstraints', 0)}, "
                            f"refinements={pre_shadow_report.get('refinements', 0)}, "
                            f"elapsedMs={pre_shadow_report.get('elapsedMs', 0)}"
                        )
                    elif shadow_status not in {"disabled", "skipped-small-component"}:
                        eprint(
                            f"[warn] {project}: shadow z3 {mode} status={shadow_status}; "
                            f"detail={str(pre_shadow_report.get('detail') or '')[:240]}"
                        )

                if authoritative_backend == "z3":
                    exact_report = _run_z3_peer_component(
                        component,
                        rows_by_name,
                        domains,
                        client,
                        mode,
                        learned_nogoods,
                        solver_config,
                        residual_targets,
                    )
                    exact_status = str(exact_report.get("status") or "")
                    if exact_status == "optimal" and isinstance(exact_report.get("assignment"), dict):
                        component_assignment = dict(exact_report["assignment"])
                        diagnostics.update(status="optimal", backend="z3")
                        emit_observability_event(
                            "solver.component.finish",
                            project=project,
                            mode=mode,
                            packageCount=len(component),
                            changed=int(exact_report.get("changed") or 0),
                            hardConstraintCount=int(exact_report.get("hardConstraints") or 0),
                            refinements=int(exact_report.get("refinements") or 0),
                            durationMs=int(exact_report.get("elapsedMs") or 0),
                            status=exact_status,
                        )
                        eprint(
                            f"[info] {project}: exact z3 {mode}; packages={len(component)}, "
                            f"changed={exact_report.get('changed', 0)}, "
                            f"constraints={exact_report.get('hardConstraints', 0)}, "
                            f"refinements={exact_report.get('refinements', 0)}, "
                            f"elapsedMs={exact_report.get('elapsedMs', 0)}"
                        )
                    elif exact_status == "unsat":
                        raise _baseline_terminal_error(
                            BaselineTerminalStatus.UNSAT_PROVEN,
                            "EXACT_SOLVER_UNSAT_PROVEN",
                            f"{project}/{mode}: component={','.join(component)}; "
                            f"detail={str(exact_report.get('detail') or '')[:500]}; "
                            "the authoritative finite-domain component has no satisfying assignment",
                            source="z3",
                        )
                    elif exact_status == "unknown_refinement_budget":
                        raise _baseline_terminal_error(
                            BaselineTerminalStatus.BUDGET_EXHAUSTED,
                            "EXACT_SOLVER_BUDGET_EXHAUSTED",
                            f"{project}/{mode}: component={','.join(component)}; "
                            f"detail={str(exact_report.get('detail') or '')[:500]}; "
                            "exact refinement budget ended without a proof",
                            source="z3",
                        )
                    elif exact_status in {"unknown", "sat_unproven"}:
                        raise _baseline_terminal_error(
                            BaselineTerminalStatus.SOLVER_UNKNOWN,
                            "EXACT_SOLVER_UNKNOWN",
                            f"{project}/{mode}: component={','.join(component)} status={exact_status}; "
                            f"detail={str(exact_report.get('detail') or '')[:500]}; "
                            "unfinished exact proof is not a dependency decision",
                            source="z3",
                        )
                    elif exact_status == "unavailable":
                        raise _baseline_terminal_error(
                            BaselineTerminalStatus.SOLVER_UNAVAILABLE,
                            "EXACT_SOLVER_UNAVAILABLE",
                            f"{project}/{mode}: component={','.join(component)}; "
                            f"detail={str(exact_report.get('detail') or '')[:500]}; "
                            "no heuristic fallback was used",
                            source="z3",
                        )
                    else:
                        raise _baseline_terminal_error(
                            _terminal_status_for_exact_solver(exact_status),
                            "EXACT_SOLVER_ERROR",
                            f"{project}/{mode}: component={','.join(component)} status={exact_status or 'error'}; "
                            f"detail={str(exact_report.get('detail') or '')[:500]}; "
                            "no heuristic fallback was used",
                            source="z3",
                        )
                    mode_exact_reports.append(dict(exact_report))

                    # The old solver is diagnostic only once exact solving is
                    # authoritative, and never runs on a large component unless
                    # explicitly requested.  It cannot change the chosen plan.
                    if (
                        backend_options["compareLegacySolver"]
                        and len(component) <= backend_options["legacyComparatorMaxComponentSize"]
                    ):
                        legacy_diagnostics: Dict[str, Any] = {}
                        if residual_targets:
                            legacy_assignment = _solve_peer_component(
                                component, rows_by_name, domains, graph, client, mode,
                                learned_nogoods, legacy_diagnostics, residual_targets
                            )
                        else:
                            legacy_assignment = _solve_peer_component(
                                component, rows_by_name, domains, graph, client, mode,
                                learned_nogoods, legacy_diagnostics
                            )
                        model = _build_peer_optimization_model(
                            component, rows_by_name, domains, client, mode,
                            learned_nogoods, residual_targets
                        )
                        relation = "equal"
                        exact_score = model.assignment_score(component_assignment)
                        legacy_score = model.assignment_score(legacy_assignment)
                        if exact_score > legacy_score:
                            relation = "exact-better"
                        elif exact_score < legacy_score:
                            relation = "legacy-better"
                        eprint(
                            f"[info] {project}: legacy comparator {mode}; packages={len(component)}, "
                            f"relation={relation}, sameAssignment={'yes' if legacy_assignment == component_assignment else 'no'}"
                        )
                else:
                    if residual_targets:
                        component_assignment = _solve_peer_component(
                            component, rows_by_name, domains, graph, client, mode,
                            learned_nogoods, diagnostics, residual_targets
                        )
                    else:
                        component_assignment = _solve_peer_component(
                            component, rows_by_name, domains, graph, client, mode,
                            learned_nogoods, diagnostics
                        )

                assignment.update(component_assignment)
                status = str(diagnostics.get("status") or "optimal")
                for name in component:
                    status_by_name[name] = status

                if pre_shadow_report is not None:
                    report = dict(pre_shadow_report)
                    if report.get("status") == "optimal" and isinstance(report.get("assignment"), dict):
                        model = _build_peer_optimization_model(
                            component, rows_by_name, domains, client, mode,
                            learned_nogoods, residual_targets
                        )
                        shadow_assignment = dict(report["assignment"])
                        production_score = model.assignment_score(component_assignment)
                        shadow_score = model.assignment_score(shadow_assignment)
                        relation = "equal"
                        if shadow_score > production_score:
                            relation = "better"
                        elif shadow_score < production_score:
                            relation = "worse"
                        report.update(
                            productionScore=list(production_score),
                            objectiveRelation=relation,
                            sameAssignment=shadow_assignment == component_assignment,
                            productionChanged=sum(
                                component_assignment[name] != rows_by_name[name].current_version
                                for name in component
                            ),
                            shadowChanged=sum(
                                shadow_assignment[name] != rows_by_name[name].current_version
                                for name in component
                            ),
                        )
                        eprint(
                            f"[info] {project}: shadow z3 {mode} COMPARE; packages={len(component)}, "
                            f"relation={relation}, sameAssignment={'yes' if report.get('sameAssignment') else 'no'}, "
                            f"productionChanged={report.get('productionChanged', 0)}, "
                            f"shadowChanged={report.get('shadowChanged', 0)}"
                        )
                    report.update(component=list(component), productionStatus=status)
                    mode_shadow_reports.append(dict(report))
                    if shadow_reports_out is not None:
                        shadow_reports_out.setdefault(project, {}).setdefault(mode, []).append(dict(report))


            if global_exact_exclusions:
                if authoritative_backend != "z3":
                    raise BaselineConstraintVerificationError(
                        f"GLOBAL_EXACT_EXCLUSION_REQUIRES_Z3: {project}/{mode}"
                    )
                try:
                    assignment = _coordinate_solver_global_exclusions(
                        components,
                        assignment,
                        global_exact_exclusions,
                        rows_by_name,
                        domains,
                        client,
                        mode,
                        learned_nogoods,
                        solver_config,
                        residual_targets,
                    )
                except GlobalExactExclusionError as exc:
                    terminal_status = _terminal_status_for_global_exact_reason(exc.reason)
                    stop_code = {
                        BaselineTerminalStatus.UNSAT_PROVEN: "GLOBAL_EXACT_EXCLUSION_UNSAT_PROVEN",
                        BaselineTerminalStatus.BUDGET_EXHAUSTED: "GLOBAL_EXACT_EXCLUSION_BUDGET_EXHAUSTED",
                        BaselineTerminalStatus.SOLVER_UNAVAILABLE: "GLOBAL_EXACT_EXCLUSION_SOLVER_UNAVAILABLE",
                    }.get(terminal_status, "GLOBAL_EXACT_EXCLUSION_SOLVER_UNKNOWN")
                    raise _baseline_terminal_error(
                        terminal_status,
                        stop_code,
                        f"{project}/{mode}: {exc}",
                        source="global-exact-coordinator",
                    ) from None

            if mode_exact_reports:
                exact_changed = sum(int(report.get("changed") or 0) for report in mode_exact_reports)
                exact_elapsed = sum(int(report.get("elapsedMs") or 0) for report in mode_exact_reports)
                exact_refinements = sum(int(report.get("refinements") or 0) for report in mode_exact_reports)
                exact_constraints = sum(int(report.get("hardConstraints") or 0) for report in mode_exact_reports)
                emit_observability_event(
                    "solver.mode.summary",
                    project=project,
                    mode=mode,
                    componentsSolved=len(mode_exact_reports),
                    componentsTotal=len(components),
                    changed=exact_changed,
                    hardConstraintCount=exact_constraints,
                    refinements=exact_refinements,
                    durationMs=exact_elapsed,
                )
                eprint(
                    f"[info] {project}: exact z3 {mode} SUMMARY; components={len(mode_exact_reports)}/{len(components)}, "
                    f"changed={exact_changed}, constraints={exact_constraints}, "
                    f"refinements={exact_refinements}, elapsedMs={exact_elapsed}"
                )

            if mode_shadow_reports:
                compared = [
                    report for report in mode_shadow_reports
                    if report.get("status") == "optimal" and report.get("objectiveRelation") != "pending-production"
                ]
                if compared:
                    better = sum(report.get("objectiveRelation") == "better" for report in compared)
                    equal = sum(report.get("objectiveRelation") == "equal" for report in compared)
                    worse = sum(report.get("objectiveRelation") == "worse" for report in compared)
                    divergent = sum(not bool(report.get("sameAssignment")) for report in compared)
                    production_changed = sum(int(report.get("productionChanged") or 0) for report in compared)
                    shadow_changed = sum(int(report.get("shadowChanged") or 0) for report in compared)
                    elapsed_ms = sum(int(report.get("elapsedMs") or 0) for report in compared)
                    eprint(
                        f"[info] {project}: shadow z3 {mode} SUMMARY; components={len(compared)}/{len(components)}, "
                        f"better={better}, equal={equal}, worse={worse}, divergent={divergent}, "
                        f"productionChanged={production_changed}, shadowChanged={shadow_changed}, elapsedMs={elapsed_ms}"
                    )

            if solver_statuses_out is not None:
                solver_statuses_out.setdefault(project, {})[mode] = dict(status_by_name)

            assignments_by_project[project][mode] = dict(assignment)
            if not apply_results:
                continue
            actual_graph = _actual_peer_component_graph(rows_by_name, assignment, client)
            # Learned n-way incompatibilities are part of the execution
            # topology too.  Dropping them here can split packages that were
            # intentionally solved together into branches that are not safe to
            # execute independently.  Keep the original clauses as source of
            # truth; this graph merge is only their conservative projection.
            merge_nogood_edges(actual_graph, learned_nogoods)

            # Final-assignment compatibility is weaker than branch safety. A
            # package can drop a peer constraint in its target version while a
            # companion moves to a version that the *current* package rejects.
            # The target tuple is valid, but applying only the companion branch
            # is not. Refine peer/nogood components against the solver-neutral
            # hard-constraint IR until every remaining cohort can be toggled
            # current<->target independently of every other cohort.
            actual_components = _graph_components(actual_graph)
            existing_cohort_groups: Dict[str, Set[str]] = defaultdict(set)
            for name, row in rows_by_name.items():
                if row.compatibility_cohort:
                    existing_cohort_groups[row.compatibility_cohort].add(name)

            transition_components: List[List[str]] = []
            transition_notes: Dict[str, List[str]] = defaultdict(list)
            transition_merge_count = 0
            for solve_component in components:
                if all(assignment[name] == rows_by_name[name].current_version for name in solve_component):
                    # No dependency transition will be executed for this
                    # component (including exact-solver local defers), so it
                    # cannot create an unsafe partial branch transition.
                    continue
                component_set = set(solve_component)
                initial_groups: List[Set[str]] = []
                for group in actual_components:
                    members = set(group) & component_set
                    if members:
                        initial_groups.append(members)
                for members in existing_cohort_groups.values():
                    overlap = members & component_set
                    if len(overlap) > 1:
                        initial_groups.append(overlap)

                model = _build_peer_optimization_model(
                    solve_component, rows_by_name, domains, client, mode, learned_nogoods, residual_targets
                )
                current_assignment = {name: rows_by_name[name].current_version for name in solve_component}
                target_assignment = {name: assignment[name] for name in solve_component}
                transition = refine_transition_safe_groups(
                    model,
                    current_assignment,
                    target_assignment,
                    initial_groups=initial_groups,
                )
                if not transition.safe:
                    detail = '; '.join(transition.unresolved)
                    raise RuntimeError(
                        f"BASELINE_PLAN_BROKEN: {project}/{mode}: transition-safety proof failed: {detail}"
                    )
                transition_merge_count += len(transition.merges)
                for merge in transition.merges:
                    reason = f"TRANSITION_COHORT: {merge.reason}"
                    for name in set(merge.left) | set(merge.right):
                        transition_notes[name].append(reason)
                for group in transition.groups:
                    if len(group) > 1:
                        transition_components.append(list(group))

            if transition_merge_count:
                eprint(
                    f"[info] {project}: transition safety {mode}; "
                    f"mergedBoundaries={transition_merge_count}, cohorts={len(transition_components)}"
                )

            cohort_for_name: Dict[str, str] = {}
            for component in transition_components:
                cohort = _cohort_name_for_component(component, rows_by_name)
                for name in component:
                    cohort_for_name[name] = cohort

            changed = 0
            for name, representative in rows_by_name.items():
                resolved_version = assignment.get(name, representative.current_version)
                desired = _desired_target_for_mode(representative, mode)
                resolved_target = resolved_version if resolved_version != representative.current_version else NO_ACTION
                reason = _resolution_change_reason(
                    representative,
                    desired,
                    resolved_version,
                    assignment,
                    rows_by_name,
                    client,
                    mode,
                    status_by_name.get(name, "optimal"),
                )
                for row in rows_for_name[name]:
                    old_target = getattr(row, _target_attr(mode))
                    setattr(row, _target_attr(mode), resolved_target)
                    setattr(row, _resolution_reason_attr(mode), reason)
                    if reason:
                        setattr(row, _target_reason_attr(mode), target_reason_join([
                            getattr(row, _target_reason_attr(mode), ""), reason,
                        ]))
                        row.compatibility_note = target_reason_join([row.compatibility_note, reason])
                    if name in cohort_for_name:
                        row.compatibility_cohort = cohort_for_name[name]
                    for transition_note in transition_notes.get(name, []):
                        row.compatibility_note = target_reason_join([row.compatibility_note, transition_note])
                    # The compatibility solver is the canonical owner of a
                    # resolved peer-connected/fallback target.  Dashboard JS
                    # also carries pre-solver non-lag/lag ingredients so the
                    # user can change lag policy, but recombining those fields
                    # after the solver has rejected or narrowed a version can
                    # resurrect the incompatible desired target.  Freeze only
                    # rows whose result actually depended on compatibility;
                    # ordinary independent rows remain dynamically adjustable.
                    if reason or name in cohort_for_name or old_target != resolved_target:
                        setattr(row, f"target_{mode}_dynamic_locked", True)
                    if old_target != resolved_target:
                        changed += 1
            if changed:
                eprint(f"[info] {project}: peer compatibility solver adjusted {changed} target(s) for {mode}")

    return assignments_by_project



class BaselineTerminalStatus(str, Enum):
    SAT_PROVEN = "SAT_PROVEN"
    UNSAT_PROVEN = "UNSAT_PROVEN"
    SOLVER_UNKNOWN = "SOLVER_UNKNOWN"
    SOLVER_UNAVAILABLE = "SOLVER_UNAVAILABLE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    PLATEAU = "PLATEAU"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    HARD_SAFETY_LIMIT = "HARD_SAFETY_LIMIT"


class BaselineConstraintVerificationError(RuntimeError):
    """A pre-agent verification failure that must not be converted into replan."""

    def __init__(
        self,
        message: str,
        *,
        terminal_status: BaselineTerminalStatus | str | None = None,
        terminal_source: str = "",
        stop_code: str = "",
    ) -> None:
        super().__init__(message)
        self.terminal_status = (
            terminal_status.value
            if isinstance(terminal_status, BaselineTerminalStatus)
            else str(terminal_status or "")
        )
        self.terminal_source = str(terminal_source or "")
        self.stop_code = str(stop_code or "")


def _baseline_terminal_error(
    status: BaselineTerminalStatus,
    stop_code: str,
    message: str,
    *,
    source: str,
) -> BaselineConstraintVerificationError:
    return BaselineConstraintVerificationError(
        f"{stop_code}: terminalStatus={status.value}; terminalSource={source}; {message}",
        terminal_status=status,
        terminal_source=source,
        stop_code=stop_code,
    )


def _terminal_status_for_exact_solver(status: str) -> BaselineTerminalStatus:
    normalized = str(status or "").strip().lower()
    if normalized == "optimal":
        return BaselineTerminalStatus.SAT_PROVEN
    if normalized == "unsat":
        return BaselineTerminalStatus.UNSAT_PROVEN
    if normalized == "unknown_refinement_budget":
        return BaselineTerminalStatus.BUDGET_EXHAUSTED
    if normalized == "unavailable":
        return BaselineTerminalStatus.SOLVER_UNAVAILABLE
    return BaselineTerminalStatus.SOLVER_UNKNOWN


def _terminal_status_for_global_exact_reason(reason: str) -> BaselineTerminalStatus:
    normalized = str(reason or "").strip().lower()
    if normalized == "unsat-proven":
        return BaselineTerminalStatus.UNSAT_PROVEN
    if normalized == "budget-exhausted":
        return BaselineTerminalStatus.BUDGET_EXHAUSTED
    if normalized == "solver-unavailable":
        return BaselineTerminalStatus.SOLVER_UNAVAILABLE
    return BaselineTerminalStatus.SOLVER_UNKNOWN


def _changed_assignment(
    assignment: Dict[str, str],
    rows_by_name: Dict[str, DependencyRow],
    package_names: Optional[Set[str]] = None,
) -> Dict[str, str]:
    names = package_names if package_names is not None else set(assignment)
    return {
        name: assignment[name]
        for name in sorted(names)
        if name in assignment and name in rows_by_name and assignment[name] != rows_by_name[name].current_version
    }


def _verification_assignment(
    assignment: Dict[str, str],
    rows_by_name: Dict[str, DependencyRow],
) -> Dict[str, str]:
    # Complete solver-managed direct assignment. Fixed git/file/workspace/http
    # declarations remain untouched in package.json and are still proven as
    # resolver inputs through manifest/lock/source/environment identity.
    return {
        name: assignment[name]
        for name in sorted(assignment)
        if name in rows_by_name
    }




def _targeted_adaptive_confirmation_commands(
    result: BaselineVerifyResult,
    config: BaselineVerifyConfig,
) -> Tuple[str, ...]:
    """Commands that actually produced adaptive structural evidence."""
    if (
        result.kind != "project"
        or config.project_checks != "adaptive"
        or not result.project_failures
    ):
        return config.commands

    responsible: List[str] = []
    for failure in result.project_failures:
        single = BaselineVerifyResult(
            False,
            "project",
            f"project preflight failed: {failure.command}",
            command=failure.command,
            output=failure.output,
            project_failures=(failure,),
        )
        if structural_project_failure_signatures(single) and failure.command not in responsible:
            responsible.append(failure.command)
    return tuple(responsible) or config.commands


def _verification_inputs_for_units(
    assignment: Dict[str, str],
    rows_by_name: Dict[str, DependencyRow],
    units: Iterable[VerificationUnit],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (materialization_delta, solver_context_clause) for verification units.

    VerificationUnit.packages deliberately includes unchanged peer/interaction
    companions. They belong in a learned solver clause, but they must NOT be
    materialized as exact package.json versions when reproducing a localization
    probe. Reproduction must execute the same delta that ddmin observed.
    """
    package_names = {
        name
        for unit in units
        for name in unit.packages
    }
    materialization = _changed_assignment(assignment, rows_by_name, package_names)
    clause = {
        name: assignment[name]
        for name in sorted(package_names)
        if name in assignment
    }
    return materialization, clause


def _expand_verification_component_context(
    component: Sequence[str],
    component_for: Mapping[str, Sequence[str]],
    interactions_by_name: Mapping[str, Sequence[InteractionEdge]],
    *,
    context_radius: int,
) -> Tuple[str, ...]:
    """Expand a hard component through bounded verification-only interactions.

    Radius 1 is exactly the historical behavior: the hard peer/learned-nogood
    component plus its directly interacting neighbor components. Higher radii
    are used only after repeated diagnostic failures and remain non-authoritative
    until the resulting exact candidate is freshly certified.
    """
    packages: Set[str] = {str(name) for name in component}
    frontier: Set[str] = set(packages)
    radius = max(0, int(context_radius))
    for _hop in range(radius):
        next_frontier: Set[str] = set()
        for name in sorted(frontier):
            for edge in interactions_by_name.get(name, ()):
                other = edge.right if edge.left == name else edge.left
                for member in component_for.get(other, (other,)):
                    member = str(member)
                    if member not in packages:
                        next_frontier.add(member)
        if not next_frontier:
            break
        packages.update(next_frontier)
        frontier = next_frontier
    return tuple(sorted(packages))


def _verification_units_for_assignment(
    rows_by_name: Dict[str, DependencyRow],
    assignment: Dict[str, str],
    mode: str,
    client: LiveDataClient,
    learned_nogoods: List[Dict[str, str]],
    *,
    context_radius: int = 1,
) -> List[VerificationUnit]:
    """Build bounded conflict-localization units for an exact assignment.

    Peer/learned-nogood components remain atomic. Ordinary direct-dependency
    interactions do *not* become solver constraints up front. Radius 1 preserves
    the original behavior. A larger radius is diagnostic navigation only and is
    requested by repeated-conflict escalation; fresh package-manager/project
    certification is still required before any clause becomes authoritative.
    """
    domains = {
        name: (
            [row.current_version]
            if _is_fixed_dependency_input(row)
            else _candidate_domain(row, mode, client)
        )
        for name, row in rows_by_name.items()
    }
    hard_graph = _potential_peer_graph(rows_by_name, domains, client)
    merge_nogood_edges(hard_graph, learned_nogoods)
    hard_components = _graph_components(hard_graph)

    component_for: Dict[str, Tuple[str, ...]] = {}
    for component in hard_components:
        frozen = tuple(component)
        for name in component:
            component_for[name] = frozen

    interactions = _potential_interaction_edges(rows_by_name, domains, client)
    interactions_by_name = edge_index(
        edge for edge in interactions if edge.kind == DIRECT_SHADOWING
    )

    units: List[VerificationUnit] = []
    for component in hard_components:
        changed_here = any(
            assignment.get(name, rows_by_name[name].current_version)
            != rows_by_name[name].current_version
            for name in component
        )
        if not changed_here:
            continue
        ordered = _expand_verification_component_context(
            component,
            component_for,
            interactions_by_name,
            context_radius=context_radius,
        )
        digest = hashlib.sha256(
            "\0".join(ordered).encode("utf-8")
        ).hexdigest()[:10]
        units.append(
            VerificationUnit(id=f"constraint-{digest}", packages=ordered)
        )
    return units




EVIDENCE_HARD_MODEL = "HARD_MODEL"
EVIDENCE_CONFIRMED_CONSTRAINT = "CONFIRMED_CONSTRAINT"
EVIDENCE_DIAGNOSTIC_HINT = "DIAGNOSTIC_HINT"
EVIDENCE_NAVIGATION_ONLY = "NAVIGATION_ONLY"

GRAPH_GENERALIZATION_MAX_LITERALS = 12
GRAPH_GENERALIZATION_PROJECT_PROOFS = 2
# Repetition is navigation evidence only. It may widen what we freshly verify,
# but it never becomes solver authority by itself.
GRAPH_GENERALIZATION_MAX_CONTEXT_RADIUS = 4
GRAPH_GENERALIZATION_ESCALATED_MAX_LITERALS = 32
# Literal growth is deliberately finer-grained than graph-radius growth.
# Repetition chooses the next diagnostic experiment only; fresh certification
# remains the sole route to solver authority.
GRAPH_GENERALIZATION_LITERAL_BUDGET_STEPS = (4, 6, 8, 12, 16, 24, 32)


@dataclasses.dataclass(frozen=True)
class GraphGeneralizationProposal:
    candidate: Dict[str, str]
    seed_candidate_fingerprint: str
    navigation_key: str
    context_radius: int
    repeat_count: int
    predicate_key: str
    family_key: str
    literal_budget: int
    bounded_slice: bool
    seed_source: str


def _failure_package_hints(
    result: BaselineVerifyResult,
    rows_by_name: Mapping[str, DependencyRow],
) -> Set[str]:
    hints: Set[str] = set()
    signatures = structural_project_failure_signatures(result)
    for signature in signatures:
        if ":" not in signature:
            continue
        prefix, value = signature.split(":", 1)
        candidate = value.strip().lower()
        if prefix in {
            "ts-module-resolution",
            "esm-cjs",
            "duplicate-type-universe",
        }:
            for name in rows_by_name:
                if name.lower() == candidate:
                    hints.add(name)
        elif prefix == "toolchain-runtime-api" and candidate.startswith("sass."):
            if "sass" in rows_by_name:
                hints.add("sass")

    text = f"{result.summary}\n{result.output}".lower()
    for name in rows_by_name:
        lowered = name.lower()
        if lowered and lowered in text:
            hints.add(name)
    return hints


def _select_graph_guided_candidate_clause(
    assignment: Mapping[str, str],
    rows_by_name: Mapping[str, DependencyRow],
    units: Sequence[VerificationUnit],
    hints: Set[str],
    *,
    max_literals: int = GRAPH_GENERALIZATION_MAX_LITERALS,
) -> Optional[Dict[str, str]]:
    """Pick one bounded diagnostic candidate; certification decides authority."""
    if not hints or not units:
        return None

    changed_names = {
        name
        for name, version in assignment.items()
        if name in rows_by_name
        and version != rows_by_name[name].current_version
    }
    ranked: List[Tuple[int, int, str, VerificationUnit]] = []
    for unit in units:
        package_set = set(unit.packages)
        overlap = len(package_set & hints)
        if overlap <= 0 or not (package_set & changed_names):
            continue
        ranked.append((-overlap, len(package_set), unit.id, unit))
    if not ranked:
        return None

    _overlap, _size, _id, selected = min(ranked)
    clause = {
        name: str(assignment[name])
        for name in sorted(selected.packages)
        if name in assignment
    }
    if not clause:
        return None
    if len(clause) > max(1, int(max_literals)):
        return None
    if len(clause) >= len(assignment):
        return None
    if not any(name in changed_names for name in clause):
        return None
    return clause


def _graph_guided_generalization_candidate(
    rows_by_name: Dict[str, DependencyRow],
    assignment: Dict[str, str],
    mode: str,
    client: LiveDataClient,
    learned_nogoods: List[Dict[str, str]],
    result: BaselineVerifyResult,
    *,
    context_radius: int = 1,
    max_literals: int = GRAPH_GENERALIZATION_MAX_LITERALS,
) -> Optional[Dict[str, str]]:
    """Return one whole bounded diagnostic clause; authority still requires certification."""
    units = _verification_units_for_assignment(
        rows_by_name,
        assignment,
        mode,
        client,
        learned_nogoods,
        context_radius=context_radius,
    )
    hints = _failure_package_hints(result, rows_by_name)
    return _select_graph_guided_candidate_clause(
        assignment,
        rows_by_name,
        units,
        hints,
        max_literals=max_literals,
    )


def _graph_generalization_literal_budget(
    repeat_count: int,
    seed_size: int,
) -> int:
    """Return the deterministic candidate-size budget for one failure family."""
    if repeat_count <= 1:
        return max(1, int(seed_size))
    index = min(
        max(0, int(repeat_count) - 2),
        len(GRAPH_GENERALIZATION_LITERAL_BUDGET_STEPS) - 1,
    )
    return min(
        GRAPH_GENERALIZATION_ESCALATED_MAX_LITERALS,
        max(int(seed_size), GRAPH_GENERALIZATION_LITERAL_BUDGET_STEPS[index]),
    )


def _bounded_graph_guided_generalization_candidate(
    rows_by_name: Dict[str, DependencyRow],
    assignment: Dict[str, str],
    mode: str,
    client: LiveDataClient,
    learned_nogoods: List[Dict[str, str]],
    result: BaselineVerifyResult,
    *,
    context_radius: int,
    literal_budget: int,
    required_packages: Sequence[str],
) -> Optional[Dict[str, str]]:
    """Slice an oversized graph-local unit into a deterministic diagnostic subset.

    The subset is navigation-only. The existing fresh graph-certification path
    must reproduce the authoritative failure predicate before it enters learned.
    """
    units = _verification_units_for_assignment(
        rows_by_name,
        assignment,
        mode,
        client,
        learned_nogoods,
        context_radius=context_radius,
    )
    required = {str(name) for name in required_packages if str(name)}
    hints = _failure_package_hints(result, rows_by_name)
    effective_hints = set(hints) | required
    if not effective_hints or not units:
        return None

    changed_names = {
        name
        for name, version in assignment.items()
        if name in rows_by_name
        and version != rows_by_name[name].current_version
    }
    if not changed_names:
        return None

    ranked: List[Tuple[int, int, str, VerificationUnit]] = []
    for unit in units:
        package_set = set(unit.packages) & set(assignment)
        overlap = len(package_set & effective_hints)
        if overlap <= 0 or not (package_set & changed_names):
            continue
        ranked.append((-overlap, len(package_set), unit.id, unit))
    if not ranked:
        return None

    _overlap, _size, _id, selected = min(ranked)
    available = {
        str(name)
        for name in selected.packages
        if name in assignment
    }
    if not available:
        return None

    max_allowed = min(
        max(1, int(literal_budget)),
        max(0, len(assignment) - 1),
    )
    if max_allowed <= 0:
        return None

    ordered: List[str] = []
    seen: Set[str] = set()

    def add_names(names: Iterable[str]) -> None:
        for name in sorted({str(item) for item in names}):
            if name in available and name not in seen:
                seen.add(name)
                ordered.append(name)

    add_names(required)
    add_names(hints)
    add_names(available & changed_names)
    add_names(available)

    chosen = ordered[:max_allowed]
    if not chosen:
        return None

    if not (set(chosen) & changed_names):
        changed_available = sorted(available & changed_names)
        if not changed_available:
            return None
        replacement = changed_available[0]
        replace_index = None
        for index in range(len(chosen) - 1, -1, -1):
            if chosen[index] not in required:
                replace_index = index
                break
        if replace_index is None:
            return None
        chosen[replace_index] = replacement
        chosen = list(dict.fromkeys(chosen))

    clause = {
        name: str(assignment[name])
        for name in chosen
        if name in assignment
    }
    if not clause or len(clause) >= len(assignment):
        return None
    if not any(name in changed_names for name in clause):
        return None
    return clause



def _graph_generalization_repeat_predicate(
    result: BaselineVerifyResult,
) -> str:
    """Stable navigation identity only; never solver authority."""
    structural = structural_project_failure_signatures(result)
    if structural:
        return "structural:" + "|".join(sorted(structural))
    signature = dependency_failure_navigation_signature(
        summary=result.summary,
        output=result.output,
    )
    return f"{result.kind}:{signature}"



def _adaptive_graph_guided_generalization_proposal(
    rows_by_name: Dict[str, DependencyRow],
    assignment: Dict[str, str],
    mode: str,
    client: LiveDataClient,
    learned_nogoods: List[Dict[str, str]],
    result: BaselineVerifyResult,
    *,
    project_key: str,
    repeat_tracker: Dict[str, int],
    failed_candidates: Set[Tuple[str, str]],
    seed_packages_by_family: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> Optional[GraphGeneralizationProposal]:
    """Keep localization alive across exact assignments without creating authority.

    Oversized radii are sliced into gradually larger deterministic candidates.
    A seed package shape may be carried across the same stable failure family
    when a later resolver rendering omits package hints. Both mechanisms are
    navigation-only; every candidate still requires fresh certification.
    """
    predicate_key = _graph_generalization_repeat_predicate(result)
    family_payload = {
        "project": str(project_key),
        "mode": str(mode),
        "kind": str(result.kind),
        "predicate": predicate_key,
    }
    family_key = hashlib.sha256(
        json.dumps(
            family_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]

    seed_store = seed_packages_by_family
    fresh_seed = _graph_guided_generalization_candidate(
        rows_by_name,
        assignment,
        mode,
        client,
        learned_nogoods,
        result,
        context_radius=1,
        max_literals=GRAPH_GENERALIZATION_MAX_LITERALS,
    )

    seed_source = "fresh"
    seed_is_bounded = False
    if fresh_seed is None:
        # Oversized first-pass components used to fall straight through to an
        # exact full-assignment exclusion. Build a bounded DIAGNOSTIC seed now;
        # fresh certification and proof-preserving minimization still gate authority.
        fresh_hints = _failure_package_hints(result, rows_by_name)
        if fresh_hints:
            initial_budget = min(
                GRAPH_GENERALIZATION_ESCALATED_MAX_LITERALS,
                max(GRAPH_GENERALIZATION_LITERAL_BUDGET_STEPS[0], len(fresh_hints)),
            )
            fresh_seed = _bounded_graph_guided_generalization_candidate(
                rows_by_name, assignment, mode, client, learned_nogoods, result,
                context_radius=1, literal_budget=initial_budget,
                required_packages=tuple(sorted(fresh_hints)),
            )
            if fresh_seed is not None:
                seed_source = "bounded-fresh"
                seed_is_bounded = True

    if fresh_seed is not None:
        seed = dict(fresh_seed)
        seed_shape = tuple(sorted(seed))
        if seed_store is not None:
            seed_store[family_key] = seed_shape
    else:
        stored_shape = (
            tuple(seed_store.get(family_key, ()))
            if seed_store is not None
            else ()
        )
        seed_shape = tuple(name for name in stored_shape if name in assignment)
        if not seed_shape or len(seed_shape) >= len(assignment):
            return None
        seed = {name: str(assignment[name]) for name in seed_shape}
        seed_source = "carry-forward"

    seed_fingerprint = assignment_fingerprint(seed)
    navigation_payload = {
        "family": family_key,
        "seedPackages": list(seed_shape),
    }
    navigation_key = hashlib.sha256(
        json.dumps(
            navigation_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]

    repeat_count = repeat_tracker.get(navigation_key, 0) + 1
    repeat_tracker[navigation_key] = repeat_count
    literal_budget = _graph_generalization_literal_budget(
        repeat_count,
        len(seed),
    )

    if repeat_count <= 1 and seed_source in {"fresh", "bounded-fresh"}:
        candidate_fingerprint = assignment_fingerprint(seed)
        if (navigation_key, candidate_fingerprint) not in failed_candidates:
            return GraphGeneralizationProposal(
                candidate=dict(seed),
                seed_candidate_fingerprint=seed_fingerprint,
                navigation_key=navigation_key,
                context_radius=1,
                repeat_count=repeat_count,
                predicate_key=predicate_key,
                family_key=family_key,
                literal_budget=literal_budget,
                bounded_slice=seed_is_bounded,
                seed_source=seed_source,
            )

    max_radius = max(1, int(GRAPH_GENERALIZATION_MAX_CONTEXT_RADIUS))
    start_radius = min(max(2, repeat_count), max_radius)

    for radius in range(start_radius, max_radius + 1):
        candidate = _graph_guided_generalization_candidate(
            rows_by_name,
            assignment,
            mode,
            client,
            learned_nogoods,
            result,
            context_radius=radius,
            max_literals=literal_budget,
        )
        bounded_slice = False

        if candidate is None:
            candidate = _bounded_graph_guided_generalization_candidate(
                rows_by_name,
                assignment,
                mode,
                client,
                learned_nogoods,
                result,
                context_radius=radius,
                literal_budget=literal_budget,
                required_packages=seed_shape,
            )
            bounded_slice = candidate is not None

        if candidate is None:
            continue

        candidate_fingerprint = assignment_fingerprint(candidate)
        if (navigation_key, candidate_fingerprint) in failed_candidates:
            continue

        return GraphGeneralizationProposal(
            candidate=dict(candidate),
            seed_candidate_fingerprint=seed_fingerprint,
            navigation_key=navigation_key,
            context_radius=radius,
            repeat_count=repeat_count,
            predicate_key=predicate_key,
            family_key=family_key,
            literal_budget=literal_budget,
            bounded_slice=bounded_slice,
            seed_source=seed_source,
        )

    return None






def _adaptive_predicate_families(
    stable_predicate: str,
    *,
    adaptive_project: bool,
) -> Tuple[str, ...]:
    """Split a certified multi-signature project failure into independent families.

    The split itself is navigation only. Every family still has to survive the
    same fresh proof-preserving minimization gate before it becomes Solver
    authority.
    """
    value = str(stable_predicate or "").strip()
    if not value:
        return ()
    if not adaptive_project:
        return (value,)
    return tuple(sorted({item for item in value.split("|") if item}))


GRAPH_GENERALIZATION_HISTORY_LIMIT = 6


def _cross_iteration_consensus_proposal(
    proposal: GraphGeneralizationProposal,
    assignment: Mapping[str, str],
    history_by_family: Dict[str, List[Tuple[str, ...]]],
    failed_candidates: Set[Tuple[str, str]],
) -> GraphGeneralizationProposal:
    # Navigation-only recurrent package core across one stable failure family.
    # The candidate gets no authority here: existing fresh certification must
    # reproduce the authoritative predicate, then Block F minimizes it again.
    history = history_by_family.setdefault(proposal.navigation_key, [])
    observation = tuple(sorted(proposal.candidate))
    if observation and observation not in history:
        history.append(observation)
        if len(history) > GRAPH_GENERALIZATION_HISTORY_LIMIT:
            del history[:-GRAPH_GENERALIZATION_HISTORY_LIMIT]

    if len(history) < 2:
        return proposal

    support: Dict[str, int] = {}
    for package_shape in history:
        for name in set(package_shape):
            support[name] = support.get(name, 0) + 1

    required_support = max(2, (2 * len(history) + 2) // 3)
    recurrent = tuple(
        sorted(
            name
            for name, count in support.items()
            if count >= required_support and name in assignment
        )
    )
    if not recurrent:
        return proposal

    candidate = {name: str(assignment[name]) for name in recurrent}
    if not candidate or len(candidate) >= len(proposal.candidate):
        return proposal

    candidate_fingerprint = assignment_fingerprint(candidate)
    if (proposal.navigation_key, candidate_fingerprint) in failed_candidates:
        return proposal

    return dataclasses.replace(
        proposal,
        candidate=candidate,
        bounded_slice=True,
        seed_source="cross-iteration-consensus",
    )


NOGOOD_MINIMIZATION_MAX_CHECKS = 12


@dataclasses.dataclass(frozen=True)
class NogoodMinimizationResult:
    original: Dict[str, str]
    minimized: Dict[str, str]
    predicate: str
    checks: int
    accepted_shrinks: int
    shrink_history: Tuple[int, ...]
    exhausted: bool


def _nogood_minimization_check_budget(literals: int) -> int:
    # Bound fresh serial proof cost while scaling with clause size.
    size = max(1, int(literals))
    if size <= 1:
        return 0
    depth = (size - 1).bit_length()
    return min(NOGOOD_MINIMIZATION_MAX_CHECKS, max(4, 2 * depth + 2))


def _partition_names(names: Sequence[str], parts: int) -> List[Tuple[str, ...]]:
    ordered = tuple(sorted({str(name) for name in names if str(name)}))
    if not ordered:
        return []
    count = min(max(1, int(parts)), len(ordered))
    base, extra = divmod(len(ordered), count)
    chunks: List[Tuple[str, ...]] = []
    offset = 0
    for index in range(count):
        size = base + (1 if index < extra else 0)
        chunk = ordered[offset : offset + size]
        offset += size
        if chunk:
            chunks.append(chunk)
    return chunks


def _proof_preserving_minimize_nogood(
    nogood: Mapping[str, str],
    certify: Callable[[Dict[str, str], int], str],
    *,
    max_checks: Optional[int] = None,
    initial_predicate: str = "",
    min_literals: int = 1,
) -> NogoodMinimizationResult:
    # Deterministic ddmin. certify() is the only authority gate.
    original = {
        str(name): str(version)
        for name, version in sorted(nogood.items())
        if str(name) and str(version)
    }
    if not original:
        return NogoodMinimizationResult(
            original={},
            minimized={},
            predicate="",
            checks=0,
            accepted_shrinks=0,
            shrink_history=(),
            exhausted=False,
        )

    floor = min(max(1, int(min_literals)), len(original))
    budget = (
        _nogood_minimization_check_budget(len(original))
        if max_checks is None
        else max(0, int(max_checks))
    )
    current = dict(original)
    predicate = str(initial_predicate or "")
    checks = 0
    accepted = 0
    history: List[int] = [len(current)]

    # Localized clauses may only have unit-level proof. Certify the exact
    # literal clause before attempting any shrink.
    if not predicate:
        if budget <= 0:
            return NogoodMinimizationResult(
                original=original,
                minimized=current,
                predicate="",
                checks=0,
                accepted_shrinks=0,
                shrink_history=tuple(history),
                exhausted=True,
            )
        checks += 1
        predicate = str(certify(dict(current), checks) or "")
        if not predicate:
            return NogoodMinimizationResult(
                original=original,
                minimized=current,
                predicate="",
                checks=checks,
                accepted_shrinks=0,
                shrink_history=tuple(history),
                exhausted=False,
            )

    if len(current) <= floor or budget <= checks:
        return NogoodMinimizationResult(
            original=original,
            minimized=current,
            predicate=predicate,
            checks=checks,
            accepted_shrinks=accepted,
            shrink_history=tuple(history),
            exhausted=(budget <= checks and len(current) > floor),
        )

    granularity = 2
    while len(current) > floor and checks < budget:
        names = tuple(sorted(current))
        chunks = _partition_names(names, granularity)
        reduced = False

        for chunk in chunks:
            if checks >= budget:
                break
            removed = set(chunk)
            trial = {
                name: current[name]
                for name in names
                if name not in removed
            }
            if len(trial) < floor or len(trial) >= len(current):
                continue

            checks += 1
            observed = str(certify(trial, checks) or "")
            if not observed or observed != predicate:
                continue

            current = trial
            accepted += 1
            history.append(len(current))
            granularity = max(2, granularity - 1)
            reduced = True
            break

        if reduced:
            continue
        if granularity >= len(current):
            break
        granularity = min(len(current), granularity * 2)

    return NogoodMinimizationResult(
        original=original,
        minimized=current,
        predicate=predicate,
        checks=checks,
        accepted_shrinks=accepted,
        shrink_history=tuple(history),
        exhausted=(checks >= budget and len(current) > floor),
    )


def _annotate_constraint_preflight(
    rows: List[DependencyRow],
    mode: str,
    assignment: Dict[str, str],
    result: BaselineVerifyResult,
    *,
    iteration: int,
) -> None:
    changed = {name for name, version in assignment.items() if any(r.name == name and r.current_version != version for r in rows)}
    if not changed:
        return
    payload = {
        "status": result.kind,
        "ok": result.ok,
        "summary": result.summary,
        "command": result.command,
        "outputTail": result.output[-8000:],
        "assignment": assignment_fingerprint(assignment),
        "iteration": iteration,
    }
    for row in rows:
        if row.name in changed:
            row.constraint_preflight[mode] = dict(payload)


def _normalize_baseline_progress_details(details: Mapping[str, object]) -> Dict[str, object]:
    # Verifier telemetry may have its own nested phase (e.g. confirmation).
    # Rename it so BaselineProgressReporter.emit(..., phase, **details) cannot
    # receive two values for its positional phase argument.
    normalized = dict(details)
    nested_phase = normalized.pop("phase", None)
    if nested_phase is not None:
        normalized["localizationPhase"] = nested_phase
    return normalized




class BaselineLivenessBudget:
    # Progress-aware soft budget with an independent bounded hard safety ceiling.
    # Every NEW authoritative formula strengthening may buy one more solve. This
    # includes both generalized learned clauses and freshly confirmed full-tuple
    # exact exclusions; diagnostic/navigation evidence never extends liveness.

    def __init__(
        self,
        *,
        base_iterations: int,
        max_learning_extensions: int,
        starting_learned_constraints: int = 0,
    ) -> None:
        self.base_iterations = max(1, int(base_iterations))
        # Historical public/config name kept for compatibility. Semantically this
        # is now the total authority-extension budget shared by all clause kinds.
        self.max_learning_extensions = max(0, int(max_learning_extensions))
        self.starting_learned_constraints = max(0, int(starting_learned_constraints))
        self.learned_constraints = self.starting_learned_constraints
        self.certified_extensions = 0
        self.user_extensions = 0
        self.learned_extensions = 0
        self.exact_extension_credits = 0
        self.exact_exclusions = 0
        self.exact_since_learning = 0
        self.generalization_attempts = 0
        self.diagnostics = 0

    @property
    def hard_iterations(self) -> int:
        return self.base_iterations + self.max_learning_extensions

    @property
    def allowed_iterations(self) -> int:
        return min(
            self.hard_iterations,
            self.base_iterations + self.certified_extensions + self.user_extensions,
        )

    def grant_user_extensions(self, count: int) -> int:
        requested = max(0, int(count))
        room = max(0, self.max_learning_extensions - self.user_extensions)
        granted = min(requested, room)
        self.user_extensions += granted
        return granted

    def _grant_authority_extensions(self, count: int) -> int:
        requested = max(0, int(count))
        available = max(0, self.max_learning_extensions - self.certified_extensions)
        granted = min(requested, available)
        self.certified_extensions += granted
        return granted

    def observe_learned_constraints(self, learned_constraints: int) -> None:
        current = max(self.starting_learned_constraints, int(learned_constraints))
        if current <= self.learned_constraints:
            return
        delta = current - self.learned_constraints
        self.learned_constraints = current
        granted = self._grant_authority_extensions(delta)
        self.learned_extensions += granted
        self.exact_since_learning = 0

    def record_certified_constraint(self) -> None:
        self.observe_learned_constraints(self.learned_constraints + 1)

    def record_exact_exclusion(self) -> bool:
        self.exact_exclusions += 1
        self.exact_since_learning += 1
        granted = self._grant_authority_extensions(1)
        self.exact_extension_credits += granted
        return bool(granted)

    def record_generalization_attempt(self) -> None:
        self.generalization_attempts += 1

    def record_diagnostic(self) -> None:
        self.diagnostics += 1

    def snapshot(self, *, learned_constraints: int) -> Dict[str, int]:
        self.observe_learned_constraints(learned_constraints)
        return {
            "baseIterations": self.base_iterations,
            "allowedIterations": self.allowed_iterations,
            "hardIterations": self.hard_iterations,
            "learningExtensionLimit": self.max_learning_extensions,
            "authorityExtensionLimit": self.max_learning_extensions,
            "certifiedExtensions": self.certified_extensions,
            "userExtensions": self.user_extensions,
            "learnedExtensions": self.learned_extensions,
            "exactExtensionCredits": self.exact_extension_credits,
            "learnedConstraints": self.learned_constraints,
            "exactExclusions": self.exact_exclusions,
            "exactSinceLearning": self.exact_since_learning,
            "generalizationAttempts": self.generalization_attempts,
            "diagnostics": self.diagnostics,
        }


class BaselineProgressReporter:
    """Live + persisted progress for long deterministic verification phases."""

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._terminal = False

    def emit(self, project: str, mode: str, phase: str, **details: object) -> None:
        with self._lock:
            if phase in {"solve-and-verify-started", "external-evidence-localization-started"}:
                self._terminal = False
            if self._terminal and ("check" in phase or "heartbeat" in phase or "running" in phase):
                return
            if phase in {
                "localization-timeout", "mode-passed", "mode-passed-no-changes",
                "budget-exhausted", "solver-terminal", "verification-terminal",
            }:
                self._terminal = True
            payload = {
                "schemaVersion": 1,
                "project": project,
                "mode": mode,
                "phase": phase,
                "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                **details,
            }

            # Versioned machine channel for Desktop. The persisted latest-state
            # file deliberately stays schemaVersion=1 for backwards compatibility.
            stream_payload = {
                **payload,
                "schemaVersion": 2,
                "type": "deploom-baseline-progress",
            }
            eprint(
                "DEPLOOM_PROGRESS_V2 "
                + json.dumps(
                    stream_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temp = self.path.with_suffix(self.path.suffix + ".tmp")
                    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    os.replace(temp, self.path)
                except OSError:
                    pass


class BaselineLocalizationCheckpointStore:
    """Durable ddmin recovery state guarded by an exact proof identity."""

    def __init__(self, progress_path: Optional[Path]) -> None:
        self.path = (
            progress_path.with_name("baseline-localization-checkpoint.json")
            if progress_path is not None
            else None
        )
        self._lock = threading.Lock()

    @staticmethod
    def _slot(project: str, mode: str) -> str:
        return hashlib.sha256(f"{project}\0{mode}".encode("utf-8")).hexdigest()[:24]

    def _read_locked(self) -> Dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {"schemaVersion": 1, "entries": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            # Atomic writes mean malformed JSON should be rare. Preserve the
            # evidence instead of silently erasing a potentially multi-hour run.
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            corrupt = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{os.getpid()}")
            try:
                os.replace(self.path, corrupt)
                eprint(
                    f"[warn] Baseline localization checkpoint is corrupt and was preserved as {corrupt}: {exc}"
                )
            except OSError as preserve_error:
                eprint(
                    f"[warn] Baseline localization checkpoint is corrupt and could not be preserved: "
                    f"{self.path}: {exc}; preserveError={preserve_error}"
                )
            return {"schemaVersion": 1, "entries": {}}
        except OSError:
            return {"schemaVersion": 1, "entries": {}}
        if not isinstance(payload, dict) or int(payload.get("schemaVersion") or 0) != 1:
            return {"schemaVersion": 1, "entries": {}}
        if not isinstance(payload.get("entries"), dict):
            payload["entries"] = {}
        return payload

    def _write_locked(self, payload: Dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, self.path)

    def load(self, project: str, mode: str, identity: str) -> Optional[Dict[str, object]]:
        if self.path is None:
            return None
        with self._lock:
            payload = self._read_locked()
            entry = payload.get("entries", {}).get(self._slot(project, mode))
            if not isinstance(entry, dict) or entry.get("identity") != identity:
                return None
            state = entry.get("state")
            return dict(state) if isinstance(state, dict) else None

    def has_project_checkpoint(self, project: str) -> bool:
        if self.path is None:
            return False
        with self._lock:
            payload = self._read_locked()
            entries = payload.get("entries")
            if not isinstance(entries, dict):
                return False
            for mode in ("yellow", "green", "default"):
                entry = entries.get(self._slot(project, mode))
                if isinstance(entry, dict) and isinstance(entry.get("state"), dict):
                    return True
            return False

    def save(self, project: str, mode: str, identity: str, state: Mapping[str, object], *, source_head: str = "") -> None:
        if self.path is None:
            return
        with self._lock:
            payload = self._read_locked()
            entries = payload.setdefault("entries", {})
            entries[self._slot(project, mode)] = {
                "identity": identity,
                "sourceHead": source_head,
                "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                "state": dict(state),
            }
            self._write_locked(payload)

    def clear(self, project: str, mode: str) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = self._read_locked()
            entries = payload.get("entries")
            if not isinstance(entries, dict):
                return
            if entries.pop(self._slot(project, mode), None) is not None:
                self._write_locked(payload)


def _proof_source_head_clean_and_entries(
    project_path: Path,
    *,
    require_git: bool = True,
) -> tuple[str, bool, Tuple[str, ...]]:
    project_path = project_path.resolve()
    root_result = subprocess.run(
        ["git", "-C", str(project_path), "rev-parse", "--show-toplevel"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if root_result.returncode != 0 or not root_result.stdout.strip():
        if require_git:
            raise BaselineConstraintVerificationError(
                "PROVEN_DEPENDENCY_SOURCE_SNAPSHOT_INVALID: actionable dependency proof "
                "requires a git HEAD; non-git content snapshots are diagnostic/no-op only"
            )
        source_key = source_snapshot_fingerprint(project_path)
        return "non-git", bool(source_key), ()

    git_root = Path(root_result.stdout.strip()).resolve()
    head_result = subprocess.run(
        ["git", "-C", str(git_root), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if head_result.returncode != 0 or not head_result.stdout.strip():
        raise BaselineConstraintVerificationError(
            "PROVEN_DEPENDENCY_SOURCE_SNAPSHOT_INVALID: cannot read git HEAD"
        )
    status_result = subprocess.run(
        [
            "git", "-C", str(git_root),
            "status", "--porcelain=v1", "--untracked-files=all", "--",
            ".",
            ":(exclude).dependency-roadmap/**",
            ":(glob,exclude)**/.dependency-roadmap/**",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if status_result.returncode != 0:
        raise BaselineConstraintVerificationError(
            "PROVEN_DEPENDENCY_SOURCE_SNAPSHOT_INVALID: git status failed while sealing proof source"
        )
    relevant_status = tuple(relevant_porcelain_entries(status_result.stdout))
    return head_result.stdout.strip(), not bool(relevant_status), relevant_status


def _proof_source_head_and_clean(
    project_path: Path,
    *,
    require_git: bool = True,
) -> tuple[str, bool]:
    # Backward-compatible source-cleanliness contract.
    # Existing callers/tests consume exactly (head, clean). Detailed
    # dirty entries stay internal to ProofEnvelope diagnostics.
    head, clean, _entries = _proof_source_head_clean_and_entries(
        project_path,
        require_git=require_git,
    )
    return head, clean


def _build_proven_envelope_for_mode(
    project: str,
    mode: str,
    spec: ProjectSpec,
    rows_by_name: Dict[str, DependencyRow],
    assignment: Dict[str, str],
    removals: Set[str],
    config: BaselineVerifyConfig,
    client: LiveDataClient,
) -> Dict[str, Any]:
    manager = detect_package_manager(spec.path)
    executable = resolve_executable(manager)
    if not executable:
        raise BaselineConstraintVerificationError(
            f"PROVEN_DEPENDENCY_PROOF_IDENTITY_UNAVAILABLE: {project}/{mode}: "
            f"package manager {manager} is not available"
        )

    requires_execution = bool(_changed_assignment(assignment, rows_by_name) or removals)
    requires_fixed_resolver_proof = any(
        _is_fixed_dependency_input(row) for row in rows_by_name.values()
    )
    requires_resolver_proof = requires_execution or requires_fixed_resolver_proof
    # Dirty is no longer a proof error: the dirty bytes themselves are the
    # sealed SourceSnapshot subject. sourceHead is capture-time provenance only.
    try:
        source_head = source_snapshot_provenance_head(
            spec.path, require_git=requires_execution
        )
    except SourceCaptureError as exc:
        raise BaselineConstraintVerificationError(
            f"PROVEN_DEPENDENCY_SOURCE_IDENTITY_UNAVAILABLE: {project}/{mode}: {exc}"
        ) from exc

    identity = build_verification_proof_identity(
        spec.path,
        assignment=assignment,
        remove_packages=tuple(sorted(removals)),
        manager=manager,
        manager_executable=executable,
        registry=client.registry,
        project_checks=(
            config.project_checks
            if config.project_checks != "off" and config.commands
            else "off"
        ),
        commands=(
            config.commands
            if config.project_checks != "off" and config.commands
            else ()
        ),
        environment=dict(os.environ),
    )
    proof_store = VerificationProofStore(
        Path(config.proof_cache_dir) if config.proof_cache_dir else None
    )
    resolver_record = proof_store.lookup_pass(
        "resolver", identity.resolver_input_key
    )
    resolved_state = (
        load_resolved_dependency_state(
            resolver_record.metadata,
            proof_cache_dir=proof_store.root,
        )
        if resolver_record is not None
        else None
    )
    if resolver_record is not None and resolved_state is None:
        raise BaselineConstraintVerificationError(
            f"PROVEN_DEPENDENCY_RESOLVED_STATE_MISSING: {project}/{mode}: "
            f"ResolverProofKey={identity.resolver_input_key} does not carry a valid "
            "content-addressed post-resolve lockfile artifact"
        )
    resolver_pass = resolver_record is not None and resolved_state is not None
    if resolved_state is not None:
        identity = bind_resolved_state_identity(
            identity,
            resolved_state.key,
            project_checks=config.project_checks,
            commands=config.commands,
        )
    observed_resolved_hash = (
        str(resolver_record.metadata.get("observedResolvedHash") or "")
        if resolver_record is not None
        else ""
    )
    if requires_resolver_proof and (
        len(observed_resolved_hash) != 64
        or any(ch not in "0123456789abcdef" for ch in observed_resolved_hash.lower())
    ):
        raise BaselineConstraintVerificationError(
            f"PROVEN_DEPENDENCY_OBSERVED_PROOF_MISSING: {spec.name}/{mode}: "
            "ResolverProof does not carry the full installed direct-tree hash"
        )
    preparation_pass = (
        proof_store.lookup_pass("preparation", identity.preparation_proof_key) is not None
    )
    project_pass = (
        proof_store.lookup_pass("project", identity.project_proof_key) is not None
    )

    if requires_resolver_proof and not resolver_pass:
        raise BaselineConstraintVerificationError(
            f"PROVEN_DEPENDENCY_RESOLVER_PROOF_MISSING: {project}/{mode}: "
            f"ResolverInputKey={identity.resolver_input_key}"
        )
    requires_project_checks = (
        requires_execution
        and config.project_checks != "off"
        and bool(config.commands)
    )
    if requires_project_checks and not preparation_pass:
        raise BaselineConstraintVerificationError(
            f"PROVEN_DEPENDENCY_PREPARATION_PROOF_MISSING: {project}/{mode}: "
            f"PreparationProofKey={identity.preparation_proof_key}"
        )
    if (
        requires_project_checks
        and config.project_checks == "strict"
        and not project_pass
    ):
        raise BaselineConstraintVerificationError(
            f"PROVEN_DEPENDENCY_PROJECT_PROOF_MISSING: {project}/{mode}: strict "
            f"ProjectProofKey={identity.project_proof_key}"
        )

    return build_proven_dependency_envelope(
        project=project,
        mode=mode,
        proof_schema=identity.schema_version,
        source_head=source_head,
        source_snapshot_key=identity.source_snapshot_key,
        assignment_key=identity.assignment_key,
        resolver_input_key=identity.resolver_input_key,
        fixed_resolver_inputs_key=identity.fixed_resolver_inputs_key,
        preparation_proof_key=identity.preparation_proof_key,
        project_proof_key=identity.project_proof_key,
        observed_resolved_hash=observed_resolved_hash,
        resolved_state_key=(resolved_state.key if resolved_state is not None else ""),
        resolved_lockfile_path=(resolved_state.lockfile_path if resolved_state is not None else ""),
        resolved_lockfile_hash=(resolved_state.lockfile_hash if resolved_state is not None else ""),
        assignment=assignment,
        removals=tuple(sorted(removals)),
        verification_commands=config.commands,
        project_checks=config.project_checks,
        resolver_proof_status=(
            RESOLVER_PROOF_STATUS_PASSED
            if resolver_pass
            else RESOLVER_PROOF_STATUS_NOT_REQUIRED_NO_OP
        ),
        preparation_proof_status=(
            "passed"
            if preparation_pass
            else ("not-required" if not requires_project_checks else "missing")
        ),
        project_proof_status=(
            "passed"
            if project_pass
            else (
                "not-required"
                if not requires_project_checks
                else "diagnostic-red"
            )
        ),
    )


def resolve_peer_compatibility_with_verification(
    rows_by_project: Dict[str, List[DependencyRow]],
    projects_by_name: Dict[str, ProjectSpec],
    client: LiveDataClient,
    *,
    modes: Tuple[str, ...] = ("yellow", "green", "default"),
    residual_targets_by_project: Optional[Dict[str, Dict[str, str]]] = None,
    external_evidence_by_project: Optional[Dict[str, CompatibilityEvidence]] = None,
    progress_path: Optional[Path] = None,
    proof_envelopes_out: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Solve -> materialize -> learn nogood -> solve, before Executor exists.

    Dependency-resolution failures become learned constraints and are fed back
    into the metadata solver. Infrastructure failures are fatal *for this
    generation* but never alter package targets. Project-check failures are
    diagnostic by default because source repair belongs to Executor; strict mode
    can explicitly promote them to learned constraints.
    """
    progress_reporter = BaselineProgressReporter(progress_path)
    localization_checkpoint_store = BaselineLocalizationCheckpointStore(progress_path)
    # BLOCK_V_BASELINE_RECOVERY_V1
    from block_v_recovery import (
        BaselineRunRecoveryStore,
        baseline_resume_policy,
        baseline_run_identity,
        build_run_state,
        derive_recovery_epochs,
        restore_liveness_budget,
        restore_run_state,
    )
    run_recovery_store = BaselineRunRecoveryStore(progress_path)
    # BLOCK_VD2_SEMANTIC_RECOVERY_V1
    recovery_epochs = derive_recovery_epochs(Path(__file__).resolve().parent)
    # BLOCK_V_PREDICATE_GUIDANCE_V2
    from block_v_predicate_search import (
        PredicateObservation,
        PredicateProbePolicy,
        load_hint_snapshot,
        predicate_package,
        prioritize_probe_preference,
        rank_version_probes,
    )
    from block_v_predicate_state import PredicateSearchStateStore
    from block_vf_active_search import (
        PROBE_OUTCOME_ABSENT,
        PROBE_OUTCOME_INCONCLUSIVE,
        PROBE_OUTCOME_PRESENT,
        ProbeExecution,
        run_active_predicate_search,
    )
    compatibility_hints = load_hint_snapshot(
        os.environ.get("DEPLOOM_COMPATIBILITY_HINTS")
    )
    predicate_state_store = PredicateSearchStateStore(progress_path)
    predicate_probe_observations: Dict[
        Tuple[str, str, str, str], List[PredicateObservation]
    ] = {}
    # Navigation preferences are per project/mode and never enter hard-model
    # constraints. They only reorder complete fallback domains.
    predicate_diagnostic_preferences: Dict[
        str, Dict[str, Dict[str, str]]
    ] = {}
    learned: Dict[str, Dict[str, List[Dict[str, str]]]] = {
        project: {mode: [] for mode in modes} for project in rows_by_project
    }
    global_exact_exclusions: Dict[str, Dict[str, List[Dict[str, str]]]] = {
        project: {mode: [] for mode in modes} for project in rows_by_project
    }
    graph_generalization_repeats: Dict[str, int] = {}
    graph_generalization_failed_candidates: Set[Tuple[str, str]] = set()
    graph_generalization_seed_packages: Dict[str, Tuple[str, ...]] = {}
    graph_generalization_history_by_family: Dict[str, List[Tuple[str, ...]]] = {}
    final_assignments: Dict[str, Dict[str, Dict[str, str]]] = {}
    resolver_cache: Dict[str, BaselineVerifyResult] = {}
    # Project cache keys include the resulting ResolvedStateKey, source snapshot,
    # command set and check policy. Display fingerprints never participate.
    project_preflight_cache: Dict[str, BaselineVerifyResult] = {}
    verification_config_by_project: Dict[str, BaselineVerifyConfig] = {}

    for project, rows in rows_by_project.items():
        spec = projects_by_name.get(project)
        if spec is None:
            continue
        fallback_commands: List[str] = []
        if isinstance(spec.migration_config, dict):
            configured_checks = spec.migration_config.get("verificationCommands") or spec.migration_config.get("integrationVerificationCommands")
            if isinstance(configured_checks, list):
                fallback_commands = [str(item).strip() for item in configured_checks if isinstance(item, str) and item.strip()]
        if not fallback_commands and isinstance(spec.release_config, dict):
            release_checks = spec.release_config.get("finalGateCommands", [])
            if isinstance(release_checks, list):
                fallback_commands = [str(item).strip() for item in release_checks if isinstance(item, str) and item.strip()]
        if not fallback_commands:
            fallback_commands = list(discover_baseline_project_checks(spec.path))
            if fallback_commands:
                eprint(f"[info] {project}: Baseline structural checks auto-discovered: {', '.join(fallback_commands)}")
        config = BaselineVerifyConfig.from_mapping(spec.constraint_verify_config, fallback_commands=fallback_commands)
        predicate_probe_policy = PredicateProbePolicy.from_sources(
            spec.constraint_verify_config, os.environ
        )
        verification_telemetry_path = (
            progress_path.with_name("baseline-verification-telemetry.jsonl")
            if progress_path is not None
            else None
        )
        verification_proof_cache_dir = (
            progress_path.parent.parent / "cache" / "baseline-proofs"
            if progress_path is not None
            else None
        )
        config = dataclasses.replace(
            config,
            registry=client.registry,
            telemetry_path=str(verification_telemetry_path) if verification_telemetry_path is not None else "",
            proof_cache_dir=str(verification_proof_cache_dir) if verification_proof_cache_dir is not None else "",
        )
        configure_observability_path(
            verification_telemetry_path,
            reset=True,
            context={"project": project, "projectPath": str(spec.path)},
        )
        emit_observability_event(
            "baseline.run.start",
            project=project,
            projectPath=str(spec.path),
            packageManager=detect_package_manager(spec.path),
            projectChecks=config.project_checks,
            commands=list(config.commands),
        )
        if not config.enabled:
            raise BaselineConstraintVerificationError(
                f"BASELINE_VERIFICATION_REQUIRED: {project}: constraintVerify.enabled=false "
                "cannot produce an executable dependency plan. Use a report-only workflow instead."
            )
        verification_config_by_project[project] = config

        # BLOCK_X_SOURCE_TRUTH_V1
        # One source epoch is captured before any physical compatibility trial.
        # Every resolver/project experiment in this project run consumes bytes
        # cloned from this sealed snapshot, never from the changing live checkout.
        try:
            source_snapshot = activate_source_snapshot_epoch(
                spec.path,
                timeout_seconds=config.snapshot_copy_timeout_seconds,
                progress=lambda message, project=project: eprint(
                    f"[info] {project}: {message}"
                ),
                progress_interval_seconds=config.progress_interval_seconds,
                replace=True,
            )
        except SourceCaptureError as exc:
            raise BaselineConstraintVerificationError(
                f"SOURCE_SNAPSHOT_CAPTURE_FAILED: {project}: {exc}"
            ) from exc

        identity_manager = detect_package_manager(source_snapshot.project_path)
        identity_executable = resolve_executable(identity_manager)
        project_resolver_context_key = ""
        if identity_executable:
            project_resolver_context_key = build_resolver_context_key(
                source_snapshot.project_path,
                manager=identity_manager,
                manager_executable=identity_executable,
                registry=client.registry,
                environment=dict(os.environ),
            )
        project_source_snapshot_key = (
            source_snapshot.key
            if config.project_checks != "off" and config.commands
            else ""
        )

        external_evidence = (external_evidence_by_project or {}).get(project)
        if external_evidence is not None:
            try:
                progress_reporter.emit(project, external_evidence.target_mode, "external-evidence-localization-started", maxChecks=config.max_delta_checks, parallelism=config.parallelism)
                evidence_nogood, evidence_signatures = localize_compatibility_evidence(
                    external_evidence, base_config=config,
                    parallelism=config.parallelism, max_checks=config.max_delta_checks,
                    progress=lambda event, details: (
                        progress_reporter.emit(project, external_evidence.target_mode, f"external-evidence-{event}", **details),
                        eprint(f"[info] {project}: post-Executor evidence {event}; " + ", ".join(f"{key}={value}" for key, value in details.items()))
                    ),
                )
                progress_reporter.emit(project, external_evidence.target_mode, "external-evidence-localization-complete", signatures=list(evidence_signatures), literals=len(evidence_nogood))
            except CompatibilityEvidenceError as exc:
                raise BaselineConstraintVerificationError(
                    f"BASELINE_VERIFY_INCONCLUSIVE_EXTERNAL_EVIDENCE: {project}: {exc}. "
                    "The post-Executor report was preserved but did not become solver authority."
                ) from None
            evidence_detail = ", ".join(
                f"{name}@{version}" for name, version in sorted(evidence_nogood.items())
            )
            exact_external_assignment = dict(external_evidence.exact_assignment)
            if exact_external_assignment:
                evidence_mode = external_evidence.target_mode
                if exact_external_assignment not in global_exact_exclusions[project][evidence_mode]:
                    global_exact_exclusions[project][evidence_mode].append(
                        exact_external_assignment
                    )
                exact_fingerprint = assignment_fingerprint(exact_external_assignment)
                eprint(
                    f"[warn] {project}: post-Executor evidence certified the full ProofEnvelope "
                    f"assignment as failing for {evidence_mode}; exact={exact_fingerprint}; "
                    f"localizedCandidate=NOT({evidence_detail}) remains diagnostic-only; "
                    f"authority={EVIDENCE_CONFIRMED_CONSTRAINT}"
                )
                progress_reporter.emit(
                    project,
                    evidence_mode,
                    "external-evidence-exact-assignment-blocked",
                    assignment=exact_fingerprint,
                    envelopeKey=external_evidence.proof_envelope_key,
                    localizedCandidate=evidence_detail,
                    authority=EVIDENCE_CONFIRMED_CONSTRAINT,
                    topologyMerged=False,
                )
            else:
                eprint(
                    f"[warn] {project}: legacy post-Executor evidence has no ProofEnvelope; "
                    f"localized candidate NOT({evidence_detail}) stays diagnostic-only and "
                    "does not become solver authority"
                )
                progress_reporter.emit(
                    project,
                    external_evidence.target_mode,
                    "external-evidence-diagnostic-only",
                    literals=len(evidence_nogood),
                    authority=EVIDENCE_DIAGNOSTIC_HINT,
                )

        def adaptive_structural_evidence(result: BaselineVerifyResult) -> Tuple[str, ...]:
            """Return structural signatures introduced by this assignment.

            A baseline command can already be red for ordinary migration debt. The
            old all-or-nothing `control.ok` rule then masked a *new* structural
            failure in the same command (for example TS2307 caused by an exports-only
            plugin under legacy moduleResolution). Compare structural signatures
            per command instead of aggregate exit codes.
            """
            if config.project_checks != "adaptive":
                return ()

            failure_records = result.project_failures
            if not failure_records:
                command = str(result.command or "").strip()
                if not command:
                    return ()
                # Synthetic/legacy results do not carry structured failures. Keep
                # them compatible by treating the single command/output as one.
                candidate_by_command = [(command, set(structural_project_failure_signatures(result)))]
            else:
                candidate_by_command = []
                for failure in failure_records:
                    single = BaselineVerifyResult(
                        False, "project", f"project preflight failed: {failure.command}",
                        command=failure.command, output=failure.output,
                        project_failures=(failure,),
                    )
                    signatures = set(structural_project_failure_signatures(single))
                    if signatures:
                        candidate_by_command.append((failure.command, signatures))

            introduced: Set[str] = set()
            for command, candidate_signatures in candidate_by_command:
                if not candidate_signatures:
                    continue
                # Do not maintain an alternate project/command-only authority cache.
                # verify_assignment already owns the strong Resolver/ProjectProof
                # cache and binds project reuse to an exact ResolvedStateKey.
                control_config = dataclasses.replace(config, project_checks="diagnostic", commands=(command,))
                control = verify_assignment(
                    spec.path, {}, config=control_config, run_project_checks=True, remove_packages=(),
                    progress=lambda message, command=command: (
                        progress_reporter.emit(project, "control", "adaptive-control", command=command, message=message),
                        eprint(f"[info] {project}: Baseline control {command}: {message}"),
                    ),
                    progress_label=f"Baseline control {command}",
                )
                if control.kind in {"infrastructure", "unknown"}:
                    raise BaselineConstraintVerificationError(
                        f"BASELINE_VERIFY_INCONCLUSIVE_CONTROL: {project}: adaptive structural comparison "
                        f"for {command!r} is {control.kind} ({control.summary}); "
                        "UNKNOWN is not equivalent to NOT_INTRODUCED"
                    )
                baseline_signatures = set(structural_project_failure_signatures(control))
                introduced.update(candidate_signatures - baseline_signatures)

            if candidate_by_command and not introduced:
                existing = sorted({sig for _command, sigs in candidate_by_command for sig in sigs})
                eprint(
                    f"[info] {project}: structural diagnostics are already present on current baseline; "
                    f"not learning a new version constraint: {', '.join(existing)}"
                )
            return tuple(sorted(introduced))

        def adaptive_structural_regression(result: BaselineVerifyResult) -> bool:
            return bool(adaptive_structural_evidence(result))

        backend_options = _peer_solver_backend_options(spec.constraint_verify_config)
        if backend_options["persistentLearning"] and spec.constraint_cache_path is not None:
            if not project_resolver_context_key:
                eprint(
                    f"[warn] {project}: package-manager executable is unavailable; "
                    "persistent resolver constraints are not loaded without a canonical ResolverContextKey"
                )
            else:
                cached_nogoods = load_verified_nogoods(
                    spec.constraint_cache_path,
                    project_path=spec.path,
                    environment_fingerprint=project_resolver_context_key,
                )
                if cached_nogoods:
                    for cached_mode in modes:
                        for nogood in cached_nogoods:
                            if nogood not in learned[project][cached_mode]:
                                learned[project][cached_mode].append(dict(nogood))
                    eprint(
                        f"[info] {project}: loaded {len(cached_nogoods)} persistent verified resolver constraint(s); "
                        f"resolverContext={project_resolver_context_key[:12]}"
                    )

        persistent_control_ok: Optional[bool] = None

        rows_for_name: Dict[str, List[DependencyRow]] = defaultdict(list)
        for row in rows:
            rows_for_name[row.name].append(row)
        rows_by_name = {
            name: _aggregate_duplicate_package_row(items)
            for name, items in rows_for_name.items()
        }
        fixed_input_names = tuple(
            sorted(
                name
                for name, row in rows_by_name.items()
                if _is_fixed_dependency_input(row)
            )
        )
        solver_managed_inputs = len(rows_by_name) - len(fixed_input_names)
        baseline_current_versions = {name: row.current_version for name, row in rows_by_name.items()}
        baseline_keep_current = sorted(name for name in rows_by_name if _baseline_intent_policy(name) == "keep-current")
        baseline_required = sorted(name for name in rows_by_name if _baseline_intent_policy(name) == "required")
        if baseline_keep_current or baseline_required:
            eprint(
                f"[info] {project}: user Baseline intent applied; keepCurrent={baseline_keep_current}, "
                f"required={baseline_required}; authority=USER_POLICY"
            )

        for mode in modes:
            recovery_identity = baseline_run_identity(
                project=project,
                mode=mode,
                source_snapshot_key=project_source_snapshot_key,
                resolver_context_key=project_resolver_context_key,
                config={
                    "commands": list(config.commands),
                    "projectChecks": config.project_checks,
                    "registry": config.registry,
                    "maxIterations": config.max_iterations,
                    "maxDeltaChecks": config.max_delta_checks,
                    "parallelism": config.parallelism,
                    "timeoutSeconds": config.timeout_seconds,
                    "attemptTimeoutSeconds": config.attempt_timeout_seconds,
                    "localizationTimeoutSeconds": config.localization_timeout_seconds,
                    "baselineIntent": {
                        "keepCurrent": baseline_keep_current,
                        "required": baseline_required,
                    },
                },
            )
            recovery_plan = run_recovery_store.begin(
                project, mode, identity=recovery_identity,
                policy=baseline_resume_policy(),
                epochs=recovery_epochs,
            )
            if recovery_plan.reason == "active-run":
                raise BaselineConstraintVerificationError(
                    f"BASELINE_RECOVERY_CONCURRENT_RUN: {project}/{mode}: "
                    f"another live Baseline owns the recovery slot "
                    f"(pid={recovery_plan.active_owner_pid})"
                )
            if baseline_resume_policy() == "continue" and not recovery_plan.resumable:
                progress_reporter.emit(project, mode, "recovery-continue-unavailable", reason=recovery_plan.reason, previousStatus=recovery_plan.previous_status, changedEpochs=list(recovery_plan.changed_epochs), invalidatedComponents=list(recovery_plan.invalidated_components), recheckFrom=recovery_plan.recheck_from, authority="ORCHESTRATION_HINT")
                raise BaselineConstraintVerificationError(f"BASELINE_RECOVERY_CONTINUE_UNAVAILABLE: {project}/{mode}: reason={recovery_plan.reason or 'checkpoint-missing'}; previousStatus={recovery_plan.previous_status or '<none>'}. Use Start over explicitly to reset the Baseline checkpoint.")
            if (
                recovery_plan.reason == "restart-requested"
                or "predicate" in recovery_plan.changed_epochs
            ):
                predicate_state_store.clear_run(project, mode)
            predicate_diagnostic_preferences.setdefault(project, {})[mode] = (
                predicate_state_store.preferred_versions(
                    project, mode, run_identity=recovery_identity
                )
            )
            restored_failed: Set[str] = set()
            restored_iteration = 0
            restored_liveness: Dict[str, object] = {}
            if recovery_plan.resumable:
                (
                    restored_learned, restored_exclusions, restored_failed,
                    restored_iteration, restored_liveness,
                ) = restore_run_state(recovery_plan.state)
                for clause in restored_learned:
                    if clause not in learned[project][mode]:
                        learned[project][mode].append(clause)
                for clause in restored_exclusions:
                    if clause not in global_exact_exclusions[project][mode]:
                        global_exact_exclusions[project][mode].append(clause)
                eprint(
                    f"[info] {project}: Baseline recovery {mode}; "
                    f"resumeFromCompletedIteration={restored_iteration}, "
                    f"learned={len(restored_learned)}, exactExclusions={len(restored_exclusions)}, "
                    f"interrupted={str(recovery_plan.interrupted).lower()}, "
                    f"authority=ORCHESTRATION_HINT"
                )
                progress_reporter.emit(
                    project, mode, "recovery-resumed",
                    completedIteration=restored_iteration,
                    learnedConstraints=len(restored_learned),
                    exactExclusions=len(restored_exclusions),
                    interrupted=recovery_plan.interrupted,
                    changedEpochs=list(recovery_plan.changed_epochs),
                    invalidatedComponents=list(recovery_plan.invalidated_components),
                    recheckFrom=recovery_plan.recheck_from,
                    preservedAuthority=recovery_plan.preserved_authority,
                    authority="ORCHESTRATION_HINT",
                )
            elif recovery_plan.found and recovery_plan.reason == "authority-epoch-changed":
                progress_reporter.emit(
                    project, mode, "recovery-recheck-required",
                    recheckFrom=recovery_plan.recheck_from,
                    changedEpochs=list(recovery_plan.changed_epochs),
                    invalidatedComponents=list(recovery_plan.invalidated_components),
                    preservedAuthority=recovery_plan.preserved_authority,
                    authority="ORCHESTRATION_HINT",
                )

            liveness = BaselineLivenessBudget(
                base_iterations=config.max_iterations,
                max_learning_extensions=config.max_iterations,
                starting_learned_constraints=len(learned[project][mode]),
            )
            user_extra_iterations = _baseline_extra_iterations()
            liveness.max_learning_extensions = max(
                liveness.max_learning_extensions,
                config.max_iterations + user_extra_iterations,
            )
            if restored_liveness:
                restore_liveness_budget(liveness, restored_liveness)
            try:
                restored_user_extensions = max(0, int(restored_liveness.get("userExtensions") or 0))
            except (TypeError, ValueError):
                restored_user_extensions = 0
            consumed_user_extensions = max(
                0,
                restored_iteration - liveness.base_iterations - liveness.certified_extensions,
            )
            liveness.user_extensions = min(
                liveness.max_learning_extensions,
                max(restored_user_extensions, consumed_user_extensions),
            )
            decision_grant_iterations = _baseline_decision_grant_iterations()
            granted_user_iterations = liveness.grant_user_extensions(decision_grant_iterations)
            decision_prompt_not_before = restored_iteration + granted_user_iterations
            eprint(
                f"[info] {project}: Baseline solve-and-verify {mode} started; "
                f"maxIterations={config.max_iterations}, hardIterations={liveness.hard_iterations}, "
                f"learningExtensionLimit={liveness.max_learning_extensions}, parallelism={config.parallelism}, "
                f"solverManagedInputs={solver_managed_inputs}, fixedInputs={len(fixed_input_names)}"
            )
            progress_reporter.emit(
                project,
                mode,
                "solve-and-verify-started",
                maxIterations=config.max_iterations,
                parallelism=config.parallelism,
                maxDeltaChecks=config.max_delta_checks,
                subprocessTimeoutSeconds=config.timeout_seconds,
                attemptHardTimeoutSeconds=config.attempt_timeout_seconds,
                localizationHardTimeoutSeconds=config.localization_timeout_seconds,
                solverManagedInputs=solver_managed_inputs,
                fixedInputs=len(fixed_input_names),
                **liveness.snapshot(learned_constraints=len(learned[project][mode])),
            )
            last_fingerprint = ""
            confirmed_failed_assignments: Set[str] = set(restored_failed)
            iteration = restored_iteration

            def checkpoint_baseline_run(
                phase: str,
                *,
                completed_iteration: Optional[int] = None,
                last_assignment: str = "",
                last_predicate: str = "",
                status: str = "running",
            ) -> None:
                safe_iteration = iteration if completed_iteration is None else completed_iteration
                state = build_run_state(
                    iteration=safe_iteration,
                    learned_constraints=learned[project][mode],
                    global_exact_exclusions=global_exact_exclusions[project][mode],
                    confirmed_failed_assignments=sorted(confirmed_failed_assignments),
                    liveness=liveness.snapshot(learned_constraints=len(learned[project][mode])),
                    last_assignment=last_assignment,
                    last_predicate=last_predicate,
                )
                if status in {"completed", "passed"}:
                    run_recovery_store.mark_terminal(
                        project, mode, identity=recovery_identity, status=status,
                        state=state, phase=phase, epochs=recovery_epochs,
                    )
                else:
                    run_recovery_store.checkpoint(
                        project, mode, identity=recovery_identity,
                        state=state, status=status, phase=phase, epochs=recovery_epochs,
                    )

            # This is a SAFE cursor: no current subprocess is represented as done.
            # If the process dies later, this completed-iteration state is retried fresh.
            checkpoint_baseline_run("ready", completed_iteration=iteration)
            while iteration < liveness.allowed_iterations:
                iteration += 1
                progress_reporter.emit(
                    project,
                    mode,
                    "iteration-started",
                    iteration=iteration,
                    **liveness.snapshot(learned_constraints=len(learned[project][mode])),
                )
                solver_statuses: Dict[str, Dict[str, Dict[str, str]]] = {}
                try:
                    candidate_map = resolve_peer_compatibility(
                        {project: rows}, client,
                        modes=(mode,),
                        learned_nogoods_by_project_mode=learned,
                        global_exact_exclusions_by_project_mode=global_exact_exclusions,
                        apply_results=False,
                        solver_statuses_out=solver_statuses,
                        shadow_solver_config_by_project={project: spec.constraint_verify_config},
                        residual_targets_by_project=residual_targets_by_project,
                        diagnostic_preferences_by_project_mode=predicate_diagnostic_preferences,
                    )
                except BaselineConstraintVerificationError as exc:
                    active_intent_packages = sorted(set(baseline_keep_current) | set(baseline_required))
                    if (
                        _baseline_interactive()
                        and exc.terminal_status == BaselineTerminalStatus.UNSAT_PROVEN.value
                        and active_intent_packages
                    ):
                        focus_name = active_intent_packages[0] if len(active_intent_packages) == 1 else ""
                        checkpoint_baseline_run(
                            "human-decision-required",
                            completed_iteration=max(restored_iteration, iteration - 1),
                            status="decision-required",
                        )
                        decision_payload = {
                            "schemaVersion": 1,
                            "reason": "policy-unsat",
                            "project": project,
                            "mode": mode,
                            "iteration": iteration,
                            "hardIterations": liveness.hard_iterations,
                            "learnedConstraints": len(learned[project][mode]),
                            **({
                                "package": focus_name,
                                "currentVersion": baseline_current_versions.get(focus_name, ""),
                            } if focus_name else {}),
                        }
                        progress_reporter.emit(
                            project, mode, "human-decision-required",
                            iteration=iteration,
                            stopCode="BASELINE_HUMAN_DECISION_REQUIRED",
                            terminalStatus="HUMAN_DECISION_REQUIRED",
                            reason="policy-unsat",
                        )
                        _raise_baseline_human_decision(decision_payload)
                    if exc.terminal_status:
                        progress_reporter.emit(
                            project,
                            mode,
                            "solver-terminal",
                            iteration=iteration,
                            terminalStatus=exc.terminal_status,
                            terminalSource=exc.terminal_source,
                            stopCode=exc.stop_code,
                            **liveness.snapshot(learned_constraints=len(learned[project][mode])),
                        )
                    raise
                assignment = candidate_map[project][mode]
                component_statuses = solver_statuses.get(project, {}).get(mode, {})
                unknown_budget_names = sorted(
                    name for name, status in component_statuses.items() if status == "unknown_budget"
                )
                sat_unproven_names = sorted(
                    name for name, status in component_statuses.items() if status == "sat_unproven"
                )
                changed = _changed_assignment(assignment, rows_by_name)
                verification_assignment = _verification_assignment(assignment, rows_by_name)
                removals = _types_stub_removals_for_assignment(rows_by_name, assignment, mode, client)
                fingerprint = assignment_fingerprint(verification_assignment)
                if fingerprint in confirmed_failed_assignments:
                    raise BaselineConstraintVerificationError(
                        f"BASELINE_SOLVER_REPEATED_FAILED_ASSIGNMENT: {project}/{mode}: "
                        f"solver returned previously confirmed failing assignment {fingerprint}; "
                        "authoritative learned/exact constraints were not respected"
                    )
                if not changed and not removals:
                    # Zero managed delta still needs resolver proof when fixed
                    # inputs participate in the real package-manager graph.
                    if fixed_input_names:
                        eprint(
                            f"[info] {project}: Baseline verify {mode}: no managed target changes; "
                            f"certifying resolver state with fixedInputs={len(fixed_input_names)}"
                        )
                        noop_result = verify_assignment(
                            spec.path,
                            verification_assignment,
                            config=config,
                            run_project_checks=False,
                            remove_packages=(),
                            progress=lambda message: (
                                progress_reporter.emit(
                                    project,
                                    mode,
                                    "noop-fixed-resolver-verification",
                                    iteration=iteration,
                                    assignment=fingerprint,
                                    message=message,
                                ),
                                eprint(f"[info] {project}: {message}"),
                            ),
                            progress_label=f"Baseline {mode} no-op fixed resolver {fingerprint}",
                        )
                        if noop_result.kind == "infrastructure":
                            raise _baseline_terminal_error(
                                BaselineTerminalStatus.INFRASTRUCTURE_FAILURE,
                                "BASELINE_VERIFY_INFRA_ERROR",
                                f"{project}/{mode}: {noop_result.summary}; no-op dependency proof was not produced",
                                source="package-manager-verification",
                            )
                        if noop_result.kind == "unknown":
                            raise _baseline_terminal_error(
                                BaselineTerminalStatus.SOLVER_UNKNOWN,
                                "BASELINE_VERIFY_UNKNOWN_ERROR",
                                f"{project}/{mode}: {noop_result.summary}; no-op dependency proof was not produced",
                                source="package-manager-verification",
                            )
                        if not noop_result.ok:
                            raise BaselineConstraintVerificationError(
                                f"BASELINE_NOOP_RESOLVER_INVALID: {project}/{mode}: "
                                f"fixed-input resolver state is not installable: {noop_result.summary}"
                            )
                    if unknown_budget_names:
                        eprint(
                            f"[warn] {project}: Baseline verify {mode}: solver UNKNOWN_BUDGET for "
                            f"{len(unknown_budget_names)} package(s); current assignment is only a fallback, not an UNSAT proof"
                        )
                    else:
                        eprint(f"[info] {project}: Baseline verify {mode}: no target changes to materialize")
                    final_assignments.setdefault(project, {})[mode] = assignment
                    checkpoint_baseline_run(
                        "mode-passed-no-changes", completed_iteration=iteration,
                        last_assignment=fingerprint, status="completed",
                    )
                    progress_reporter.emit(
                        project,
                        mode,
                        "mode-passed-no-changes",
                        iteration=iteration,
                        assignment=fingerprint,
                        terminalStatus=(
                            BaselineTerminalStatus.SOLVER_UNKNOWN.value
                            if unknown_budget_names
                            else BaselineTerminalStatus.SAT_PROVEN.value
                        ),
                        fixedResolverProofRequired=bool(fixed_input_names),
                        **liveness.snapshot(learned_constraints=len(learned[project][mode])),
                    )
                    break

                if not project_resolver_context_key:
                    # The verifier will report the missing package manager as an
                    # infrastructure failure. Do not invent a cache identity.
                    resolver_trial_key = ""
                else:
                    resolver_trial_key = build_resolver_trial_key(
                        resolver_context_key=project_resolver_context_key,
                        assignment=verification_assignment,
                        remove_packages=tuple(sorted(removals)),
                    )
                result = resolver_cache.get(resolver_trial_key) if resolver_trial_key else None
                if result is None:
                    eprint(
                        f"[info] {project}: Baseline verify {mode} iteration {iteration}: "
                        f"proving exact solver-managed assignment ({len(verification_assignment)} managed direct; "
                        f"fixedInputs={len(fixed_input_names)}; {len(changed)} changed), assignment={fingerprint}"
                    )
                    # BLOCK_U_EARLY_PROJECT_SCREEN_V1
                    # Resolver authority first. Project checks are scheduled below
                    # so adaptive mode can reject a freshly introduced structural
                    # regression before paying for the complete project suite.
                    result = verify_assignment(
                        spec.path,
                        verification_assignment,
                        config=config,
                        run_project_checks=False,
                        remove_packages=removals,
                        progress=lambda message: (
                            progress_reporter.emit(
                                project, mode, "resolver-verification",
                                iteration=iteration,
                                assignment=fingerprint,
                                message=message,
                            ),
                            eprint(f"[info] {project}: {message}"),
                        ),
                        progress_label=(
                            f"Baseline {mode} iteration {iteration} "
                            f"resolver {fingerprint}"
                        ),
                    )
                    if resolver_trial_key and (
                        result.ok
                        or (result.kind == "dependency" and not result.resolved_state_key)
                    ):
                        resolver_cache[resolver_trial_key] = result
                else:
                    eprint(
                        f"[info] {project}: Baseline verify {mode}: reused exact ResolverTrialKey "
                        f"{resolver_trial_key[:12]} (display assignment={fingerprint})"
                    )

                if result.kind == "infrastructure":
                    progress_reporter.emit(
                        project, mode, "verification-terminal", iteration=iteration,
                        assignment=fingerprint,
                        terminalStatus=BaselineTerminalStatus.INFRASTRUCTURE_FAILURE.value,
                        terminalSource="package-manager-verification",
                        stopCode="BASELINE_VERIFY_INFRA_ERROR",
                    )
                    raise _baseline_terminal_error(
                        BaselineTerminalStatus.INFRASTRUCTURE_FAILURE,
                        "BASELINE_VERIFY_INFRA_ERROR",
                        f"{project}/{mode}: {result.summary}; dependency plan was preserved; fix infrastructure and rerun generation",
                        source="package-manager-verification",
                    )
                if result.kind == "unknown":
                    diagnostic_tail = _sanitized_baseline_failure_tail(result.output)
                    diagnostic = (
                        f"\nResolver diagnostic tail (sanitized):\n{diagnostic_tail}"
                        if diagnostic_tail
                        else ""
                    )
                    progress_reporter.emit(
                        project, mode, "verification-terminal", iteration=iteration,
                        assignment=fingerprint,
                        terminalStatus=BaselineTerminalStatus.SOLVER_UNKNOWN.value,
                        terminalSource="package-manager-verification",
                        stopCode="BASELINE_VERIFY_UNKNOWN_ERROR",
                    )
                    raise _baseline_terminal_error(
                        BaselineTerminalStatus.SOLVER_UNKNOWN,
                        "BASELINE_VERIFY_UNKNOWN_ERROR",
                        f"{project}/{mode}: {result.summary}; resolver failure was not classified as dependency evidence, so no compatibility constraint was learned{diagnostic}",
                        source="package-manager-verification",
                    )

                if result.ok:
                    _annotate_constraint_preflight(rows, mode, assignment, result, iteration=iteration)
                    # Code/lifecycle checks are evidence, not version authority by default.
                    if config.project_checks != "off" and config.commands:
                        project_cache_key = ""
                        if resolver_trial_key and result.resolved_state_key:
                            project_cache_key = build_project_trial_key(
                                resolver_trial_key=resolver_trial_key,
                                resolved_state_key=result.resolved_state_key,
                                source_snapshot_key=project_source_snapshot_key,
                                project_checks=config.project_checks,
                                commands=config.commands,
                            )
                        project_result = (
                            project_preflight_cache.get(project_cache_key)
                            if project_cache_key
                            else None
                        )
                        screen_structural_evidence: Tuple[str, ...] = ()
                        if (
                            project_result is None
                            and config.project_checks == "adaptive"
                            and len(config.commands) > 1
                        ):
                            screen_command = config.commands[0]
                            screen_config = dataclasses.replace(
                                config, commands=(screen_command,)
                            )
                            progress_reporter.emit(
                                project, mode, "adaptive-screen-started",
                                iteration=iteration,
                                assignment=fingerprint,
                                command=screen_command,
                            )
                            screen_result = verify_assignment(
                                spec.path,
                                verification_assignment,
                                config=screen_config,
                                run_project_checks=True,
                                remove_packages=removals,
                                progress=lambda message: (
                                    progress_reporter.emit(
                                        project, mode, "adaptive-screen-running",
                                        iteration=iteration,
                                        assignment=fingerprint,
                                        command=screen_command,
                                        message=message,
                                    ),
                                    eprint(
                                        f"[info] {project}: Baseline adaptive "
                                        f"screen {screen_command}: {message}"
                                    ),
                                ),
                                progress_label=(
                                    f"Baseline {mode} iteration {iteration} "
                                    f"adaptive screen {fingerprint}"
                                ),
                            )
                            if screen_result.kind == "infrastructure":
                                raise BaselineConstraintVerificationError(
                                    f"BASELINE_VERIFY_INFRA_ERROR: "
                                    f"{project}/{mode}: adaptive screen "
                                    f"{screen_command}: {screen_result.summary}"
                                )
                            if screen_result.kind == "unknown":
                                raise BaselineConstraintVerificationError(
                                    f"BASELINE_VERIFY_UNKNOWN_ERROR: "
                                    f"{project}/{mode}: adaptive screen "
                                    f"{screen_command}: {screen_result.summary}"
                                )
                            if not screen_result.ok:
                                screen_structural_evidence = (
                                    adaptive_structural_evidence(screen_result)
                                )
                            if screen_structural_evidence:
                                project_result = screen_result
                                progress_reporter.emit(
                                    project, mode,
                                    "adaptive-screen-introduced-regression",
                                    iteration=iteration,
                                    assignment=fingerprint,
                                    command=screen_command,
                                    structuralEvidence=list(
                                        screen_structural_evidence
                                    ),
                                )
                                eprint(
                                    f"[warn] {project}: adaptive screen rejected "
                                    f"assignment {fingerprint} before remaining "
                                    f"{len(config.commands) - 1} project check(s); "
                                    f"introduced="
                                    f"{', '.join(screen_structural_evidence)}"
                                )
                            else:
                                progress_reporter.emit(
                                    project, mode, "adaptive-screen-complete",
                                    iteration=iteration,
                                    assignment=fingerprint,
                                    command=screen_command,
                                    outcome=(
                                        "pass"
                                        if screen_result.ok
                                        else "baseline-preexisting-or-nonstructural"
                                    ),
                                )

                        # Successful candidates still need the full configured
                        # ProjectProof. Screening only short-circuits a freshly
                        # proven introduced structural regression.
                        if project_result is None:
                            project_result = verify_assignment(
                                spec.path, verification_assignment, config=config, run_project_checks=True, remove_packages=removals,
                                progress=lambda message: (progress_reporter.emit(project, mode, "project-preflight", iteration=iteration, assignment=fingerprint, message=message), eprint(f"[info] {project}: {message}")),
                                progress_label=f"Baseline {mode} iteration {iteration} project preflight {fingerprint}",
                            )
                            if (
                                project_result.kind not in {"infrastructure", "unknown"}
                                and resolver_trial_key
                                and project_result.resolved_state_key
                            ):
                                exact_project_key = build_project_trial_key(
                                    resolver_trial_key=resolver_trial_key,
                                    resolved_state_key=project_result.resolved_state_key,
                                    source_snapshot_key=project_source_snapshot_key,
                                    project_checks=config.project_checks,
                                    commands=config.commands,
                                )
                                project_preflight_cache[exact_project_key] = project_result
                        else:
                            eprint(
                                f"[info] {project}: Baseline project preflight reused exact ProjectTrialKey "
                                f"{project_cache_key[:12]} (display assignment={fingerprint})"
                            )
                        if project_result.kind == "infrastructure":
                            raise BaselineConstraintVerificationError(
                                f"BASELINE_VERIFY_INFRA_ERROR: {project}/{mode}: {project_result.summary}. "
                                "Dependency plan was preserved; fix infrastructure and rerun generation."
                            )
                        if project_result.kind == "unknown":
                            raise BaselineConstraintVerificationError(
                                f"BASELINE_VERIFY_UNKNOWN_ERROR: {project}/{mode}: {project_result.summary}. "
                                "Project preflight failure was not classified as dependency evidence."
                            )
                        _annotate_constraint_preflight(rows, mode, assignment, project_result, iteration=iteration)
                        structural_evidence = (
                            screen_structural_evidence
                            or adaptive_structural_evidence(project_result)
                        )
                        structural_failure = bool(structural_evidence)
                        if not project_result.ok and (
                            config.project_checks == "strict" or structural_failure
                        ):
                            result = project_result
                            evidence_text = ", ".join(structural_evidence) if structural_evidence else "strict-project-check"
                            eprint(
                                f"[warn] {project}: {mode} structural project preflight rejected assignment "
                                f"{fingerprint}: structural regressions=[{evidence_text}]; failedChecks={project_result.summary}"
                            )
                        elif not project_result.ok:
                            eprint(
                                f"[info] {project}: {mode} dependency assignment is resolver-green; "
                                f"project migration is expected: {project_result.summary}"
                            )
                    if result.ok:
                        final_assignments.setdefault(project, {})[mode] = assignment
                        if unknown_budget_names:
                            eprint(
                                f"[warn] {project}: Baseline solve-and-verify {mode} VERIFIED_FALLBACK after "
                                f"{iteration} iteration(s); assignment={fingerprint}; solver=UNKNOWN_BUDGET; "
                                f"affectedPackages={len(unknown_budget_names)}"
                            )
                        elif sat_unproven_names:
                            eprint(
                                f"[warn] {project}: Baseline solve-and-verify {mode} VERIFIED_SAT_UNPROVEN after "
                                f"{iteration} iteration(s); assignment={fingerprint}; affectedPackages={len(sat_unproven_names)}"
                            )
                        else:
                            eprint(
                                f"[info] {project}: Baseline solve-and-verify {mode} PASSED "
                                f"after {iteration} iteration(s); assignment={fingerprint}"
                            )
                        checkpoint_baseline_run(
                            "mode-passed", completed_iteration=iteration,
                            last_assignment=fingerprint, status="completed",
                        )
                        progress_reporter.emit(
                            project,
                            mode,
                            "mode-passed",
                            iteration=iteration,
                            assignment=fingerprint,
                            terminalStatus=(
                                BaselineTerminalStatus.SOLVER_UNKNOWN.value
                                if unknown_budget_names or sat_unproven_names
                                else BaselineTerminalStatus.SAT_PROVEN.value
                            ),
                            **liveness.snapshot(learned_constraints=len(learned[project][mode])),
                        )
                        break

                # Proof-preserving fast path: forbid exactly the full assignment
                # that failed, then re-solve immediately. A localized witness is
                # not automatically a context-independent Solver nogood.
                if result.kind in {"dependency", "preparation", "project"}:
                    exact_nogood = dict(verification_assignment)
                    if not exact_nogood:
                        raise BaselineConstraintVerificationError(
                            f"BASELINE_PLAN_BROKEN: {project}/{mode}: failing assignment has no solver-owned literals"
                        )

                    confirmation_project_checks = result.kind in {"preparation", "project"}
                    confirmation_config = config
                    if result.kind == "project" and config.project_checks == "adaptive":
                        targeted_commands = _targeted_adaptive_confirmation_commands(result, config)
                        if targeted_commands != config.commands:
                            confirmation_config = dataclasses.replace(config, commands=targeted_commands)
                            eprint(
                                f"[info] {project}: exact confirmation narrowed project checks "
                                f"from {len(config.commands)} to {len(targeted_commands)} responsible command(s): "
                                f"{', '.join(targeted_commands)}"
                            )
                            progress_reporter.emit(
                                project, mode, "exact-assignment-confirmation-targeted",
                                iteration=iteration, assignment=fingerprint,
                                commands=list(targeted_commands),
                            )
                    expected_signature = dependency_failure_signature(
                        summary=result.summary, output=result.output
                    )
                    expected_structural = (
                        set(adaptive_structural_evidence(result))
                        if result.kind == "project" and config.project_checks == "adaptive"
                        else set()
                    )
                    eprint(
                        f"[info] {project}: Baseline exact-assignment confirmation {mode} started; "
                        f"assignment={fingerprint}; literals={len(exact_nogood)}; origin={result.kind}"
                    )
                    progress_reporter.emit(
                        project, mode, "exact-assignment-confirmation-started",
                        iteration=iteration, assignment=fingerprint,
                        literals=len(exact_nogood), origin=result.kind,
                    )
                    confirmation = verify_assignment(
                        spec.path, verification_assignment, config=confirmation_config,
                        run_project_checks=confirmation_project_checks, remove_packages=removals,
                        progress=lambda message: (
                            progress_reporter.emit(
                                project, mode, "exact-assignment-confirmation-running",
                                iteration=iteration, assignment=fingerprint, message=message,
                            ),
                            eprint(f"[info] {project}: Baseline exact confirmation {mode}: {message}"),
                        ),
                        progress_label=f"Baseline exact confirmation {mode} {fingerprint}",
                    )
                    if confirmation.kind in {"infrastructure", "unknown"}:
                        raise BaselineConstraintVerificationError(
                            f"BASELINE_VERIFY_INCONCLUSIVE_CONFIRMATION: {project}/{mode}: "
                            f"{confirmation.kind}: {confirmation.summary}; no solver constraint was learned"
                        )

                    if result.kind == "dependency":
                        observed_signature = (
                            matching_dependency_failure_signature(
                                expected_summary=result.summary,
                                expected_output=result.output,
                                observed_summary=confirmation.summary,
                                observed_output=confirmation.output,
                            )
                            if confirmation.kind == "dependency"
                            else ""
                        )
                        confirmed_exact_failure = bool(observed_signature)
                    elif result.kind == "preparation":
                        observed_signature = dependency_failure_signature(
                            summary=confirmation.summary,
                            output=confirmation.output,
                        )
                        confirmed_exact_failure = (
                            confirmation.kind == "preparation"
                            and observed_signature == expected_signature
                        )
                    elif config.project_checks == "adaptive":
                        observed_structural = (
                            set(adaptive_structural_evidence(confirmation))
                            if confirmation.kind == "project" else set()
                        )
                        confirmed_exact_failure = (
                            bool(expected_structural)
                            and confirmation.kind == "project"
                            and observed_structural == expected_structural
                        )
                    else:
                        observed_signature = dependency_failure_signature(
                            summary=confirmation.summary, output=confirmation.output
                        )
                        confirmed_exact_failure = (
                            confirmation.kind == "project" and observed_signature == expected_signature
                        )

                    if not confirmed_exact_failure:
                        raise BaselineConstraintVerificationError(
                            f"BASELINE_VERIFY_INCONCLUSIVE_CONFIRMATION: {project}/{mode}: "
                            "the exact assignment did not reproduce the same authoritative failure; "
                            "no solver constraint was learned"
                        )
                    confirmed_failed_assignments.add(fingerprint)

                    # BLOCK_VF_ACTIVE_PREDICATE_EXECUTION_V1
                    # Exact confirmation above is the authority boundary. Point
                    # probes below are diagnostic experiments only. A useful probe
                    # may steer the next exact solve by reordering a complete domain,
                    # but cannot create a clause or prune any version.
                    predicate_search_steered = False
                    if expected_structural:
                        for target_predicate in sorted(expected_structural):
                            predicate_pkg = predicate_package(target_predicate)
                            current_probe_version = str(
                                verification_assignment.get(predicate_pkg, "")
                            )
                            if not predicate_pkg or not current_probe_version:
                                continue
                            meta = client.npm_cache.get(predicate_pkg)
                            row = rows_by_name.get(predicate_pkg)
                            if not isinstance(meta, dict) or row is None:
                                continue

                            observation_key = (
                                project, mode, predicate_pkg.lower(), target_predicate
                            )
                            session = predicate_state_store.load_session(
                                project, mode,
                                run_identity=recovery_identity,
                                package=predicate_pkg,
                                predicate=target_predicate,
                            )
                            observations = predicate_probe_observations.setdefault(
                                observation_key, list(session.observations)
                            )
                            for restored_point in session.observations:
                                if restored_point not in observations:
                                    observations.append(restored_point)

                            # If a prior soft preference itself reproduced the same
                            # predicate, it has served its purpose and must not keep
                            # steering future solves. This is navigation state only.
                            if session.preferred_version == current_probe_version:
                                predicate_state_store.clear_preferred_version(
                                    project, mode,
                                    run_identity=recovery_identity,
                                    package=predicate_pkg,
                                    predicate=target_predicate,
                                )
                                predicate_diagnostic_preferences.setdefault(project, {}).setdefault(mode, {}).pop(
                                    predicate_pkg, None
                                )

                            point = PredicateObservation(
                                package=predicate_pkg,
                                version=current_probe_version,
                                predicate=target_predicate,
                                present=True,
                                assignment_fingerprint=fingerprint,
                                other_predicates=tuple(sorted(
                                    item for item in expected_structural
                                    if item != target_predicate
                                )),
                            )
                            if point not in observations:
                                observations.append(point)
                            predicate_state_store.save_observations(
                                project, mode,
                                run_identity=recovery_identity,
                                package=predicate_pkg,
                                predicate=target_predicate,
                                observations=observations,
                            )

                            published = published_versions(
                                meta, include_prerelease=False
                            )
                            structural_versions, _ = client.registry_structural_candidates(
                                meta, published
                            )
                            probe_domain = [
                                version for version in structural_versions
                                if compare_semver(version, row.current_version) in {0, 1}
                            ]
                            ranked = rank_version_probes(
                                package=predicate_pkg,
                                predicate=target_predicate,
                                versions=probe_domain,
                                observations=observations,
                                hints=compatibility_hints,
                            )
                            if ranked:
                                suggested = ranked[0]
                                progress_reporter.emit(
                                    project, mode, "predicate-probe-suggested",
                                    iteration=iteration,
                                    assignment=fingerprint,
                                    predicate=target_predicate,
                                    package=predicate_pkg,
                                    currentVersion=current_probe_version,
                                    suggestedVersion=suggested.version,
                                    remainingCandidates=len(ranked),
                                    reasons=list(suggested.reasons),
                                    authority=EVIDENCE_DIAGNOSTIC_HINT,
                                )
                                eprint(
                                    f"[info] {project}: predicate-guided probe suggestion {mode}; "
                                    f"predicate={target_predicate}, package={predicate_pkg}, "
                                    f"current={current_probe_version}, suggested={suggested.version}, "
                                    f"remaining={len(ranked)}, reasons={','.join(suggested.reasons)}, "
                                    f"authority={EVIDENCE_DIAGNOSTIC_HINT}"
                                )

                            def run_predicate_probe(
                                probe_version: str,
                                probe_assignment: Mapping[str, str],
                            ) -> ProbeExecution:
                                probe_materialization = dict(probe_assignment)
                                probe_fingerprint = assignment_fingerprint(
                                    probe_materialization
                                )
                                probe_removals = _types_stub_removals_for_assignment(
                                    rows_by_name, probe_materialization, mode, client
                                )
                                progress_reporter.emit(
                                    project, mode, "predicate-probe-active-started",
                                    iteration=iteration,
                                    assignment=fingerprint,
                                    candidate=probe_fingerprint,
                                    package=predicate_pkg,
                                    version=probe_version,
                                    predicate=target_predicate,
                                    authority=EVIDENCE_DIAGNOSTIC_HINT,
                                )
                                eprint(
                                    f"[info] {project}: predicate-guided active probe {mode}; "
                                    f"package={predicate_pkg}, version={probe_version}, "
                                    f"predicate={target_predicate}, candidate={probe_fingerprint}, "
                                    f"authority={EVIDENCE_DIAGNOSTIC_HINT}"
                                )
                                probe_result = verify_assignment(
                                    spec.path,
                                    probe_materialization,
                                    config=confirmation_config,
                                    run_project_checks=True,
                                    remove_packages=probe_removals,
                                    progress=lambda message, probe_version=probe_version, probe_fingerprint=probe_fingerprint: (
                                        progress_reporter.emit(
                                            project, mode, "predicate-probe-active-running",
                                            iteration=iteration, assignment=fingerprint,
                                            candidate=probe_fingerprint,
                                            package=predicate_pkg, version=probe_version,
                                            predicate=target_predicate, message=message,
                                        ),
                                        eprint(
                                            f"[info] {project}: predicate probe {mode} "
                                            f"{predicate_pkg}@{probe_version}: {message}"
                                        ),
                                    ),
                                    progress_label=(
                                        f"Baseline predicate probe {mode} "
                                        f"{predicate_pkg}@{probe_version} {probe_fingerprint}"
                                    ),
                                )
                                if probe_result.kind in {"infrastructure", "unknown", "dependency", "preparation"}:
                                    return ProbeExecution(
                                        version=probe_version,
                                        outcome=PROBE_OUTCOME_INCONCLUSIVE,
                                        assignment_fingerprint=probe_fingerprint,
                                        detail=f"{probe_result.kind}:{probe_result.summary}",
                                    )
                                if probe_result.ok:
                                    return ProbeExecution(
                                        version=probe_version,
                                        outcome=PROBE_OUTCOME_ABSENT,
                                        assignment_fingerprint=probe_fingerprint,
                                    )
                                if probe_result.kind != "project":
                                    return ProbeExecution(
                                        version=probe_version,
                                        outcome=PROBE_OUTCOME_INCONCLUSIVE,
                                        assignment_fingerprint=probe_fingerprint,
                                        detail=f"unexpected:{probe_result.kind}",
                                    )
                                probe_structural = set(
                                    structural_project_failure_signatures(probe_result)
                                )
                                present = target_predicate in probe_structural
                                return ProbeExecution(
                                    version=probe_version,
                                    outcome=(
                                        PROBE_OUTCOME_PRESENT
                                        if present
                                        else PROBE_OUTCOME_ABSENT
                                    ),
                                    assignment_fingerprint=probe_fingerprint,
                                    other_predicates=tuple(sorted(
                                        item for item in probe_structural
                                        if item != target_predicate
                                    )),
                                )

                            search_result = run_active_predicate_search(
                                package=predicate_pkg,
                                predicate=target_predicate,
                                base_assignment=verification_assignment,
                                project_current_version=row.current_version,
                                versions=probe_domain,
                                observations=observations,
                                attempted_versions=session.attempted_versions,
                                hints=compatibility_hints,
                                policy=predicate_probe_policy,
                                run_probe=run_predicate_probe,
                            )
                            if search_result.activated:
                                predicate_probe_observations[observation_key] = list(
                                    search_result.observations
                                )
                                predicate_state_store.save_observations(
                                    project, mode,
                                    run_identity=recovery_identity,
                                    package=predicate_pkg,
                                    predicate=target_predicate,
                                    observations=search_result.observations,
                                )
                                for probe_execution in search_result.executions:
                                    predicate_state_store.mark_attempt(
                                        project, mode,
                                        run_identity=recovery_identity,
                                        package=predicate_pkg,
                                        predicate=target_predicate,
                                        version=probe_execution.version,
                                    )
                                    progress_reporter.emit(
                                        project, mode, "predicate-probe-observed",
                                        iteration=iteration, assignment=fingerprint,
                                        package=predicate_pkg,
                                        version=probe_execution.version,
                                        predicate=target_predicate,
                                        outcome=probe_execution.outcome,
                                        detail=probe_execution.detail,
                                        authority=(
                                            "POINT_EVIDENCE"
                                            if probe_execution.outcome != PROBE_OUTCOME_INCONCLUSIVE
                                            else EVIDENCE_DIAGNOSTIC_HINT
                                        ),
                                    )
                                    eprint(
                                        f"[info] {project}: predicate probe observation {mode}; "
                                        f"package={predicate_pkg}, version={probe_execution.version}, "
                                        f"predicate={target_predicate}, outcome={probe_execution.outcome}, "
                                        f"authority={'POINT_EVIDENCE' if probe_execution.outcome != PROBE_OUTCOME_INCONCLUSIVE else EVIDENCE_DIAGNOSTIC_HINT}"
                                    )

                            preferred_version = search_result.preferred_version
                            if preferred_version:
                                predicate_state_store.set_preferred_version(
                                    project, mode,
                                    run_identity=recovery_identity,
                                    package=predicate_pkg,
                                    predicate=target_predicate,
                                    version=preferred_version,
                                )
                                predicate_diagnostic_preferences.setdefault(project, {}).setdefault(mode, {})[
                                    predicate_pkg
                                ] = preferred_version
                                progress_reporter.emit(
                                    project, mode, "predicate-search-steering-selected",
                                    iteration=iteration, assignment=fingerprint,
                                    package=predicate_pkg,
                                    predicate=target_predicate,
                                    preferredVersion=preferred_version,
                                    repeatCount=search_result.repeat_count,
                                    probes=len(search_result.executions),
                                    remainingCandidates=search_result.remaining_candidates,
                                    authority=EVIDENCE_DIAGNOSTIC_HINT,
                                )
                                eprint(
                                    f"[info] {project}: predicate-guided search selected soft fallback {mode}; "
                                    f"package={predicate_pkg}, preferred={preferred_version}, "
                                    f"predicate={target_predicate}, repeat={search_result.repeat_count}, "
                                    f"probes={len(search_result.executions)}, "
                                    f"authority={EVIDENCE_DIAGNOSTIC_HINT}; solver domain remains complete"
                                )

                                # The current full assignment itself was freshly
                                # confirmed above, so excluding exactly that point is
                                # authoritative. Probe observations remain diagnostic.
                                if exact_nogood in global_exact_exclusions[project][mode]:
                                    raise BaselineConstraintVerificationError(
                                        f"BASELINE_CONSTRAINT_LOOP_STUCK: {project}/{mode}: "
                                        f"global exact assignment {fingerprint} was already excluded"
                                    )
                                global_exact_exclusions[project][mode].append(exact_nogood)
                                extension_granted = liveness.record_exact_exclusion()
                                localization_checkpoint_store.clear(project, mode)
                                checkpoint_baseline_run(
                                    "predicate-search-steered",
                                    completed_iteration=iteration,
                                    last_assignment=fingerprint,
                                    last_predicate=target_predicate,
                                )
                                progress_reporter.emit(
                                    project, mode, "predicate-search-steered",
                                    iteration=iteration, assignment=fingerprint,
                                    package=predicate_pkg,
                                    predicate=target_predicate,
                                    preferredVersion=preferred_version,
                                    literals=len(exact_nogood),
                                    extensionGranted=extension_granted,
                                    exactAssignmentAuthority=EVIDENCE_CONFIRMED_CONSTRAINT,
                                    probeAuthority=EVIDENCE_DIAGNOSTIC_HINT,
                                    topologyMerged=False,
                                    **liveness.snapshot(learned_constraints=len(learned[project][mode])),
                                )
                                predicate_search_steered = True
                                break

                    if predicate_search_steered:
                        continue

                    generalized_nogood: Optional[Dict[str, str]] = None
                    proposal = _adaptive_graph_guided_generalization_proposal(
                        rows_by_name,
                        assignment,
                        mode,
                        client,
                        learned[project][mode],
                        result,
                        project_key=project,
                        repeat_tracker=graph_generalization_repeats,
                        failed_candidates=graph_generalization_failed_candidates,
                        seed_packages_by_family=graph_generalization_seed_packages,
                    )
                    if proposal is not None:
                        proposal = _cross_iteration_consensus_proposal(
                            proposal,
                            assignment,
                            graph_generalization_history_by_family,
                            graph_generalization_failed_candidates,
                        )
                        if proposal.seed_source == "cross-iteration-consensus":
                            consensus_fingerprint = assignment_fingerprint(
                                proposal.candidate
                            )
                            eprint(
                                f"[info] {project}: cross-iteration conflict consensus {mode}; "
                                f"candidate={consensus_fingerprint}, "
                                f"literals={len(proposal.candidate)}, "
                                f"family={proposal.navigation_key[:12]}, "
                                f"authority={EVIDENCE_DIAGNOSTIC_HINT}"
                            )
                            progress_reporter.emit(
                                project,
                                mode,
                                "cross-iteration-consensus-proposed",
                                iteration=iteration,
                                assignment=fingerprint,
                                candidate=consensus_fingerprint,
                                literals=len(proposal.candidate),
                                repeatCount=proposal.repeat_count,
                                seedSource=proposal.seed_source,
                                authority=EVIDENCE_DIAGNOSTIC_HINT,
                            )
                    candidate = proposal.candidate if proposal is not None else None
                    if proposal is not None and proposal.repeat_count > 1:
                        eprint(
                            f"[info] {project}: repeated graph-guided conflict {mode}; "
                            f"seed={proposal.seed_candidate_fingerprint}, repeat={proposal.repeat_count}, "
                            f"expanding verification context radius={proposal.context_radius}, "
                            f"candidate={assignment_fingerprint(proposal.candidate)}, "
                            f"literals={len(proposal.candidate)}, literalBudget={proposal.literal_budget}, "
                            f"boundedSlice={str(proposal.bounded_slice).lower()}, seedSource={proposal.seed_source}, "
                            f"authority={EVIDENCE_DIAGNOSTIC_HINT}"
                        )
                        progress_reporter.emit(
                            project,
                            mode,
                            "generalization-context-expanded",
                            iteration=iteration,
                            assignment=fingerprint,
                            seedCandidate=proposal.seed_candidate_fingerprint,
                            candidate=assignment_fingerprint(proposal.candidate),
                            repeatCount=proposal.repeat_count,
                            contextRadius=proposal.context_radius,
                            literalBudget=proposal.literal_budget,
                            boundedSlice=proposal.bounded_slice,
                            seedSource=proposal.seed_source,
                            literals=len(proposal.candidate),
                            authority=EVIDENCE_DIAGNOSTIC_HINT,
                        )
                    if candidate is not None:
                        liveness.record_generalization_attempt()
                        candidate_fingerprint = assignment_fingerprint(candidate)
                        progress_reporter.emit(
                            project, mode, "generalization-proposed",
                            iteration=iteration, assignment=fingerprint,
                            candidate=candidate_fingerprint,
                            literals=len(candidate),
                            contextRadius=(proposal.context_radius if proposal is not None else 1),
                            repeatCount=(proposal.repeat_count if proposal is not None else 1),
                            literalBudget=(proposal.literal_budget if proposal is not None else len(candidate)),
                            boundedSlice=(proposal.bounded_slice if proposal is not None else False),
                            seedSource=(proposal.seed_source if proposal is not None else "legacy"),
                            authority=EVIDENCE_DIAGNOSTIC_HINT,
                        )
                        eprint(
                            f"[info] {project}: graph-guided generalization proposal {mode}; "
                            f"candidate={candidate_fingerprint}, literals={len(candidate)}, "
                            f"contextRadius={(proposal.context_radius if proposal is not None else 1)}, "
                            f"repeat={(proposal.repeat_count if proposal is not None else 1)}, "
                            f"literalBudget={(proposal.literal_budget if proposal is not None else len(candidate))}, "
                            f"boundedSlice={str(proposal.bounded_slice if proposal is not None else False).lower()}, "
                            f"seedSource={(proposal.seed_source if proposal is not None else 'legacy')}, "
                            f"authority={EVIDENCE_DIAGNOSTIC_HINT}"
                        )
                        candidate_removals = _types_stub_removals_for_assignment(
                            rows_by_name, candidate, mode, client
                        )
                        proof_count = (
                            1 if result.kind == "dependency"
                            else GRAPH_GENERALIZATION_PROJECT_PROOFS
                        )
                        stable_predicate = ""
                        certified = True
                        for proof_index in range(proof_count):
                            proof_number = proof_index + 1
                            candidate_result = verify_assignment(
                                spec.path,
                                candidate,
                                config=confirmation_config if result.kind == "project" else config,
                                run_project_checks=confirmation_project_checks,
                                remove_packages=candidate_removals,
                                progress=lambda message, proof_number=proof_number: (
                                    progress_reporter.emit(
                                        project, mode, "generalization-certification-running",
                                        iteration=iteration, assignment=fingerprint,
                                        candidate=candidate_fingerprint,
                                        proof=proof_number, proofs=proof_count,
                                        message=message,
                                    ),
                                    eprint(
                                        f"[info] {project}: graph certification {mode} "
                                        f"{proof_number}/{proof_count}: {message}"
                                    ),
                                ),
                                progress_label=(
                                    f"Baseline graph certification {mode} "
                                    f"{proof_number}/{proof_count} {candidate_fingerprint}"
                                ),
                            )
                            if candidate_result.kind in {"infrastructure", "unknown"}:
                                certified = False
                                eprint(
                                    f"[warn] {project}: graph-guided candidate remained diagnostic; "
                                    f"certification={candidate_result.kind}: {candidate_result.summary}"
                                )
                                break

                            predicate = ""
                            if result.kind == "dependency":
                                if candidate_result.kind == "dependency" and not candidate_result.ok:
                                    observed = matching_dependency_failure_signature(
                                        expected_summary=result.summary,
                                        expected_output=result.output,
                                        observed_summary=candidate_result.summary,
                                        observed_output=candidate_result.output,
                                    )
                                    if observed:
                                        predicate = observed
                            elif result.kind == "preparation":
                                if candidate_result.kind == "preparation" and not candidate_result.ok:
                                    observed = dependency_failure_signature(
                                        summary=candidate_result.summary,
                                        output=candidate_result.output,
                                    )
                                    if observed == expected_signature:
                                        predicate = observed
                            elif config.project_checks == "adaptive":
                                if candidate_result.kind == "project" and not candidate_result.ok:
                                    observed_structural = set(
                                        adaptive_structural_evidence(candidate_result)
                                    )
                                    stable = sorted(
                                        observed_structural & expected_structural
                                    )
                                    if stable:
                                        predicate = "|".join(stable)
                            else:
                                if candidate_result.kind == "project" and not candidate_result.ok:
                                    observed = dependency_failure_signature(
                                        summary=candidate_result.summary,
                                        output=candidate_result.output,
                                    )
                                    if observed == expected_signature:
                                        predicate = observed

                            if not predicate:
                                certified = False
                                break
                            if stable_predicate and predicate != stable_predicate:
                                certified = False
                                break
                            stable_predicate = predicate

                        if certified and stable_predicate:
                            certified_candidate = dict(candidate)
                            original_candidate_fingerprint = assignment_fingerprint(
                                certified_candidate
                            )

                            adaptive_project_predicate = (
                                result.kind == "project"
                                and config.project_checks == "adaptive"
                            )
                            predicate_families = _adaptive_predicate_families(
                                stable_predicate,
                                adaptive_project=adaptive_project_predicate,
                            )
                            if not predicate_families:
                                raise BaselineConstraintVerificationError(
                                    f"BASELINE_CONSTRAINT_MINIMIZATION_INCONCLUSIVE: "
                                    f"{project}/{mode}: certified graph candidate has no stable predicate"
                                )

                            minimization_budget = _nogood_minimization_check_budget(
                                len(certified_candidate)
                            )
                            # Reuse is restricted to an identical exact trial
                            # inside this invocation. Every cache entry below was
                            # produced by fresh real-PM verification; no diagnostic
                            # graph evidence is promoted to authority.
                            trial_proof_cache: Dict[str, List[Tuple[str, str, str]]] = {}

                            def run_generalized_trial_proof(
                                trial: Dict[str, str],
                                minimization_check: int,
                                minimization_proof: int,
                                required_predicate: str,
                                family_index: int,
                            ) -> str:
                                trial_fingerprint = assignment_fingerprint(trial)
                                trial_removals = _types_stub_removals_for_assignment(
                                    rows_by_name, trial, mode, client
                                )
                                resolver_trial_identity = build_resolver_trial_key(
                                    resolver_context_key=project_resolver_context_key,
                                    assignment=trial,
                                    remove_packages=trial_removals,
                                )
                                # Request identity is full 256-bit and includes the
                                # required predicate. The resulting ResolvedStateKey
                                # is stored in each cache record and validated before reuse.
                                trial_request_key = hashlib.sha256(json.dumps({
                                    "resolverTrialKey": resolver_trial_identity,
                                    "sourceSnapshotKey": project_source_snapshot_key,
                                    "projectChecks": (
                                        confirmation_config.project_checks
                                        if confirmation_project_checks
                                        else "off"
                                    ),
                                    "commands": list(
                                        confirmation_config.commands
                                        if confirmation_project_checks
                                        else ()
                                    ),
                                    "predicateIdentity": required_predicate,
                                    "proofPolicy": "constraint-minimization-fresh-v1",
                                }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
                                cached = trial_proof_cache.setdefault(trial_request_key, [])
                                if len(cached) >= minimization_proof:
                                    cached_evidence_key, cached_state_key, observed = cached[minimization_proof - 1]
                                    if cached_state_key:
                                        expected_evidence_key = build_project_trial_key(
                                            resolver_trial_key=resolver_trial_identity,
                                            resolved_state_key=cached_state_key,
                                            source_snapshot_key=project_source_snapshot_key,
                                            project_checks=(
                                                confirmation_config.project_checks
                                                if confirmation_project_checks
                                                else "off"
                                            ),
                                            commands=(
                                                confirmation_config.commands
                                                if confirmation_project_checks
                                                else ()
                                            ),
                                            predicate_identity=required_predicate,
                                        )
                                        if cached_evidence_key != expected_evidence_key:
                                            raise BaselineConstraintVerificationError(
                                                f"BASELINE_TRIAL_CACHE_IDENTITY_INVALID: {project}/{mode}: "
                                                f"candidate={trial_fingerprint}"
                                            )
                                    elif cached_evidence_key != resolver_trial_identity:
                                        raise BaselineConstraintVerificationError(
                                            f"BASELINE_TRIAL_CACHE_IDENTITY_INVALID: {project}/{mode}: "
                                            f"candidate={trial_fingerprint}"
                                        )
                                    progress_reporter.emit(
                                        project, mode, "constraint-minimization-proof-reused",
                                        iteration=iteration, assignment=fingerprint,
                                        source="graph-generalization",
                                        originalCandidate=original_candidate_fingerprint,
                                        candidate=trial_fingerprint,
                                        originalLiterals=len(certified_candidate),
                                        literals=len(trial), check=minimization_check,
                                        maxChecks=minimization_budget,
                                        proof=minimization_proof, proofs=proof_count,
                                        predicateFamily=required_predicate,
                                        familyIndex=family_index,
                                        families=len(predicate_families),
                                        observedPredicate=observed,
                                        authority=EVIDENCE_CONFIRMED_CONSTRAINT,
                                    )
                                    eprint(
                                        f"[info] {project}: Baseline conflict minimization {mode} "
                                        f"check {minimization_check}/{minimization_budget} "
                                        f"proof {minimization_proof}/{proof_count}: reused exact fresh "
                                        f"trial evidence; literals={len(trial)}, "
                                        f"family={family_index}/{len(predicate_families)}"
                                    )
                                    return observed
                                if len(cached) != minimization_proof - 1:
                                    raise BaselineConstraintVerificationError(
                                        f"BASELINE_CONSTRAINT_MINIMIZATION_PROOF_ORDER_INVALID: "
                                        f"{project}/{mode}: candidate={trial_fingerprint}, "
                                        f"proof={minimization_proof}, cached={len(cached)}"
                                    )

                                trial_removals = _types_stub_removals_for_assignment(
                                    rows_by_name, trial, mode, client
                                )
                                progress_reporter.emit(
                                    project, mode, "constraint-minimization-check-started",
                                    iteration=iteration, assignment=fingerprint,
                                    source="graph-generalization",
                                    originalCandidate=original_candidate_fingerprint,
                                    candidate=trial_fingerprint,
                                    originalLiterals=len(certified_candidate),
                                    literals=len(trial), check=minimization_check,
                                    maxChecks=minimization_budget,
                                    proof=minimization_proof, proofs=proof_count,
                                    predicateFamily=required_predicate,
                                    familyIndex=family_index,
                                    families=len(predicate_families),
                                    authority=EVIDENCE_DIAGNOSTIC_HINT,
                                )
                                eprint(
                                    f"[info] {project}: Baseline conflict minimization {mode} "
                                    f"check {minimization_check}/{minimization_budget} "
                                    f"proof {minimization_proof}/{proof_count} started; "
                                    f"literals={len(trial)}, family={family_index}/{len(predicate_families)}, "
                                    f"candidate={trial_fingerprint}"
                                )
                                trial_result = verify_assignment(
                                    spec.path,
                                    trial,
                                    config=(confirmation_config if result.kind == "project" else config),
                                    run_project_checks=confirmation_project_checks,
                                    remove_packages=trial_removals,
                                    progress=lambda message, trial_fingerprint=trial_fingerprint, minimization_check=minimization_check, minimization_proof=minimization_proof, required_predicate=required_predicate, family_index=family_index: (
                                        progress_reporter.emit(
                                            project, mode, "constraint-minimization-check-running",
                                            iteration=iteration, assignment=fingerprint,
                                            source="graph-generalization",
                                            originalCandidate=original_candidate_fingerprint,
                                            candidate=trial_fingerprint,
                                            originalLiterals=len(certified_candidate),
                                            literals=len(trial), check=minimization_check,
                                            maxChecks=minimization_budget,
                                            proof=minimization_proof, proofs=proof_count,
                                            predicateFamily=required_predicate,
                                            familyIndex=family_index,
                                            families=len(predicate_families), message=message,
                                        ),
                                        eprint(
                                            f"[info] {project}: Baseline conflict minimization {mode} "
                                            f"check {minimization_check}/{minimization_budget} "
                                            f"proof {minimization_proof}/{proof_count}: {message}"
                                        ),
                                    ),
                                    progress_label=(
                                        f"Baseline nogood minimization {mode} check {minimization_check} "
                                        f"proof {minimization_proof}/{proof_count} {trial_fingerprint}"
                                    ),
                                )
                                if trial_result.kind in {"infrastructure", "unknown"}:
                                    raise BaselineConstraintVerificationError(
                                        f"BASELINE_CONSTRAINT_MINIMIZATION_INCONCLUSIVE: "
                                        f"{project}/{mode}: fresh minimization proof {minimization_check} "
                                        f"is {trial_result.kind}: {trial_result.summary}"
                                    )

                                trial_predicate = ""
                                if result.kind == "dependency":
                                    if trial_result.kind == "dependency" and not trial_result.ok:
                                        trial_predicate = matching_dependency_failure_signature(
                                            expected_summary=result.summary,
                                            expected_output=result.output,
                                            observed_summary=trial_result.summary,
                                            observed_output=trial_result.output,
                                        )
                                elif result.kind == "preparation":
                                    if trial_result.kind == "preparation" and not trial_result.ok:
                                        observed = dependency_failure_signature(
                                            summary=trial_result.summary, output=trial_result.output,
                                        )
                                        if observed == expected_signature:
                                            trial_predicate = observed
                                elif adaptive_project_predicate:
                                    if trial_result.kind == "project" and not trial_result.ok:
                                        trial_structural = set(adaptive_structural_evidence(trial_result))
                                        stable = sorted(trial_structural & expected_structural)
                                        if stable:
                                            trial_predicate = "|".join(stable)
                                else:
                                    if trial_result.kind == "project" and not trial_result.ok:
                                        observed = dependency_failure_signature(
                                            summary=trial_result.summary, output=trial_result.output,
                                        )
                                        if observed == expected_signature:
                                            trial_predicate = observed

                                if trial_result.resolved_state_key:
                                    evidence_key = build_project_trial_key(
                                        resolver_trial_key=resolver_trial_identity,
                                        resolved_state_key=trial_result.resolved_state_key,
                                        source_snapshot_key=project_source_snapshot_key,
                                        project_checks=(
                                            confirmation_config.project_checks
                                            if confirmation_project_checks
                                            else "off"
                                        ),
                                        commands=(
                                            confirmation_config.commands
                                            if confirmation_project_checks
                                            else ()
                                        ),
                                        predicate_identity=required_predicate,
                                    )
                                    cached.append((
                                        evidence_key,
                                        trial_result.resolved_state_key,
                                        trial_predicate,
                                    ))
                                else:
                                    # Resolver failures have no ResolvedState by
                                    # definition; their exact ResolverTrialKey is
                                    # the authority identity for this observation.
                                    cached.append((
                                        resolver_trial_identity,
                                        "",
                                        trial_predicate,
                                    ))
                                progress_reporter.emit(
                                    project, mode, "constraint-minimization-proof-observed",
                                    iteration=iteration, assignment=fingerprint,
                                    source="graph-generalization", candidate=trial_fingerprint,
                                    literals=len(trial), check=minimization_check,
                                    maxChecks=minimization_budget,
                                    proof=minimization_proof, proofs=proof_count,
                                    predicateFamily=required_predicate,
                                    familyIndex=family_index, families=len(predicate_families),
                                    observedPredicate=trial_predicate,
                                )
                                return trial_predicate

                            def certify_generalized_minimization(
                                trial: Dict[str, str],
                                minimization_check: int,
                                required_predicate: str,
                                family_index: int,
                            ) -> str:
                                trial_fingerprint = assignment_fingerprint(trial)
                                for minimization_proof in range(1, proof_count + 1):
                                    observed = run_generalized_trial_proof(
                                        trial, minimization_check, minimization_proof,
                                        required_predicate, family_index,
                                    )
                                    if adaptive_project_predicate:
                                        reproduced = required_predicate in {
                                            item for item in observed.split("|") if item
                                        }
                                    else:
                                        reproduced = observed == required_predicate
                                    if not reproduced:
                                        progress_reporter.emit(
                                            project, mode, "constraint-minimization-check-rejected",
                                            iteration=iteration, assignment=fingerprint,
                                            source="graph-generalization",
                                            originalCandidate=original_candidate_fingerprint,
                                            candidate=trial_fingerprint,
                                            originalLiterals=len(certified_candidate),
                                            literals=len(trial), check=minimization_check,
                                            maxChecks=minimization_budget,
                                            proof=minimization_proof, proofs=proof_count,
                                            predicateFamily=required_predicate,
                                            familyIndex=family_index, families=len(predicate_families),
                                            reason="authoritative-predicate-not-reproduced",
                                            authority=EVIDENCE_DIAGNOSTIC_HINT,
                                        )
                                        eprint(
                                            f"[info] {project}: Baseline conflict minimization {mode} "
                                            f"check {minimization_check}/{minimization_budget} rejected; "
                                            f"literals={len(trial)}, family={family_index}/{len(predicate_families)}"
                                        )
                                        return ""

                                progress_reporter.emit(
                                    project, mode, "constraint-minimization-check-certified",
                                    iteration=iteration, assignment=fingerprint,
                                    source="graph-generalization",
                                    originalCandidate=original_candidate_fingerprint,
                                    candidate=trial_fingerprint,
                                    originalLiterals=len(certified_candidate),
                                    literals=len(trial), check=minimization_check,
                                    maxChecks=minimization_budget, proofs=proof_count,
                                    predicate=required_predicate,
                                    predicateFamily=required_predicate,
                                    familyIndex=family_index, families=len(predicate_families),
                                    authority=EVIDENCE_CONFIRMED_CONSTRAINT,
                                )
                                eprint(
                                    f"[info] {project}: Baseline conflict minimization {mode} accepted shrink; "
                                    f"literals={len(trial)}, family={family_index}/{len(predicate_families)}, "
                                    f"authority={EVIDENCE_CONFIRMED_CONSTRAINT}"
                                )
                                return required_predicate

                            family_results: List[Tuple[str, Dict[str, str], NogoodMinimizationResult]] = []
                            for family_index, required_predicate in enumerate(predicate_families, start=1):
                                progress_reporter.emit(
                                    project, mode, "constraint-minimization-family-started",
                                    iteration=iteration, assignment=fingerprint,
                                    source="graph-generalization",
                                    originalCandidate=original_candidate_fingerprint,
                                    candidate=original_candidate_fingerprint,
                                    originalLiterals=len(certified_candidate),
                                    literals=len(certified_candidate), check=0,
                                    maxChecks=minimization_budget, proof=0, proofs=proof_count,
                                    predicateFamily=required_predicate,
                                    familyIndex=family_index, families=len(predicate_families),
                                    shrinkHistory=[len(certified_candidate)],
                                    authority=EVIDENCE_CONFIRMED_CONSTRAINT,
                                )
                                eprint(
                                    f"[info] {project}: Baseline conflict minimization {mode} "
                                    f"family {family_index}/{len(predicate_families)} started; "
                                    f"literals={len(certified_candidate)}, maxChecks={minimization_budget}, "
                                    f"predicate={required_predicate}"
                                )
                                minimization = _proof_preserving_minimize_nogood(
                                    certified_candidate,
                                    lambda trial, check, required_predicate=required_predicate, family_index=family_index: certify_generalized_minimization(
                                        trial, check, required_predicate, family_index
                                    ),
                                    max_checks=minimization_budget,
                                    initial_predicate=required_predicate,
                                )
                                if not minimization.predicate:
                                    raise BaselineConstraintVerificationError(
                                        f"BASELINE_CONSTRAINT_MINIMIZATION_INCONCLUSIVE: "
                                        f"{project}/{mode}: certified graph candidate lost "
                                        f"predicate family {required_predicate!r} during minimization"
                                    )
                                generalized = dict(minimization.minimized)
                                generalized_fingerprint = assignment_fingerprint(generalized)
                                family_results.append((required_predicate, generalized, minimization))
                                if len(generalized) < len(certified_candidate):
                                    eprint(
                                        f"[warn] {project}: proof-preserving constraint minimized {mode}; "
                                        f"family={family_index}/{len(predicate_families)}; "
                                        f"{len(certified_candidate)}->{len(generalized)} literals; "
                                        f"checks={minimization.checks}; "
                                        f"history={'->'.join(str(item) for item in minimization.shrink_history)}; "
                                        f"authority={EVIDENCE_CONFIRMED_CONSTRAINT}"
                                    )
                                progress_reporter.emit(
                                    project, mode, "constraint-minimization-completed",
                                    iteration=iteration, assignment=fingerprint,
                                    source="graph-generalization",
                                    originalCandidate=original_candidate_fingerprint,
                                    candidate=generalized_fingerprint,
                                    originalLiterals=len(certified_candidate),
                                    minimizedLiterals=len(generalized), literals=len(generalized),
                                    checks=minimization.checks, maxChecks=minimization_budget,
                                    acceptedShrinks=minimization.accepted_shrinks,
                                    shrinkHistory=list(minimization.shrink_history),
                                    exhausted=minimization.exhausted,
                                    predicate=minimization.predicate,
                                    predicateFamily=required_predicate,
                                    familyIndex=family_index, families=len(predicate_families),
                                    authority=EVIDENCE_CONFIRMED_CONSTRAINT,
                                )

                            new_constraints: List[Dict[str, str]] = []
                            new_family_results: List[Tuple[str, Dict[str, str], NogoodMinimizationResult]] = []
                            for family_result in family_results:
                                _predicate, generalized, _minimization = family_result
                                if generalized in learned[project][mode] or generalized in new_constraints:
                                    continue
                                new_constraints.append(generalized)
                                new_family_results.append(family_result)
                            if not new_constraints:
                                raise BaselineConstraintVerificationError(
                                    f"BASELINE_CONSTRAINT_LOOP_STUCK: {project}/{mode}: all freshly "
                                    "certified predicate-family constraints were already learned"
                                )

                            for generalized in new_constraints:
                                learned[project][mode].append(generalized)
                            liveness.observe_learned_constraints(len(learned[project][mode]))
                            localization_checkpoint_store.clear(project, mode)

                            learned_fingerprints = [assignment_fingerprint(item) for item in new_constraints]
                            strongest_family = min(
                                new_family_results,
                                key=lambda item: (len(item[1]), assignment_fingerprint(item[1]), item[0]),
                            )
                            strongest = strongest_family[1]
                            strongest_minimization = strongest_family[2]
                            candidate_fingerprint = assignment_fingerprint(strongest)
                            if _baseline_interactive() and iteration >= decision_prompt_not_before:
                                decision_focus = _baseline_human_decision_focus(
                                    learned[project][mode], baseline_current_versions,
                                    min_confirmed=BASELINE_INTERACTIVE_UNARY_THRESHOLD,
                                )
                                if decision_focus is not None:
                                    checkpoint_baseline_run(
                                        "human-decision-required",
                                        completed_iteration=iteration,
                                        last_assignment=fingerprint,
                                        last_predicate=strongest_family[0],
                                        status="decision-required",
                                    )
                                    decision_payload = {
                                        "schemaVersion": 1,
                                        "reason": "repeated-package-conflict",
                                        "project": project,
                                        "mode": mode,
                                        "iteration": iteration,
                                        "hardIterations": liveness.hard_iterations,
                                        "learnedConstraints": len(learned[project][mode]),
                                        "predicate": strongest_family[0],
                                        **decision_focus,
                                    }
                                    progress_reporter.emit(
                                        project, mode, "human-decision-required",
                                        iteration=iteration,
                                        stopCode="BASELINE_HUMAN_DECISION_REQUIRED",
                                        terminalStatus="HUMAN_DECISION_REQUIRED",
                                        **{key: value for key, value in decision_payload.items() if key not in {"project", "mode", "iteration", "schemaVersion"}},
                                    )
                                    _raise_baseline_human_decision(decision_payload)
                            eprint(
                                f"[warn] {project}: graph-guided constraint family certified; "
                                f"learned={len(new_constraints)}, "
                                f"literals={[len(item) for item in new_constraints]}, "
                                f"authority={EVIDENCE_CONFIRMED_CONSTRAINT}; re-solving"
                            )
                            checkpoint_baseline_run(
                                "generalization-certified",
                                completed_iteration=iteration,
                                last_assignment=fingerprint,
                                last_predicate=stable_predicate,
                            )
                            progress_reporter.emit(
                                project, mode, "generalization-certified",
                                iteration=iteration, assignment=fingerprint,
                                candidate=candidate_fingerprint,
                                originalCandidate=original_candidate_fingerprint,
                                originalLiterals=len(certified_candidate), literals=len(strongest),
                                minimizationChecks=sum(item[2].checks for item in family_results),
                                shrinkHistory=list(strongest_minimization.shrink_history),
                                learnedCandidates=learned_fingerprints,
                                learnedThisIteration=len(new_constraints),
                                predicateFamilies=list(predicate_families),
                                contextRadius=(proposal.context_radius if proposal is not None else 1),
                                repeatCount=(proposal.repeat_count if proposal is not None else 1),
                                literalBudget=(proposal.literal_budget if proposal is not None else len(strongest)),
                                boundedSlice=(proposal.bounded_slice if proposal is not None else False),
                                seedSource=(proposal.seed_source if proposal is not None else "legacy"),
                                authority=EVIDENCE_CONFIRMED_CONSTRAINT,
                                predicate=stable_predicate,
                                **liveness.snapshot(learned_constraints=len(learned[project][mode])),
                            )
                            continue

                        liveness.record_diagnostic()
                        progress_reporter.emit(
                            project, mode, "generalization-not-certified",
                            iteration=iteration, assignment=fingerprint,
                            candidate=candidate_fingerprint,
                            authority=EVIDENCE_DIAGNOSTIC_HINT,
                        )

                    if candidate is not None and proposal is not None:
                        graph_generalization_failed_candidates.add(
                            (proposal.navigation_key, candidate_fingerprint)
                        )
                        eprint(
                            f"[info] {project}: graph-guided candidate remains diagnostic {mode}; "
                            f"candidate={candidate_fingerprint}, repeat={proposal.repeat_count}, "
                            f"contextRadius={proposal.context_radius}, "
                            f"authority={EVIDENCE_DIAGNOSTIC_HINT}; "
                            "fresh certification did not reproduce a stable authoritative predicate"
                        )
                        progress_reporter.emit(
                            project,
                            mode,
                            "generalization-diagnostic",
                            iteration=iteration,
                            assignment=fingerprint,
                            seedCandidate=proposal.seed_candidate_fingerprint,
                            candidate=candidate_fingerprint,
                            repeatCount=proposal.repeat_count,
                            contextRadius=proposal.context_radius,
                            literals=len(candidate),
                            authority=EVIDENCE_DIAGNOSTIC_HINT,
                            reason="authoritative-predicate-not-certified",
                        )

                    if exact_nogood in global_exact_exclusions[project][mode]:
                        raise BaselineConstraintVerificationError(
                            f"BASELINE_CONSTRAINT_LOOP_STUCK: {project}/{mode}: "
                            f"global exact assignment {fingerprint} was already excluded"
                        )

                    global_exact_exclusions[project][mode].append(exact_nogood)
                    extension_granted = liveness.record_exact_exclusion()
                    localization_checkpoint_store.clear(project, mode)
                    eprint(
                        f"[warn] {project}: blocked exact failing assignment {fingerprint} "
                        f"as global exact exclusion; literals={len(exact_nogood)}, "
                        f"authority={EVIDENCE_CONFIRMED_CONSTRAINT}; "
                        "solver components remain independent"
                    )
                    checkpoint_baseline_run(
                        "exact-assignment-blocked",
                        completed_iteration=iteration,
                        last_assignment=fingerprint,
                    )
                    progress_reporter.emit(
                        project, mode, "exact-assignment-blocked",
                        iteration=iteration, assignment=fingerprint,
                        literals=len(exact_nogood), origin=result.kind,
                        authority=EVIDENCE_CONFIRMED_CONSTRAINT,
                        topologyMerged=False,
                        extensionGranted=extension_granted,
                        **liveness.snapshot(learned_constraints=len(learned[project][mode])),
                    )
                    continue

                # Hard dependency (or explicitly strict project) failure. Localize
                # the failing set concurrently and teach the solver one nogood.
                units = _verification_units_for_assignment(
                    rows_by_name, assignment, mode, client, learned[project][mode]
                )
                if not units:
                    raise BaselineConstraintVerificationError(
                        f"BASELINE_PLAN_BROKEN: {project}/{mode}: resolver preflight failed but no changed solver component exists: "
                        f"{result.summary}"
                    )

                learn_project_failure = result.kind == "project" and (
                    config.project_checks == "strict" or adaptive_structural_regression(result)
                )
                localization_started = time.monotonic()
                eprint(
                    f"[info] {project}: Baseline localization {mode} started; units={len(units)}, "
                    f"maxChecks={config.max_delta_checks}, parallelism={config.parallelism}, "
                    f"attemptHardTimeout={config.attempt_timeout_seconds}s, localizationHardTimeout={config.localization_timeout_seconds}s"
                )
                progress_reporter.emit(
                    project, mode, "localization-started", iteration=iteration, assignment=fingerprint,
                    units=len(units), maxChecks=config.max_delta_checks, parallelism=config.parallelism,
                    attemptHardTimeoutSeconds=config.attempt_timeout_seconds, localizationHardTimeoutSeconds=config.localization_timeout_seconds,
                )

                def localization_progress(event: str, details: Dict[str, object]) -> None:
                    safe_details = _normalize_baseline_progress_details(details)
                    progress_reporter.emit(project, mode, f"localization-{event}", iteration=iteration, assignment=fingerprint, **safe_details)
                    if event in {"start", "resume", "wave-start", "heartbeat", "check-finish", "confirmation-start", "confirmation-heartbeat", "confirmation-finish", "shrink", "timeout", "checkpoint-error", "finish"}:
                        detail_text = ", ".join(f"{key}={value}" for key, value in safe_details.items())
                        level = "warn" if event == "timeout" else "info"
                        eprint(f"[{level}] {project}: Baseline localization {mode} {event}; {detail_text}")

                def subset_fails(subset: Tuple[VerificationUnit, ...]) -> bool:
                    subset_changed, _subset_clause = _verification_inputs_for_units(
                        assignment, rows_by_name, subset
                    )
                    if not subset_changed:
                        return False
                    subset_removals = _types_stub_removals_for_assignment(rows_by_name, subset_changed, mode, client)
                    subset_label = assignment_fingerprint(subset_changed)
                    subset_result = verify_assignment(
                        spec.path, subset_changed, config=config, run_project_checks=learn_project_failure,
                        remove_packages=subset_removals,
                        progress=lambda message: progress_reporter.emit(
                            project, mode, "localization-check-running", iteration=iteration, assignment=fingerprint,
                            subset=subset_label, packages=len(subset_changed), message=message,
                        ),
                        progress_label=f"localization subset {subset_label} ({len(subset_changed)} packages)",
                    )
                    if subset_result.kind == "infrastructure":
                        raise BaselineConstraintVerificationError(
                            f"BASELINE_VERIFY_INFRA_ERROR: {project}/{mode}: {subset_result.summary}. "
                            "No compatibility constraint was learned."
                        )
                    if subset_result.kind == "unknown":
                        raise BaselineConstraintVerificationError(
                            f"BASELINE_VERIFY_UNKNOWN_ERROR: {project}/{mode}: {subset_result.summary}. "
                            "No compatibility constraint was learned."
                        )
                    if learn_project_failure:
                        if config.project_checks == "adaptive":
                            structural = adaptive_structural_evidence(subset_result)
                            progress_reporter.emit(
                                project, mode, "localization-check-result",
                                iteration=iteration, assignment=fingerprint,
                                subset=subset_label, materialization=subset_label,
                                changes=len(subset_changed), removals=sorted(subset_removals),
                                kind=subset_result.kind, ok=subset_result.ok,
                                structuralSignatures=list(structural),
                            )
                            return bool(structural)
                        failed = subset_result.kind == "project" and not subset_result.ok
                        progress_reporter.emit(
                            project, mode, "localization-check-result",
                            iteration=iteration, assignment=fingerprint,
                            subset=subset_label, materialization=subset_label,
                            changes=len(subset_changed), removals=sorted(subset_removals),
                            kind=subset_result.kind, ok=subset_result.ok,
                            failed=failed,
                        )
                        return failed
                    failed = subset_result.kind == "dependency" and not subset_result.ok
                    progress_reporter.emit(
                        project, mode, "localization-check-result",
                        iteration=iteration, assignment=fingerprint,
                        subset=subset_label, materialization=subset_label,
                        changes=len(subset_changed), removals=sorted(subset_removals),
                        kind=subset_result.kind, ok=subset_result.ok,
                        failed=failed,
                    )
                    return failed

                try:
                    source_head_result = subprocess.run(
                        ["git", "-C", str(spec.path), "rev-parse", "HEAD"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    source_head = source_head_result.stdout.strip() if source_head_result.returncode == 0 else ""
                except (OSError, subprocess.SubprocessError):
                    source_head = ""

                localization_identity_payload = {
                    "algorithm": "baseline-ddmin-resume-v1",
                    "sourceHead": source_head,
                    "environment": project_resolver_context_key,
                    "mode": mode,
                    "assignment": sorted(assignment.items()),
                    "commands": list(config.commands),
                    "projectChecks": config.project_checks,
                    "units": [{"id": unit.id, "packages": list(unit.packages)} for unit in units],
                }
                localization_identity = hashlib.sha256(
                    json.dumps(
                        localization_identity_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                localization_resume_state = localization_checkpoint_store.load(
                    project, mode, localization_identity
                )
                if localization_resume_state is not None:
                    eprint(
                        f"[info] {project}: Baseline localization {mode} resume checkpoint found; "
                        f"currentUnits={len(localization_resume_state.get('currentUnitIds') or [])}, "
                        f"checksStarted={localization_resume_state.get('checksStarted', 0)}, "
                        f"reason={localization_resume_state.get('reason', 'unknown')}"
                    )

                try:
                    culprit_units = parallel_ddmin(
                        units,
                        subset_fails,
                        parallelism=config.parallelism,
                        max_checks=config.max_delta_checks,
                        progress=localization_progress,
                        progress_interval_seconds=config.progress_interval_seconds,
                        timeout_seconds=config.localization_timeout_seconds,
                        # parallel_ddmin assumes a deterministic predicate. A
                        # project verifier cannot prove that merely from separate
                        # workspaces because concurrent probes still share host
                        # resources. Screen in parallel, but only let a candidate
                        # shrink the search after the same experiment fails again
                        # with no sibling probe running.
                        confirm_failure=subset_fails,
                        resume_state=localization_resume_state,
                        checkpoint=lambda state: localization_checkpoint_store.save(
                            project, mode, localization_identity, state, source_head=source_head
                        ),
                    )
                except LocalizationTimeoutError as exc:
                    progress_reporter.emit(
                        project, mode, "localization-timeout", iteration=iteration,
                        assignment=fingerprint, error=str(exc),
                        terminalStatus=BaselineTerminalStatus.BUDGET_EXHAUSTED.value,
                        terminalSource="conflict-localization",
                        stopCode="BASELINE_LOCALIZATION_TIMEOUT",
                    )
                    raise _baseline_terminal_error(
                        BaselineTerminalStatus.BUDGET_EXHAUSTED,
                        "BASELINE_LOCALIZATION_TIMEOUT",
                        f"{project}/{mode}: {exc}; dependency plan was preserved and no compatibility constraint was learned",
                        source="conflict-localization",
                    ) from None
                eprint(
                    f"[info] {project}: Baseline localization {mode} completed; culpritUnits={len(culprit_units)}, "
                    f"elapsed={int(time.monotonic() - localization_started)}s"
                )
                progress_reporter.emit(
                    project, mode, "localization-completed", iteration=iteration, assignment=fingerprint,
                    culpritUnits=len(culprit_units), elapsedSeconds=int(time.monotonic() - localization_started),
                )
                culprit_changed, nogood = _verification_inputs_for_units(
                    assignment, rows_by_name, culprit_units
                )
                if not culprit_changed:
                    raise BaselineConstraintVerificationError(
                        f"BASELINE_PLAN_BROKEN: {project}/{mode}: localized culprit has no materialized changes"
                    )
                if not nogood:
                    nogood = dict(culprit_changed)

                # This fingerprint is the identity of the *experiment*, not the
                # richer learned solver clause. Every fresh reproduction below
                # must materialize this exact delta (plus the exact same removals).
                culprit_materialization = assignment_fingerprint(culprit_changed)
                culprit_removals = _types_stub_removals_for_assignment(
                    rows_by_name, culprit_changed, mode, client
                )
                progress_reporter.emit(
                    project, mode, "localization-materialization-confirmed",
                    iteration=iteration, assignment=fingerprint,
                    materialization=culprit_materialization,
                    changes=len(culprit_changed),
                    contextLiterals=len(nogood),
                    removals=sorted(culprit_removals),
                )

                # A project/build/typecheck observation becomes solver authority
                # only after the localized literal set reproduces in fresh
                # workspaces with a stable signature.  This keeps flaky caches,
                # racey tests and transient tool output from poisoning the exact
                # solve session. Resolver nogoods have their own stronger durable
                # proof path below.
                expected_structural = (
                    set(adaptive_structural_evidence(result))
                    if config.project_checks == "adaptive"
                    else set()
                )
                if learn_project_failure:
                    proof_signatures: List[str] = []
                    for _proof_index in range(2):
                        proof_number = _proof_index + 1
                        eprint(
                            f"[info] {project}: Baseline reproduction {mode} {proof_number}/2 started; "
                            f"literals={len(nogood)}, changes={len(culprit_changed)}, contextLiterals={len(nogood)}, "
                            f"materialization={culprit_materialization}"
                        )
                        progress_reporter.emit(
                            project, mode, "reproduction-started",
                            iteration=iteration, assignment=fingerprint,
                            proof=proof_number, proofs=2,
                            changes=len(culprit_changed), contextLiterals=len(nogood),
                            materialization=culprit_materialization,
                            removals=sorted(culprit_removals),
                        )
                        proof_result = verify_assignment(
                            spec.path, culprit_changed, config=config, run_project_checks=True,
                            remove_packages=culprit_removals,
                            progress=lambda message, proof_number=proof_number: (
                                progress_reporter.emit(project, mode, "reproduction-running", iteration=iteration, assignment=fingerprint, proof=proof_number, proofs=2, message=message),
                                eprint(f"[info] {project}: Baseline reproduction {mode} {proof_number}/2: {message}"),
                            ),
                            progress_label=f"Baseline reproduction {mode} {proof_number}/2",
                        )
                        if proof_result.kind in {"infrastructure", "unknown"}:
                            raise BaselineConstraintVerificationError(
                                f"BASELINE_VERIFY_INCONCLUSIVE_PROJECT_ERROR: {project}/{mode}: "
                                f"project evidence could not be reproduced safely ({proof_result.kind}: {proof_result.summary}); "
                                "no compatibility constraint was learned"
                            )
                        if proof_result.ok or proof_result.kind != "project":
                            raise BaselineConstraintVerificationError(
                                f"BASELINE_VERIFY_INCONCLUSIVE_PROJECT_ERROR: {project}/{mode}: "
                                "localized project failure did not reproduce; no compatibility constraint was learned"
                            )
                        if config.project_checks == "adaptive":
                            signatures = set(adaptive_structural_evidence(proof_result))
                            stable = sorted(signatures & expected_structural)
                            if not stable:
                                raise BaselineConstraintVerificationError(
                                    f"BASELINE_VERIFY_INCONCLUSIVE_PROJECT_ERROR: {project}/{mode}: "
                                    "localized structural signature was not stable across fresh reproductions; "
                                    "no compatibility constraint was learned"
                                )
                            proof_signatures.append("|".join(stable))
                        else:
                            proof_signatures.append(dependency_failure_signature(
                                summary=proof_result.summary, output=proof_result.output,
                            ))
                        eprint(f"[info] {project}: Baseline reproduction {mode} {proof_number}/2 PASS")
                        progress_reporter.emit(project, mode, "reproduction-completed", iteration=iteration, assignment=fingerprint, proof=proof_number, proofs=2)
                    if not proof_signatures or len(set(proof_signatures)) != 1:
                        raise BaselineConstraintVerificationError(
                            f"BASELINE_VERIFY_INCONCLUSIVE_PROJECT_ERROR: {project}/{mode}: "
                            "project failure signature changed across reproductions; no compatibility constraint was learned"
                        )
                    eprint(
                        f"[info] {project}: reproducible project compatibility evidence confirmed; "
                        f"signature={proof_signatures[0]}, proofs={len(proof_signatures)}"
                    )

                localized_original = dict(nogood)
                localized_original_fingerprint = assignment_fingerprint(
                    localized_original
                )
                localized_proof_count = 2 if learn_project_failure else 1
                localized_minimization_budget = _nogood_minimization_check_budget(
                    len(localized_original)
                )

                def certify_localized_minimization(
                    trial: Dict[str, str],
                    minimization_check: int,
                ) -> str:
                    trial_fingerprint = assignment_fingerprint(trial)
                    trial_removals = _types_stub_removals_for_assignment(
                        rows_by_name, trial, mode, client
                    )
                    observed_predicate = ""

                    for minimization_proof_index in range(localized_proof_count):
                        minimization_proof = minimization_proof_index + 1
                        progress_reporter.emit(
                            project, mode, "constraint-minimization-check-started",
                            iteration=iteration, assignment=fingerprint,
                            source="localized",
                            originalCandidate=localized_original_fingerprint,
                            candidate=trial_fingerprint,
                            originalLiterals=len(localized_original), literals=len(trial),
                            check=minimization_check, maxChecks=localized_minimization_budget,
                            proof=minimization_proof, proofs=localized_proof_count,
                            authority=EVIDENCE_DIAGNOSTIC_HINT,
                        )
                        eprint(
                            f"[info] {project}: Baseline conflict minimization {mode} "
                            f"check {minimization_check}/{localized_minimization_budget} "
                            f"proof {minimization_proof}/{localized_proof_count} started; "
                            f"literals={len(trial)}, candidate={trial_fingerprint}"
                        )
                        trial_result = verify_assignment(
                            spec.path,
                            trial,
                            config=config,
                            run_project_checks=learn_project_failure,
                            remove_packages=trial_removals,
                            progress=lambda message, trial_fingerprint=trial_fingerprint, minimization_check=minimization_check, minimization_proof=minimization_proof: (
                                progress_reporter.emit(
                                    project, mode, "constraint-minimization-check-running",
                                    iteration=iteration, assignment=fingerprint,
                                    source="localized",
                                    originalCandidate=localized_original_fingerprint,
                                    candidate=trial_fingerprint,
                                    originalLiterals=len(localized_original), literals=len(trial),
                                    check=minimization_check, maxChecks=localized_minimization_budget,
                                    proof=minimization_proof, proofs=localized_proof_count,
                                    message=message,
                                ),
                                eprint(
                                    f"[info] {project}: Baseline conflict minimization {mode} "
                                    f"check {minimization_check}/{localized_minimization_budget} "
                                    f"proof {minimization_proof}/{localized_proof_count}: {message}"
                                ),
                            ),
                            progress_label=(
                                f"Baseline localized nogood minimization {mode} "
                                f"check {minimization_check} proof "
                                f"{minimization_proof}/{localized_proof_count} "
                                f"{trial_fingerprint}"
                            ),
                        )
                        if trial_result.kind in {"infrastructure", "unknown"}:
                            raise BaselineConstraintVerificationError(
                                f"BASELINE_CONSTRAINT_MINIMIZATION_INCONCLUSIVE: "
                                f"{project}/{mode}: localized minimization proof "
                                f"{minimization_check} is {trial_result.kind}: "
                                f"{trial_result.summary}"
                            )

                        trial_predicate = ""
                        if result.kind == "dependency":
                            if (
                                trial_result.kind == "dependency"
                                and not trial_result.ok
                            ):
                                trial_predicate = (
                                    matching_dependency_failure_signature(
                                        expected_summary=result.summary,
                                        expected_output=result.output,
                                        observed_summary=trial_result.summary,
                                        observed_output=trial_result.output,
                                    )
                                )
                        elif learn_project_failure:
                            if (
                                trial_result.kind == "project"
                                and not trial_result.ok
                            ):
                                if config.project_checks == "adaptive":
                                    trial_structural = set(
                                        adaptive_structural_evidence(trial_result)
                                    )
                                    stable = sorted(
                                        trial_structural & expected_structural
                                    )
                                    if stable:
                                        trial_predicate = "|".join(stable)
                                else:
                                    expected = dependency_failure_signature(
                                        summary=result.summary,
                                        output=result.output,
                                    )
                                    observed = dependency_failure_signature(
                                        summary=trial_result.summary,
                                        output=trial_result.output,
                                    )
                                    if observed == expected:
                                        trial_predicate = observed

                        if not trial_predicate:
                            return ""
                        if (
                            observed_predicate
                            and observed_predicate != trial_predicate
                        ):
                            return ""
                        observed_predicate = trial_predicate

                    return observed_predicate

                localized_minimization = _proof_preserving_minimize_nogood(
                    localized_original,
                    certify_localized_minimization,
                    max_checks=localized_minimization_budget,
                    initial_predicate="",
                )
                if not localized_minimization.predicate:
                    raise BaselineConstraintVerificationError(
                        f"BASELINE_CONSTRAINT_MINIMIZATION_INCONCLUSIVE: "
                        f"{project}/{mode}: localized solver clause "
                        f"{localized_original_fingerprint} did not reproduce "
                        "its authoritative predicate at literal granularity"
                    )
                nogood = dict(localized_minimization.minimized)
                progress_reporter.emit(
                    project,
                    mode,
                    "constraint-minimization-completed",
                    iteration=iteration,
                    assignment=fingerprint,
                    source="localized",
                    originalCandidate=localized_original_fingerprint,
                    candidate=assignment_fingerprint(nogood),
                    originalLiterals=len(localized_original),
                    minimizedLiterals=len(nogood),
                    literals=len(nogood),
                    checks=localized_minimization.checks,
                    maxChecks=localized_minimization_budget,
                    acceptedShrinks=localized_minimization.accepted_shrinks,
                    shrinkHistory=list(localized_minimization.shrink_history),
                    exhausted=localized_minimization.exhausted,
                    predicate=localized_minimization.predicate,
                    authority=EVIDENCE_CONFIRMED_CONSTRAINT,
                )

                if nogood in learned[project][mode]:
                    raise BaselineConstraintVerificationError(
                        f"BASELINE_CONSTRAINT_LOOP_STUCK: {project}/{mode}: verification repeated already learned assignment "
                        f"{assignment_fingerprint(nogood)}; {result.summary}"
                    )
                learned[project][mode].append(nogood)
                liveness.observe_learned_constraints(len(learned[project][mode]))
                # Keep the finished localization checkpoint through project
                # reproductions; clear only after the clause becomes solver authority.
                localization_checkpoint_store.clear(project, mode)
                detail = ", ".join(f"{name}@{version}" for name, version in sorted(nogood.items()))

                # Persist only package-manager dependency failures that reproduce
                # in fresh workspaces under the same resolver environment.
                # Project/build failures and unknown/infra failures never become
                # durable version authority.
                if (
                    result.kind == "dependency"
                    and backend_options["persistentLearning"]
                    and spec.constraint_cache_path is not None
                ):
                    if persistent_control_ok is None:
                        control_result = verify_assignment(
                            spec.path, {}, config=config, run_project_checks=False
                        )
                        persistent_control_ok = bool(control_result.ok)
                        if not persistent_control_ok:
                            eprint(
                                f"[warn] {project}: current resolver control is not green; "
                                "learned constraint remains session-local and will not be persisted"
                            )

                    if persistent_control_ok:
                        signatures: List[str] = []
                        reproducible = True
                        for _proof_index in range(backend_options["learningReproductions"]):
                            proof_result = verify_assignment(
                                spec.path, nogood, config=config, run_project_checks=False
                            )
                            if proof_result.ok or proof_result.kind != "dependency":
                                reproducible = False
                                break
                            signatures.append(
                                dependency_failure_signature(
                                    summary=proof_result.summary,
                                    output=proof_result.output,
                                )
                            )
                        if reproducible and signatures and len(set(signatures)) == 1:
                            persisted = persist_verified_nogood(
                                spec.constraint_cache_path,
                                LearnedConstraintProof(
                                    project_path=str(spec.path.resolve()),
                                    environment_fingerprint=project_resolver_context_key,
                                    literals=dict(nogood),
                                    failure_signature=signatures[0],
                                    verified_count=len(signatures),
                                ),
                            )
                            eprint(
                                f"[info] {project}: persistent resolver constraint "
                                f"{'stored' if persisted else 'already known'}; "
                                f"proofs={len(signatures)}, resolverContext={project_resolver_context_key[:12]}"
                            )
                        else:
                            eprint(
                                f"[warn] {project}: resolver constraint was not reproducible with a stable signature; "
                                "kept session-local only"
                            )

                eprint(
                    f"[info] {project}: learned constraint {mode} #{len(learned[project][mode])}: "
                    f"NOT({detail}); verification checks localized in parallel"
                )
                checkpoint_baseline_run(
                    "constraint-learned",
                    completed_iteration=iteration,
                    last_assignment=fingerprint,
                    last_predicate=localized_minimization.predicate,
                )
                progress_reporter.emit(
                    project,
                    mode,
                    "constraint-learned",
                    iteration=iteration,
                    assignment=fingerprint,
                    constraintNumber=len(learned[project][mode]),
                    literals=dict(nogood),
                    **liveness.snapshot(learned_constraints=len(learned[project][mode])),
                )
                if fingerprint == last_fingerprint and len(learned[project][mode]) > 1:
                    eprint(f"[warn] {project}: solver repeated assignment {fingerprint}; learned constraint should force next repair")
                last_fingerprint = fingerprint
            else:
                summary = liveness.snapshot(
                    learned_constraints=len(learned[project][mode])
                )
                hard_exhausted = iteration >= summary["hardIterations"]
                if hard_exhausted and _baseline_interactive():
                    decision_focus = _baseline_human_decision_focus(
                        learned[project][mode], baseline_current_versions,
                        min_confirmed=1,
                    ) or {}
                    checkpoint_baseline_run(
                        "human-decision-required",
                        completed_iteration=iteration,
                        last_assignment=last_fingerprint,
                        status="decision-required",
                    )
                    decision_payload = {
                        "schemaVersion": 1,
                        "reason": "budget-exhausted",
                        "project": project,
                        "mode": mode,
                        "iteration": iteration,
                        "hardIterations": summary["hardIterations"],
                        "learnedConstraints": summary["learnedConstraints"],
                        **decision_focus,
                    }
                    progress_reporter.emit(
                        project, mode, "human-decision-required",
                        iteration=iteration,
                        stopCode="BASELINE_HUMAN_DECISION_REQUIRED",
                        terminalStatus="HUMAN_DECISION_REQUIRED",
                        **{key: value for key, value in decision_payload.items() if key not in {"project", "mode", "iteration", "schemaVersion"}},
                    )
                    _raise_baseline_human_decision(decision_payload)
                terminal_status = (
                    BaselineTerminalStatus.HARD_SAFETY_LIMIT
                    if hard_exhausted
                    else BaselineTerminalStatus.PLATEAU
                )
                stop_code = (
                    "BASELINE_VERIFICATION_HARD_SAFETY_LIMIT"
                    if hard_exhausted
                    else "BASELINE_VERIFICATION_PLATEAU"
                )
                stop_reason = (
                    "absolute-hard-safety-ceiling"
                    if hard_exhausted
                    else "soft-budget-ended-without-fresh-authoritative-progress"
                )
                progress_reporter.emit(
                    project,
                    mode,
                    "budget-exhausted",
                    iteration=iteration,
                    assignment=last_fingerprint,
                    reason=stop_reason,
                    stopCode=stop_code,
                    terminalStatus=terminal_status.value,
                    terminalSource="solve-and-verify",
                    uniqueConfirmedFailedAssignments=len(
                        confirmed_failed_assignments
                    ),
                    **summary,
                )
                raise _baseline_terminal_error(
                    terminal_status,
                    stop_code,
                    f"{project}/{mode}: no resolver-green assignment "
                    f"after {iteration} solve-and-verify iteration(s); "
                    f"reason={stop_reason}; "
                    f"baseIterations={summary['baseIterations']}, "
                    f"allowedIterations={summary['allowedIterations']}, "
                    f"hardIterations={summary['hardIterations']}, "
                    f"certifiedExtensions={summary['certifiedExtensions']}, "
                    f"learnedExtensions={summary['learnedExtensions']}, "
                    f"exactExtensionCredits={summary['exactExtensionCredits']}, "
                    f"learnedConstraints={summary['learnedConstraints']}, "
                    f"exactExclusions={summary['exactExclusions']}, "
                    f"exactSinceLearning={summary['exactSinceLearning']}, "
                    f"uniqueConfirmedFailedAssignments={len(confirmed_failed_assignments)}",
                    source="solve-and-verify",
                )

    # Re-run only as a fail-closed consistency assertion while legacy target/cohort
    # materialization still lives inside resolve_peer_compatibility(). This second
    # solve has NO authority to choose a different assignment: the package-manager
    # verified final_assignments object is the handoff contract.
    applied = resolve_peer_compatibility(
        rows_by_project,
        client,
        modes=modes,
        learned_nogoods_by_project_mode=learned,
        global_exact_exclusions_by_project_mode=global_exact_exclusions,
        apply_results=True,
        shadow_solver_config_by_project={
            name: spec.constraint_verify_config
            for name, spec in projects_by_name.items()
            if name in rows_by_project
        },
        residual_targets_by_project=residual_targets_by_project,
        diagnostic_preferences_by_project_mode=predicate_diagnostic_preferences,
    )
    for project, mode_assignments in final_assignments.items():
        for mode, proven in mode_assignments.items():
            replayed = (applied.get(project) or {}).get(mode)
            if replayed != proven:
                raise BaselineConstraintVerificationError(
                    f"PROVEN_ASSIGNMENT_REOPENED: {project}/{mode}: "
                    f"verified={assignment_fingerprint(proven)}, "
                    f"replayed={assignment_fingerprint(replayed or {})}. "
                    "A post-verification solve produced a different assignment; no artifact was emitted."
                )
            for row in rows_by_project.get(project, []):
                setattr(row, f"target_{mode}_dynamic_locked", True)

    for project, mode_assignments in final_assignments.items():
        spec = projects_by_name.get(project)
        config = verification_config_by_project.get(project)
        if spec is None or config is None:
            raise BaselineConstraintVerificationError(
                f"PROVEN_DEPENDENCY_PROOF_CONTEXT_MISSING: {project}"
            )
        rows = rows_by_project.get(project, [])
        rows_for_name: Dict[str, List[DependencyRow]] = defaultdict(list)
        for row in rows:
            rows_for_name[row.name].append(row)
        rows_by_name = {
            name: _aggregate_duplicate_package_row(items)
            for name, items in rows_for_name.items()
        }

        for mode, proven in mode_assignments.items():
            removals = _types_stub_removals_for_assignment(
                rows_by_name, dict(proven), mode, client
            )
            for row in rows:
                if row.name not in removals:
                    continue
                exact_literal = str(proven.get(row.name) or row.current_version)
                _set_mode_target(
                    row,
                    mode,
                    exact_literal,
                    "PROVEN_REMOVE_TARGET: removal is part of the exact Baseline ProofEnvelope",
                )
                setattr(row, f"target_{mode}_dynamic_locked", True)

            if proof_envelopes_out is not None:
                envelope = _build_proven_envelope_for_mode(
                    project,
                    mode,
                    spec,
                    rows_by_name,
                    dict(proven),
                    set(removals),
                    config,
                    client,
                )
                proof_envelopes_out.setdefault(project, {})[mode] = envelope

    assert_proven_assignment_conformance(
        rows_by_project,
        final_assignments,
        modes=modes,
    )
    return final_assignments

def assert_proven_assignment_conformance(
    rows_by_project: Dict[str, List[DependencyRow]],
    proven_assignments: Mapping[str, Mapping[str, Mapping[str, str]]],
    *,
    modes: Tuple[str, ...] = ("yellow", "green", "default"),
) -> None:
    """Fail if any post-proof code changed one literal of a proven assignment."""
    mismatches: List[str] = []
    for project, mode_assignments in proven_assignments.items():
        rows = rows_by_project.get(project, [])
        for mode in modes:
            proven = mode_assignments.get(mode)
            if proven is None:
                continue
            for row in rows:
                if row.name not in proven:
                    continue
                target = str(getattr(row, _target_attr(mode), "") or "")
                actual = target if target_is_action(target) else row.current_version
                expected = str(proven[row.name])
                intent_policy = _baseline_intent_policy(row.name)
                resolved_version = str(proven.get(row.name) or row.current_version)
                if intent_policy == "keep-current" and resolved_version != row.current_version:
                    mismatches.append(f"{project}/{mode}:{row.name} USER_BASELINE_KEEP_CURRENT expected={row.current_version} resolved={resolved_version}")
                if intent_policy == "required" and resolved_version == row.current_version:
                    mismatches.append(f"{project}/{mode}:{row.name} USER_BASELINE_REQUIRED_UPDATE remained={resolved_version}")
                if actual != expected:
                    mismatches.append(
                        f"{project}/{mode}:{row.name} expected={expected} actual={actual}"
                    )
    if mismatches:
        raise BaselineConstraintVerificationError(
            "PROVEN_ASSIGNMENT_MUTATED: "
            + " | ".join(mismatches[:20])
            + (" | ..." if len(mismatches) > 20 else "")
        )


def _peer_scope_blocker(
    row: DependencyRow,
    target: str,
    mode: str,
    rows_by_name: Dict[str, DependencyRow],
    client: LiveDataClient,
) -> str:
    """Compatibility check for an already resolved target assignment.

    Kept for post-resolution Yellow trimming and backwards-compatible tests. The
    function deliberately ignores group/subgroup/branch identity: every planned
    direct target participates in one final assignment.
    """
    if not target_is_action(target):
        return ""
    assignment = {
        name: (getattr(peer, _target_attr(mode)) if target_is_action(getattr(peer, _target_attr(mode))) else peer.current_version)
        for name, peer in rows_by_name.items()
    }
    assignment[row.name] = target
    names = set(rows_by_name)
    return _assignment_constraint_issue(names, assignment, rows_by_name, client, partial=False)


def auto_expand_direct_peer_scope(
    rows_by_project: Dict[str, List[DependencyRow]],
    client: LiveDataClient,
) -> None:
    """Backward-compatible entry point for the old peer-closure pass.

    It now delegates to the general solver. In particular it no longer copies a
    source row's group/subgroup onto a companion.
    """
    resolve_peer_compatibility(rows_by_project, client)


def enforce_direct_peer_scope_compatibility(
    rows_by_project: Dict[str, List[DependencyRow]],
    client: LiveDataClient,
    health_by_project: Optional[Dict[str, ProjectHealth]] = None,
) -> None:
    """Backward-compatible entry point for the former one-pass peer pruner."""
    resolve_peer_compatibility(rows_by_project, client)


def validate_final_peer_assignment(
    rows_by_project: Dict[str, List[DependencyRow]],
    client: LiveDataClient,
    *,
    modes: Tuple[str, ...] = ("yellow", "green", "default"),
) -> List[str]:
    """Prove the final registry-backed peer assignment after all target mutation."""
    issues: List[str] = []
    for project, rows in rows_by_project.items():
        rows_by_name = {row.name: row for row in rows}
        names = set(rows_by_name)
        for mode in modes:
            assignment = {
                name: (getattr(row, _target_attr(mode)) if target_is_action(getattr(row, _target_attr(mode))) else row.current_version)
                for name, row in rows_by_name.items()
            }
            for name, row in sorted(rows_by_name.items()):
                version = assignment[name]
                if version != row.current_version and not _candidate_registry_installable(row, version, client):
                    issues.append(f"{project}/{mode}: REGISTRY_TARGET_UNAVAILABLE: {name}@{version}")
            issue = _assignment_constraint_issue(names, assignment, rows_by_name, client, partial=False)
            if issue:
                issues.append(f"{project}/{mode}: {issue}")
    return issues


def enrich_registry_target_evidence(
    rows_by_project: Dict[str, List[DependencyRow]],
    client: LiveDataClient,
    *,
    allow_target_mutation: bool = True,
) -> None:
    """Attach target tarball proof and remove any action without Nexus evidence."""
    for project, rows in rows_by_project.items():
        for row in rows:
            meta = client.npm_cache.get(row.name)
            for mode in ("yellow", "green", "default"):
                target = getattr(row, _target_attr(mode))
                if not target_is_action(target):
                    continue
                if not isinstance(meta, dict):
                    evidence = {
                        "package": row.name,
                        "version": target,
                        "registry": client.registry,
                        "status": "metadata-unavailable",
                        "tarballUrl": "",
                        "error": "package metadata unavailable from configured registry",
                    }
                elif target == row.current_version:
                    evidence = {
                        "package": row.name,
                        "version": target,
                        "registry": client.registry,
                        "status": "current-installed",
                        "tarballUrl": client.registry_tarball_url(meta, target),
                        "error": "",
                    }
                else:
                    evidence = client.registry_version_artifact(row.name, meta, target)
                row.registry_artifacts[target] = evidence
                if evidence.get("status") not in {"available", "current-installed"}:
                    blocker = (
                        f"REGISTRY_TARGET_UNAVAILABLE: {row.name}@{target}; status={evidence.get('status')}; "
                        f"configured registry={client.registry}; {evidence.get('error') or 'tarball probe failed'}"
                    )
                    if not allow_target_mutation:
                        raise BaselineConstraintVerificationError(
                            f"FINAL_PROVEN_ASSIGNMENT_REGISTRY_DRIFT: {project}/{mode}: {blocker}. "
                            "Registry evidence changed after proof; no target was rewritten."
                        )
                    _set_mode_target(row, mode, NO_ACTION, blocker)
                    row.notes = target_reason_join([row.notes, blocker])
                    eprint(f"[warn] {project}: {blocker}")


def projected_lag_ok_count(rows: List[DependencyRow], mode: str, removed: int = 0) -> int:
    return (
        sum(
            1
            for row in rows
            if not row.scope_excluded and dependency_is_lag_ok_after_planned_target(row, mode)
        )
        + removed
    )


def _yellow_trim_is_protected(row: DependencyRow) -> bool:
    """Keep mandatory/security/Supervisor/cohort targets out of greedy trimming."""
    if row.scope_excluded or row.planner_deferred:
        return True
    if row.lag_threshold_months < 12:
        return True
    if parse_vuln_counts(row.current_vulns).get("C", 0) > 0:
        return True
    if target_is_action(row.target_yellow_non_lag):
        return True
    if row.planner_target_yellow or row.planner_target_default:
        return True
    if row.target_yellow_dynamic_locked:
        return True
    # Peer-connected targets are atomic for the final assignment. Greedy lag
    # minimization must never trim one member and leave stale dependents behind.
    if row.compatibility_cohort:
        return True
    return any(
        marker in (row.compatibility_note or "")
        for marker in ("AUTO_PEER_CLOSURE", "SUPERVISOR_SCOPE_EXPANSION")
    )


def minimize_yellow_plan_after_compatibility(
    rows_by_project: Dict[str, List[DependencyRow]],
    client: LiveDataClient,
    health_by_project: Dict[str, ProjectHealth],
) -> None:
    """Run greedy minimization only over the final executable target set.

    If compatibility/registry narrowing leaves less than the 85% planning
    reserve -- even less than the hard 80% Yellow threshold -- keep every safe
    action. An unreachable health goal is a best-effort outcome, never a reason
    to discard useful migration work.
    """
    for project, rows in rows_by_project.items():
        health = health_by_project.get(project)
        if not health or health.status_rank >= TARGET_RANK["yellow"]:
            continue
        required = required_ratio_count(health.total, YELLOW_PLANNING_RATIO)
        projected = projected_lag_ok_count(rows, "yellow", health.removed)
        if projected <= required:
            # The three target modes passed compatibility independently. A red
            # project's default mode is Yellow, so publish one canonical result
            # even when there is nothing safe to trim.
            if next_target_for_status(health.status) == "yellow":
                for row in rows:
                    row.target_default = row.target_yellow
                    row.target_default_reason = row.target_yellow_reason
                    row.target_default_non_lag = row.target_yellow_non_lag
                    row.target_default_non_lag_reason = row.target_yellow_non_lag_reason
                    row.target_default_has_lag = row.target_yellow_has_lag
                    row.target_default_dynamic_locked = row.target_yellow_dynamic_locked
            eprint(
                f"[info] {project}: post-compatibility Yellow pool kept in full; "
                f"projected={projected}/{health.total}, reserve target={required}/{health.total}"
            )
            continue

        by_name = {row.name: row for row in rows}
        removable = [
            row
            for row in rows
            if target_is_action(row.target_yellow)
            and row.target_yellow_has_lag
            and not _yellow_trim_is_protected(row)
        ]
        # Remove expensive optional work first. Every accepted removal is
        # revalidated against remaining direct peer edges.
        removable.sort(
            key=lambda row: row_update_effort_score(row, row.target_yellow),
            reverse=True,
        )
        removed_targets = 0
        for row in removable:
            old = (
                row.target_yellow,
                row.target_yellow_reason,
                row.target_yellow_has_lag,
                row.target_yellow_dynamic_locked,
            )
            replacement = row.target_yellow_non_lag if target_is_action(row.target_yellow_non_lag) else NO_ACTION
            row.target_yellow = replacement
            row.target_yellow_reason = target_reason_join([
                row.target_yellow_non_lag_reason if target_is_action(replacement) else "",
                "POST_COMPAT_GREEDY_TRIMMED: необязательный lag-target удалён после peer/registry проверки; запас плана остаётся >=85%",
            ])
            row.target_yellow_has_lag = False
            row.target_yellow_dynamic_locked = False
            next_projected = projected_lag_ok_count(rows, "yellow", health.removed)
            peer_broken = any(
                _peer_scope_blocker(candidate, candidate.target_yellow, "yellow", by_name, client)
                for candidate in rows
                if target_is_action(candidate.target_yellow)
            )
            if next_projected < required or peer_broken:
                (
                    row.target_yellow,
                    row.target_yellow_reason,
                    row.target_yellow_has_lag,
                    row.target_yellow_dynamic_locked,
                ) = old
                continue
            projected = next_projected
            removed_targets += 1

        # Default mode for a red project is Yellow. Keep it byte-for-byte in
        # sync with the post-compatibility result instead of running a second
        # independent greedy pass.
        if next_target_for_status(health.status) == "yellow":
            for row in rows:
                row.target_default = row.target_yellow
                row.target_default_reason = row.target_yellow_reason
                row.target_default_non_lag = row.target_yellow_non_lag
                row.target_default_non_lag_reason = row.target_yellow_non_lag_reason
                row.target_default_has_lag = row.target_yellow_has_lag
                row.target_default_dynamic_locked = row.target_yellow_dynamic_locked
        eprint(
            f"[info] {project}: post-compatibility greedy removed {removed_targets} optional target(s); "
            f"projected={projected}/{health.total}, reserve target={required}/{health.total}"
        )


def render_suggestion_table(f, suggestions: List[WorkSuggestion]) -> None:
    f.write("| Группа | Предложение | Ветка | Пакеты | Почему вместе | Проверки | Риски/остатки | Confidence |\n")
    f.write("|---:|---|---|---|---|---|---|---|\n")
    for s in sorted(suggestions, key=lambda x: (x.group, x.family, x.project)):
        vals = [
            s.group,
            s.title,
            f"`{s.suggested_branch}`",
            ", ".join(f"`{p}`" for p in s.packages),
            s.rationale,
            s.checks,
            s.risk_note,
            s.confidence,
        ]
        f.write("| " + " | ".join(md_escape(v) for v in vals) + " |\n")
    f.write("\n")



def write_markdown(
    rows_by_project: Dict[str, List[DependencyRow]],
    out: Path,
    baseline_comparisons: Optional[Dict[str, Dict[str, Any]]] = None,
    project_specs: Optional[Dict[str, ProjectSpec]] = None,
    health_by_project: Optional[Dict[str, ProjectHealth]] = None,
) -> None:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    baseline_comparisons = baseline_comparisons or {}
    project_specs = project_specs or {}
    health_by_project = health_by_project or enrich_project_targets(rows_by_project)
    with out.open("w", encoding="utf-8") as f:
        f.write("# Live dependency roadmap\n\n")
        f.write(f"Дата генерации: `{now}`.\n\n")
        f.write("Источники: direct versions из `package.json`; project lockfile для install-consistency; isolated package-lock/Nexus audit; npm registry и OSV.dev.\n\n")
        f.write("> Важно: dashboard использует OSV для известной уязвимости конкретной package/version пары и registry metadata для версий/дат. Ручной Nexus/npm audit — отдельная пользовательская сверка транзитивного дерева и внутренних пакетов; `generate` его не запускает.\n\n")
        f.write(GROUP_PRINCIPLES + "\n\n")

        # Summary.
        f.write("## Сводка по группам\n\n")
        summary_group_ids = sorted({r.group for rows in rows_by_project.values() for r in rows})
        group_headers = " | ".join(f"Г{g}" for g in summary_group_ids)
        f.write(f"| Проект | Статус | {group_headers + ' | ' if group_headers else ''}Всего |\n")
        f.write("|---|---|" + "---:|" * (len(summary_group_ids) + 1) + "\n")
        for project in sorted(rows_by_project):
            rows = rows_by_project[project]
            active_rows = [r for r in rows if not r.scope_excluded]
            counts = {g: len([r for r in active_rows if r.group == g]) for g in summary_group_ids}
            h = health_by_project[project]
            group_cells = " | ".join(str(counts[g]) for g in summary_group_ids)
            f.write(f"| {md_escape(project)} | {STATUS_NAMES[h.status]} | {group_cells + ' | ' if group_cells else ''}{len(active_rows)} |\n")
        f.write("\n")

        suggestions_by_project, global_suggestions = build_all_suggestions(rows_by_project)

        f.write("## Текущее состояние проектов и целевые переходы\n\n")
        f.write("| Проект | Статус | Baseline transition | Lag OK ≥80% | C | H | M | L | Что значит режим `По умолчанию` | Причина статуса |\n")
        f.write("|---|---|---|---:|---:|---:|---:|---:|---|---|\n")
        for project in sorted(rows_by_project):
            h = health_by_project[project]
            default_goal = next_target_for_status(h.status)
            default_label = "ничего не делать" if default_goal == "none" else ("до жёлтого" if default_goal == "yellow" else "до зелёного")
            cmp = baseline_comparisons.get(project)
            f.write(f"| {md_escape(project)} | {STATUS_NAMES[h.status]} | {md_escape(comparison_badge_text(cmp))} | {h.lag_ok_pct:.1f}% | {h.critical} | {h.high} | {h.moderate} | {h.low} | {default_label} | {md_escape(h.reason)} |\n")
        f.write("\n")

        f.write("## Suggestions: предлагаемые подветки / batch-MR\n\n")
        f.write("Скрипт использует display-группы только как представление и дополнительно предлагает ветки по типу работы: router, SignalR, build-toolchain, lint/stylelint/TS, UI-kit, auth, widget-framework, DEV/local и т.д. Это не приказ, а черновик плана веток: перед MR стоит сверить usage и реальные CI scripts.\n\n")
        if global_suggestions:
            f.write("### Cross-project suggestions\n\n")
            render_suggestion_table(f, global_suggestions)
        else:
            f.write("Cross-project suggestions не найдены: похожие работы либо единичные, либо не набрали достаточного сигнала.\n\n")

        for project in sorted(rows_by_project):
            rows = rows_by_project[project]
            h = health_by_project[project]
            f.write(f"# {md_escape(project)} — {STATUS_NAMES[h.status]}\n\n")
            f.write(f"> Статус: **{STATUS_NAMES[h.status]}**. Lag OK: **{h.lag_ok_pct:.1f}%** ({h.lag_ok_12m}/{h.total}), C/H/M/L: **{h.critical}/{h.high}/{h.moderate}/{h.low}**. Причина: {md_escape(h.reason)}.\n\n")
            cmp = baseline_comparisons.get(project)
            if cmp:
                f.write(f"> Baseline: **{md_escape(comparison_badge_text(cmp))}**. {md_escape(comparison_note(cmp))}.\n\n")
            audit_info = (project_specs.get(project).current_audit if project_specs.get(project) else {}) or {}
            if audit_info.get("mode") == "manual-only":
                canonical_lock = (project_specs.get(project).lockfile_state or {}).get("lockfile", "project lockfile")
                f.write(
                    f"> Vulnerability audit: **manual-only**. Dashboard uses **{md_escape(Path(str(canonical_lock)).name)}**; "
                    "run the bundled `manual_dependency_audit.py` (or the Desktop Manual audit action) for an explicit Nexus reconciliation.\n\n"
                )
            elif audit_info:
                totals = audit_info.get("totals") or {}
                f.write(
                    f"> Manual vulnerability audit: engine **{md_escape(audit_info.get('engine') or 'unknown')}**, "
                    f"registry **{md_escape(audit_info.get('effectiveRegistry') or audit_info.get('requestedRegistry') or 'unknown')}**, "
                    f"C/H/M/L **{md_escape(totals.get('critical', '?'))}/{md_escape(totals.get('high', '?'))}/"
                    f"{md_escape(totals.get('moderate', '?'))}/{md_escape(totals.get('low', '?'))}**, "
                    f"complete **{str(bool(audit_info.get('complete'))).lower()}**.\n\n"
                )
            project_suggestions = suggestions_by_project.get(project, [])
            if project_suggestions:
                f.write("## Suggested subbranches for this project\n\n")
                render_suggestion_table(f, project_suggestions)
            for group in group_ids_for_rows(rows):
                sub = [r for r in rows if r.group == group]
                if not sub:
                    continue
                f.write(f"## {group_display_name(group)}\n\n")
                f.write("| Либа | runtime/dev | Текущая версия | До какой обновить (по умолчанию) | Latest/max | Уязвимости текущей | Что снимаем / риск | Min без Critical | Min без Critical/High | Min без известных vuln | Min lag ≤12м | Min lag ≤9м | Min lag ≤6м | Причина группы | Почему обновить / остаточный риск |\n")
                f.write("|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---|---|\n")
                for r in sorted(sub, key=lambda x: x.name.lower()):
                    target_note = r.target_default_reason if r.target_default != NO_ACTION else (r.notes or "—")
                    vals = [
                        r.name, r.kind, r.current_version, r.target_default, r.latest_version,
                        r.current_vulns, vulnerability_work_note(r), r.min_no_critical, r.min_no_high, r.min_no_vuln,
                        r.min_lag_12m, r.min_lag_9m, r.min_lag_6m,
                        r.reason, target_note,
                    ]
                    f.write("| " + " | ".join(md_escape(v) for v in vals) + " |\n")
                f.write("\n")



def file_sha256(path: Optional[Path]) -> Optional[str]:
    if not path or not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def direct_dependency_snapshot(project_dir: Path) -> Dict[str, Dict[str, str]]:
    pkg_path = project_dir / "package.json"
    try:
        pkg_json = read_json(pkg_path)
    except Exception:
        return {}
    result: Dict[str, Dict[str, str]] = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        values = pkg_json.get(section) or {}
        if not isinstance(values, dict):
            continue
        for name, spec in values.items():
            result[f"{section}:{name}"] = {"section": section, "name": str(name), "spec": str(spec)}
    return result


def baseline_project_dir(baselines_dir: Path, project: str) -> Path:
    return baselines_dir / slugify(project)


def baseline_snapshot_path(baselines_dir: Path, project: str, captured_at: str) -> Path:
    safe_time = captured_at.replace(":", "-").replace(".", "-")
    return baseline_project_dir(baselines_dir, project) / f"{safe_time}.json"


def _baseline_captured_at(snapshot: Dict[str, Any], path: Path) -> dt.datetime:
    raw = str(snapshot.get("capturedAt") or "").strip()
    if raw:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except Exception:
            pass
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    except OSError:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def load_latest_baseline(baselines_dir: Path, project: str) -> Optional[Dict[str, Any]]:
    """Load the newest valid baseline by capturedAt, not by filename spelling.

    Baseline filenames are normally timestamp-derived, but copied files, legacy
    formats and manual renames can break lexicographic ordering.  Scan all valid
    snapshots and select the greatest capturedAt value; a corrupt newest file
    must not hide an older valid baseline.
    """
    project_dir = baseline_project_dir(baselines_dir, project)
    if not project_dir.exists():
        return None
    valid: List[Tuple[dt.datetime, str, Dict[str, Any]]] = []
    for candidate in project_dir.glob("*.json"):
        try:
            snapshot = read_json(candidate)
        except Exception:
            continue
        if not isinstance(snapshot, dict):
            continue
        valid.append((_baseline_captured_at(snapshot, candidate), candidate.name, snapshot))
    if not valid:
        return None
    valid.sort(key=lambda item: (item[0], item[1]))
    return valid[-1][2]


def write_baseline_snapshot(baselines_dir: Path, snapshot: Dict[str, Any]) -> Path:
    project = str(snapshot["project"])
    captured_at = str(snapshot["capturedAt"])
    out = baseline_snapshot_path(baselines_dir, project, captured_at)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, out)
    return out


def compare_direct_dependencies(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    changed: List[Dict[str, Any]] = []
    added: List[str] = []
    removed: List[str] = []
    for name in sorted(set(before) | set(after)):
        b = before.get(name)
        a = after.get(name)
        if b == a:
            continue
        if b is None:
            added.append(name)
        elif a is None:
            removed.append(name)
        else:
            changed.append({"name": name, "before": b, "after": a})
    return {
        "changedCount": len(changed),
        "addedCount": len(added),
        "removedCount": len(removed),
        "changed": changed[:50],
        "added": added[:50],
        "removed": removed[:50],
    }


def build_baseline_snapshot(
    project: str,
    project_dir: Path,
    rows: List[DependencyRow],
    health: ProjectHealth,
    label: str = "",
    source_checkout: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    pkg_path = project_dir / "package.json"
    lock_path = find_lockfile(project_dir)
    return {
        "schemaVersion": 2,
        "type": "dependency-roadmap-baseline",
        "project": project,
        "projectDir": str(project_dir),
        "label": label,
        "capturedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "lagPlanning": {
            "anchored": True,
            "bufferMonths": LAG_TARGET_BUFFER_MONTHS,
            "rule": "compliance M months; remediation target max(3, M-3) months",
        },
        "sourceCheckout": source_checkout or {"verified": False},
        "files": {
            "packageJson": str(pkg_path),
            "packageJsonSha256": file_sha256(pkg_path),
            "lockfile": str(lock_path) if lock_path else None,
            "lockfileSha256": file_sha256(lock_path),
        },
        "health": dataclasses.asdict(health),
        "directDependencies": direct_dependency_snapshot(project_dir),
        "rows": [row_json(r) for r in rows],
    }


def build_baseline_comparison(
    project: str,
    rows: List[DependencyRow],
    health: ProjectHealth,
    baseline: Optional[Dict[str, Any]],
    project_dir: Path,
    *,
    captured_this_run: bool = False,
) -> Optional[Dict[str, Any]]:
    if not baseline:
        return None
    baseline_health = baseline.get("health") or {}
    baseline_status = str(baseline_health.get("status", "unknown"))
    baseline_rank = int(baseline_health.get("status_rank", STATUS_RANK.get(baseline_status, 0)))
    baseline_deps = baseline.get("directDependencies") or {}
    current_deps = direct_dependency_snapshot(project_dir)
    return {
        "project": project,
        "baselineCapturedAt": baseline.get("capturedAt"),
        "baselineLabel": baseline.get("label", ""),
        "baselineStatus": baseline_status,
        "currentStatus": health.status,
        "transition": f"{baseline_status} -> {health.status}",
        "statusDelta": health.status_rank - baseline_rank,
        "improved": health.status_rank > baseline_rank,
        "regressed": health.status_rank < baseline_rank,
        "lagOkPctBefore": baseline_health.get("lag_ok_pct"),
        "lagOkPctAfter": health.lag_ok_pct,
        "criticalBefore": baseline_health.get("critical"),
        "criticalAfter": health.critical,
        "highBefore": baseline_health.get("high"),
        "highAfter": health.high,
        "moderateBefore": baseline_health.get("moderate"),
        "moderateAfter": health.moderate,
        "lowBefore": baseline_health.get("low"),
        "lowAfter": health.low,
        "directDependencyDiff": compare_direct_dependencies(baseline_deps, current_deps),
        "baselineCapturedThisRun": bool(captured_this_run),
    }


def comparison_badge_text(comparison: Optional[Dict[str, Any]]) -> str:
    if not comparison:
        return "No baseline"
    if comparison.get("baselineCapturedThisRun"):
        return f"{comparison.get('currentStatus')} baseline captured"
    marker = "improved" if comparison.get("improved") else ("regressed" if comparison.get("regressed") else "unchanged")
    return f"{comparison.get('transition')} ({marker})"


def format_optional_pct(value: Any) -> str:
    return f"{float(value):.1f}%" if isinstance(value, (int, float)) else "unknown"


def comparison_note(comparison: Optional[Dict[str, Any]]) -> str:
    if not comparison:
        return "baseline snapshot not found"
    if comparison.get("baselineCapturedThisRun"):
        label = str(comparison.get("baselineLabel") or "").strip()
        label_note = f"; label {label}" if label else ""
        return (
            f"baseline {comparison.get('baselineCapturedAt')} captured in this run{label_note}; "
            "run generate again without --capture-baseline to compare a changed checkout against it"
        )
    diff = comparison.get("directDependencyDiff") or {}
    return (
        f"baseline {comparison.get('baselineCapturedAt')}; "
        f"lag {format_optional_pct(comparison.get('lagOkPctBefore'))} -> {format_optional_pct(comparison.get('lagOkPctAfter'))}; "
        f"C/H {comparison.get('criticalBefore')}/{comparison.get('highBefore')} -> {comparison.get('criticalAfter')}/{comparison.get('highAfter')}; "
        f"direct changes: changed {diff.get('changedCount', 0)}, added {diff.get('addedCount', 0)}, removed {diff.get('removedCount', 0)}"
    )


def compact_history_snapshot(
    rows_by_project: Dict[str, List[DependencyRow]],
    health_by_project: Dict[str, ProjectHealth],
    label: str = "",
    *,
    baseline_comparisons: Optional[Dict[str, Dict[str, Any]]] = None,
    project_specs: Optional[Dict[str, ProjectSpec]] = None,
    dashboard_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a complete, replayable dashboard snapshot.

    The legacy implementation intentionally stored only a compact subset of
    dependency facts.  That was enough for a small before/after badge, but not
    enough to answer the much more useful question "what exactly did the
    dashboard show at that moment?".  Schema v2 preserves every roadmap row,
    project health, branch suggestions, baseline comparison, git provenance and
    dashboard overrides.  Older schema-v1 snapshots remain readable by the UI.
    """
    captured_at = dt.datetime.now(dt.timezone.utc).isoformat()
    baseline_comparisons = baseline_comparisons or {}
    project_specs = project_specs or {}
    suggestions_by_project, global_suggestions = build_all_suggestions(rows_by_project)
    projects: Dict[str, Any] = {}
    for project, rows in rows_by_project.items():
        spec = project_specs.get(project)
        projects[project] = {
            "health": dataclasses.asdict(health_by_project[project]),
            "baselineComparison": baseline_comparisons.get(project),
            "git": {
                "projectDir": str(spec.path),
                "sourceBranch": spec.source_branch,
                "baseBranch": spec.base_branch,
                "mergedBranch": spec.resolved_merged_branch(),
                "releaseBranch": spec.resolved_release_branch(),
                "sourceCheckout": spec.source_checkout,
                "lockfile": spec.lockfile_state,
                "manualAudit": spec.current_audit,
            } if spec else {},
            "suggestions": [dataclasses.asdict(item) for item in suggestions_by_project.get(project, [])],
            "dependencies": [row_json(row) for row in rows],
        }
    status_counts = {"red": 0, "yellow": 0, "green": 0}
    for health in health_by_project.values():
        status_counts[health.status] = status_counts.get(health.status, 0) + 1
    return {
        "schemaVersion": 2,
        "type": "dependency-roadmap-dashboard-snapshot",
        "capturedAt": captured_at,
        "label": label,
        "source": "generate",
        "summary": {
            "projects": len(projects),
            "dependencies": sum(sum(1 for row in rows if not row.scope_excluded) for rows in rows_by_project.values()),
            "statusCounts": status_counts,
        },
        "dashboardState": dashboard_state or {"schemaVersion": 1, "packageOverrides": {}},
        "globalSuggestions": [dataclasses.asdict(item) for item in global_suggestions],
        "projects": projects,
    }

def write_history_snapshot(history_dir: Path, snapshot: Dict[str, Any]) -> Path:
    snapshots_dir = history_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    safe_time = str(snapshot.get("capturedAt") or dt.datetime.now(dt.timezone.utc).isoformat()).replace(":", "-").replace(".", "-")
    path = snapshots_dir / f"{safe_time}.json"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return path


def load_history_snapshots(history_dir: Path, limit: int = 30) -> List[Dict[str, Any]]:
    snapshots_dir = history_dir / "snapshots"
    if not snapshots_dir.exists():
        return []
    result: List[Dict[str, Any]] = []
    for path in sorted(snapshots_dir.glob("*.json"), reverse=True)[:limit]:
        try:
            item = read_json(path)
            item["_file"] = str(path)
            result.append(item)
        except Exception as exc:
            eprint(f"[warn] cannot read history snapshot {path}: {exc}")
    return result


def html_escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def json_for_script(value: Any) -> str:
    """Serialize JSON safely inside a classic <script> element."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def vuln_flags(summary: str) -> Tuple[int, int, int, int, int, int]:
    counts = parse_vuln_counts(summary)
    any_count = sum(counts.values())
    ch_count = counts.get("C", 0) + counts.get("H", 0)
    return counts.get("C", 0), counts.get("H", 0), counts.get("M", 0), counts.get("L", 0), counts.get("U", 0), any_count if summary not in ("0", "unknown", "неизвестно", "—") else 0


def write_html(
    rows_by_project: Dict[str, List[DependencyRow]],
    out: Path,
    history_dir: Optional[Path] = None,
    registry: str = NPM_REGISTRY,
    settings_sources = None,
    baseline_comparisons: Optional[Dict[str, Dict[str, Any]]] = None,
    project_specs: Optional[Dict[str, ProjectSpec]] = None,
    health_by_project: Optional[Dict[str, ProjectHealth]] = None,
    dashboard_state_path: Optional[Path] = None,
    history_snapshots: Optional[List[Dict[str, Any]]] = None,
    roadmap_json_path: Optional[Path] = None,
    knowledge_entries: Optional[List[Dict[str, Any]]] = None,
    knowledge_log_path: Optional[Path] = None,
    proven_dependency_state: Optional[Dict[str, Any]] = None,
    proven_dependency_state_path: Optional[Path] = None,
) -> None:
    """Write an interactive self-contained HTML report.

    The HTML is intentionally dependency-free: it can be opened from disk,
    attached to a MR, or published as a static artifact without npm install.
    """
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    history_dir = history_dir or (Path(__file__).resolve().parent / ".dependency-update-history")
    settings_sources = settings_sources or []
    baseline_comparisons = baseline_comparisons or {}
    project_specs = project_specs or {}
    history_snapshots = history_snapshots or []
    knowledge_entries = knowledge_entries or []
    health_by_project = health_by_project or enrich_project_targets(rows_by_project)
    suggestions_by_project, global_suggestions = build_all_suggestions(rows_by_project)
    dashboard_state_data: Dict[str, Any] = {"schemaVersion": 1, "packageOverrides": {}}
    if dashboard_state_path and dashboard_state_path.exists():
        try:
            loaded_state = read_json(dashboard_state_path)
            if isinstance(loaded_state, dict) and isinstance(loaded_state.get("packageOverrides", {}), dict):
                dashboard_state_data = loaded_state
        except Exception as exc:
            eprint(f"[warn] cannot embed dashboard state {dashboard_state_path}: {exc}")
    report_context = {
        "generatedAt": now,
        "registry": str(registry or ""),
        "historyDir": str(history_dir),
        "settingsSources": [str(p) for p in settings_sources],
        "baselineComparisons": baseline_comparisons,
        "dashboardStatePath": str(dashboard_state_path or ""),
        "dashboardState": dashboard_state_data,
        "historySnapshots": history_snapshots,
        "roadmapJsonPath": str(roadmap_json_path or ""),
        "knowledgeLogPath": str(knowledge_log_path or ""),
        "knowledgeEntries": knowledge_entries,
        "provenDependencyState": proven_dependency_state or {"schemaVersion": 1, "projects": {}},
        "provenDependencyStatePath": str(proven_dependency_state_path or ""),
        "projectHealth": {project: dataclasses.asdict(health) for project, health in (health_by_project or {}).items()},
        "suggestions": {
            "global": [dataclasses.asdict(item) for item in global_suggestions],
            "byProject": {project: [dataclasses.asdict(item) for item in items] for project, items in suggestions_by_project.items()},
        },
        "agentRunbookPath": str(
            (roadmap_json_path.parent.parent.parent / "AGENT_RUNBOOK.md")
            if roadmap_json_path else Path("AGENT_RUNBOOK.md")
        ),
        "validatorPath": str(Path(__file__).resolve().parent / "validate_dependency_update.py"),
        "releaseBranchToolPath": str(Path(__file__).resolve().parent / "dependency_release_branch.py"),
        "gitHookPolicyToolPath": str(Path(__file__).resolve().parent / "git_hook_policy.py"),
        "projectGit": {
            name: {
                "sourceBranch": spec.source_branch,
                "baseBranch": spec.base_branch,
                "configuredBaseBranch": spec.base_branch,
                "branchPrefix": spec.resolved_branch_prefix(),
                "mergedBranch": spec.resolved_merged_branch(),
                "releaseBranch": spec.resolved_release_branch(),
                "push": spec.git_push,
                "remote": spec.git_remote,
                "sourceCheckout": spec.source_checkout,
                "gitHooks": spec.git_hooks,
                "release": spec.release_config,
                "manualAudit": spec.current_audit,
                "projectDir": str(spec.path),
            }
            for name, spec in project_specs.items()
        },
    }

    def render_knowledge_html() -> str:
        if not knowledge_entries:
            return '<p class="muted">No active package migration knowledge is configured.</p>'
        rows_html: List[str] = []
        for entry in knowledge_entries:
            applicability = json.dumps(entry.get("applicability", {}), ensure_ascii=False, separators=(",", ":"))
            rows_html.append(
                "<tr>"
                f"<td><code>{html_escape(entry['id'])}</code><div class='muted'>{html_escape(entry.get('recordedAt', ''))}</div></td>"
                f"<td>{', '.join('<code>' + html_escape(p) + '</code>' for p in entry['packages'])}</td>"
                f"<td><strong>{html_escape(entry['title'])}</strong><div class='muted'>{html_escape(entry.get('confidence', 'hypothesis'))}</div></td>"
                f"<td>{html_escape(entry['symptom'])}</td><td>{html_escape(entry['cause'])}</td>"
                f"<td>{html_escape(entry['guidance'])}<div class='muted'>Verification: {html_escape('; '.join(entry['verification']))}</div></td>"
                f"<td><code>{html_escape(applicability)}</code></td>"
                "</tr>"
            )
        return (
            "<div class='table-wrap'><table><thead><tr><th>ID</th><th>Packages</th><th>Knowledge</th>"
            "<th>Symptom</th><th>Cause</th><th>Guidance and proof</th><th>Applicability</th></tr></thead><tbody>"
            + "".join(rows_html) + "</tbody></table></div>"
        )

    all_rows = [r for rows in rows_by_project.values() for r in rows]
    active_all_rows = [r for r in all_rows if not r.scope_excluded]
    all_projects = sorted(rows_by_project)
    display_group_ids = group_ids_for_rows(all_rows)
    total_counts = {g: len([r for r in active_all_rows if r.group == g]) for g in display_group_ids}
    status_counts = {"red": 0, "yellow": 0, "green": 0}
    for h in health_by_project.values():
        status_counts[h.status] += 1
    vuln_total = {"C": 0, "H": 0, "M": 0, "L": 0, "U": 0}
    for r in active_all_rows:
        counts = parse_vuln_counts(r.current_vulns)
        for k in vuln_total:
            vuln_total[k] += counts.get(k, 0)

    def render_suggestions_html(suggestions: List[WorkSuggestion]) -> str:
        if not suggestions:
            return '<p class="muted">Suggestions не найдены.</p>'
        rows_html: List[str] = []
        for s in sorted(suggestions, key=lambda x: (x.group, x.family, x.project)):
            rows_html.append(
                "<tr>"
                f"<td>Г{s.group}</td>"
                f"<td><strong>{html_escape(s.title)}</strong><div class='muted'>{html_escape(s.family)}</div></td>"
                f"<td><code>{html_escape(s.suggested_branch)}</code></td>"
                f"<td>{', '.join('<code>' + html_escape(p) + '</code>' for p in s.packages)}</td>"
                f"<td>{html_escape(s.rationale)}</td>"
                f"<td>{html_escape(s.checks)}</td>"
                f"<td>{html_escape(s.risk_note)}</td>"
                f"<td>{html_escape(s.confidence)}</td>"
                "</tr>"
            )
        return """
        <table class="suggestions-table">
          <thead>
            <tr><th>Группа</th><th>Предложение</th><th>Ветка</th><th>Пакеты</th><th>Почему вместе</th><th>Проверки</th><th>Риски/остатки</th><th>Confidence</th></tr>
          </thead>
          <tbody>
        """ + "\n".join(rows_html) + "</tbody></table>"

    def render_dependency_table(rows: List[DependencyRow]) -> str:
        body: List[str] = []
        release_labels = {
            "breaking-confirmed": "Breaking найдены",
            "breaking-likely": "Breaking вероятны",
            "no-breaking-found": "Явных breaking нет",
            "coverage-incomplete": "Диапазон покрыт частично",
            "unavailable": "Не проверено",
            "not-checked-limit": "Не проверено (лимит)",
            "disabled": "Отключено",
            "not-applicable": "Нет target",
        }
        for r in sorted(rows, key=lambda x: (x.name.lower(), x.kind)):
            c, h, m, l, u, any_v = vuln_flags(r.current_vulns)
            ch = c + h
            residual = "1" if (not target_is_action(r.target_default) and r.current_vulns not in ("0", "unknown", "неизвестно", "—")) else "0"
            target_text = " ".join([r.target_default, r.target_yellow, r.target_green, r.target_default_reason, r.target_yellow_reason, r.target_green_reason])
            notes_value = r.notes or "—"
            vuln_work_note = vulnerability_work_note(r)
            release_by_target = {
                target: {
                    "target": intel.target,
                    "status": intel.status,
                    "summary": intel.summary,
                    "coverage": intel.coverage,
                    "breakingChanges": intel.breaking_changes,
                    "migrationNotes": intel.migration_notes,
                    "deprecations": intel.deprecations,
                    "requirements": intel.requirements,
                    "sources": intel.sources,
                }
                for target, intel in r.release_by_target.items()
            }
            initial_release = r.release_by_target.get(r.target_default)
            initial_artifact = r.registry_artifacts.get(r.target_default, {})
            initial_status = initial_release.status if initial_release else r.release_status
            initial_summary = initial_release.summary if initial_release else r.release_summary
            release_payload = {
                "package": r.name,
                "current": r.current_version,
                "target": initial_release.target if initial_release else r.release_target,
                "status": initial_status,
                "summary": initial_summary,
                "coverage": initial_release.coverage if initial_release else r.release_coverage,
                "breakingChanges": initial_release.breaking_changes if initial_release else r.breaking_changes,
                "migrationNotes": initial_release.migration_notes if initial_release else r.migration_notes,
                "deprecations": initial_release.deprecations if initial_release else r.deprecations,
                "requirements": initial_release.requirements if initial_release else r.release_requirements,
                "sources": initial_release.sources if initial_release else r.release_sources,
                "byTarget": release_by_target,
            }
            release_json = json.dumps(release_payload, ensure_ascii=False, separators=(",", ":"))
            registry_artifacts_json = json.dumps(r.registry_artifacts, ensure_ascii=False, separators=(",", ":"))
            text = " ".join([
                r.project, r.name, r.kind, r.current_version, r.latest_version,
                r.current_vulns, r.reason, notes_value, vuln_work_note, target_text,
                r.subgroup, r.release_summary, r.compatibility_cohort, r.compatibility_note,
                r.exclusion_reason,
            ])
            lag_lines = []
            for months, value in ((12, r.min_lag_12m), (9, r.min_lag_9m), (6, r.min_lag_6m), (3, r.min_lag_3m)):
                policy = " lag-policy-active" if months == r.lag_threshold_months else ""
                lag_lines.append(f"<div class='lag-line{policy}'><span>≤{months}м</span> <code>{html_escape(value)}</code></div>")
            subgroup_html = f"<span class='subgroup-badge'>{html_escape(r.subgroup)}</span>" if r.subgroup else "<span class='subgroup-badge muted'>без подгруппы</span>"
            release_label = release_labels.get(initial_status, initial_status)
            row_classes = "dep-row scope-excluded" if r.scope_excluded else "dep-row"
            exclusion_badge = (
                f"<span class='subgroup-badge scope-excluded-badge' title='{html_escape(r.exclusion_reason)}'>исключено</span>"
                if r.scope_excluded else ""
            )
            body.append(
                f"<tr class='{row_classes}' data-project='{html_escape(r.project)}' data-group='{r.group}' data-original-group='{r.group}' data-kind='{html_escape(r.kind)}' "
                f"data-name='{html_escape(r.name)}' data-requested-spec='{html_escape(r.requested_spec)}' data-current='{html_escape(r.current_version)}' data-latest='{html_escape(r.latest_version)}' data-vulns='{html_escape(r.current_vulns)}' "
                f"data-reason='{html_escape(r.reason)}' data-notes='{html_escape(notes_value)}' data-original-notes='{html_escape(notes_value)}' data-vuln-note='{html_escape(vuln_work_note)}' data-subgroup='{html_escape(r.subgroup)}' data-original-subgroup='{html_escape(r.subgroup)}' data-lag-threshold-months='{r.lag_threshold_months}' data-original-lag-threshold-months='{r.lag_threshold_months}' "
                f"data-min-no-critical='{html_escape(r.min_no_critical)}' data-min-no-high='{html_escape(r.min_no_high)}' data-min-no-vuln='{html_escape(r.min_no_vuln)}' "
                f"data-min-lag-12m='{html_escape(r.min_lag_12m)}' data-min-lag-9m='{html_escape(r.min_lag_9m)}' data-min-lag-6m='{html_escape(r.min_lag_6m)}' data-min-lag-3m='{html_escape(r.min_lag_3m)}' "
                f"data-release-status='{html_escape(initial_status)}' data-release-summary='{html_escape(initial_summary)}' data-release-json='{html_escape(release_json)}' "
                f"data-registry-artifacts-json='{html_escape(registry_artifacts_json)}' data-compatibility-cohort='{html_escape(r.compatibility_cohort)}' data-compatibility-note='{html_escape(r.compatibility_note)}' "
                f"data-scope-excluded='{1 if r.scope_excluded else 0}' data-original-scope-excluded='{1 if r.scope_excluded else 0}' data-exclusion-reason='{html_escape(r.exclusion_reason)}' data-original-exclusion-reason='{html_escape(r.exclusion_reason)}' data-exclusion-source='{html_escape(r.exclusion_source)}' "
                f"data-c='{c}' data-h='{h}' data-ch='{ch}' data-any-vuln='{any_v}' data-residual='{residual}' "
                f"data-target-default='{html_escape(r.target_default)}' data-target-yellow='{html_escape(r.target_yellow)}' data-target-green='{html_escape(r.target_green)}' "
                f"data-planned-action-default='{html_escape(r.planned_action_default)}' data-planned-action-yellow='{html_escape(r.planned_action_yellow)}' data-planned-action-green='{html_escape(r.planned_action_green)}' "
                f"data-target-default-reason='{html_escape(r.target_default_reason)}' data-target-yellow-reason='{html_escape(r.target_yellow_reason)}' data-target-green-reason='{html_escape(r.target_green_reason)}' "
                f"data-target-default-non-lag='{html_escape(r.target_default_non_lag)}' data-target-yellow-non-lag='{html_escape(r.target_yellow_non_lag)}' data-target-green-non-lag='{html_escape(r.target_green_non_lag)}' "
                f"data-target-default-non-lag-reason='{html_escape(r.target_default_non_lag_reason)}' data-target-yellow-non-lag-reason='{html_escape(r.target_yellow_non_lag_reason)}' data-target-green-non-lag-reason='{html_escape(r.target_green_non_lag_reason)}' "
                f"data-target-default-has-lag='{1 if r.target_default_has_lag else 0}' data-target-yellow-has-lag='{1 if r.target_yellow_has_lag else 0}' data-target-green-has-lag='{1 if r.target_green_has_lag else 0}' "
                f"data-target-default-dynamic-locked='{1 if r.target_default_dynamic_locked else 0}' data-target-yellow-dynamic-locked='{1 if r.target_yellow_dynamic_locked else 0}' data-target-green-dynamic-locked='{1 if r.target_green_dynamic_locked else 0}' "
                f"data-planning-min-lag-12m='{html_escape(r.planning_min_lag_12m or r.min_lag_12m)}' data-planning-min-lag-9m='{html_escape(r.planning_min_lag_9m or r.min_lag_9m)}' data-planning-min-lag-6m='{html_escape(r.planning_min_lag_6m or r.min_lag_6m)}' data-planning-min-lag-3m='{html_escape(r.planning_min_lag_3m or r.min_lag_3m)}' data-lag-planning-source='{html_escape(r.lag_planning_source)}' "
                f"data-has-target-default='{1 if target_is_action(r.target_default) else 0}' data-has-target-yellow='{1 if target_is_action(r.target_yellow) else 0}' data-has-target-green='{1 if target_is_action(r.target_green) else 0}' "
                f"data-text='{html_escape(text.lower())}'>"
                f"<td class='selection-cell'><input type='checkbox' class='row-select' aria-label='Выбрать {html_escape(r.name)}' /></td>"
                f"<td class='sticky'><code>{html_escape(r.name)}</code><div class='row-meta'>{subgroup_html}{exclusion_badge}<button type='button' class='tiny secondary row-settings-btn'>Настроить</button></div></td>"
                f"<td><code>{html_escape(section_for_dependency_kind(r.kind))}</code></td>"
                f"<td><code>{html_escape(r.current_version)}</code></td>"
                f"<td class='target-cell'><code>{html_escape(r.target_default)}</code><div class='muted target-reason'>{html_escape(r.target_default_reason)}</div><div class='muted target-artifact'>registry artifact: {html_escape(initial_artifact.get('status') or ('not-applicable' if not target_is_action(r.target_default) else 'not-proven'))}</div></td>"
                f"<td class='col-latest'><code>{html_escape(r.latest_version)}</code></td>"
                f"<td class='vuln'>{html_escape(r.current_vulns)}</td>"
                f"<td class='col-vuln-note'>{html_escape(vuln_work_note)}</td>"
                f"<td class='col-min'><div>без C <code>{html_escape(r.min_no_critical)}</code></div><div>без C/H <code>{html_escape(r.min_no_high)}</code></div><div>без vuln <code>{html_escape(r.min_no_vuln)}</code></div></td>"
                f"<td class='col-lag'>{''.join(lag_lines)}</td>"
                f"<td class='release-cell release-{html_escape(initial_status)}'><strong>{html_escape(release_label)}</strong><div class='muted'>{html_escape(initial_summary)}</div><button type='button' class='tiny secondary release-details-btn'>Подробнее</button></td>"
                f"<td>{html_escape(r.reason)}</td>"
                f"<td class='col-notes'>{html_escape(r.notes or '—')}</td>"
                "</tr>"
            )
        return """
        <table class="deps-table sortable-table">
          <thead>
            <tr>
              <th class="selection-cell"><input type="checkbox" class="table-select-visible" aria-label="Выбрать видимые зависимости в таблице" /></th>
              <th data-sort="text">Либа / подгруппа</th>
              <th data-sort="text">Секция</th>
              <th data-sort="version">Текущая</th>
              <th data-sort="version">До какой обновить</th>
              <th class="col-latest" data-sort="version">Latest/max</th>
              <th data-sort="vuln">Уязвимости</th>
              <th class="col-vuln-note" data-sort="text">Что снимаем / риск</th>
              <th class="col-min" data-sort="version">Security targets</th>
              <th class="col-lag" data-sort="version">Lag targets / policy</th>
              <th data-sort="text">Release notes / breaking</th>
              <th data-sort="text">Причина группы</th>
              <th class="col-notes" data-sort="text">Примечания/риск</th>
            </tr>
          </thead>
          <tbody>
        """ + "\n".join(body) + "</tbody></table>"

    with out.open("w", encoding="utf-8") as f:
        f.write("""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Live dependency roadmap</title>
<script>
(function(){
  try {
    const key = 'dependency-roadmap-theme';
    const saved = localStorage.getItem(key) || 'dark';
    const systemLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
    const effective = saved === 'system' ? (systemLight ? 'light' : 'dark') : saved;
    document.documentElement.dataset.theme = effective;
    document.documentElement.dataset.themeMode = saved;
  } catch(e) {
    document.documentElement.dataset.theme = 'dark';
    document.documentElement.dataset.themeMode = 'dark';
  }
})();
</script>
<style>
html { color-scheme: dark; }
:root {
  --bg:#0f172a;
  --panel:#111827;
  --panel2:#1f2937;
  --text:#e5e7eb;
  --muted:#9ca3af;
  --line:#374151;
  --accent:#93c5fd;
  --warn:#fde68a;
  --bad:#fecaca;
  --ok:#bbf7d0;
  --thead:#020617;
  --header-grad-a:#111827;
  --header-grad-b:#1e293b;
  --control-bg:rgba(15,23,42,.72);
  --input-bg:#020617;
  --code-bg:#020617;
  --code-text:#bfdbfe;
  --hover:#172033;
  --shadow:rgba(0,0,0,.18);
  --section-title:#dbeafe;
}

html[data-theme="light"] {
  color-scheme: light;
  --bg:#f8fafc;
  --panel:#ffffff;
  --panel2:#f1f5f9;
  --text:#0f172a;
  --muted:#334155;
  --line:#94a3b8;
  --accent:#1d4ed8;
  --warn:#854d0e;
  --bad:#991b1b;
  --ok:#166534;
  --thead:#dbeafe;
  --header-grad-a:#eff6ff;
  --header-grad-b:#e0f2fe;
  --control-bg:rgba(255,255,255,.78);
  --input-bg:#ffffff;
  --code-bg:#eef2ff;
  --code-text:#1d4ed8;
  --hover:#f1f5f9;
  --shadow:rgba(15,23,42,.12);
  --section-title:#1e40af;
}

* { box-sizing:border-box; }
body { margin:0; font-family:Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:var(--bg); color:var(--text); }
header { padding:28px 32px 18px; background:linear-gradient(135deg, var(--header-grad-a), var(--header-grad-b)); border-bottom:1px solid var(--line); position:relative; z-index:20; }
h1 { margin:0 0 8px; font-size:28px; }
a { color:var(--accent); }
code { background:var(--code-bg); color:var(--code-text); padding:2px 5px; border-radius:5px; white-space:nowrap; }
.container { padding:24px 32px 48px; }
.muted { color:var(--muted); font-size:13px; }
.cards { display:flex; flex-wrap:wrap; gap:12px; margin:16px 0; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; min-width:150px; }
.card strong { display:block; font-size:22px; margin-top:4px; }
.controls { display:grid; grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); gap:12px; margin:18px 0 4px; align-items:end; }
.control { background:var(--control-bg); border:1px solid var(--line); border-radius:10px; padding:10px; }
.control label { display:block; color:var(--muted); font-size:12px; margin-bottom:6px; }
input[type="text"], input[type="number"], textarea, select { width:100%; background:var(--input-bg); color:var(--text); border:1px solid var(--line); border-radius:8px; padding:9px; }
.checks { display:flex; flex-wrap:wrap; gap:10px; margin-top:8px; }
.checks label { display:flex; gap:6px; align-items:center; color:var(--text); font-size:13px; }
.group-prompt-export { display:inline-flex; gap:6px; align-items:center; }
.group-prompt-export select { max-width:260px; }
section, details { margin:20px 0; }
details { background:var(--control-bg); border:1px solid var(--line); border-radius:12px; padding:12px 14px; }
summary { cursor:pointer; font-weight:700; font-size:18px; color:var(--text); }
.group-title { margin:0; font-size:16px; color:var(--section-title); }
.group-section { margin:12px 0; padding:10px 12px; }
.group-section > summary { color:var(--section-title); }
.project-actions { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin:12px 0; padding:10px; border:1px dashed var(--line); border-radius:10px; background:var(--panel); }
.project-actions label { display:flex; gap:6px; align-items:center; font-size:13px; color:var(--text); }
.group-hidden { display:none !important; }
.table-wrap { width:100%; overflow:auto; border:1px solid var(--line); border-radius:12px; position:relative; }
table { width:100%; border-collapse:separate; border-spacing:0; min-width:1200px; background:var(--panel); }
th, td { border-bottom:1px solid var(--line); padding:9px 10px; vertical-align:top; font-size:13px; }
th { background:var(--thead); text-align:left; color:var(--accent); user-select:none; }
.deps-table thead th, .suggestions-table thead th { position:sticky; top:0; z-index:30; box-shadow:0 1px 0 var(--line), 0 8px 16px var(--shadow); }
.deps-table thead th:nth-child(2) { left:42px; z-index:50; }
th[data-sort] { cursor:pointer; }
tr:hover td { background:var(--hover); }
.sticky { position:sticky; left:42px; background:var(--panel); z-index:10; box-shadow:1px 0 0 var(--line); }
tr:hover .sticky { background:var(--hover); }
.selection-cell { width:42px; min-width:42px; max-width:42px; text-align:center; padding-left:8px; padding-right:8px; }
.deps-table thead .selection-cell { position:sticky; left:0; z-index:55; background:var(--thead); }
.deps-table tbody .selection-cell { position:sticky; left:0; z-index:12; background:var(--panel); }
tr:hover .selection-cell { background:var(--hover); }
.vuln { font-weight:700; }
.col-vuln-note { min-width:210px; max-width:320px; }
.row-hidden { display:none !important; }
.project-hidden { display:none !important; }
.hide-col-source .col-source, .hide-col-latest .col-latest, .hide-col-min .col-min, .hide-col-lag .col-lag, .hide-col-notes .col-notes { display:none; }
.badge { display:inline-block; padding:2px 8px; border-radius:999px; background:var(--panel2); border:1px solid var(--line); font-size:12px; margin-left:8px; color:var(--muted); }
.status-badge { display:inline-block; padding:3px 9px; border-radius:999px; border:1px solid var(--line); font-size:12px; font-weight:700; margin-left:8px; }
.status-red { color:#fecaca; background:rgba(127,29,29,.35); border-color:rgba(248,113,113,.45); }
.status-yellow { color:#fde68a; background:rgba(113,63,18,.35); border-color:rgba(251,191,36,.45); }
.status-green { color:#bbf7d0; background:rgba(20,83,45,.35); border-color:rgba(74,222,128,.45); }
html[data-theme="light"] .status-red { color:#991b1b; background:#fee2e2; border-color:#fca5a5; }
html[data-theme="light"] .status-yellow { color:#854d0e; background:#fef3c7; border-color:#fcd34d; }
html[data-theme="light"] .status-green { color:#166534; background:#dcfce7; border-color:#86efac; }
.baseline-banner { margin:10px 0 8px; padding:10px 12px; border-radius:8px; border:1px solid var(--line); background:var(--panel); font-size:13px; }
.baseline-improved { border-color:rgba(74,222,128,.5); background:rgba(20,83,45,.22); }
.baseline-regressed { border-color:rgba(248,113,113,.55); background:rgba(127,29,29,.22); }
.baseline-neutral { border-color:var(--line); }
html[data-theme="light"] .baseline-improved { background:#dcfce7; border-color:#86efac; }
html[data-theme="light"] .baseline-regressed { background:#fee2e2; border-color:#fca5a5; }
.target-cell code { font-weight:700; }
.target-reason { margin-top:4px; max-width:260px; }
.suggestions-table { min-width:1100px; }
.footer-note { color:var(--muted); margin-top:24px; font-size:13px; }
button { background:var(--accent); color:var(--bg); border:1px solid var(--accent); border-radius:9px; padding:9px 12px; font-weight:700; cursor:pointer; }
button.secondary { background:var(--panel2); color:var(--text); border-color:var(--line); }
button.tiny { padding:3px 7px; border-radius:7px; font-size:11px; font-weight:600; }
button:hover { filter:brightness(1.05); }
.row-meta { display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-top:6px; }
.subgroup-badge { display:inline-block; padding:2px 6px; border-radius:999px; border:1px solid var(--line); background:var(--panel2); font-size:11px; }
.scope-excluded { opacity:.72; }
.scope-excluded td { background-image:linear-gradient(135deg, transparent 0 46%, rgba(127,127,127,.05) 46% 54%, transparent 54% 100%); background-size:10px 10px; }
.scope-excluded-badge { border-color:#b6842d; color:#d8a848; }
.lag-line { display:grid; grid-template-columns:42px minmax(90px, auto); gap:5px; margin-bottom:4px; align-items:center; }
.lag-line span { color:var(--muted); font-size:11px; }
.lag-policy-active { border-left:3px solid var(--accent); padding-left:5px; }
.release-cell { min-width:230px; max-width:360px; }
.release-breaking-confirmed strong { color:var(--bad); }
.release-breaking-likely strong, .release-coverage-incomplete strong { color:var(--warn); }
.release-no-breaking-found strong { color:var(--ok); }
.form-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }
.form-grid label { display:block; color:var(--muted); font-size:12px; }
.form-grid label > span { display:block; margin-bottom:6px; }
.detail-list { margin:8px 0 16px; padding-left:22px; }
.detail-list li { margin:5px 0; }
.history-table { min-width:900px; }
.history-view-table { min-width:1500px; }
.history-positive { color:var(--ok); }
.history-negative { color:var(--bad); }
.snapshot-summary { display:flex; flex-wrap:wrap; gap:10px; margin:12px 0; }
.snapshot-summary .badge { padding:7px 10px; }
.snapshot-source-browser { color:var(--warn); }
.snapshot-source-file { color:var(--accent); }
.snapshot-section { margin-top:18px; }
.snapshot-empty { padding:18px; border:1px dashed var(--line); border-radius:10px; }
.prompt-modal { position:fixed; inset:0; background:rgba(0,0,0,.55); display:none; align-items:center; justify-content:center; padding:22px; z-index:999; }
.prompt-modal.open { display:flex; }
.prompt-dialog { width:min(1100px, 96vw); max-height:92vh; overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:16px; box-shadow:0 24px 80px rgba(0,0,0,.35); }
.prompt-dialog header { position:sticky; top:0; padding:16px 18px; background:var(--panel); border-bottom:1px solid var(--line); }
.prompt-dialog h2 { margin:0 0 6px; font-size:20px; }
.prompt-body { padding:16px 18px 18px; }
.prompt-actions { display:flex; gap:10px; flex-wrap:wrap; margin:10px 0; }
#promptText { width:100%; min-height:55vh; resize:vertical; background:var(--input-bg); color:var(--text); border:1px solid var(--line); border-radius:12px; padding:12px; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:13px; line-height:1.45; }
.toast { position:fixed; right:18px; bottom:18px; background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:999px; padding:10px 14px; box-shadow:0 10px 30px var(--shadow); display:none; z-index:1000; }
.toast.open { display:block; }
@media print { header { position:static; } .controls, .prompt-modal, .toast { display:none; } body { background:white; color:black; } details { border:1px solid #ccc; } table { background:white; } th { position:static; background:#eee; color:black; } code { background:#eee; color:black; } }
</style>
</head>
<body>
<header>
  <h1>Live dependency roadmap</h1>
  <div class="muted">Дата генерации: <code>""" + html_escape(now) + """</code>. Источники: package.json (direct versions), project lockfile (install consistency), isolated package-lock/Nexus audit, npm registry, OSV.dev.</div>
  <div class="muted">Registry: <code>""" + html_escape(registry or "—") + """</code>.</div>
  <div class="controls">
    <div class="control"><label>Поиск</label><input id="q" type="text" placeholder="react-router-dom, C:1, oidc, stylelint…" /></div>
    <div class="control"><label>Проект</label><select id="projectFilter"><option value="">Все проекты</option>""")
        for project in all_projects:
            f.write(f"<option value='{html_escape(project)}'>{html_escape(project)}</option>")
        f.write("""</select></div>
    <div class="control"><label>Группа</label><select id="groupFilter"><option value="">Все группы</option>""")
        for group in display_group_ids:
            f.write(f"<option value='{group}'>Группа {group}</option>")
        f.write("""</select></div>
    <div class="control"><label>Секция зависимости</label><select id="kindFilter"><option value="">Все</option><option value="runtime">dependencies</option><option value="dev">devDependencies</option><option value="optional">optionalDependencies</option><option value="peer">peerDependencies</option></select></div>
    <div class="control"><label>Цель</label><select id="targetMode"><option value="default">По умолчанию: текущий уровень +1</option><option value="yellow">До жёлтого</option><option value="green">До зелёного</option></select></div>
    <div class="control"><label>Статус проекта</label><select id="statusFilter"><option value="">Все</option><option value="red">Красный</option><option value="yellow">Жёлтый</option><option value="green">Зелёный</option></select></div>
    <div class="control"><label>Scope промпта</label><select id="promptScope"><option value="project-target">Все строки выбранного проекта/группы (update + deferred)</option><option value="visible">Только видимые строки (частичный scope)</option></select></div>
    <div class="control"><label>Формат промпта</label><select id="promptFormat"><option value="compact">Компактный — для моделей с малым контекстом</option><option value="full">Полный — диагностический</option></select></div>
    <div class="control"><label>Тема</label><select id="themeSelect"><option value="dark">Тёмная</option><option value="light">Светлая</option><option value="system">Как в системе</option></select></div>
  </div>
  <div class="checks">
    <label><input type="checkbox" id="onlyVuln"> только с vuln</label>
    <label><input type="checkbox" id="onlyCH"> только C/H</label>
    <label><input type="checkbox" id="onlyResidual"> только остаточный dependency risk</label>
    <label><input type="checkbox" id="onlyTargetRows"> только что обновлять</label>
    <label><input type="checkbox" id="hideSatisfiedProjects" checked> скрывать проекты, уже достигшие цели</label>
    <label><input type="checkbox" id="expandAllDetails"> развернуть всё</label>
    <label title="Добавляет в агентский промпт явную политику комментариев: пояснять причины только в сложных изменённых местах и конфигурациях."><input type="checkbox" id="detailedCodeComments"> подробно комментировать сложные изменения/конфиги</label>
    <button type="button" id="exportPromptBtn">Выгрузить промпт</button>
    <span class="group-prompt-export" title="Промпт только на одну ветку Branch plan: для оркестратора, который сам создаёт/мержит ветки и запускает короткую отдельную сессию агента на каждую группу вместо одной растущей на весь прогон."><select id="groupPromptBranch"></select><button type="button" class="secondary" id="exportGroupPromptBtn">Промпт для ветки</button></span>
    <button type="button" class="secondary" id="exportTaskSpecBtn">Выгрузить ТЗ</button>
    <button type="button" class="secondary" id="exportPackagePatchBtn">Выгрузить package.json и предложения</button>
    <button type="button" class="secondary" id="saveSnapshotBtn">Сохранить снимок дашборда</button>
    <button type="button" class="secondary" id="historyBtn">История / сравнение</button>
    <button type="button" class="secondary" id="exportStateBtn">Сохранить настройки dashboard</button>
    <button type="button" class="secondary" id="importStateBtn">Импортировать настройки</button>
    <input type="file" id="importStateFile" accept="application/json,.json" hidden />
    <span class="badge" id="selectedRowsCounter">Выбрано: 0</span>
    <button type="button" id="excludeSelectedBtn" disabled>Исключить выбранные</button>
    <button type="button" class="secondary" id="includeSelectedBtn" disabled>Вернуть выбранные</button>
    <button type="button" class="secondary" id="clearSelectionBtn" disabled>Снять выбор</button>
    <span class="badge" id="visibleCounter"></span>
  </div>
  <div class="checks">
    <span class="muted">Колонки:</span>
    <label><input type="checkbox" data-col="latest" checked> latest</label>
    <label><input type="checkbox" data-col="min" checked> min vuln</label>
    <label><input type="checkbox" data-col="lag" checked> lag</label>
    <label><input type="checkbox" data-col="notes" checked> примечания</label>
  </div>
  <div class="muted" style="margin-top:8px">Колонка <code>До какой обновить</code> пересчитывается в браузере по режиму цели: до жёлтого, до зелёного или текущий уровень проекта +1. Все секции и группы по умолчанию свернуты; фильтры не сбрасывают текущую развернутость. Внутри проекта можно раскрыть все группы сразу. Заголовки таблиц и первая колонка закреплены.</div>
  <div class="muted" style="margin-top:6px">По умолчанию промпт берёт все строки выбранного проекта и группы, включая update и deferred, и формируется в компактном режиме. Чекбокс подробных комментариев управляет только агентским промптом: при включении агенту разрешено добавлять точечные комментарии «почему» в сложных изменённых местах/конфигах; при выключении промпт требует минимальных комментариев без миграционного дневника. «Выгрузить ТЗ» создаёт человеко-читаемое описание задачи без агентского протокола. Снимок дашборда сохраняет весь текущий roadmap, настройки и выбранный target mode в браузерную историю; JSON можно скачать или импортировать. Автоматические файловые снимки: <code>""" + html_escape(history_dir) + """/snapshots</code>.</div>
</header>
<div class="container">
  <div class="cards">
    <div class="card"><span class="muted">Учитываемых зависимостей</span><strong id="totalDependenciesValue">""" + str(len(active_all_rows)) + """</strong></div>
    <div class="card"><span class="muted">Проектов</span><strong>""" + str(len(all_projects)) + """</strong></div>
    <div class="card"><span class="muted">Статусы R/Y/G</span><strong id="statusCountsValue">""" + html_escape(f"{status_counts['red']} / {status_counts['yellow']} / {status_counts['green']}") + """</strong></div>
    """ + "".join(
        f'<div class="card"><span class="muted">Группа {group}</span><strong data-group-count="{group}">{total_counts.get(group, 0)}</strong></div>'
        for group in display_group_ids
    ) + """
    <div class="card"><span class="muted">C/H/M/L/U</span><strong id="vulnerabilityTotalsValue">""" + html_escape(" / ".join(str(vuln_total[k]) for k in ("C", "H", "M", "L", "U"))) + """</strong></div>
  </div>

  <details>
    <summary>Принцип распределения по группам</summary>
    <p>Группа — это очередь и стратегия работы по сочетанию важности, сложности и runtime/API/CI/security-риска. Скрипт не должен решать только по <code>deprecated</code>, semver или <code>devDependencies</code>.</p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Группа</th><th>Когда сюда относить</th><th>Что делать</th></tr></thead>
        <tbody>
          <tr><td>1</td><td>Срочно и несложно: быстро снять Critical/High, обновить до безопасной версии или выпилить простое использование.</td><td>Маленький security/removal MR + OSV/outdated diff; manual audit только по запросу.</td></tr>
          <tr><td>2</td><td>Нужные и относительно простые runtime/API/CI обновления: compatible replacement, import-only fork, minor/small-major, CI/tooling update с малым diff.</td><td>Обычный MR или небольшой batch + локальные проверки/targeted smoke.</td></tr>
          <tr><td>3</td><td>Runtime/API/CI изменение, которое не проходит критерий “несложное и относительно простое”: риск поведения, неясный changelog, связанный набор пакетов.</td><td>Отдельный MR/подгруппа + профильный smoke/regression.</td></tr>
          <tr><td>4</td><td>Сложно, platform или blocked: stylelint/Vite/TS/UI/auth/shared/published widget/latest-vulnerable/upstream blocker.</td><td>Отдельная задача, владелец, план миграции, risk register при необходимости.</td></tr>
          <tr><td>5</td><td>Остальное: lag без срочности, настоящий DEV/local/test/storybook/mkcert без влияния на runtime/CI/release.</td><td>Квартальный batch или отложить; остаточные vuln явно фиксировать.</td></tr>
        </tbody>
      </table>
    </div>
  </details>

  <details>
    <summary>Suggestions: предлагаемые подветки / batch-MR</summary>
    <h3>Cross-project suggestions</h3>
    <div class="table-wrap">""")
        f.write(render_suggestions_html(global_suggestions))
        f.write("""</div>
  </details>
""")

        f.write("<details><summary>Package migration knowledge</summary>")
        f.write(
            f"<p class='muted'>Source: <code>{html_escape(knowledge_log_path or 'not configured')}</code>. "
            "Treat entries as diagnostic guidance: verify applicability and reproduce the listed proof before use.</p>"
        )
        f.write(render_knowledge_html())
        f.write("</details>")

        for project in all_projects:
            rows = rows_by_project[project]
            h = health_by_project[project]
            cmp = baseline_comparisons.get(project)
            cmp_class = "baseline-improved" if cmp and cmp.get("improved") else ("baseline-regressed" if cmp and cmp.get("regressed") else "baseline-neutral")
            f.write(f"<details class='project-section' data-project-section='{html_escape(project)}' data-project-status='{html_escape(h.status)}'><summary>{html_escape(project)} <span class='status-badge status-{html_escape(h.status)}'>{html_escape(STATUS_NAMES[h.status])}</span> <span class='badge project-count'></span><div class='muted project-health-summary'>Lag OK: {h.lag_ok_pct:.1f}% ({h.lag_ok_12m}/{h.total}), C/H/M/L: {h.critical}/{h.high}/{h.moderate}/{h.low}. {html_escape(h.reason)}</div></summary>\n")
            f.write(f"<div class='baseline-banner {cmp_class}'><strong>Baseline:</strong> {html_escape(comparison_badge_text(cmp))}. {html_escape(comparison_note(cmp))}</div>\n")
            audit_info = (project_specs.get(project).current_audit if project_specs.get(project) else {}) or {}
            audit_totals = audit_info.get("totals") or {}
            if audit_info.get("mode") == "manual-only":
                canonical_lock = (project_specs.get(project).lockfile_state or {}).get("lockfile", "project lockfile")
                f.write(
                    "<div class='baseline-banner baseline-neutral'><strong>Vulnerability audit:</strong> "
                    f"manual-only. Dashboard uses {html_escape(Path(str(canonical_lock)).name)}. "
                    "Run bundled <code>manual_dependency_audit.py</code> (or the Desktop Manual audit action) for an explicit Nexus reconciliation.</div>\n"
                )
            elif audit_info:
                audit_complete = bool(audit_info.get("complete"))
                audit_class = "baseline-improved" if audit_complete else "baseline-regressed"
                f.write(
                    f"<div class='baseline-banner {audit_class}'><strong>Manual vulnerability audit:</strong> "
                    f"engine={html_escape(audit_info.get('engine') or 'unknown')}, "
                    f"registry={html_escape(audit_info.get('effectiveRegistry') or audit_info.get('requestedRegistry') or 'unknown')}, "
                    f"C/H/M/L={html_escape(audit_totals.get('critical', '?'))}/{html_escape(audit_totals.get('high', '?'))}/"
                    f"{html_escape(audit_totals.get('moderate', '?'))}/{html_escape(audit_totals.get('low', '?'))}, "
                    f"complete={str(audit_complete).lower()}.</div>\n"
                )
            project_suggestions = suggestions_by_project.get(project, [])
            f.write("<div class='project-actions'><label><input type='checkbox' class='project-expand-groups'> развернуть группы этого проекта</label><span class='muted'>Группы и project suggestions свернуты по умолчанию.</span></div>\n")
            project_suggestions = suggestions_by_project.get(project, [])
            if project_suggestions:
                f.write("<details class='project-suggestions-section'><summary>Suggested subbranches for this project</summary><div class='table-wrap'>")
                f.write(render_suggestions_html(project_suggestions))
                f.write("</div></details>\n")
            for group in group_ids_for_rows(rows):
                sub = [r for r in rows if r.group == group]
                if not sub:
                    continue
                f.write(f"<details class='group-section' data-group-section='{group}'><summary class='group-title'>{html_escape(group_display_name(group))} <span class='badge group-count'></span></summary>\n")
                f.write("<div class='table-wrap'>")
                f.write(render_dependency_table(sub))
                f.write("</div>\n")
                f.write("</details>\n")
            f.write("</details>\n")

        f.write("""
  <p class="footer-note">Dashboard использует OSV и registry metadata. Ручной Nexus/npm audit запускается пользователем отдельно как независимая сверка транзитивного дерева; generate его не запускает.</p>
</div>
<div class="prompt-modal" id="promptModal" aria-hidden="true">
  <div class="prompt-dialog" role="dialog" aria-modal="true" aria-labelledby="promptModalTitle">
    <header>
      <h2 id="promptModalTitle">Промпт для агента</h2>
      <div class="muted" id="promptDescription">Сформирован по выбранному scope и цели. Компактный режим хранит точный manifest в коротком формате и заставляет агента читать тяжёлые данные из файлов только по мере необходимости.</div>
      <div class="muted" id="promptMeta"></div>
    </header>
    <div class="prompt-body">
      <div class="prompt-actions">
        <button type="button" id="copyPromptBtn">Скопировать</button>
        <button type="button" id="downloadPromptBtn" class="secondary">Скачать .md</button>
        <button type="button" id="closePromptBtn" class="secondary">Закрыть</button>
      </div>
      <textarea id="promptText" spellcheck="false"></textarea>
    </div>
  </div>
</div>
<div class="prompt-modal" id="releaseModal" aria-hidden="true">
  <div class="prompt-dialog" role="dialog" aria-modal="true">
    <header><h2 id="releaseModalTitle">Release notes</h2><div class="muted" id="releaseModalSummary"></div></header>
    <div class="prompt-body" id="releaseModalBody"></div>
  </div>
</div>
<div class="prompt-modal" id="settingsModal" aria-hidden="true">
  <div class="prompt-dialog" role="dialog" aria-modal="true">
    <header><h2>Настройка зависимости</h2><div class="muted" id="settingsIdentity"></div></header>
    <div class="prompt-body">
      <div class="form-grid">
        <label><span>Группа</span><input id="settingsGroup" type="number" min="1" step="1" inputmode="numeric"></label>
        <label><span>Подгруппа</span><input type="text" id="settingsSubgroup" placeholder="например team-maintenance" /></label>
        <label><span>Допустимый lag</span><select id="settingsLag"><option value="12">12 месяцев</option><option value="9">9 месяцев</option><option value="6">6 месяцев</option><option value="3">3 месяца</option></select></label>
      </div>
      <label style="display:flex;gap:8px;align-items:center;margin-top:12px"><input type="checkbox" id="settingsExcluded" /> <span>Полностью не учитывать зависимость в текущем расчёте и плане</span></label>
      <label style="display:block;margin-top:12px"><span class="muted">Причина исключения (обязательна)</span><textarea id="settingsExclusionReason" rows="3" placeholder="Например: нужен backend change; заблокировано совместимостью; отдельная продуктовая миграция"></textarea></label>
      <label style="display:block;margin-top:12px"><span class="muted">Причина / заметка команды</span><textarea id="settingsNote" rows="4" placeholder="Почему переопределили группу или lag-policy"></textarea></label>
      <div class="prompt-actions">
        <button type="button" id="saveRowSettingsBtn">Применить</button>
        <button type="button" class="secondary" id="applyVisibleSettingsBtn">Применить к видимым строкам</button>
        <button type="button" class="secondary" id="clearRowSettingsBtn">Сбросить override</button>
        <button type="button" class="secondary" id="closeSettingsBtn">Закрыть</button>
      </div>
      <div class="muted">Исключённая строка остаётся видимой с пометкой и попадает в manifest как <code>action=excluded</code>, но не входит в проценты, числа уязвимостей, цвет проекта, target и план веток. Для репозитория сохраните <code>dashboard-state.json</code> кнопкой в шапке.</div>
    </div>
  </div>
</div>
<div class="prompt-modal" id="historyModal" aria-hidden="true">
  <div class="prompt-dialog" role="dialog" aria-modal="true">
    <header><h2>История дашборда</h2><div class="muted">Полный просмотр снимка, сравнение любых двух снимков и перенос JSON между машинами. Файловые снимки встраиваются при generate; снимки кнопкой хранятся локально в браузере.</div></header>
    <div class="prompt-body">
      <div class="prompt-actions">
        <button type="button" id="saveSnapshotFromHistoryBtn">Сохранить текущий снимок</button>
        <button type="button" class="secondary" id="exportSelectedSnapshotBtn">Скачать выбранный JSON</button>
        <button type="button" class="secondary" id="importSnapshotBtn">Импортировать JSON</button>
        <button type="button" class="secondary" id="deleteSelectedSnapshotBtn">Удалить локальный снимок</button>
        <button type="button" class="secondary" id="closeHistoryBtn">Закрыть</button>
        <input type="file" id="importSnapshotFile" accept="application/json,.json" hidden />
      </div>
      <div id="historyModalBody"></div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const REPORT_CONTEXT = """ + json_for_script(report_context) + """;
const THEME_STORAGE_KEY = 'dependency-roadmap-theme';
function resolveTheme(mode){
  if (mode === 'system') {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  return mode === 'light' ? 'light' : 'dark';
}
function setTheme(mode){
  const normalized = ['dark','light','system'].includes(mode) ? mode : 'dark';
  const effective = resolveTheme(normalized);
  document.documentElement.dataset.theme = effective;
  document.documentElement.dataset.themeMode = normalized;
  try { localStorage.setItem(THEME_STORAGE_KEY, normalized); } catch(e) {}
  const select = document.getElementById('themeSelect');
  if (select && select.value !== normalized) select.value = normalized;
}
function initThemeControl(){
  let saved = 'dark';
  try { saved = localStorage.getItem(THEME_STORAGE_KEY) || document.documentElement.dataset.themeMode || 'dark'; } catch(e) {}
  const select = document.getElementById('themeSelect');
  if (select) {
    select.value = ['dark','light','system'].includes(saved) ? saved : 'dark';
    select.addEventListener('change', () => setTheme(select.value));
  }
  setTheme(saved);
  if (window.matchMedia) {
    const media = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = () => {
      let mode = 'dark';
      try { mode = localStorage.getItem(THEME_STORAGE_KEY) || 'dark'; } catch(e) {}
      if (mode === 'system') setTheme('system');
    };
    if (media.addEventListener) media.addEventListener('change', onChange);
    else if (media.addListener) media.addListener(onChange);
  }
}
function norm(s){ return (s || '').toString().toLowerCase(); }
function projectSatisfiesGoal(status, mode){
  if (mode === 'default') return status === 'green';
  if (mode === 'yellow') return status === 'yellow' || status === 'green';
  if (mode === 'green') return status === 'green';
  return false;
}
function semverParts(value){
  const match = String(value || '').match(/(\\d+)\\.(\\d+)\\.(\\d+)(?:[-+][0-9A-Za-z.-]+)?/);
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
}
function compareSemverText(a,b){
  const av = semverParts(a), bv = semverParts(b);
  if (!av || !bv) return 0;
  for (let i=0;i<3;i++) { if (av[i] !== bv[i]) return av[i] < bv[i] ? -1 : 1; }
  return 0;
}
function maxSemverText(a,b){
  if (!semverParts(a)) return b;
  if (!semverParts(b)) return a;
  return compareSemverText(a,b) >= 0 ? a : b;
}
function lagComplianceTargetForDomRow(row, explicitMonths){
  const months = Number(explicitMonths || row.dataset.lagThresholdMonths || 12);
  return row.getAttribute(`data-min-lag-${months}m`) ||
    row.dataset[`minLag-${months}m`] ||
    row.dataset[`minLagM${months}`] ||
    '—';
}
function lagTargetForDomRow(row, explicitMonths){
  const months = Number(explicitMonths || row.dataset.lagThresholdMonths || 12);
  // data-*-3m is exposed by DOMStringMap as the awkward key
  // \"minLag-3m\" (a dash before a digit is not camel-cased). Reading the
  // attribute directly keeps dashboard cells, prompt export and saved scope in
  // sync for every 3/6/9/12-month policy.
  return row.getAttribute(`data-planning-min-lag-${months}m`) ||
    row.getAttribute(`data-min-lag-${months}m`) ||
    row.dataset[`minLag-${months}m`] ||
    row.dataset[`minLagM${months}`] ||
    '—';
}
function lagUpdateTargetForDomRow(row, explicitMonths){
  const months = Number(explicitMonths || row.dataset.lagThresholdMonths || 12);
  const bufferedMonths = Math.max(3, months - 3);
  return lagTargetForDomRow(row, bufferedMonths);
}
function targetForRow(row, mode){
  if (row.dataset.scopeExcluded === '1') {
    return {
      value: '—',
      reason: `исключено из текущего scope: ${row.dataset.exclusionReason || 'причина не указана'}`,
      has: false
    };
  }
  let result;
  if (mode === 'yellow') {
    result = {
      value: row.dataset.targetYellow || '—',
      reason: row.dataset.targetYellowReason || '—',
      nonLagValue: row.dataset.targetYellowNonLag || '—',
      nonLagReason: row.dataset.targetYellowNonLagReason || '—',
      lagSelected: row.dataset.targetYellowHasLag === '1',
      dynamicLocked: row.dataset.targetYellowDynamicLocked === '1'
    };
  } else if (mode === 'green') {
    result = {
      value: row.dataset.targetGreen || '—',
      reason: row.dataset.targetGreenReason || '—',
      nonLagValue: row.dataset.targetGreenNonLag || '—',
      nonLagReason: row.dataset.targetGreenNonLagReason || '—',
      lagSelected: row.dataset.targetGreenHasLag === '1',
      dynamicLocked: row.dataset.targetGreenDynamicLocked === '1'
    };
  } else {
    result = {
      value: row.dataset.targetDefault || '—',
      reason: row.dataset.targetDefaultReason || '—',
      nonLagValue: row.dataset.targetDefaultNonLag || '—',
      nonLagReason: row.dataset.targetDefaultNonLagReason || '—',
      lagSelected: row.dataset.targetDefaultHasLag === '1',
      dynamicLocked: row.dataset.targetDefaultDynamicLocked === '1'
    };
  }
  if (result.dynamicLocked || (typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('desktop-export'))) {
    result.has = !!semverParts(result.value);
    return result;
  }
  const lagMonths = Number(row.dataset.lagThresholdMonths || 12);
  const lagComplianceTarget = lagComplianceTargetForDomRow(row, lagMonths);
  const lagUpdateTarget = lagUpdateTargetForDomRow(row, lagMonths);
  const lagNeedsUpdate = semverParts(lagComplianceTarget) &&
    compareSemverText(row.dataset.current || '0.0.0', lagComplianceTarget) < 0;
  const includeLag = lagNeedsUpdate && (result.lagSelected || lagMonths < 12);
  result.value = result.nonLagValue;
  result.reason = result.nonLagReason;
  if (includeLag) {
    result.value = maxSemverText(result.value, lagUpdateTarget);
    result.reason = [
      result.reason,
      `lag-policy ≤${row.dataset.lagThresholdMonths || 12}м; baseline target с запасом 3м`
    ].filter(x => x && x !== '—').join('; ');
  }
  result.has = !!semverParts(result.value);
  if (!result.has && (!result.reason || result.reason === '—')) result.reason = '—';
  return result;
}
function releaseForTarget(row, target){
  let payload = {};
  try { payload = JSON.parse(row.dataset.releaseJson || '{}'); } catch(e) {}
  const exact = payload.byTarget && payload.byTarget[target];
  if (exact) return {...exact, package:payload.package, current:payload.current};
  if (!semverParts(target)) {
    return {package:payload.package, current:payload.current, target, status:'not-applicable', summary:'Для выбранного режима нет target-обновления', coverage:'', breakingChanges:[], migrationNotes:[], deprecations:[], requirements:[], sources:[]};
  }
  return {package:payload.package, current:payload.current, target, status:'not-checked-target', summary:'Release notes для точного target не анализировались. Перегенерируйте отчёт после сохранения lag/group state.', coverage:'не проверено для выбранного target', breakingChanges:[], migrationNotes:[], deprecations:[], requirements:[], sources:[]};
}
function registryArtifactForTarget(row, target){
  let payload = {};
  try { payload = JSON.parse(row.dataset.registryArtifactsJson || '{}'); } catch(e) {}
  if (payload && payload[target]) return payload[target];
  if (!semverParts(target)) return {status:'not-applicable', version:target, tarballUrl:'', error:''};
  return {status:'not-proven', version:target, tarballUrl:'', error:'target has no configured-registry artifact evidence'};
}
function releaseStatusLabel(status){
  return ({
    'breaking-confirmed':'Breaking найдены',
    'breaking-likely':'Breaking вероятны',
    'coverage-incomplete':'Диапазон покрыт частично',
    'no-breaking-found':'Явных breaking нет',
    'unavailable':'Не проверено',
    'not-checked-limit':'Не проверено (лимит)',
    'not-checked-target':'Не проверен target',
    'disabled':'Отключено',
    'not-applicable':'Нет target'
  })[status] || status || 'Не проверено';
}
function updateReleaseCells(){
  const mode = document.getElementById('targetMode').value;
  document.querySelectorAll('tr.dep-row').forEach(row => {
    const target = targetForRow(row, mode).value;
    const intel = releaseForTarget(row, target);
    row.dataset.releaseStatus = intel.status || 'not-checked-target';
    row.dataset.releaseSummary = intel.summary || '—';
    const cell = row.querySelector('.release-cell');
    if (!cell) return;
    Array.from(cell.classList).filter(name => name !== 'release-cell' && name.startsWith('release-')).forEach(name => cell.classList.remove(name));
    cell.classList.add('release-' + (intel.status || 'not-checked-target'));
    const label = cell.querySelector('strong');
    if (label) label.textContent = releaseStatusLabel(intel.status);
    const summary = cell.querySelector('.muted');
    if (summary) summary.textContent = intel.summary || '—';
  });
}
function updateTargetCells(){
  const mode = document.getElementById('targetMode').value;
  document.querySelectorAll('tr.dep-row').forEach(row => {
    const t = targetForRow(row, mode);
    const cell = row.querySelector('.target-cell');
    if (cell) {
      cell.querySelector('code').textContent = t.value || '—';
      const reason = cell.querySelector('.target-reason');
      if (reason) reason.textContent = t.reason || '—';
      const artifact = registryArtifactForTarget(row, t.value);
      const artifactLine = cell.querySelector('.target-artifact');
      if (artifactLine) artifactLine.textContent = 'registry artifact: ' + (artifact.status || 'not-proven');
    }
  });
}
function vulnerabilityCountsForDomRow(row){
  const result = {C:0,H:0,M:0,L:0,U:0};
  const text = String(row.dataset.vulns || '');
  for (const match of text.matchAll(/(C|H|M|L|U):(\\d+)/g)) result[match[1]] += Number(match[2]);
  return result;
}
function calculateProjectHealthFromDom(section){
  const project = section.dataset.projectSection || '';
  const rows = Array.from(section.querySelectorAll('tr.dep-row'));
  const activeRows = rows.filter(row => row.dataset.scopeExcluded !== '1');
  const excluded = rows.length - activeRows.length;
  const knownRows = activeRows.filter(row => !!semverParts(lagComplianceTargetForDomRow(row)));
  const lagUnknown = activeRows.length - knownRows.length;
  const removed = Number(REPORT_CONTEXT.projectHealth?.[project]?.removed || 0);
  const total = knownRows.length + removed;
  const lagOk = knownRows.filter(row => compareSemverText(row.dataset.current || '', lagComplianceTargetForDomRow(row)) >= 0).length + removed;
  const lagBad = total - lagOk;
  const lagPct = total ? lagOk / total * 100 : 100;
  const targetMode = document.getElementById('targetMode')?.value || 'default';
  const projectedLagOk = knownRows.filter(row => {
    if (compareSemverText(row.dataset.current || '', lagComplianceTargetForDomRow(row)) >= 0) return true;
    const target = targetForRow(row, targetMode).value;
    return isActionTargetValue(target) && compareSemverText(target, lagComplianceTargetForDomRow(row)) >= 0;
  }).length + removed;
  const projectedLagPct = total ? projectedLagOk / total * 100 : 100;
  const yellowPlanRequired = total ? Math.ceil(total * 0.85) : 0;
  const yellowPlanShortfall = Math.max(0, yellowPlanRequired - projectedLagOk);
  const totals = {C:0,H:0,M:0,L:0,U:0};
  for (const row of activeRows) {
    const counts = vulnerabilityCountsForDomRow(row);
    for (const key of Object.keys(totals)) totals[key] += counts[key] || 0;
  }
  let status = 'yellow';
  let reason = '';
  if (totals.C > 0) {
    status = 'red'; reason = `есть Critical: ${totals.C}`;
  } else if (total === 0) {
    status = lagUnknown ? 'yellow' : 'green';
    reason = lagUnknown ? `lag-policy target неизвестен для ${lagUnknown} зависимостей` : 'нет зависимостей в активном расчёте';
  } else if (lagPct < 80) {
    status = 'red'; reason = `только ${lagPct.toFixed(1)}% библиотек соблюдают свою lag-policy (<80%)`;
  } else if (lagBad === 0 && lagUnknown === 0 && totals.H === 0 && (totals.M + totals.L) <= 20) {
    status = 'green'; reason = '0 нарушений lag-policy, 0 C/H, Low+Moderate ≤20';
  } else {
    status = 'yellow';
    const parts = [`${lagPct.toFixed(1)}% библиотек соблюдают lag-policy`, '0 Critical'];
    if (lagUnknown) parts.push(`lag-policy target неизвестен: ${lagUnknown}`);
    if (totals.H) parts.push(`High остаются: ${totals.H}`);
    if (totals.M || totals.L) parts.push(`M/L: ${totals.M + totals.L}`);
    reason = parts.join('; ');
  }
  if (excluded) reason += `${reason ? '; ' : ''}не учитывается: ${excluded}`;
  return {project,status,total,lag_ok_12m:lagOk,lag_bad_12m:lagBad,lag_ok_pct:lagPct,
    critical:totals.C,high:totals.H,moderate:totals.M,low:totals.L,unknown:totals.U,
    reason,excluded,lag_unknown:lagUnknown,removed,
    yellow_plan_required:yellowPlanRequired,yellow_projected_lag_ok:projectedLagOk,
    yellow_projected_lag_pct:projectedLagPct,yellow_plan_shortfall:yellowPlanShortfall};
}
function updateProjectHealthVisual(section, health){
  section.dataset.projectStatus = health.status;
  const badge = section.querySelector('summary .status-badge');
  if (badge) {
    badge.classList.remove('status-red','status-yellow','status-green');
    badge.classList.add('status-' + health.status);
    badge.textContent = ({red:'Красный',yellow:'Жёлтый',green:'Зелёный'})[health.status] || health.status;
  }
  const summary = section.querySelector('summary .project-health-summary');
  if (summary) summary.textContent = `Lag OK: ${health.lag_ok_pct.toFixed(1)}% (${health.lag_ok_12m}/${health.total}), ` +
    `C/H/M/L: ${health.critical}/${health.high}/${health.moderate}/${health.low}. ${health.reason}`;
}
function recalculateDashboardHealth(){
  liveProjectHealth = {};
  document.querySelectorAll('.project-section').forEach(section => {
    const health = calculateProjectHealthFromDom(section);
    liveProjectHealth[health.project] = health;
    updateProjectHealthVisual(section, health);
  });
  const activeRows = Array.from(document.querySelectorAll('tr.dep-row')).filter(row => row.dataset.scopeExcluded !== '1');
  const statusCounts = {red:0,yellow:0,green:0};
  Object.values(liveProjectHealth).forEach(health => { statusCounts[health.status] = (statusCounts[health.status] || 0) + 1; });
  const vulnerabilities = {C:0,H:0,M:0,L:0,U:0};
  for (const row of activeRows) {
    const counts = vulnerabilityCountsForDomRow(row);
    for (const key of Object.keys(vulnerabilities)) vulnerabilities[key] += counts[key] || 0;
  }
  const setText = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = String(value); };
  setText('totalDependenciesValue', activeRows.length);
  setText('statusCountsValue', `${statusCounts.red} / ${statusCounts.yellow} / ${statusCounts.green}`);
  document.querySelectorAll('[data-group-count]').forEach(node => {
    const group = node.dataset.groupCount || '';
    node.textContent = String(activeRows.filter(row => row.dataset.group === group).length);
  });
  setText('vulnerabilityTotalsValue', ['C','H','M','L','U'].map(key => vulnerabilities[key]).join(' / '));
  updateSelectionControls();
}
function applyFilters(){
  recalculateDashboardHealth();
  updateTargetCells();
  updateReleaseCells();
  const q = norm(document.getElementById('q').value);
  const project = document.getElementById('projectFilter').value;
  const group = document.getElementById('groupFilter').value;
  const kind = document.getElementById('kindFilter').value;
  const status = document.getElementById('statusFilter').value;
  const targetMode = document.getElementById('targetMode').value;
  const onlyVuln = document.getElementById('onlyVuln').checked;
  const onlyCH = document.getElementById('onlyCH').checked;
  const onlyResidual = document.getElementById('onlyResidual').checked;
  const onlyTargetRows = document.getElementById('onlyTargetRows').checked;
  const hideSatisfiedProjects = document.getElementById('hideSatisfiedProjects').checked;
  let visible = 0;
  let visibleActive = 0;
  document.querySelectorAll('tr.dep-row').forEach(row => {
    const t = targetForRow(row, targetMode);
    let ok = true;
    if (q && !norm(row.dataset.text + ' ' + t.value + ' ' + t.reason).includes(q)) ok = false;
    if (project && row.dataset.project !== project) ok = false;
    if (group && row.dataset.group !== group) ok = false;
    if (kind && row.dataset.kind !== kind) ok = false;
    if (status) {
      const section = row.closest('.project-section');
      if (!section || section.dataset.projectStatus !== status) ok = false;
    }
    if (onlyVuln && Number(row.dataset.anyVuln || 0) <= 0) ok = false;
    if (onlyCH && Number(row.dataset.ch || 0) <= 0) ok = false;
    if (onlyResidual && row.dataset.residual !== '1') ok = false;
    if (onlyTargetRows && !t.has) ok = false;
    row.classList.toggle('row-hidden', !ok);
    if (ok) {
      visible++;
      if (row.dataset.scopeExcluded !== '1') visibleActive++;
    }
  });
  const healthValues = Object.values(liveProjectHealth);
  const projection = project ? liveProjectHealth[project] : (healthValues.length === 1 ? healthValues[0] : null);
  const projectionText = onlyTargetRows && projection
    ? ` · прогноз проекта: ${projection.yellow_projected_lag_pct.toFixed(1)}% (${projection.yellow_projected_lag_ok}/${projection.total})` +
      (projection.yellow_plan_shortfall ? ` · до запаса 85% не хватает ${projection.yellow_plan_shortfall}` : ' · запас 85% соблюдён')
    : '';
  document.getElementById('visibleCounter').textContent = onlyTargetRows
    ? `К обновлению: ${visibleActive}${projectionText}`
    : `Показано: ${visible}; учитывается в health: ${visibleActive}`;
  document.querySelectorAll('.project-section').forEach(section => {
    const rows = Array.from(section.querySelectorAll('tr.dep-row'));
    const visibleRows = rows.filter(r => !r.classList.contains('row-hidden'));
    const count = visibleRows.length;
    const activeCount = visibleRows.filter(r => r.dataset.scopeExcluded !== '1').length;
    const excludedCount = count - activeCount;
    const targetSatisfied = projectSatisfiesGoal(section.dataset.projectStatus, targetMode);
    const shouldHideAsSatisfied = hideSatisfiedProjects && targetSatisfied && !project;
    section.classList.toggle('project-hidden', count === 0 || shouldHideAsSatisfied);
    const badge = section.querySelector('.project-count');
    if (badge) {
      const extra = targetSatisfied ? ' · цель достигнута' : '';
      const excludedText = excludedCount ? ` · не учитывается ${excludedCount}` : '';
      badge.textContent = activeCount + ' учитывается' + excludedText + extra;
    }
    section.querySelectorAll('.group-section').forEach(groupSection => {
      const groupRows = Array.from(groupSection.querySelectorAll('tr.dep-row'));
      const visibleGroupRows = groupRows.filter(r => !r.classList.contains('row-hidden'));
      const groupCount = visibleGroupRows.length;
      const activeGroupCount = visibleGroupRows.filter(r => r.dataset.scopeExcluded !== '1').length;
      const excludedGroupCount = groupCount - activeGroupCount;
      groupSection.classList.toggle('group-hidden', groupCount === 0);
      const groupBadge = groupSection.querySelector('.group-count');
      if (groupBadge) groupBadge.textContent = activeGroupCount + ' учитывается' + (excludedGroupCount ? ` · не учитывается ${excludedGroupCount}` : '');
    });
  });
}
['q','projectFilter','groupFilter','kindFilter','statusFilter','targetMode','onlyVuln','onlyCH','onlyResidual','onlyTargetRows','hideSatisfiedProjects'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('input', applyFilters);
  if (el) el.addEventListener('change', applyFilters);
});
document.querySelectorAll('[data-col]').forEach(cb => {
  cb.addEventListener('change', () => {
    document.body.classList.toggle('hide-col-' + cb.dataset.col, !cb.checked);
  });
});
function versionKey(s){
  const m = (s || '').match(/(\\d+)\\.(\\d+)\\.(\\d+)/);
  if (!m) return -1;
  return Number(m[1])*100000000 + Number(m[2])*10000 + Number(m[3]);
}
function vulnKey(s){
  const text = s || '';
  const c = Number((text.match(/C:(\\d+)/)||[])[1]||0);
  const h = Number((text.match(/H:(\\d+)/)||[])[1]||0);
  const m = Number((text.match(/M:(\\d+)/)||[])[1]||0);
  const l = Number((text.match(/L:(\\d+)/)||[])[1]||0);
  return c*1000000 + h*10000 + m*100 + l;
}
document.querySelectorAll('table.sortable-table th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const table = th.closest('table');
    const tbody = table.querySelector('tbody');
    const idx = Array.from(th.parentNode.children).indexOf(th);
    const type = th.dataset.sort;
    const asc = th.dataset.asc !== '1';
    th.dataset.asc = asc ? '1' : '0';
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a,b) => {
      const av = a.children[idx]?.innerText.trim() || '';
      const bv = b.children[idx]?.innerText.trim() || '';
      let ak = av, bk = bv;
      if (type === 'version') { ak = versionKey(av); bk = versionKey(bv); }
      if (type === 'vuln') { ak = vulnKey(av); bk = vulnKey(bv); }
      if (ak < bk) return asc ? -1 : 1;
      if (ak > bk) return asc ? 1 : -1;
      return 0;
    });
    rows.forEach(r => tbody.appendChild(r));
  });
});

function setProjectGroupsOpen(projectSection, open){
  if (!projectSection) return;
  projectSection.querySelectorAll('.group-section').forEach(details => {
    details.open = open;
  });
}
function syncProjectGroupToggle(projectSection){
  if (!projectSection) return;
  const cb = projectSection.querySelector('.project-expand-groups');
  if (!cb) return;
  const groups = Array.from(projectSection.querySelectorAll('.group-section'));
  cb.checked = groups.length > 0 && groups.every(g => g.open);
}
function syncAllProjectGroupToggles(){
  document.querySelectorAll('.project-section').forEach(syncProjectGroupToggle);
}
function setAllDetailsOpen(open){
  document.querySelectorAll('details').forEach(details => {
    details.open = open;
  });
  syncAllProjectGroupToggles();
}
function initDetailsExpandControl(){
  const cb = document.getElementById('expandAllDetails');
  if (!cb) return;
  cb.checked = false;
  cb.addEventListener('change', () => {
    setAllDetailsOpen(cb.checked);
  });
}
function initProjectGroupToggles(){
  document.querySelectorAll('.project-expand-groups').forEach(cb => {
    cb.checked = false;
    cb.addEventListener('change', () => {
      setProjectGroupsOpen(cb.closest('.project-section'), cb.checked);
    });
  });
  document.querySelectorAll('.group-section').forEach(group => {
    group.addEventListener('toggle', () => syncProjectGroupToggle(group.closest('.project-section')));
  });
}


const DEP_HISTORY_DIR = """ + json.dumps(str(history_dir), ensure_ascii=False) + """;

function showToast(message, durationMs){
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('open');
  window.setTimeout(() => toast.classList.remove('open'), durationMs || 2400);
}
function escapeMdCell(value){
  return (value || '—').toString().replace(/[|]/g, '\\\\|').replace(/\\n/g, '<br>');
}
function detailedCodeCommentsEnabled(){
  return !!document.getElementById('detailedCodeComments')?.checked;
}
function codeCommentPolicy(){
  if (detailedCodeCommentsEnabled()) {
    return {
      mode: 'detailed-why-comments',
      instruction: 'В изменённых сложных местах кода и конфигурации добавляй точечные содержательные комментарии, объясняющие почему изменение необходимо, какое breaking/compatibility ограничение оно компенсирует и какую неочевидную гарантию сохраняет. Не комментируй очевидный синтаксис, не дублируй код словами и не оставляй временный migration diary. Обновляй существующие комментарии только если это изменение сделало их устаревшими.'
    };
  }
  return {
    mode: 'minimal-comments',
    instruction: 'Не добавляй подробные поясняющие комментарии только ради документирования миграции. Сохраняй принятый в проекте стиль и делай код/конфиг самодостаточными. Не удаляй существующие комментарии; обновляй их только если изменение сделало их неверными. Короткий комментарий допустим лишь там, где без него останется неочевидное ограничение корректности.'
  };
}
function currentFilterSummary(){
  const getVal = (id) => document.getElementById(id)?.value || '';
  const checked = (id) => !!document.getElementById(id)?.checked;
  const labels = [];
  const project = getVal('projectFilter');
  const group = getVal('groupFilter');
  const kind = getVal('kindFilter');
  const status = getVal('statusFilter');
  const target = getVal('targetMode');
  const q = getVal('q');
  labels.push('targetMode=' + (target || 'default'));
  if (project) labels.push('project=' + project);
  if (group) labels.push('group=' + group);
  if (kind) labels.push('kind=' + kind);
  if (status) labels.push('status=' + status);
  if (q) labels.push('search=' + q);
  ['onlyVuln','onlyCH','onlyResidual','onlyTargetRows','hideSatisfiedProjects'].forEach(id => {
    if (checked(id)) labels.push(id + '=true');
  });
  labels.push('codeCommentPolicy=' + codeCommentPolicy().mode);
  return labels.join('; ');
}
function targetModeTitle(){
  const select = document.getElementById('targetMode');
  if (!select) return 'По умолчанию';
  return select.options[select.selectedIndex]?.textContent || select.value;
}
function rowToPromptData(row, mode){
  const t = targetForRow(row, mode);
  const release = releaseForTarget(row, t.value);
  const registryArtifact = registryArtifactForTarget(row, t.value);
  return {
    project: row.dataset.project || '—',
    group: row.dataset.group || '—',
    subgroup: row.dataset.subgroup || '',
    name: row.dataset.name || row.children[0]?.innerText.trim() || '—',
    kind: row.dataset.kind || '—',
    current: row.dataset.current || '—',
    requestedSpec: row.dataset.requestedSpec || '—',
    target: t.value || '—',
    targetReason: t.reason || '—',
    hasTarget: !!t.has,
    plannedAction: ({default: row.dataset.plannedActionDefault, yellow: row.dataset.plannedActionYellow, green: row.dataset.plannedActionGreen}[mode] || ''),
    latest: row.dataset.latest || '—',
    vulns: row.dataset.vulns || '0',
    minNoCritical: row.dataset.minNoCritical || '—',
    minNoHigh: row.dataset.minNoHigh || '—',
    minNoVuln: row.dataset.minNoVuln || '—',
    minLag12m: lagTargetForDomRow(row, 12),
    minLag9m: lagTargetForDomRow(row, 9),
    minLag6m: lagTargetForDomRow(row, 6),
    minLag3m: lagTargetForDomRow(row, 3),
    lagPolicyTarget: lagTargetForDomRow(row),
    lagUpdateTarget: lagUpdateTargetForDomRow(row),
    lagPlanningSource: row.dataset.lagPlanningSource || 'live',
    lagThresholdMonths: Number(row.dataset.lagThresholdMonths || 12),
    reason: row.dataset.reason || '—',
    vulnNote: row.dataset.vulnNote || '—',
    notes: row.dataset.notes || '—',
    releaseStatus: release.status || 'not-checked-target',
    releaseSummary: release.summary || '—',
    release,
    registryArtifact,
    compatibilityCohort: row.dataset.compatibilityCohort || '',
    compatibilityNote: row.dataset.compatibilityNote || '',
    scopeExcluded: row.dataset.scopeExcluded === '1',
    exclusionReason: row.dataset.exclusionReason || '',
    exclusionSource: row.dataset.exclusionSource || '',
    ch: Number(row.dataset.ch || 0),
    anyVuln: Number(row.dataset.anyVuln || 0),
    residual: row.dataset.residual === '1'
  };
}
function visiblePromptRows(){
  const mode = document.getElementById('targetMode')?.value || 'default';
  return Array.from(document.querySelectorAll('tr.dep-row')).filter(row => {
    const projectSection = row.closest('.project-section');
    const groupSection = row.closest('.group-section');
    return !row.classList.contains('row-hidden') &&
      !(projectSection && projectSection.classList.contains('project-hidden')) &&
      !(groupSection && groupSection.classList.contains('group-hidden'));
  }).map(row => rowToPromptData(row, mode));
}
function projectTargetPromptRows(){
  const mode = document.getElementById('targetMode')?.value || 'default';
  const project = document.getElementById('projectFilter')?.value || '';
  const group = document.getElementById('groupFilter')?.value || '';
  return Array.from(document.querySelectorAll('tr.dep-row')).filter(row => {
    if (project && row.dataset.project !== project) return false;
    if (group && row.dataset.group !== group) return false;
    return true;
  }).map(row => rowToPromptData(row, mode));
}
// Used by the group-scoped prompt (buildGroupScopedCompactPrompt): the
// projectFilter/groupFilter dropdowns only filter by numeric group, not by
// the subgroup-derived bucket a Branch plan entry actually is, so they can't
// isolate a single branch when a group splits into subgroups. This ignores
// both dropdowns and returns every row for the given project so the caller
// can resolve the real branch->packages mapping via gitPlanForRows itself.
function projectTargetPromptRowsForProject(project){
  const mode = document.getElementById('targetMode')?.value || 'default';
  return Array.from(document.querySelectorAll('tr.dep-row'))
    .filter(row => row.dataset.project === project)
    .map(row => rowToPromptData(row, mode));
}
function planningConsistencyIssues(rows){
  const issues = rows.filter(r => {
    if (r.scopeExcluded) return false;
    const strictPolicy = Number(r.lagThresholdMonths || 12) < 12;
    const policyTarget = r.lagPolicyTarget || '—';
    const policyDue = strictPolicy && semverParts(policyTarget) &&
      compareSemverText(r.current || '0.0.0', policyTarget) < 0;
    return policyDue && !(r.hasTarget && isActionTargetValue(r.target));
  }).map(r => `${r.project}:${r.name} current=${r.current}, lagPolicy=≤${r.lagThresholdMonths}м, lagPolicyTarget=${r.lagPolicyTarget}, target=${r.target}`);
  rows.filter(r => !r.scopeExcluded && r.hasTarget && isActionTargetValue(r.target)).forEach(r => {
    const status = r.registryArtifact?.status || 'not-proven';
    if (!['available','current-installed'].includes(status)) {
      issues.push(`${r.project}:${r.name}@${r.target} registryArtifact=${status}, url=${r.registryArtifact?.tarballUrl || '—'}`);
    }
  });
  return issues;
}
function assertPlanningConsistency(rows){
  const issues = planningConsistencyIssues(rows);
  if (issues.length) {
    throw new Error(`ROADMAP_TARGET_DESYNC: ${issues.join('; ')}`);
  }
}
function promptRowsForExport(){
  const scope = document.getElementById('promptScope')?.value || 'project-target';
  const rows = scope === 'visible' ? visiblePromptRows() : projectTargetPromptRows();
  assertPlanningConsistency(rows);
  return rows;
}
function knowledgeForRows(rows){
  const packages = new Set(rows.map(row => row.name));
  return (REPORT_CONTEXT.knowledgeEntries || []).filter(entry =>
    (entry.packages || []).some(packageName => packages.has(packageName))
  );
}
function proofEnvelopeForProject(project, mode){
  const state = REPORT_CONTEXT.provenDependencyState || {};
  return state?.projects?.[project]?.[mode] || null;
}
function proofEnvelopesForProjects(projects, mode){
  const result = {};
  for (const project of projects) {
    const envelope = proofEnvelopeForProject(project, mode);
    if (!envelope) throw new Error(`PROVEN_DEPENDENCY_PROOF_MISSING: ${project}/${mode}`);
    result[project] = envelope;
  }
  return result;
}
function scopeManifestRowFor(r){
  const test = testPolicyForRow(r);
  const shouldUpdate = rowNeedsAction(r);
  const artifact = r.registryArtifact || {};
  return {
    project: r.project || '—', group: Number(r.group), subgroup: r.subgroup || '',
    kind: r.kind || '—', package: r.name || '—', section: sectionForKind(r.kind),
    requestedSpec: r.requestedSpec, current: r.current || '—', target: r.target || '—',
    targetReason: r.targetReason || '—',
    shouldUpdate: r.scopeExcluded ? false : shouldUpdate,
    action: r.scopeExcluded ? 'excluded' : (shouldUpdate ? recommendedActionForRow(r) : 'deferred'),
    lagPolicyMonths: r.lagThresholdMonths, lagPolicyTarget: r.lagPolicyTarget || '—',
    targetArtifactStatus: artifact.status || (shouldUpdate ? 'not-proven' : 'not-applicable'),
    targetArtifactUrl: artifact.finalUrl || artifact.tarballUrl || '',
    targetArtifactError: artifact.error || '',
    compatibilityCohort: r.compatibilityCohort || '',
    compatibilityNote: r.compatibilityNote || '',
    scopeExcluded: !!r.scopeExcluded,
    exclusionReason: r.exclusionReason || '',
    exclusionSource: r.exclusionSource || '',
    testPolicy: test.policy || '', testReason: test.reason || ''
  };
}
function simpleScopeHash(rows, hashVersion=5){
  const text = rows.map(r => {
    const row = scopeManifestRowFor(r);
    const fields = [row.project,row.group,row.subgroup,row.kind,row.package,row.current,row.target,row.shouldUpdate ? 'true' : 'false'];
    if (hashVersion >= 2) fields.push(row.action,row.testPolicy,row.testReason);
    if (hashVersion >= 3) fields.push(row.lagPolicyMonths,row.lagPolicyTarget,row.targetReason);
    if (hashVersion >= 4) fields.push(row.targetArtifactStatus,row.targetArtifactUrl,row.targetArtifactError,row.compatibilityCohort,row.compatibilityNote);
    if (hashVersion >= 5) fields.push(row.scopeExcluded ? 'true' : 'false',row.exclusionReason,row.exclusionSource);
    return fields.join('|');
  }).sort().join('\\n');
  let hash = 2166136261;
  for (let i=0;i<text.length;i++) { hash ^= text.charCodeAt(i); hash = Math.imul(hash, 16777619); }
  return ('00000000' + (hash >>> 0).toString(16)).slice(-8);
}

function isActionTargetValue(value){
  return !!semverParts(value);
}
function packageSpecForTarget(requestedSpec, target){
  const version = (target || '').toString().match(/(\\d+\\.\\d+\\.\\d+(?:[-+][0-9A-Za-z.-]+)?)/)?.[1] || target;
  const spec = (requestedSpec || '').toString().trim();
  if (!version || !isActionTargetValue(version)) return version || target || '—';
  if (spec.startsWith('^')) return '^' + version;
  if (spec.startsWith('~')) return '~' + version;
  if (/^(>=|>|<=|<|=)/.test(spec)) return '^' + version;
  if (/^workspace:|^file:|^link:|^portal:|^git\\+|^https?:/.test(spec)) return version;
  return version;
}
function buildPackageJsonExportFromCurrentView(){
  applyFilters();
  const rows = promptRowsForExport();
  const actionRows = rows.filter(r => rowNeedsAction(r));
  const mode = document.getElementById('targetMode')?.value || 'default';
  const now = new Date().toISOString();
  if (!actionRows.length) {
    return `# Package.json proposals

Дата формирования: ${now}
Registry из settings: \\`${REPORT_CONTEXT.registry || 'не указан'}\\`
Правило registry: если registry указан, все проверки версий/metadata/outdated/install выполняй через него; не обращайся к public npm registry без явного разрешения. Ручной audit выполняется только отдельным скриптом по запросу пользователя.
Режим цели: **${targetModeTitle()}** (${mode})
Фильтры отчёта: \\`${currentFilterSummary()}\\`

В текущем срезе нет строк с target-действием. Изменять package.json по этому срезу не нужно.
`;
  }
  const byProject = new Map();
  for (const r of actionRows) {
    if (!byProject.has(r.project)) byProject.set(r.project, []);
    byProject.get(r.project).push(r);
  }
  const sectionFor = (r) => ({runtime:'dependencies', dev:'devDependencies', optional:'optionalDependencies', peer:'peerDependencies'}[r.kind] || 'dependencies');
  const buildPatchObject = (projectRows) => {
    const result = {dependencies: {}, devDependencies: {}, optionalDependencies: {}, peerDependencies: {}};
    for (const r of projectRows) {
      result[sectionFor(r)][r.name] = packageSpecForTarget(r.requestedSpec, r.target);
    }
    for (const section of ['dependencies','devDependencies','optionalDependencies','peerDependencies']) {
      if (!Object.keys(result[section]).length) delete result[section];
    }
    return result;
  };
  const table = (projectRows) => [
    '| Section | Package | Было в package.json | Текущая resolved | Предложение | Уязвимости | Что снимаем / риск | Почему выбрано |',
    '|---|---|---|---|---|---|---|---|',
    ...projectRows.map(r => `| ${sectionFor(r)} | \\`${escapeMdCell(r.name)}\\` | \\`${escapeMdCell(r.requestedSpec)}\\` | \\`${escapeMdCell(r.current)}\\` | \\`${escapeMdCell(packageSpecForTarget(r.requestedSpec, r.target))}\\` | ${escapeMdCell(r.vulns)} | ${escapeMdCell(r.vulnNote)} | ${escapeMdCell(r.targetReason || r.reason)} |`)
  ].join('\\n');

  let output = `# Package.json proposals

Дата формирования: ${now}
Registry из settings: \\`${REPORT_CONTEXT.registry || 'не указан'}\\`
Правило registry: если registry указан, все проверки версий/metadata/outdated/install выполняй через него; не обращайся к public npm registry без явного разрешения. Ручной audit выполняется только отдельным скриптом по запросу пользователя.
Режим цели: **${targetModeTitle()}** (${mode})
Фильтры отчёта: \\`${currentFilterSummary()}\\`

Это не полный package.json, а безопасный patch-фрагмент для ручного переноса. После применения нужно выполнить install, проверить канонический lockfile, OSV/outdated diff, changelog/release notes и breaking changes по каждой зависимости. Ручной audit — отдельная пользовательская сверка.

`;
  for (const [project, projectRows] of Array.from(byProject.entries()).sort((a,b) => a[0].localeCompare(b[0]))) {
    projectRows.sort((a,b) => sectionFor(a).localeCompare(sectionFor(b)) || a.name.localeCompare(b.name));
    const patch = buildPatchObject(projectRows);
    output += `## ${project}

### package.json patch

\\`\\`\\`json
${JSON.stringify(patch, null, 2)}
\\`\\`\\`

### Предложения и риски

${table(projectRows)}

`;
  }
  output += `## Что обязательно проверить руками

- Не применять patch вслепую: сначала сравнить с текущим package.json и lockfile.
- Для major/runtime/API/CI/platform изменений прочитать changelog/release notes и разделы breaking changes.
- Для C/H перепроверь fresh OSV/registry facts после install. Корпоративный Nexus/npm audit запускается отдельно через \\`manual_dependency_audit.py\\` только по запросу пользователя.
- Для DEV-зависимостей проверить, что они не участвуют в CI/build/release/published artifact; иначе поднять риск и вынести в отдельную задачу.
`;
  return output;
}

function slugifyBranchPart(value){
  return (value || 'work').toString().toLowerCase()
    .replace(/[^a-z0-9а-яё._-]+/gi, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48) || 'work';
}
function sectionForKind(kind){
  return ({runtime:'dependencies', dev:'devDependencies', optional:'optionalDependencies', peer:'peerDependencies'}[kind] || 'dependencies');
}
function releaseIntelForPrompt(row){
  const release = row.release || {};
  return {
    target: release.target || row.target || '—',
    status: release.status || row.releaseStatus || 'not-checked',
    summary: release.summary || row.releaseSummary || '—',
    coverage: release.coverage || '—',
    breakingChanges: Array.isArray(release.breakingChanges) ? release.breakingChanges : [],
    migrationNotes: Array.isArray(release.migrationNotes) ? release.migrationNotes : [],
    deprecations: Array.isArray(release.deprecations) ? release.deprecations : [],
    requirements: Array.isArray(release.requirements) ? release.requirements : [],
    sources: Array.isArray(release.sources) ? release.sources : []
  };
}
function criticalReleaseDossierForRows(rows){
  const notableStatuses = new Set([
    'breaking-confirmed', 'breaking-likely', 'coverage-incomplete',
    'unavailable', 'not-checked-limit', 'not-checked-target'
  ]);
  return rows
    .filter(r => !r.scopeExcluded && r.hasTarget && isActionTargetValue(r.target))
    .map(r => {
      const intel = releaseIntelForPrompt(r);
      return {
        project: r.project,
        package: r.name,
        current: r.current,
        target: r.target,
        status: intel.status,
        summary: intel.summary,
        coverage: intel.coverage,
        breakingChanges: intel.breakingChanges,
        migrationNotes: intel.migrationNotes,
        deprecations: intel.deprecations,
        requirements: intel.requirements,
        sources: intel.sources
      };
    })
    .filter(item =>
      notableStatuses.has(item.status) ||
      item.breakingChanges.length ||
      item.migrationNotes.length ||
      item.deprecations.length ||
      item.requirements.length
    );
}
function compactCriticalReleaseDossierForRows(rows){
  // The full dossier embeds every breaking-change/migration/source entry per
  // package with no cap; for a real scope (dozens of actionable rows, each with
  // many GitHub-release sources) that makes the "compact" prompt hundreds of KB,
  // blowing past small-context agent models before they ever reach the branch
  // plan near the end of the document. Truncate to a preview here; rule 5 already
  // sends the agent to read the untruncated per-package entry from roadmapPath.
  const maxListItems = 3;
  const maxSources = 3;
  const trim = (list) => Array.isArray(list) ? list.slice(0, maxListItems) : [];
  return criticalReleaseDossierForRows(rows).map(item => ({
    ...item,
    breakingChanges: trim(item.breakingChanges),
    migrationNotes: trim(item.migrationNotes),
    deprecations: trim(item.deprecations),
    requirements: trim(item.requirements),
    sources: Array.isArray(item.sources) ? item.sources.slice(0, maxSources) : []
  }));
}
function recommendedActionForRow(row){
  if (row.scopeExcluded) return 'excluded';
  const planned = String(row.plannedAction || '').toLowerCase();
  if (['update','remove','deferred','excluded'].includes(planned)) return planned;
  if (!(row.hasTarget && isActionTargetValue(row.target))) return 'deferred';
  // Backward compatibility for old saved HTML snapshots that predate
  // solver-owned executable actions. New roadmaps always carry plannedAction.
  const release = releaseIntelForPrompt(row);
  const text = [release.summary, ...(release.deprecations || []), ...(release.migrationNotes || [])].join(' ').toLowerCase();
  const removableTypesStub = String(row.name || '').startsWith('@types/') && (
    text.includes('stub types definition') ||
    text.includes('stub type definition') ||
    text.includes('provides its own type definitions') ||
    text.includes('do not need this installed')
  );
  return removableTypesStub ? 'remove' : 'update';
}
function rowNeedsAction(row){
  const action = recommendedActionForRow(row);
  if (action === 'remove') return true;
  if (action !== 'update') return false;
  // A frozen baseline target is a historical goal, not perpetual executable
  // work.  Replans are generated from the cumulative merged tree; once that
  // tree already satisfies (or exceeds) the exact target, putting the row back
  // into shouldUpdate/Branch plan fabricates scope additions and used to cause
  // huge MIGRATION_PLAN_APPROVAL_REQUIRED banners after successful groups.
  const current = semverParts(row.current);
  const target = semverParts(row.target);
  if (current && target) return compareSemverText(row.current, row.target) < 0;
  // Keep conservative behavior for non-semver/custom specs: the deterministic
  // validator/agent still owns those rare actions.
  return true;
}
function testPolicyForRow(row){
  const name = String(row.name || '').toLowerCase();
  const kind = String(row.kind || '');
  const buildOrCi = ['typescript','vite','webpack','rollup','esbuild','swc','babel','jest','vitest','playwright','cypress','storybook','eslint','stylelint','postcss','sass','less','ts-jest','lint-staged','husky'].some(token => name.includes(token));
  const focused = ['runtime','optional','peer'].includes(kind) || buildOrCi;
  return {
    policy: focused ? 'focused-check-required' : 'standard-check-allowed',
    reason: focused
      ? (buildOrCi ? 'dependency affects build/test/CI/release behavior; use existing focused checks or project gates' : `dependency kind=${kind} needs focused verification, not generated regression tests`)
      : 'dev-only dependency; ordinary project checks are enough after proving it does not affect CI/build/release/runtime'
  };
}
function scopeManifestForRows(rows){
  return rows.map(r => ({
    project: r.project,
    group: Number(r.group),
    subgroup: r.subgroup || '',
    package: r.name,
    section: sectionForKind(r.kind),
    kind: r.kind,
    requestedSpec: r.requestedSpec,
    current: r.current,
    target: r.target,
    shouldUpdate: rowNeedsAction(r),
    action: rowNeedsAction(r) ? recommendedActionForRow(r) : (r.scopeExcluded ? 'excluded' : 'deferred'),
    testPolicy: testPolicyForRow(r).policy,
    testReason: testPolicyForRow(r).reason,
    targetReason: r.targetReason,
    reason: r.reason,
    notes: r.notes,
    lagPolicyMonths: r.lagThresholdMonths,
    lagTargets: {m12:r.minLag12m, m9:r.minLag9m, m6:r.minLag6m, m3:r.minLag3m},
    registryArtifact: r.registryArtifact || {},
    compatibilityCohort: r.compatibilityCohort || '',
    compatibilityNote: r.compatibilityNote || '',
    scopeExcluded: !!r.scopeExcluded,
    exclusionReason: r.exclusionReason || '',
    exclusionSource: r.exclusionSource || '',
    vulnerabilities: r.vulns,
    vulnerabilityWorkNote: r.vulnNote,
    releaseIntelligence: releaseIntelForPrompt(r)
  }));
}
function automaticBranchFamily(row){
  const current = semverParts(row.current);
  const target = semverParts(row.target);
  const major = !!(current && target && target[0] > current[0]);
  if (Number(row.ch || 0) > 0) return 'security';
  if (['runtime','optional','peer'].includes(String(row.kind || ''))) return major ? 'runtime-major' : 'runtime';
  if (testPolicyForRow(row).policy === 'focused-check-required') return major ? 'toolchain-major' : 'toolchain';
  return major ? 'dev-major' : 'dev';
}
function stableBranchBucketKey(family, packages){
  const payload = Array.from(new Set(packages)).sort().join('\\n');
  let hash = 2166136261;
  for (let i=0;i<payload.length;i++) { hash ^= payload.charCodeAt(i); hash = Math.imul(hash, 16777619); }
  const digest = ('00000000' + (hash >>> 0).toString(16)).slice(-8);
  return `auto-${family}-${digest}`;
}
function automaticBranchBuckets(rows, maxBatchSize){
  const limit = Math.max(1, Math.min(8, Number(maxBatchSize) || 4));
  const cohorts = new Map();
  const families = new Map();
  const orderedRows = rows.slice().sort((a,b) =>
    String(a.name).localeCompare(String(b.name)) || String(a.kind).localeCompare(String(b.kind))
  );
  for (const row of orderedRows) {
    if (row.compatibilityCohort) {
      const key = `cohort-${row.compatibilityCohort}`;
      if (!cohorts.has(key)) cohorts.set(key, []);
      cohorts.get(key).push(row.name);
      continue;
    }
    const family = automaticBranchFamily(row);
    if (!families.has(family)) families.set(family, []);
    families.get(family).push(row.name);
  }
  const buckets = new Map(Array.from(cohorts.entries()).sort((a,b) => a[0].localeCompare(b[0])));
  for (const [family, names] of Array.from(families.entries()).sort((a,b) => a[0].localeCompare(b[0]))) {
    const packages = Array.from(new Set(names)).sort();
    for (let offset=0; offset<packages.length; offset += limit) {
      const chunk = packages.slice(offset, offset + limit);
      // Bucket identity follows its package set, not its ordinal position.
      // After a residual replan, removing an earlier sibling must not rename
      // every later branch from major-3 -> major-2 -> major-1.
      buckets.set(stableBranchBucketKey(family, chunk), chunk);
    }
  }
  return buckets;
}
function gitPlanForRows(rows){
  const projects = Array.from(new Set(rows.map(r => r.project))).sort();
  return projects.map(project => {
    const config = REPORT_CONTEXT.projectGit?.[project] || {};
    const projectRows = rows.filter(r => r.project === project && rowNeedsAction(r));
    const baseBranch = config.baseBranch || 'libs';
    const prefix = config.branchPrefix || baseBranch;
    const mergedBranch = config.mergedBranch || `${baseBranch}-merged`;
    const releaseBranch = config.releaseBranch || `${prefix}-release`;
    const gitHooks = {
      intermediateCommits: 'skip',
      intermediateMerges: 'skip',
      intermediatePushes: 'skip',
      releaseCommit: 'run',
      releasePush: 'run'
    };
    const release = {
      strategy: config.release?.strategy || 'squash',
      cleanupAuditWorkspace: config.release?.cleanupAuditWorkspace !== false,
      commitMessage: config.release?.commitMessage || 'chore(deps): update dependencies',
      finalGateCommands: Array.isArray(config.release?.finalGateCommands) ? config.release.finalGateCommands : []
    };
    const buckets = automaticBranchBuckets(projectRows, config.branchBatchSize);
    const usedBranches = new Set();
    const branches = Array.from(buckets.entries()).map(([bucket, packages], index) => {
      const branchStem = `${prefix}-${slugifyBranchPart(bucket)}`;
      let branch = branchStem;
      let suffix = 2;
      while (usedBranches.has(branch)) branch = `${branchStem}-${suffix++}`;
      usedBranches.add(branch);
      return {
        order: index + 1,
        bucket,
        branch,
        packages: Array.from(new Set(packages)).sort()
      };
    });
    return {
      project,
      projectDir: config.projectDir || '',
      sourceRemote: config.remote || 'origin',
      sourceBranch: config.sourceBranch || '<current-branch>',
      sourceCommit: config.sourceCheckout?.sourceCommit || '',
      sourceCheckoutVerified: !!config.sourceCheckout?.verified,
      baseBranch,
      branchPrefix: prefix,
      mergedBranch,
      releaseBranch,
      gitHooks,
      release,
      push: !!config.push,
      manualAudit: config.manualAudit || {mode: 'manual-only'},
      branches
    };
  });
}
function buildFullPromptFromCurrentView(){
  applyFilters();
  const rows = promptRowsForExport();
  if (!rows.length) return '';
  const projects = Array.from(new Set(rows.map(r => r.project))).sort();
  const mode = document.getElementById('targetMode')?.value || 'default';
  const now = new Date().toISOString();
  const actionRows = rows.filter(r => rowNeedsAction(r));
  const excludedRows = rows.filter(r => r.scopeExcluded);
  const deferredRows = rows.filter(r => !r.scopeExcluded && !actionRows.includes(r));
  const riskyActionRows = actionRows.filter(r => ['runtime','optional','peer'].includes(r.kind) || r.compatibilityCohort || r.residual || r.ch > 0);
  const riskyDeferredRows = deferredRows.filter(r => ['runtime','optional','peer'].includes(r.kind) || r.compatibilityCohort || r.residual || r.ch > 0);
  const scopeManifest = scopeManifestForRows(rows);
  const scopeHash = simpleScopeHash(rows);
  const branchPlan = gitPlanForRows(rows);
  const commentPolicy = codeCommentPolicy();
  const projectWarning = projects.length > 1
    ? `\n> ВАЖНО: выбрано несколько проектов (${projects.join(', ')}). Обрабатывай их строго по одному checkout за раз; не смешивай файлы и ветки разных репозиториев.\n`
    : `\nРабочая директория: корень frontend-проекта **${projects[0]}**.\n`;
  const buildTable = (promptRows) => [
    '| Проект | Группа / подгруппа | Пакет | Section | Current | Target | Lag policy | Vulnerabilities | Breaking/release status | Причина |',
    '|---|---|---|---|---|---|---|---|---|---|',
    ...promptRows.map(r => `| ${escapeMdCell(r.project)} | ${escapeMdCell(r.group)} / ${escapeMdCell(r.subgroup || '—')} | \\`${escapeMdCell(r.name)}\\` | ${sectionForKind(r.kind)} | \\`${escapeMdCell(r.current)}\\` | \\`${escapeMdCell(r.target)}\\` | ≤${r.lagThresholdMonths} мес. | ${escapeMdCell(r.vulns)} | ${escapeMdCell(r.releaseStatus)}: ${escapeMdCell(r.releaseSummary)} | ${escapeMdCell(r.targetReason || r.reason)} |`)
  ].join('\\n');
  const projectScopes = Object.fromEntries(projects.map(project => {
    const projectRows = rows.filter(row => row.project === project);
    const projectActions = projectRows.filter(row => rowNeedsAction(row));
    const projectExcluded = projectRows.filter(row => row.scopeExcluded);
    return [project, {
      selectedRows: projectRows.length,
      actionRows: projectActions.length,
      deferredRows: projectRows.length - projectActions.length - projectExcluded.length,
      excludedRows: projectExcluded.length,
      scopeHash: simpleScopeHash(projectRows)
    }];
  }));
  const scopeJson = JSON.stringify({
    schemaVersion: 1,
    scopeHashVersion: 5,
    generatedAt: now,
    registry: REPORT_CONTEXT.registry || '',
    registryPolicy: {
      artifactSource: 'configured-registry-only',
      requireTargetTarball: true,
      forbidForeignPackageManagerUrls: true
    },
    targetMode: mode,
    codeCommentPolicy: commentPolicy.mode,
    scopeHash,
    selectedRows: rows.length,
    actionRows: actionRows.length,
    deferredRows: deferredRows.length,
    excludedRows: excludedRows.length,
    projects,
    projectScopes,
    proofEnvelopes: proofEnvelopesForProjects(projects, mode),
    rows: scopeManifest
  }, null, 2);
  const actionJson = JSON.stringify(scopeManifest.filter(r => r.shouldUpdate), null, 2);
  const branchPlanJson = JSON.stringify(branchPlan, null, 2);
  const criticalReleaseDossier = criticalReleaseDossierForRows(actionRows);
  const criticalReleaseJson = JSON.stringify({
    releaseIntelligence: criticalReleaseDossier,
    priorMigrationKnowledge: knowledgeForRows(actionRows),
    knowledgePolicy: 'Validate applicability and verification on the current checkout; never apply blindly.'
  }, null, 2);

  if (actionRows.length === 0) {
    return `# Задача для агента: подтверждение no-op по dependency roadmap

Дата: ${now}
Target mode: **${targetModeTitle()}** (${mode})
Scope: ${rows.length} строк, action=0, deferred=${deferredRows.length}, excluded=${excludedRows.length}, scopeHash=\\`${scopeHash}\\`.
${projectWarning}
## Контекст

- Registry: \\`${REPORT_CONTEXT.registry || 'не указан'}\\` — используй только его и проектный config; public npm registry запрещён без явного разрешения.
- Roadmap JSON: \\`${REPORT_CONTEXT.roadmapJsonPath || '.dependency-roadmap/artifacts/dependency-roadmap.json'}\\`
- Dashboard state: \\`${REPORT_CONTEXT.dashboardStatePath || '.dependency-roadmap/state/dashboard-state.json'}\\`
- History: \\`${REPORT_CONTEXT.historyDir || DEP_HISTORY_DIR}\\`

## Обязательная проверка scope

1. Прочитай package.json и собери прямые зависимости из \\`dependencies\\`, \\`devDependencies\\`, \\`optionalDependencies\\`, \\`peerDependencies\\`.
2. Сопоставь их со всеми строками manifest ниже по ключу \\`section + package\\`.
3. Подтверди ровно ${rows.length} строк и hash \\`${scopeHash}\\`. При несовпадении ничего не меняй и сообщи \\`SCOPE_MANIFEST_MISMATCH\\`.
4. Не превращай deferred-строки в update. Строки \\`action=excluded\\` запрещено менять; их причина hash-защищена.

\\`\\`\\`json
${scopeJson}
\\`\\`\\`

## Действие

Не изменяй package.json, lockfile, код и тесты. Запиши append-only no-op event в history с количеством строк, scopeHash и причиной. Итог: updated=0, deferred=${deferredRows.length}, excluded=${excludedRows.length}, riskyDeferred=${riskyDeferredRows.length}.
`;
  }

  return `# Автономная задача: dependency update + branch/merge cycle

Дата формирования: ${now}
Target mode: **${targetModeTitle()}** (${mode})
Фильтры: \\`${currentFilterSummary()}\\`
Scope: selected=${rows.length}, update=${actionRows.length}, deferred=${deferredRows.length}, excluded=${excludedRows.length}, riskyUpdate=${riskyActionRows.length}, scopeHash=\\`${scopeHash}\\`.
${projectWarning}
## 1. Неподвижный контракт scope — проверить ДО любых изменений

В manifest перечислены **все** строки выбранного проекта/группы: update, deferred и excluded. Нельзя работать по памяти, только по manifest. Строки \\`action=excluded\\` не входят в branch plan и запрещены к изменению.

1. Прочитай \\`package.json\\` и собери прямые объявления из четырёх секций: \\`dependencies\\`, \\`devDependencies\\`, \\`optionalDependencies\\`, \\`peerDependencies\\`.
2. Сверь каждую строку manifest по составному ключу \\`project + section + package\\`; учти одинаковый пакет в разных секциях как разные объявления.
3. Должно быть: selected=${rows.length}, update=${actionRows.length}, deferred=${deferredRows.length}, excluded=${excludedRows.length}, scopeHash=\\`${scopeHash}\\`.
4. Поля \\`lagPolicyMonths\\`, \\`lagPolicyTarget\\` и \\`targetReason\\` входят в hash. Если package-policy строже 12 месяцев, current ниже concrete \\`lagPolicyTarget\\`, но строка помечена deferred — остановись с \\`ROADMAP_TARGET_DESYNC\\`; не угадывай intent самостоятельно.
5. Если update-пакет отсутствует, дублируется, оказался в другой секции или manifest не соответствует checkout — **остановись до изменения файлов** с \\`SCOPE_MANIFEST_MISMATCH\\` и точным diff.
6. Меняй только строки \\`shouldUpdate=true\\` и соблюдай immutable \\`action=update|remove\\`. Для \\`remove\\` удали прямое объявление и stale lock entry; не устанавливай deprecated stub. Deferred-строки сохраняй в отчёте как осознанно не изменённые. Excluded-строки сохраняй без изменений и с указанной причиной; они не дают разрешения на транзитивное или попутное обновление.

### Полный scope manifest

Сохрани JSON ниже без изменений в tracked run-артефакт, например \\`<historyDir>/runs/<timestamp>_<project>_scope.json\\`, и передай этот же файл final validator.

\\`\\`\\`json
${scopeJson}
\\`\\`\\`

## 2. Контекст и ограничения

- Registry: \\`${REPORT_CONTEXT.registry || 'не указан'}\\`. Все metadata/install/outdated команды выполняй через него или project config. Ручной vulnerability audit запускается отдельно пользователем. Public npm registry запрещён без явного разрешения.
- Roadmap JSON для final target→fact validation: \\`${REPORT_CONTEXT.roadmapJsonPath || '.dependency-roadmap/artifacts/dependency-roadmap.json'}\\`
- Dashboard state: \\`${REPORT_CONTEXT.dashboardStatePath || '.dependency-roadmap/state/dashboard-state.json'}\\`
- Append-only history: \\`${REPORT_CONTEXT.historyDir || DEP_HISTORY_DIR}\\`
- Settings sources: \\`${(REPORT_CONTEXT.settingsSources || []).join(', ') || 'не указаны'}\\`
- Target означает **минимальную требуемую версию, зафиксированную при baseline с однократным запасом 3 месяца**, а не разрешение обновить всё до latest. Compliance и target разделены: policy 12/9/6/3м проверяется по baseline-границе, remediation target берётся как 9/6/3/3м. Обычная повторная генерация не сдвигает baseline и не прибавляет запас снова.
- **NO_REFACTORING:** запрещены попутный рефакторинг, cleanup, переименование/перемещение/разбиение production-файлов, изменение архитектуры/public API, массовое форматирование, удаление «лишнего» кода, переписывание существующих тестов и широкие autofix/format-only диффы. Разрешены только минимальные compatibility changes, напрямую требуемые выбранным package current→target и подтверждённые release/migration evidence. Если без более широкого рефакторинга обновление невозможно — останови конкретную ветку с \\`REFACTOR_REQUIRED\\`, не выполняй его самовольно.
- **MIGRATION_DOCUMENTATION:** в ходе работы обязательно веди три tracked документа. \\`docs/dependency-upgrades.md\\` — полный технический changelog current→target: версии, breaking changes, migration/deprecation notes, требования, изменённые API/config, compatibility fixes, проверки, gaps и ссылки на источники. \\`docs/dependency-update-summary.md\\` — понятный команде итог без канцелярита: какие критичные баги/уязвимости реально исправлены обновлением, чем это помогло продукту/бизнесу/разработке, какие новые возможности стали доступны, что сломалось во время миграции и как было исправлено; не выдумывай пользу без evidence, неизвестное помечай явно. \\`docs/dependency-update-review-notes.md\\` — полная карта ревью: каждое нетривиальное изменение вне lockfile, путь, package, зачем изменение понадобилось, source evidence, чем проверено и почему это не попутный рефакторинг. Если файла нет — создай его с понятными разделами; lockfile не разбирай построчно.
- **CHANGE_RATIONALE_REQUIRED:** у каждого изменения в коде/конфигурации должен быть краткий rationale в \\`docs/dependency-update-review-notes.md\\`; inline code comments добавляй только по \\`CODE_COMMENT_POLICY\\`, чтобы не превращать код в migration diary.
- **CODE_COMMENT_POLICY (\\`${commentPolicy.mode}\\`):** ${commentPolicy.instruction} Сохрани выбранную policy в run state/evidence, чтобы continuation-сессия не потеряла её.
## 3. Автономный Git-цикл

Исполни branch plan ниже. Не делай destructive reset, force-push, удаление чужих веток или потерю незакоммиченных изменений.

\\`\\`\\`json
${branchPlanJson}
\\`\\`\\`

Команды управляемых Git-операций (подставь значения из branch plan):

\\`\\`\\`bash
# Любой промежуточный commit / merge / push: hooks отключены только для этой команды.
python "${REPORT_CONTEXT.gitHookPolicyToolPath || 'tools/dependency-roadmap-tool/git_hook_policy.py'}" \\
  --project-dir "<projectDir>" --mode skip -- commit -m "<message>"

# Ручной vulnerability audit запускается пользователем отдельно через manual_dependency_audit.py.
# Его npm package-lock живёт только в artifacts и не является project lockfile.

# Финал: sourceCommit -> releaseBranch, squash mergedBranch, удалить legacy tool workspace если он остался,
# выполнить final gates и сделать единственный обычный commit с hooks.
python "${REPORT_CONTEXT.releaseBranchToolPath || 'tools/dependency-roadmap-tool/dependency_release_branch.py'}" \\
  --project-dir "<projectDir>" \\
  --source-branch "<sourceBranch>" \\
  --source-commit "<sourceCommit>" \\
  --merged-branch "<mergedBranch>" \\
  --release-branch "<releaseBranch>" \\
  --commit-message "<release.commitMessage>" \\
  --gate-command "<full final gate command>"
\\`\\`\\`
Для каждого проекта:

1. Проверь \\`git status --porcelain\\`. Полностью игнорируй editor/OS noise (.idea/, .vs/, .vscode/, .fleet/, swap/user-файлы, .DS_Store, Thumbs.db): не stash/commit/delete их и не считай причиной blocker. Если есть другие несвязанные изменения, сохрани их без потери и остановись, если нельзя гарантировать чистый scope.
2. До создания веток проверь provenance из branch plan: \\`sourceCheckoutVerified=true\\`, fetched \\`sourceRemote/sourceBranch\\` всё ещё обязан совпадать с \\`sourceCommit\\`; при расхождении остановись с \\`SOURCE_COMMIT_CHANGED_AFTER_ROADMAP\\` и перегенерируй roadmap. Создай или проверь \\`baseBranch\\` ровно от \\`sourceCommit\\`; автоматического audit bootstrap больше нет.
3. Для каждого элемента \\`branches\\` создай ветку **от точного baseBranch HEAD**. В этой ветке обновляй только перечисленные packages и минимально необходимые peer/transitive/compatibility изменения. Не рефактори код «заодно» и не меняй файлы, не связанные с доказанной миграцией конкретного пакета. Каждый промежуточный commit выполняй через \\`git_hook_policy.py --mode skip\\`; обычный \\`git commit\\` и один лишь \\`--no-verify\\` для внутренних шагов запрещены. Для Yarn меняй только \\`yarn.lock\\`; для npm — только \\`package-lock.json/npm-shrinkwrap.json\\`. Никогда не создавай cross-manager lockfile в корне проекта.
4. После каждой подгруппы выполни install, проверку проектного lockfile и релевантные существующие lint/typecheck/test/build/project gates. Не создавай новые regression tests/gates без явного запроса пользователя; если существующего покрытия нет, зафиксируй gap и риск в review notes/run evidence. Не мержи красную ветку. Не создавай production \\`package-lock.json\\` в корне Yarn-проекта.
5. Создай \\`mergedBranch\\` от baseBranch и последовательно merge ветки в порядке \\`order\\` с \\`--no-ff\\`, но каждый merge/merge-commit выполняй через \\`git_hook_policy.py --mode skip\\`. Конфликты разрешай семантически: итоговый package.json должен содержать объединение targets/actions, канонический lockfile **того же package manager** должен соответствовать package.json, кодовые адаптации и тесты не должны исчезнуть. После каждого merge машинно сверь cumulative packages уже смерженных веток с manifest; любое падение ниже target или возврат удаляемого пакета — \\`MERGE_TARGET_REGRESSION\\`. Затем повторяй быстрые проверки; после последнего — полный набор.
6. На полностью собранном \\`mergedBranch\\` запусти ordinary \\`generate.* --only-project <project>\\`. Generator проверит/обновит только канонический project lockfile: \\`yarn.lock\\` для Yarn или \\`package-lock.json/npm-shrinkwrap.json\\` для npm. Он не запускает vulnerability audit и не создаёт npm lock для Yarn. При необходимости пользователь отдельно запускает \\`manual_dependency_audit.py\\` как ручную сверку; её artifacts не коммить в production branch.
7. После успешных target→fact и project checks вызови \\`dependency_release_branch.py\\`. Он создаёт \\`releaseBranch\\` от точного \\`sourceCommit\\`, squash-ит \\`mergedBranch\\`, удаляет только legacy/tool audit workspace если он остался, запускает все \\`release.finalGateCommands\\`/переданные \\`--gate-command\\`, останавливается с \\`RELEASE_FINAL_GATE_DIRTY\\`, если gate оставил unstaged/untracked файлы, и только затем делает **единственный обычный commit с hooks**. Нельзя передавать \\`--no-verify\\` или переопределять hooksPath для release commit. Если hook падает — \\`RELEASE_COMMIT_OR_HOOK_FAILED\\`; не обходи его.
8. Внутренние push выполняй без hooks только через hook-policy wrapper и только при \\`push=true\\`. Push releaseBranch выполняется обычно, чтобы pre-push hook тоже мог сработать. Финальный checkout — releaseBranch. В итоговом отчёте перечисли source/base/subbranches/merged/release branches, commits, package-manager lockfile, merge order, conflicts, final gates и release commit. Ручной audit report указывай отдельно только если пользователь его действительно запускал.

## Критичный BREAKING / migration dossier из dashboard

Этот блок входит прямо в промпт и обязателен к разбору до изменения соответствующего пакета. Он содержит только предварительно найденные критичные/неполные случаи; полный \\`releaseIntelligence\\` для **каждой** строки также находится в scope manifest выше. Нельзя проигнорировать вложенные \\`breakingChanges\\`, \\`migrationNotes\\`, \\`deprecations\\`, \\`requirements\\`, \\`coverage\\` и \\`sources\\`.

\\`\\`\\`json
${criticalReleaseJson}
\\`\\`\\`

## 4. Release intelligence и breaking changes — обязательны ПО КАЖДОЙ update-строке

Данные dashboard ниже — предварительная разведка, не замена инженерной проверке. Для диапазона **current → target**:

1. Прочитай official changelog/release notes/migration guide/upgrade guide и package repository. Используй источники из manifest; если статус \\`unavailable/not-checked/limit\\`, найди источники самостоятельно через registry metadata/repository.
2. Не анализируй только target release: покрой **все промежуточные версии** в диапазоне.
3. Запиши структурно:
   - \\`breakingChanges: confirmed|likely|none-found|unknown\\`;
   - конкретные несовместимости и затронутые API/config/runtime;
   - migration actions и обязательные кодовые/config изменения;
   - deprecations;
   - peer/Node/browser/build requirements;
   - source URLs и фактически покрытый диапазон.
4. \\`none-found\\` допустим только при реально прочитанных источниках. Нет источника = \\`unknown\\`, а не “breaking changes нет”.
5. Если breaking change затрагивает usage проекта, сначала найди все usages, сделай адаптацию и regression-проверку. Если оценить нельзя — останови конкретную подгруппу как blocker, не маскируй риск.

### Update actions с предварительным release intelligence

${buildTable(actionRows)}

\\`\\`\\`json
${actionJson}
\\`\\`\\`

## 5. Реализация и проверочный контракт

1. Сними baseline: resolved versions, package manager/lockfile, audit C/H/M/L из roadmap, outdated, scripts и текущий status релевантных test/build/typecheck/lint команд.
2. Поле \\`testPolicy\\` в manifest является legacy-именем для уровня проверки, а не требованием писать новые тесты. Для \\`focused-check-required\\` определи concrete behavior/config/build invariant, usage/config files и существующую команду, которая лучше всего подтверждает миграцию. Для \\`standard-check-allowed\\` достаточно ordinary project checks после доказательства, что пакет не влияет на CI/build/release/runtime.
3. Сначала переиспользуй существующие unit/integration/typecheck/lint/build/storybook/config validation gates. Не добавляй новые dependency-regression tests, отдельные regression folders/scripts, mutation probes или искусственные fixtures без явного запроса пользователя.
4. Если существующего покрытия недостаточно, не блокируй всю миграцию ради искусственного теста: зафиксируй \\`verificationGap\\` и residual risk в \\`docs/dependency-update-review-notes.md\\`, run JSON и итоговом ответе; усиливай проверку доступными проектными командами.
5. Tooling migration проверяй воспроизводимой существующей командой: config validation, typecheck, lint, build или smoke script, если он уже есть в проекте. Не выдумывай отсутствующие команды и не переписывай существующие тесты.
6. Фиксируй baseline failures отдельно от новых regressions. Зеленый install сам по себе не закрывает runtime/API риск, но отсутствие идеального теста не является причиной писать бесполезный generated test.
7. После финального merge повторно выполни ordinary tests/checks и fresh OSV/outdated сравнение. Ручной audit запускай только если его запросил пользователь.
## 6. Машинная финальная приёмка target → fact

На mergedBranch обязательно запусти:

\\`\\`\\`bash
python "${REPORT_CONTEXT.validatorPath || 'tools/dependency-roadmap-tool/validate_dependency_update.py'}" \\
  --roadmap-json "${REPORT_CONTEXT.roadmapJsonPath || '.dependency-roadmap/artifacts/dependency-roadmap.json'}" \\
  --project "<project name из branch plan>" \\
  --project-dir "<projectDir из branch plan или текущий checkout>" \\
  --target-mode "${mode}" \\
  --scope-manifest "<точный *_scope.json, сохранённый из этого промпта>" \\
  --final-roadmap-json "<fresh roadmap json, сгенерированный на mergedBranch>" \\
  --require-final-status \\
  --strict-above-target \\

\\`\\`\\`

Если tool лежит в другом месте — найди его, но не пропускай validation. Результат должен быть PASS без error. Warning \\`above-target\\` разрешён только с документированным peer/registry/changelog основанием.

Дополнительно проверь scope completeness: все ${actionRows.length} update-строк присутствуют в final fact, ни одна не ниже target; перечисли все отклонения и все direct dependency изменения вне manifest. Непредусмотренные изменения — ошибка, пока не объяснены.

После merge обязательно заново сгенерируй roadmap JSON из фактического checkout обычной командой \\`generate.* --only-project <project>\\`. Без \\`--capture-baseline\\` generator анализирует текущую ветку и не переключается на sourceBranch. Deprecated \\`--skip-audit-bootstrap\\` ничего не делает. \\`--require-final-status\\` должен подтвердить требуемый status. Если fresh roadmap содержит новые action rows, работа НЕ завершена: создай closure branch и повтори цикл либо зафиксируй blocker.

## 7. Долговременная память и before/after

Ничего не перетирай. Добавь:

- append-only \\`events.jsonl\\` event;
- \\`runs/<timestamp>_<project>.json\\` и \\`.md\\`;
- строку в \\`index.md\\`;
- при доступности — новый dashboard snapshot.

Run JSON должен содержать: scopeHash, manifest counts, codeCommentPolicy, branchPlan, commits/merges/conflicts, packages from/requestedTarget/fact, releaseAnalysis по каждой зависимости, commands, OSV/outdated before/after, optional manual-audit reference, verification, filesChanged, docsUpdated, residualRisks и nextSteps.

## 8. Итоговый ответ

Не пиши просто “готово”. Дай:

- exact scope counts и hash; доказательство, что ни одна update/deferred строка не потеряна;
- ветки/коммиты/merge order/conflicts;
- \\`package: current → target → fact\\`;
- для каждого пакета breaking status, migration/deprecation notes, sources и покрытый release range;
- C/H/M/L и lag-policy до/после;
- verificationPolicy, использованные существующие проверки/gates и gaps;
- команды и результаты;
- validator PASS/fail;
- ссылки на все три migration docs; отдельно перечисли доказанные критичные исправления, реальную пользу, новые возможности, миграционные поломки/фиксы, residual risks, deferred count=${deferredRows.length} и следующие шаги.

Обязательная итоговая таблица:

| Package | Current → Target → Fact | Breaking / migration summary | Sources / coverage | Verification / gap | Audit/lag result | Branch | Result |
|---|---|---|---|---|---|---|---|
`;
}

function compactScopeManifestForRows(rows, mode, now){
  const projects = Array.from(new Set(rows.map(r => r.project))).sort();
  const commentPolicy = codeCommentPolicy();
  const manifestRows = rows.map(scopeManifestRowFor);
  const actionRows = manifestRows.filter(r => r.shouldUpdate);
  const excludedRows = manifestRows.filter(r => r.scopeExcluded);
  const projectScopes = Object.fromEntries(projects.map(project => {
    const projectRows = rows.filter(r => r.project === project);
    const projectManifestRows = projectRows.map(scopeManifestRowFor);
    const projectActions = projectManifestRows.filter(r => r.shouldUpdate);
    const projectExcluded = projectManifestRows.filter(r => r.scopeExcluded);
    return [project, {
      selectedRows: projectRows.length,
      actionRows: projectActions.length,
      deferredRows: projectRows.length - projectActions.length - projectExcluded.length,
      excludedRows: projectExcluded.length,
      scopeHash: simpleScopeHash(projectRows)
    }];
  }));
  return {
    schemaVersion: 2,
    scopeHashVersion: 5,
    format: 'compact-v1',
    generatedAt: now,
    registry: REPORT_CONTEXT.registry || '',
    registryPolicy: {
      artifactSource: 'configured-registry-only',
      requireTargetTarball: true,
      forbidForeignPackageManagerUrls: true
    },
    targetMode: mode,
    codeCommentPolicy: commentPolicy.mode,
    scopeHash: simpleScopeHash(rows),
    selectedRows: rows.length,
    actionRows: actionRows.length,
    deferredRows: rows.length - actionRows.length - excludedRows.length,
    excludedRows: excludedRows.length,
    projects,
    projectScopes,
    proofEnvelopes: proofEnvelopesForProjects(projects, mode),
    columns: ['project','group','subgroup','kind','package','section','requestedSpec','current','target','targetReason','shouldUpdate','action','lagPolicyMonths','lagPolicyTarget','targetArtifactStatus','targetArtifactUrl','targetArtifactError','compatibilityCohort','compatibilityNote','scopeExcluded','exclusionReason','exclusionSource','testPolicy','testReason'],
    rows: manifestRows.map(r => [
      r.project,
      r.group,
      r.subgroup,
      r.kind,
      r.package,
      r.section,
      r.requestedSpec,
      r.current,
      r.target,
      r.targetReason,
      r.shouldUpdate,
      r.action,
      r.lagPolicyMonths,
      r.lagPolicyTarget,
      r.targetArtifactStatus,
      r.targetArtifactUrl,
      r.targetArtifactError,
      r.compatibilityCohort,
      r.compatibilityNote,
      r.scopeExcluded,
      r.exclusionReason,
      r.exclusionSource,
      r.testPolicy,
      r.testReason
    ])
  };
}
function compactBranchPlanForRows(rows){
  return gitPlanForRows(rows).map(plan => ({
    project: plan.project,
    projectDir: plan.projectDir,
    source: plan.sourceBranch,
    sourceCommit: plan.sourceCommit,
    base: plan.baseBranch,
    merged: plan.mergedBranch,
    release: plan.releaseBranch,
    gitHooks: plan.gitHooks,
    releasePolicy: plan.release,
    push: plan.push,
    manualAudit: plan.manualAudit,
    branches: plan.branches.map(branch => ({order: branch.order, branch: branch.branch, packages: branch.packages}))
  }));
}
function buildCompactPromptFromCurrentView(){
  applyFilters();
  const rows = promptRowsForExport();
  if (!rows.length) return '';
  const projects = Array.from(new Set(rows.map(r => r.project))).sort();
  const mode = document.getElementById('targetMode')?.value || 'default';
  const now = new Date().toISOString();
  const actions = rows.filter(r => !r.scopeExcluded && r.hasTarget && isActionTargetValue(r.target));
  const excluded = rows.filter(r => r.scopeExcluded).length;
  const deferred = rows.length - actions.length - excluded;
  const scopeHash = simpleScopeHash(rows);
  const manifest = compactScopeManifestForRows(rows, mode, now);
  const branchPlan = compactBranchPlanForRows(rows);
  const commentPolicy = codeCommentPolicy();
  const manifestJson = JSON.stringify(manifest);
  const branchPlanJson = JSON.stringify(branchPlan, null, 2);
  const criticalReleaseDossier = compactCriticalReleaseDossierForRows(actions);
  const criticalReleaseJson = JSON.stringify({
    releaseIntelligence: criticalReleaseDossier,
    priorMigrationKnowledge: knowledgeForRows(actions),
    knowledgePolicy: 'Validate applicability and verification on the current checkout; never apply blindly.'
  }, null, 2);
  const roadmapPath = REPORT_CONTEXT.roadmapJsonPath || '.dependency-roadmap/artifacts/dependency-roadmap.json';
  const historyDir = REPORT_CONTEXT.historyDir || DEP_HISTORY_DIR;
  const runbookPath = REPORT_CONTEXT.agentRunbookPath || 'AGENT_RUNBOOK.md';
  const validatorPath = REPORT_CONTEXT.validatorPath || 'tools/dependency-roadmap-tool/validate_dependency_update.py';
  const releaseBranchToolPath = REPORT_CONTEXT.releaseBranchToolPath || 'tools/dependency-roadmap-tool/dependency_release_branch.py';
  const gitHookPolicyToolPath = REPORT_CONTEXT.gitHookPolicyToolPath || 'tools/dependency-roadmap-tool/git_hook_policy.py';
  const stateTemplate = `${historyDir}/runs/<timestamp>_<project>_state.json`;
  const scopeTemplate = `${historyDir}/runs/<timestamp>_<project>_scope.json`;
  const multiProjectWarning = projects.length > 1
    ? `\n> Выбрано несколько проектов: ${projects.join(', ')}. Обрабатывай их строго последовательно и создавай отдельный state-файл для каждого проекта. Для маленького контекста безопаснее экспортировать по одному проекту.\n`
    : '';

  if (!actions.length) {
    return `# Dependency roadmap — compact no-op task

Project: ${projects.join(', ')}
Target mode: ${mode}
Scope: selected=${rows.length}, update=0, deferred=${deferred}, excluded=${excluded}, hash=${scopeHash}
${multiProjectWarning}
1. Сохрани compact manifest ниже без изменений в \\`${scopeTemplate}\\`.
2. Сверь package.json по ключу \\`project + section + package\\` и hash. При несовпадении: \\`SCOPE_MANIFEST_MISMATCH\\`.
3. Ничего не меняй. Запиши append-only no-op event в \\`${historyDir}\\`.

\\`\\`\\`json
${manifestJson}
\\`\\`\\`
`;
  }

  return `# Dependency roadmap — compact autonomous task

Project: ${projects.join(', ')}
Target mode: ${mode}
Scope: selected=${rows.length}, update=${actions.length}, deferred=${deferred}, excluded=${excluded}, hash=${scopeHash}
Registry: ${REPORT_CONTEXT.registry || 'project config'}
Roadmap facts: ${roadmapPath}
Mandatory protocol: ${runbookPath} (read it before any edits; rules below are only a compact index)
History: ${historyDir}
Code comment policy: ${commentPolicy.mode}
${multiProjectWarning}
## Code comments and migration docs

${commentPolicy.instruction}

Carry \\`codeCommentPolicy=${commentPolicy.mode}\\` into every run state/evidence checkpoint and preserve it during \\`CONTINUE_REQUIRED\\` continuation.

Maintain three tracked migration docs **per work branch** during the run, one shard file per branch so sibling branches never conflict on the same path when merged into \\`merged\\`:
- \\`docs/dependency-upgrades/<branch>.md\\`: this branch's complete technical current→target changelog: versions, breaking changes, migration/deprecation notes, requirements, changed API/config, compatibility fixes, verification, gaps and source links.
- \\`docs/dependency-update-summary/<branch>.md\\`: this branch's human-facing outcome. Explain in plain language which critical bugs or vulnerabilities were actually fixed, the evidence-backed benefit for product/business/developer workflow, newly available capabilities, what broke during migration and how it was repaired. Never invent benefit; mark unknown impact explicitly.
- \\`docs/dependency-update-review-notes/<branch>.md\\`: this branch's complete review map. For every non-lockfile code/config/package-manifest change record path, package, why the change was necessary, release/source evidence, verification command, and why it is not unrelated refactoring.
Replace \\`<branch>\\` with the exact work branch name from the Branch plan below (e.g. for branch \\`libs-group-1\\`, write \\`docs/dependency-upgrades/libs-group-1.md\\`). Start each shard file with a \\`## <branch>\\` heading naming this exact branch, then write its content under that heading. Create the shard directory/file if missing. **Never write to the flat \\`docs/dependency-upgrades.md\\` (or its two siblings) directly** — \\`${releaseBranchToolPath}\\` assembles every branch's shard into those flat files automatically when it creates the release branch; writing the flat file yourself recreates the exact \\`add/add\\` merge conflict this sharding exists to avoid. Do not dump lockfile noise into these docs.
## Rules

1. Save the compact manifest below **unchanged** to \\`${scopeTemplate}\\`; validator supports \\`compact-v1\\`.
2. Before edits, verify every row by \\`project + section + package\\`, counts and hash. \\`lagPolicyMonths\\`, \\`lagPolicyTarget\\`, \\`targetReason\\`, target artifact proof, compatibility cohort and exclusion reason are hash-protected. On mismatch stop with \\`SCOPE_MANIFEST_MISMATCH\\`. If a non-excluded policy stricter than 12 months has current below a concrete \\`lagPolicyTarget\\` but the row is deferred, stop with \\`ROADMAP_TARGET_DESYNC\\`.
3. Change only rows with \\`shouldUpdate=true\\` and execute immutable \\`action=update|remove\\`. Rows with \\`action=excluded\\` are explicit user blockers: do not edit them, do not add them to branches, and preserve their \\`exclusionReason\\` in evidence. Every update row must have \\`targetArtifactStatus=available\\` and a tarball URL under exactly \\`${REPORT_CONTEXT.registry || 'the configured project registry'}\\`. Metadata/maintainers from \\`yarn info\\` are not proof that the tarball exists. If install cannot read the exact target tarball, or a lockfile/redirect points to npmjs/yarnpkg/another package registry, stop with \\`REGISTRY_TARGET_UNAVAILABLE\\` or \\`FOREIGN_REGISTRY_URL\\`; **do not choose another version, revert the package, or edit the immutable manifest yourself**. Storybook/cohort constraints are also immutable: incompatibility is a planner blocker, not permission for an ad-hoc substitution. For \\`remove\\`, delete the direct declaration and stale lock entry instead of installing a deprecated stub. Never silently drop deferred or excluded rows. **NO_REFACTORING:** no opportunistic cleanup, rename/move/split, architecture/public-API change, broad formatting/autofix-only diffs, unrelated code removal, or existing-test rewrite. Make only minimal package-required compatibility changes backed by release/migration evidence; otherwise stop with \\`REFACTOR_REQUIRED\\`. Every non-lockfile change needs a short rationale entry in \\`docs/dependency-update-review-notes.md\\`.
4. Follow the branch plan. Verify/create \\`base\\` exactly from the pinned \\`sourceCommit\\`; automatic audit bootstrap is not part of generation. Start every work branch from that exact base. Use only the project package manager: Yarn → \\`yarn.lock\\`; npm → \\`package-lock.json/npm-shrinkwrap.json\\`. Never create a cross-manager root lockfile. Every intermediate commit, merge and internal push MUST run through \\`${gitHookPolicyToolPath} --mode skip\\`; do not use ordinary \\`git commit\\` and do not rely on \\`--no-verify\\`. Merge successful branches in order into \\`merged\\`; no destructive reset/force-push. After every merge, reconcile all cumulative package actions against the immutable manifest; a lost target or resurrected remove action is \\`MERGE_TARGET_REGRESSION\\`.
5. Before editing a package, read its entry in the **Critical release dossier below**, then read the full target-specific release intelligence from \\`${roadmapPath}\\`. The dossier is mandatory, not decorative. Cover current→target; record \\`breakingChanges\\`, \\`migrationNotes\\`, \\`deprecations\\`, \\`requirements\\`, \\`coverage\\` and \\`sources\\` in run evidence. No source means \\`unknown\\`, not “no breaking changes”.
6. Verification policy in the manifest is immutable even though the field is still named \\`testPolicy\\` for backwards compatibility. \\`focused-check-required\\` means: map the package to concrete usage/config files and run existing focused project checks where available. It does **not** mean “write a new regression test”.
7. Do not create new dependency-regression tests, dedicated regression folders/scripts, mutation probes or artificial fixtures unless the user explicitly asks for them. Prefer existing unit/integration/typecheck/lint/build/storybook/config validation commands. A generic green \\`yarn test\\` is useful but must be paired with package-level rationale for risky runtime/API/config migrations.
8. For each branch: baseline → install → focused migration → relevant existing lint/build/typecheck/test checks → fresh OSV/outdated diff → update migration docs → intermediate commit through the skip-hooks wrapper. Manual audit runs only on explicit user request. Do not merge a red branch.
9. Store verification evidence in a tracked run JSON: package→usage/config files, rationale, release findings, structured baseline/post results, commands, verification gaps and residual risks. Do not paste raw logs into chat.
10. After every branch write/update \\`${stateTemplate}\\` with \\`codeCommentPolicy=${commentPolicy.mode}\\`, completed branch, commits, package facts, release findings, commands/results, verification evidence, docsUpdated, remaining branches and next action. Include \\`migrationOutcome.status=ready|repair-required|replan-required\\`; baseline-green → post-red is never ready, and CROSS-COHORT may defer a failure only when the referenced companion group exists in the current Branch plan.
11. If context becomes large, stop only at a clean checkpoint and return \\`CONTINUE_REQUIRED: <state-file>\\`. A fresh session should receive only: \\`Continue dependency roadmap run from <state-file>. Read the compact scope file referenced there and execute nextAction; do not replay prior chat.\\`
12. On \\`merged\\`, run full ordinary project checks and ordinary \\`generate.*\\` so the canonical project lockfile is validated/refreshed and a fresh roadmap is produced. Vulnerability audit is manual-only via \\`manual_dependency_audit.py\\` and must not create or commit a cross-manager project lockfile. Finalize through \\`${releaseBranchToolPath}\\`: create \\`release\\` from verified sourceCommit, squash merged, remove any legacy tool audit workspace, run fresh-roadmap/target→fact/full project gates as \\`--gate-command\\`; if a gate leaves unstaged/untracked files, stop with \\`RELEASE_FINAL_GATE_DIRTY\\`. Only then make the one normal release commit with repository hooks enabled. Hook failure is a blocker and must never be bypassed. Any newly actionable row means the run is not complete: return to a closure branch before release.

## Definition of done

Оркестратор после завершения сессии проверяет ровно эти пункты. Пока хотя бы один не выполнен, задача не закрыта, и продолжать нужно внутри исходного Branch plan:

1. Каждая ветка из Branch plan является предком merged (git merge-base --is-ancestor <branch> <merged> возвращает 0). Ветка, где работа сделана, но merge не выполнен, завершённой не считается.
2. Сам не создавай новые work-ветки или targets вне Branch plan. Если Git уже содержит дополнительную ветку от прошлой residual/continuation-итерации, не merge/delete/reset её и не зови пользователя: сохрани ref/diff как evidence и верни управление Supervisor'у для автономного adoption/quarantine.
3. В merged каждая строка manifest с shouldUpdate=true фактически достигла своего target в package.json, а для action=remove объявление отсутствует. Проверяется именно merged, а не текущая рабочая ветка: выполненная, но не влитая группа в этой проверке не засчитывается.
4. В рабочем дереве нет незакоммиченных отслеживаемых изменений.
5. HEAD оставлен на merged или на release-ветке, чтобы финальная проверка читала итоговое состояние.

## Critical release dossier

Mandatory preliminary BREAKING/migration facts for actionable packages. An empty array means no critical preliminary entry was selected; it does **not** remove the per-package roadmap check. \\`breakingChanges\\`/\\`migrationNotes\\`/\\`deprecations\\`/\\`requirements\\`/\\`sources\\` below are truncated previews (first entries only) to keep this prompt small; read the untruncated per-package entry from \\`${roadmapPath}\\` before editing that package, as rule 5 requires.

\\`\\`\\`json
${criticalReleaseJson}
\\`\\`\\`

## Branch plan

\\`\\`\\`json
${branchPlanJson}
\\`\\`\\`

## Intermediate Git and release commands

Use values from the branch plan:

\\`\\`\\`bash
python "${gitHookPolicyToolPath}" --project-dir "<projectDir>" --mode skip -- commit -m "<message>"
python "${gitHookPolicyToolPath}" --project-dir "<projectDir>" --mode skip -- merge --no-ff "<workBranch>"

python "${releaseBranchToolPath}" \\
  --project-dir "<projectDir>" \\
  --source-branch "<source>" \\
  --source-commit "<sourceCommit>" \\
  --merged-branch "<merged>" \\
  --release-branch "<release>" \\
  --gate-command "<full final gate command>"
\\`\\`\\`
## Exact compact scope manifest

\\`\\`\\`json
${manifestJson}
\\`\\`\\`

## Final validation

For each project on the staged/final release branch (or read-only after the release commit):

\\`\\`\\`bash
python "${validatorPath}" \\
  --roadmap-json "${roadmapPath}" \\
  --project "<project>" \\
  --project-dir "<projectDir>" \\
  --target-mode "${mode}" \\
  --scope-manifest "<saved compact scope json>" \\
  --final-roadmap-json "<fresh roadmap json generated from merged state>" \\
  --require-final-status \\
  --strict-above-target \\

\\`\\`\\`

Read \\`${runbookPath}\\` before any edits. It is mandatory and may strengthen this compact index; do not paste it into chat.
`;
}

// One project's Branch plan can span several branches; buildCompactPromptFromCurrentView
// hands the agent the whole plan and lets it own branch creation/merge itself
// across a single, ever-growing session. This instead scopes everything --
// manifest rows, release dossier, branch plan JSON, doc-write paths -- to
// exactly one already-chosen branch, for an orchestrator that creates/checks
// out that branch itself, runs a short fresh agent session against just this
// prompt, and performs the merge on its own once the branch is done. Reuses
// the exact same manifest/branch-plan/dossier builders as the full compact
// prompt against a pre-filtered row set -- no new filtering, hashing or
// truncation logic.
function buildGroupScopedCompactPrompt(project, branch, packageSubset){
  applyFilters();
  const mode = document.getElementById('targetMode')?.value || 'default';
  const now = new Date().toISOString();
  const projectRows = projectTargetPromptRowsForProject(project);
  if (!projectRows.length) throw new Error(`GROUP_NOT_FOUND: ${project}`);
  assertPlanningConsistency(projectRows);
  const fullPlan = gitPlanForRows(projectRows).find(p => p.project === project);
  const freshBranchEntry = fullPlan?.branches.find(b => b.branch === branch);
  const hasPinnedPackageScope = Array.isArray(packageSubset) && packageSubset.length;
  // Saved Branch-plan package membership is the semantic identity.  A fresh
  // residual roadmap may legitimately repartition/rename cosmetic buckets;
  // in that case the old branch name is not a reason to throw away completed
  // work or to force another replan.  When the orchestrator supplies the
  // reviewed package set, rebuild exactly that scope and let manifest target
  // comparison decide whether anything material actually changed.
  const requestedPackages = hasPinnedPackageScope
    ? Array.from(new Set(packageSubset.map(String))).sort()
    : (freshBranchEntry?.packages || []);
  if (!requestedPackages.length) throw new Error(`GROUP_NOT_FOUND: ${project}/${branch}`);
  const selectedPackages = new Set(requestedPackages);
  const rows = projectRows.filter(r => selectedPackages.has(r.name));
  const foundPackages = new Set(rows.map(r => r.name));
  const missingPackages = requestedPackages.filter(name => !foundPackages.has(name));
  if (missingPackages.length) throw new Error(`BATCH_SCOPE_DRIFT: ${project}/${branch}: saved package scope is missing ${missingPackages.join(', ')}`);
  const branchEntry = freshBranchEntry && !hasPinnedPackageScope
    ? freshBranchEntry
    : {order: 1, bucket: 'pinned-package-scope', branch, packages: requestedPackages};
  const actions = rows.filter(r => !r.scopeExcluded && r.hasTarget && isActionTargetValue(r.target));
  const scopeHash = simpleScopeHash(rows);
  const manifest = compactScopeManifestForRows(rows, mode, now);
  const branchPlan = compactBranchPlanForRows(rows);
  const scopedPlan = branchPlan.find(p => p.project === project);
  if (scopedPlan) {
    // A single-branch prompt must preserve the reviewed logical branch identity
    // even when fresh automatic buckets were renamed/repartitioned.
    scopedPlan.branches = [{order: 1, branch, packages: requestedPackages}];
  }
  const commentPolicy = codeCommentPolicy();
  const manifestJson = JSON.stringify(manifest);
  const branchPlanJson = JSON.stringify(branchPlan, null, 2);
  const criticalReleaseDossier = compactCriticalReleaseDossierForRows(actions);
  const criticalReleaseJson = JSON.stringify({
    releaseIntelligence: criticalReleaseDossier,
    priorMigrationKnowledge: knowledgeForRows(actions),
    knowledgePolicy: 'Validate applicability and verification on the current checkout; never apply blindly.'
  }, null, 2);
  const roadmapPath = REPORT_CONTEXT.roadmapJsonPath || '.dependency-roadmap/artifacts/dependency-roadmap.json';
  const historyDir = REPORT_CONTEXT.historyDir || DEP_HISTORY_DIR;
  const runbookPath = REPORT_CONTEXT.agentRunbookPath || 'AGENT_RUNBOOK.md';
  const gitHookPolicyToolPath = REPORT_CONTEXT.gitHookPolicyToolPath || 'tools/dependency-roadmap-tool/git_hook_policy.py';
  const releaseBranchToolPath = REPORT_CONTEXT.releaseBranchToolPath || 'tools/dependency-roadmap-tool/dependency_release_branch.py';
  const stateTemplate = `${historyDir}/runs/<timestamp>_${project}_${branch}_state.json`;
  const scopeTemplate = `${historyDir}/runs/<timestamp>_${project}_${branch}_scope.json`;

  return `# Dependency roadmap — compact single-branch task

Project: ${project}
Branch: ${branch} (${branchEntry.bucket})
Target mode: ${mode}
Scope: this branch only — ${rows.length} rows, update=${actions.length}, hash=${scopeHash}${Array.isArray(packageSubset) && packageSubset.length ? `, execution batch=${requestedPackages.length}/${branchEntry.packages.length}` : ''}
Registry: ${REPORT_CONTEXT.registry || 'project config'}
Roadmap facts: ${roadmapPath}
Mandatory protocol: ${runbookPath} (read it before any edits; rules below are only a compact index)
History: ${historyDir}
Code comment policy: ${commentPolicy.mode}

## Orchestrator owns branches and merge

You are already on branch \\`${branch}\\`, created by the orchestrator from the correct base. This is the **only** branch you touch:

- Do not create, switch to or delete any other branch.
- Do not run \\`git merge\\`, \\`git checkout <other-branch>\\` or \\`${releaseBranchToolPath}\\`. The orchestrator merges this branch into \\`merged\\` and eventually creates the release once every branch in the plan is done — that is outside this task.
- Every intermediate commit on this branch still MUST run through \\`${gitHookPolicyToolPath} --mode skip\\`; do not use ordinary \\`git commit\\` and do not rely on \\`--no-verify\\`.
- Leave HEAD on \\`${branch}\\` when you stop. Leaving it clean and committed is what lets the orchestrator merge it.

## Code comments and migration docs

${commentPolicy.instruction}

Carry \\`codeCommentPolicy=${commentPolicy.mode}\\` into every run state/evidence checkpoint and preserve it during \\`CONTINUE_REQUIRED\\` continuation.

Maintain three tracked migration docs for this branch, one shard file each — sibling branches write their own shards under the same directories, so writing here can never conflict with them when the orchestrator merges everything later:
- \\`docs/dependency-upgrades/${branch}.md\\`: this branch's complete technical current→target changelog: versions, breaking changes, migration/deprecation notes, requirements, changed API/config, compatibility fixes, verification, gaps and source links.
- \\`docs/dependency-update-summary/${branch}.md\\`: this branch's human-facing outcome. Explain in plain language which critical bugs or vulnerabilities were actually fixed, the evidence-backed benefit for product/business/developer workflow, newly available capabilities, what broke during migration and how it was repaired. Never invent benefit; mark unknown impact explicitly.
- \\`docs/dependency-update-review-notes/${branch}.md\\`: this branch's complete review map. For every non-lockfile code/config/package-manifest change record path, package, why the change was necessary, release/source evidence, verification command, and why it is not unrelated refactoring.
Start each shard file with a \\`## ${branch}\\` heading, then write its content under that heading. Create the shard directory/file if missing. ${Array.isArray(packageSubset) && packageSubset.length ? '**This is an execution batch inside an already-running branch:** before editing a shard, read its existing content and preserve all entries/evidence written by earlier batches. Add or update only the current batch packages; never truncate, recreate from scratch, or remove earlier package sections just because they are absent from this subset manifest. The final shard must remain cumulative for the whole branch. ' : ''}**Never write to the flat \\`docs/dependency-upgrades.md\\` (or its two siblings) directly** — the orchestrator assembles every branch's shard into those flat files once, after every branch is merged. Do not dump lockfile noise into these docs.

## Rules

1. Save the compact manifest below **unchanged** to \\`${scopeTemplate}\\`; validator supports \\`compact-v1\\`.
2. Before edits, verify every row by \\`project + section + package\\`, counts and hash. \\`lagPolicyMonths\\`, \\`lagPolicyTarget\\`, \\`targetReason\\`, target artifact proof, compatibility cohort and exclusion reason are hash-protected. On mismatch stop with \\`SCOPE_MANIFEST_MISMATCH\\`.
3. Change only rows with \\`shouldUpdate=true\\` and execute immutable \\`action=update|remove\\`. The deterministic control plane may already have materialized the exact direct targets for the whole compatibility branch before this execution batch starts; when a row is already at target, **do not rewrite/revert it and do not undo other already-materialized package targets merely because they are absent from this smaller source-migration batch**. Every update row must have \\`targetArtifactStatus=available\\` and a tarball URL under exactly \\`${REPORT_CONTEXT.registry || 'the configured project registry'}\\`. Metadata/maintainers from \\`yarn info\\` are not proof that the tarball exists. If install cannot read the exact target tarball, or a lockfile/redirect points to npmjs/yarnpkg/another package registry, stop with \\`REGISTRY_TARGET_UNAVAILABLE\\` or \\`FOREIGN_REGISTRY_URL\\`; **do not choose another version, revert the package, edit overrides/resolutions, or edit the immutable manifest yourself**. For \\`remove\\`, delete the direct declaration only when it has not already been materialized by the control plane. **NO_REFACTORING:** no opportunistic cleanup, rename/move/split, architecture/public-API change, broad formatting/autofix-only diffs, unrelated code removal, or existing-test rewrite. Make only minimal package-required source/config compatibility changes backed by release/migration evidence; otherwise stop with \\`REFACTOR_REQUIRED\\`. Every non-lockfile change needs a short rationale entry in \\`docs/dependency-update-review-notes/${branch}.md\\`.
4. Before editing a package, read its entry in the **Critical release dossier below**, then read the full target-specific release intelligence from \\`${roadmapPath}\\`. The dossier is mandatory, not decorative. Cover current→target; record \\`breakingChanges\\`, \\`migrationNotes\\`, \\`deprecations\\`, \\`requirements\\`, \\`coverage\\` and \\`sources\\` in run evidence. No source means \\`unknown\\`, not “no breaking changes”.
5. Verification policy in the manifest is immutable even though the field is still named \\`testPolicy\\` for backwards compatibility. \\`focused-check-required\\` means: map the package to concrete usage/config files and run existing focused project checks where available. It does **not** mean “write a new regression test”.
6. Do not create new dependency-regression tests, dedicated regression folders/scripts, mutation probes or artificial fixtures unless the user explicitly asks for them. Prefer existing unit/integration/typecheck/lint/build/storybook/config validation commands.
7. Order: inspect the already-materialized dependency state → run install only when needed to prepare local tools, without changing the approved assignment → focused source/config migration of every row in scope → relevant existing lint/build/typecheck/test checks → fresh OSV/outdated diff → update this branch's migration doc shards → intermediate commit through the skip-hooks wrapper. **Do not stop at the first failed project check:** diagnose failures caused by this dependency update, make the smallest in-scope source/config compatibility fix, and rerun the failed check until it passes. If a type/build/loader failure points to an incompatible dependency or transitive resolution, collect concise evidence but **do not edit direct targets, overrides/resolutions, or choose a different transitive version**. Set \\`migrationOutcome.status=replan-required\\`; Desktop persists structured compatibility evidence and sends it through deterministic candidate-vs-control reproduction/localization before exact Z3 may change the assignment. Never bypass, suppress or silently omit a failing check. Prefer fewer, larger shell invocations over many single-purpose ones when inspecting files: every additional tool call resends accumulated session context. Manual audit runs only on explicit user request.
8. Store verification evidence in a tracked run JSON: package→usage/config files, rationale, release findings, **structured baseline/post command results**, verification gaps and residual risks. Do not paste raw logs into chat. For every project check use an object like \\`{"cmd":"yarn lint:types","baseline":"exit 0","post":"exit 0"}\\` whenever baseline is known.
9. Write/update \\`${stateTemplate}\\` with \\`codeCommentPolicy=${commentPolicy.mode}\\`, commits, package facts, release findings, commands/results, verification evidence and docsUpdated once your work on this branch is complete or you stop. The state MUST contain \\`migrationOutcome\\` with exactly one machine status: \\`{"status":"ready","reason":"..."}\\`, \\`{"status":"repair-required","reason":"..."}\\`, or \\`{"status":"replan-required","reason":"..."}\\`. Use \\`ready\\` only when no check that was green at baseline is red post-update. Use \\`repair-required\\` while an in-scope source/config migration is still possible without changing dependency assignment. Use \\`replan-required\\` when passing the checks requires changing an immutable target/excluded/deferred direct dependency, adding/removing a direct dependency, or editing overrides/resolutions/transitive dependency selection. Treat that status as dependency evidence for the deterministic Planner/Verifier; it is not permission to choose another version locally. A CROSS-COHORT explanation is valid only if that companion group is actually present in the Branch plan below; otherwise it is \\`replan-required\\`, never permission to commit a red branch.
10. If context becomes large before this branch's scope is fully done, stop only at a clean checkpoint and return \\`CONTINUE_REQUIRED: <state-file>\\`. A fresh session should receive only: \\`Continue this branch's dependency roadmap work from <state-file>. Read the compact scope file referenced there and execute nextAction; do not replay prior chat.\\`

## Definition of done

The orchestrator checks exactly these points after your session ends:

1. Every scope manifest row below with \\`shouldUpdate=true\\` has reached its \\`target\\` in \\`package.json\\` (or, for \\`action=remove\\`, the declaration is gone) — installed on this branch.
2. No other branch or group was created.
3. Working tree is clean: every change is committed through the skip-hooks wrapper.
4. HEAD is left on \\`${branch}\\`.
5. Every applicable existing build/typecheck/lint/test check selected for the changed packages has a passing final result in the run evidence; an earlier failure is acceptable only when the evidence also contains the successful rerun. A check that was already red at baseline may remain documented as a pre-existing failure, but a baseline-green → post-red transition is always a migration regression and cannot be waived by prose.
6. \\`migrationOutcome.status=ready\\`. The Desktop independently re-runs configured/discovered verification commands and will return failures to you before merge, so do not claim ready merely because package targets and Git cleanliness are satisfied.

## Critical release dossier

Mandatory preliminary BREAKING/migration facts for actionable packages on this branch. An empty array means no critical preliminary entry was selected; it does **not** remove the per-package roadmap check. \\`breakingChanges\\`/\\`migrationNotes\\`/\\`deprecations\\`/\\`requirements\\`/\\`sources\\` below are truncated previews (first entries only) to keep this prompt small; read the untruncated per-package entry from \\`${roadmapPath}\\` before editing that package, as rule 4 requires.

\\`\\`\\`json
${criticalReleaseJson}
\\`\\`\\`

## Branch plan

\\`\\`\\`json
${branchPlanJson}
\\`\\`\\`

## Intermediate Git commands

\\`\\`\\`bash
python "${gitHookPolicyToolPath}" --project-dir "<projectDir>" --mode skip -- commit -m "<message>"
\\`\\`\\`

## Exact compact scope manifest

\\`\\`\\`json
${manifestJson}
\\`\\`\\`

Read \\`${runbookPath}\\` before any edits. It is mandatory and may strengthen this compact index; do not paste it into chat.
`;
}
function taskSpecHealthLine(project){
  const h = liveProjectHealth?.[project] || REPORT_CONTEXT.projectHealth?.[project] || {};
  const status = h.status || 'unknown';
  return `Статус: **${status}**; lag policy выполнена для ${Number(h.lag_ok_pct ?? 0).toFixed(1)}%; ` +
    `уязвимости C/H/M/L: ${h.critical || 0}/${h.high || 0}/${h.moderate || 0}/${h.low || 0}. ` +
    `${h.reason || ''}`.trim();
}
function taskSpecRowTable(rows){
  if (!rows.length) return '_Нет строк._';
  return [
    '| Группа | Зависимость | Секция | Текущая → цель | Уязвимости | Зачем / что проверить | Breaking / migration |',
    '|---:|---|---|---|---|---|---|',
    ...rows.map(r => {
      const intel = releaseIntelForPrompt(r);
      const releaseNote = `${releaseStatusLabel(intel.status)}: ${intel.summary || '—'}`;
      return `| ${escapeMdCell(r.group)}${r.subgroup ? ' / ' + escapeMdCell(r.subgroup) : ''} | \\`${escapeMdCell(r.name)}\\` | ${escapeMdCell(sectionForKind(r.kind))} | \\`${escapeMdCell(r.current)}\\` → \\`${escapeMdCell(r.target)}\\` | ${escapeMdCell(r.vulns)} | ${escapeMdCell(r.targetReason || r.vulnNote || r.reason)} | ${escapeMdCell(releaseNote)} |`;
    })
  ].join('\\n');
}
function buildTaskSpecification(){
  applyFilters();
  const rows = projectTargetPromptRows();
  const actions = rows.filter(r => !r.scopeExcluded && r.hasTarget && isActionTargetValue(r.target));
  const excluded = rows.filter(r => r.scopeExcluded);
  const deferred = rows.filter(r => !r.scopeExcluded && !(r.hasTarget && isActionTargetValue(r.target)));
  const projects = Array.from(new Set(rows.map(r => r.project))).sort();
  const plans = gitPlanForRows(rows);
  const mode = document.getElementById('targetMode')?.value || 'default';
  const scopeProject = document.getElementById('projectFilter')?.value || 'все выбранные проекты';
  const scopeGroup = document.getElementById('groupFilter')?.value || 'все группы';
  let out = `# Техническое задание: обновление зависимостей\n\n`;
  out += `**Сформировано:** ${new Date().toISOString()}  \n`;
  out += `**Проектный scope:** ${scopeProject}; ${scopeGroup}  \n`;
  out += `**Цель:** ${targetModeTitle()} (${mode})  \n`;
  out += `**Registry:** \\`${REPORT_CONTEXT.registry || 'не указан'}\\`\n\n`;
  out += `## Цель задачи\n\nОбновить выбранные зависимости до рассчитанных целевых версий, устранить соответствующие риски безопасности и технологическое отставание, сохранить работоспособность продукта, сборки, CI и релизного процесса. Не делать несвязанный рефакторинг.\n\n`;
  out += `## Исходное состояние\n\n`;
  for (const project of projects) out += `### ${project}\n\n${taskSpecHealthLine(project)}\n\n`;
  out += `## Объём работ\n\n- К обновлению/удалению: **${actions.length}**.\n- Осознанно исключено из задачи: **${excluded.length}**.\n- Отложено планировщиком: **${deferred.length}**.\n\n`;
  out += `### Что требуется изменить\n\n${taskSpecRowTable(actions)}\n\n`;
  if (excluded.length) {
    out += `### Явно не входит в текущую задачу\n\n`;
    out += excluded.map(r => `- \\`${r.name}\\` (${r.project}): ${r.exclusionReason || 'причина не указана'}.`).join('\\n') + '\\n\\n';
  }
  if (deferred.length) {
    out += `### Отложенные зависимости\n\n`;
    out += deferred.map(r => `- \\`${r.name}\\` (${r.project}, группа ${r.group}): ${r.targetReason || r.reason || 'target не назначен'}.`).join('\\n') + '\\n\\n';
  }
  out += `## План выполнения\n\n`;
  for (const plan of plans) {
    out += `### ${plan.project}\n\n`;
    if (!plan.branches?.length) out += `_Рабочие ветки не требуются._\n\n`;
    else for (const branch of plan.branches) out += `1. Ветка \\`${branch.branch}\\`: ${branch.packages.map(x => `\\`${x}\\``).join(', ')}.\n`;
    out += `\nИтоговая ветка: \\`${plan.releaseBranch || 'release'}\\`.\n\n`;
  }
  const risky = actions.filter(r => ['breaking-confirmed','breaking-likely','coverage-incomplete','unavailable','not-checked-limit','not-checked-target'].includes(releaseIntelForPrompt(r).status));
  out += `## Риски и обязательные проверки\n\n`;
  if (risky.length) out += risky.map(r => `- \\`${r.name}\\` ${r.current} → ${r.target}: ${releaseIntelForPrompt(r).summary || releaseIntelForPrompt(r).status}.`).join('\\n') + '\\n\\n';
  else out += `_Критичные release-intelligence риски в выбранном scope не отмечены._\n\n`;
  out += `- Перед изменением проверить доступность точного target tarball в настроенном Nexus.\n`;
  out += `- Для каждой зависимости определить затронутое поведение/конфигурацию и выполнить релевантные проверки до и после обновления.\n`;
  out += `- После каждой ветки выполнить install только штатным package manager проекта, focused tests, lint/build/typecheck и свежую проверку OSV/outdated.\n`;
  out += `- Не менять исключённые и deferred-строки без отдельного пересогласования scope.\n`;
  out += `- После объединения веток перегенерировать roadmap и убедиться, что target совпадает с фактической версией, а новые actionable-строки не появились.\n\n`;
  out += `## Критерии приёмки\n\n`;
  out += `- Все строки из раздела «Что требуется изменить» доведены до указанных target или оформлен доказанный blocker.\n`;
  out += `- package.json и канонический lockfile согласованы; в lockfile нет package-artifact URL вне настроенного registry.\n`;
  out += `- Регрессионные проверки, обычные тесты, сборка, линтер и typecheck проходят в применимой части.\n`;
  out += `- Breaking changes, migration notes, требования окружения и остаточные риски зафиксированы в результате задачи.\n`;
  out += `- Финальный свежий dashboard показывает фактическое состояние после изменений и может быть сравнён с исходным снимком.\n`;
  return out;
}

function buildPromptFromCurrentView(){
  const format = document.getElementById('promptFormat')?.value || 'compact';
  return format === 'full' ? buildFullPromptFromCurrentView() : buildCompactPromptFromCurrentView();
}

function openPromptModal(prompt, title){
  const modal = document.getElementById('promptModal');
  const heading = document.getElementById('promptModalTitle');
  const textarea = document.getElementById('promptText');
  const meta = document.getElementById('promptMeta');
  if (heading && title) heading.textContent = title;
  const description = document.getElementById('promptDescription');
  if (description) description.textContent = title === 'Техническое задание'
    ? 'Человеко-читаемая постановка задачи: scope, план, риски, проверки и критерии приёмки. Это не исполняемый агентский контракт.'
    : (title && title.includes('Package')
      ? 'Ручной package.json patch и пояснения. Перед применением требуется сверка с текущим checkout и lockfile.'
      : 'Сформирован по выбранному scope и цели. Агентский prompt содержит строгий manifest и правила выполнения.');
  if (textarea) textarea.value = prompt;
  if (meta) {
    const approxTokens = Math.max(1, Math.ceil(prompt.length / 4));
    meta.textContent = `Размер: ${prompt.length.toLocaleString()} символов, примерно ${approxTokens.toLocaleString()} токенов.`;
  }
  if (modal) {
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
  }
}
function closePromptModal(){
  const modal = document.getElementById('promptModal');
  if (modal) {
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
  }
}
async function copyPrompt(){
  const text = document.getElementById('promptText')?.value || '';
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    showToast('Текст скопирован');
  } catch(e) {
    const ta = document.getElementById('promptText');
    if (ta) { ta.focus(); ta.select(); }
    showToast('Не удалось скопировать автоматически — выделил текст');
  }
}
function downloadPrompt(){
  const text = document.getElementById('promptText')?.value || '';
  if (!text) return;
  const project = document.getElementById('projectFilter')?.value || 'selected';
  const group = document.getElementById('groupFilter')?.value || 'all-groups';
  const mode = document.getElementById('targetMode')?.value || 'default';
  const safe = (project + '-' + group + '-' + mode).replace(/[^a-zA-Z0-9а-яА-Я_-]+/g, '-').replace(/-+/g, '-');
  const blob = new Blob([text], {type: 'text/markdown;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const title = document.getElementById('promptModalTitle')?.textContent || '';
  const format = document.getElementById('promptFormat')?.value || 'compact';
  const prefix = title.includes('Техническое задание') ? 'dependency-task-spec-' : (title.includes('Package') ? 'dependency-package-json-proposals-' : `dependency-agent-prompt-${format}-`);
  a.download = prefix + safe + '.md';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  showToast('Markdown скачан');
}
// Every Branch plan group as (project, branch) pairs, ignoring the
// project/group filter dropdowns entirely -- this control lets a user
// inspect the group-scoped prompt for ANY branch regardless of what the
// rest of the dashboard happens to be filtered to right now.
function allBranchOptions(){
  const mode = document.getElementById('targetMode')?.value || 'default';
  const rows = Array.from(document.querySelectorAll('tr.dep-row')).map(row => rowToPromptData(row, mode));
  const projects = Array.from(new Set(rows.map(r => r.project))).sort();
  return projects.flatMap(project => {
    const projectRows = rows.filter(r => r.project === project);
    const plan = gitPlanForRows(projectRows).find(p => p.project === project);
    return (plan?.branches || []).map(b => ({project, branch: b.branch, bucket: b.bucket, count: b.packages.length}));
  });
}
function refreshGroupPromptOptions(){
  const select = document.getElementById('groupPromptBranch');
  if (!select) return;
  const previous = select.value;
  const options = allBranchOptions();
  select.textContent = '';
  if (!options.length) {
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = 'Нет веток с actionable scope';
    select.appendChild(empty);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const option of options) {
    const el = document.createElement('option');
    el.value = `${option.project}::${option.branch}`;
    el.textContent = `${option.project} — ${option.branch} (${option.count})`;
    select.appendChild(el);
  }
  if ([...select.options].some(o => o.value === previous)) select.value = previous;
}
function initPromptExport(){
  refreshGroupPromptOptions();
  const groupPromptSelect = document.getElementById('groupPromptBranch');
  if (groupPromptSelect) groupPromptSelect.addEventListener('mousedown', refreshGroupPromptOptions);
  const groupPromptBtn = document.getElementById('exportGroupPromptBtn');
  if (groupPromptBtn) groupPromptBtn.addEventListener('click', () => {
    try {
      const selected = document.getElementById('groupPromptBranch')?.value || '';
      const separator = selected.indexOf('::');
      if (separator < 0) {
        showToast('Нет доступной ветки для промпта');
        return;
      }
      const project = selected.slice(0, separator);
      const branch = selected.slice(separator + 2);
      const prompt = buildGroupScopedCompactPrompt(project, branch);
      openPromptModal(prompt, `Промпт для ветки ${branch}`);
    } catch (error) {
      console.error(error);
      showToast(String(error?.message || error));
    }
  });
  const exportBtn = document.getElementById('exportPromptBtn');
  if (exportBtn) exportBtn.addEventListener('click', () => {
    try {
      const prompt = buildPromptFromCurrentView();
      if (!prompt) {
        showToast('Нет видимых строк для промпта');
        return;
      }
      const format = document.getElementById('promptFormat')?.value || 'compact';
      openPromptModal(prompt, format === 'full' ? 'Полный промпт для агента' : 'Компактный промпт для агента');
    } catch (error) {
      console.error(error);
      showToast(String(error?.message || error));
    }
  });
  const taskSpecBtn = document.getElementById('exportTaskSpecBtn');
  if (taskSpecBtn) taskSpecBtn.addEventListener('click', () => {
    try {
      const content = buildTaskSpecification();
      openPromptModal(content, 'Техническое задание');
    } catch (error) {
      console.error(error);
      showToast(String(error?.message || error));
    }
  });
  const packagePatchBtn = document.getElementById('exportPackagePatchBtn');
  if (packagePatchBtn) packagePatchBtn.addEventListener('click', () => {
    try {
      const content = buildPackageJsonExportFromCurrentView();
      if (!content) {
        showToast('Нет данных для package.json export');
        return;
      }
      openPromptModal(content, 'Package.json patch и предложения');
    } catch (error) {
      console.error(error);
      showToast(String(error?.message || error));
    }
  });
  const closeBtn = document.getElementById('closePromptBtn');
  if (closeBtn) closeBtn.addEventListener('click', closePromptModal);
  const copyBtn = document.getElementById('copyPromptBtn');
  if (copyBtn) copyBtn.addEventListener('click', copyPrompt);
  const downloadBtn = document.getElementById('downloadPromptBtn');
  if (downloadBtn) downloadBtn.addEventListener('click', downloadPrompt);
  const modal = document.getElementById('promptModal');
  if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) closePromptModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePromptModal(); });
}

const DASHBOARD_STATE_KEY = 'dependency-roadmap-dashboard-state-v2:' + (REPORT_CONTEXT.dashboardStatePath || 'default');
const EMBEDDED_DASHBOARD_STATE = JSON.parse(JSON.stringify(REPORT_CONTEXT.dashboardState || {schemaVersion: 1, packageOverrides: {}}));
function stateFingerprint(value){
  const text = JSON.stringify(value || {});
  let hash = 2166136261;
  for (let i=0;i<text.length;i++) { hash ^= text.charCodeAt(i); hash = Math.imul(hash, 16777619); }
  return ('00000000' + (hash >>> 0).toString(16)).slice(-8);
}
const EMBEDDED_DASHBOARD_STATE_HASH = stateFingerprint(EMBEDDED_DASHBOARD_STATE);
let dashboardState = JSON.parse(JSON.stringify(EMBEDDED_DASHBOARD_STATE));
let activeSettingsRow = null;
let liveProjectHealth = {};

function escapeHtmlText(value){
  return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function safeExternalUrl(value){
  try {
    const url = new URL(String(value || ''));
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '#';
  } catch(e) { return '#'; }
}
function openModalById(id){
  const modal = document.getElementById(id);
  if (modal) { modal.classList.add('open'); modal.setAttribute('aria-hidden','false'); }
}
function closeModalById(id){
  const modal = document.getElementById(id);
  if (modal) { modal.classList.remove('open'); modal.setAttribute('aria-hidden','true'); }
}
function ensureProjectState(project){
  if (!dashboardState.packageOverrides) dashboardState.packageOverrides = {};
  if (!dashboardState.packageOverrides[project]) dashboardState.packageOverrides[project] = {};
  return dashboardState.packageOverrides[project];
}
// Must match desktop/electron/main.ts's DASHBOARD_SCHEME constant. When the
// desktop app embeds this report in its own "Dashboard" tab (an <iframe>
// loaded via this custom protocol, see DashboardWorkspace.tsx), the app's
// own will-download handler (setupDownloads in main.ts) silently places any
// download named "dashboard-state*" at the project's real, tracked
// dashboard-state.json -- no save dialog, no manual "export then move the
// file to the right path" step a user has to know about or remember to
// repeat after every change. Opened any other way (double-clicked file:,
// a plain browser) there is no such interception, so auto-triggering a
// download there would just spam the user's Downloads folder on every
// toggle -- that case keeps relying on the explicit "Экспортировать state"
// button instead.
const DASHBOARD_EMBEDDED_PROTOCOL = 'dependency-flow-dashboard:';
function autoPersistDashboardStateToDisk(recalculate=false){
  if (location.protocol !== DASHBOARD_EMBEDDED_PROTOCOL) return;
  try {
    const text = JSON.stringify(dashboardState, null, 2) + '\\n';
    const blob = new Blob([text], {type:'application/json;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = recalculate ? 'dashboard-state-recalculate.json' : 'dashboard-state.json';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch(e) {}
}
function saveDashboardStateLocal(recalculate=false){
  try {
    localStorage.setItem(DASHBOARD_STATE_KEY, JSON.stringify({
      schemaVersion: 1,
      baseStateHash: EMBEDDED_DASHBOARD_STATE_HASH,
      savedAt: new Date().toISOString(),
      state: dashboardState
    }));
  } catch(e) {}
  autoPersistDashboardStateToDisk(recalculate);
}
function showScopeChangeToast(message){
  const reminder = location.protocol === DASHBOARD_EMBEDDED_PROTOCOL
    ? ' Сохранено в dashboard-state.json — roadmap и targets пересчитаются автоматически.'
    : ' Нажмите «Экспортировать state», иначе изменение не сохранится.';
  showToast(message + reminder, 4200);
}
function loadDashboardStateLocal(){
  try {
    const raw = localStorage.getItem(DASHBOARD_STATE_KEY);
    if (!raw) return;
    const draft = JSON.parse(raw);
    const draftOverrides = draft?.state?.packageOverrides;
    if (!draftOverrides || typeof draftOverrides !== 'object') return;
    // Merge per-project/per-package instead of replacing dashboardState
    // wholesale. A hash mismatch only means the tracked baseline moved on
    // since this draft was saved (e.g. Верификация ran); it says nothing
    // about whether the draft's *own* overrides are still valid, since
    // packageOverrides is keyed by package name/kind, not tied to which
    // packages the current roadmap happens to include. The old
    // all-or-nothing replace discarded the entire draft on any mismatch --
    // so excluding one more package after a baseline regeneration could
    // silently drop every exclusion made before it. Draft entries win on
    // conflict, since they represent the most recent user intent.
    if (draft.baseStateHash !== EMBEDDED_DASHBOARD_STATE_HASH) {
      showToast('Локальный черновик относится к старому tracked state — слит поверх текущего, ничего не потеряно.');
    }
    if (!dashboardState.packageOverrides) dashboardState.packageOverrides = {};
    for (const project of Object.keys(draftOverrides)) {
      const draftProject = draftOverrides[project];
      if (!draftProject || typeof draftProject !== 'object') continue;
      const target = dashboardState.packageOverrides[project] = dashboardState.packageOverrides[project] || {};
      for (const key of Object.keys(draftProject)) target[key] = draftProject[key];
    }
    if (draft.state?.promptOptions) dashboardState.promptOptions = {...(dashboardState.promptOptions || {}), ...draft.state.promptOptions};
  } catch(e) { showToast('Локальный state повреждён — использую tracked state отчёта'); }
}
function rowStateKey(row){ return `${row.dataset.kind}:${row.dataset.name}`; }
function rowOverride(row){
  const projectState = dashboardState.packageOverrides?.[row.dataset.project] || {};
  return projectState[rowStateKey(row)] || projectState[row.dataset.name] || null;
}
function moveRowToGroup(row, group){
  const projectSection = row.closest('.project-section');
  if (!projectSection) return;
  const targetBody = projectSection.querySelector(`.group-section[data-group-section="${group}"] tbody`);
  if (targetBody && row.parentElement !== targetBody) targetBody.appendChild(row);
}
function refreshRowVisuals(row){
  const subgroup = row.dataset.subgroup || '';
  const badge = row.querySelector('.subgroup-badge:not(.scope-excluded-badge)');
  if (badge) {
    badge.textContent = subgroup || 'без подгруппы';
    badge.classList.toggle('muted', !subgroup);
  }
  const meta = row.querySelector('.row-meta');
  let excludedBadge = row.querySelector('.scope-excluded-badge');
  if (row.dataset.scopeExcluded === '1') {
    if (!excludedBadge && meta) {
      excludedBadge = document.createElement('span');
      excludedBadge.className = 'subgroup-badge scope-excluded-badge';
      excludedBadge.textContent = 'исключено';
      meta.insertBefore(excludedBadge, meta.querySelector('.row-settings-btn'));
    }
    if (excludedBadge) excludedBadge.title = row.dataset.exclusionReason || '';
  } else if (excludedBadge) {
    excludedBadge.remove();
  }
  row.classList.toggle('scope-excluded', row.dataset.scopeExcluded === '1');
  const active = Number(row.dataset.lagThresholdMonths || 12);
  row.querySelectorAll('.lag-line').forEach(line => {
    const months = Number((line.querySelector('span')?.textContent || '').replace(/\\D/g,''));
    line.classList.toggle('lag-policy-active', months === active);
  });
}
function applyOverrideToRow(row, override, persist=false){
  const originalGroup = Number(row.dataset.originalGroup || row.dataset.group || 5);
  const originalSubgroup = row.dataset.originalSubgroup || '';
  const originalLag = Number(row.dataset.originalLagThresholdMonths || 12);
  const originalNotes = row.dataset.originalNotes || '—';
  const originalExcluded = row.dataset.originalScopeExcluded === '1';
  const originalExclusionReason = row.dataset.originalExclusionReason || '';
  const group = Number(override?.group || originalGroup);
  const subgroup = String(override?.subgroup ?? originalSubgroup);
  const lagMonths = Number(override?.lagMonths || override?.lagThresholdMonths || originalLag);
  const note = String(override?.note || override?.reason || '').trim();
  const excluded = override && Object.prototype.hasOwnProperty.call(override, 'excluded')
    ? !!override.excluded
    : (override && Object.prototype.hasOwnProperty.call(override, 'excludeFromScope')
      ? !!override.excludeFromScope
      : originalExcluded);
  const exclusionReason = String(
    override?.exclusionReason ?? override?.excludeReason ?? originalExclusionReason
  ).trim();
  row.dataset.group = String(Number.isInteger(group) && group > 0 ? group : originalGroup);
  row.dataset.subgroup = subgroup;
  row.dataset.lagThresholdMonths = String([3,6,9,12].includes(lagMonths) ? lagMonths : originalLag);
  row.dataset.notes = note ? (originalNotes.includes(note) ? originalNotes : `${originalNotes}; комментарий команды: ${note}`) : originalNotes;
  row.dataset.scopeExcluded = excluded ? '1' : '0';
  row.dataset.exclusionReason = excluded ? exclusionReason : '';
  row.dataset.exclusionSource = excluded ? String(override?.exclusionSource || 'dashboard-state') : '';
  moveRowToGroup(row, row.dataset.group);
  refreshRowVisuals(row);
  if (persist) saveDashboardStateLocal(true);
}
function savePromptOptionsToDashboardState(){
  dashboardState.promptOptions = {
    ...(dashboardState.promptOptions || {}),
    detailedCodeComments: detailedCodeCommentsEnabled()
  };
  saveDashboardStateLocal();
}
function applyDashboardState(){
  document.querySelectorAll('tr.dep-row').forEach(row => applyOverrideToRow(row, rowOverride(row), false));
  const detailedComments = dashboardState.promptOptions?.detailedCodeComments;
  const detailedCommentsCheckbox = document.getElementById('detailedCodeComments');
  if (detailedCommentsCheckbox && typeof detailedComments === 'boolean') detailedCommentsCheckbox.checked = detailedComments;
  applyFilters();
}
function openRowSettings(row){
  activeSettingsRow = row;
  const override = rowOverride(row) || {};
  document.getElementById('settingsIdentity').textContent = `${row.dataset.project} · ${row.dataset.name} · ${row.dataset.kind}`;
  document.getElementById('settingsGroup').value = String(override.group || row.dataset.group || 5);
  document.getElementById('settingsSubgroup').value = override.subgroup ?? row.dataset.subgroup ?? '';
  document.getElementById('settingsLag').value = String(override.lagMonths || row.dataset.lagThresholdMonths || 12);
  document.getElementById('settingsExcluded').checked = Object.prototype.hasOwnProperty.call(override, 'excluded')
    ? !!override.excluded
    : row.dataset.scopeExcluded === '1';
  document.getElementById('settingsExclusionReason').value = override.exclusionReason ?? row.dataset.exclusionReason ?? '';
  document.getElementById('settingsNote').value = override.note || '';
  openModalById('settingsModal');
}
function settingsFormValue(){
  const excluded = document.getElementById('settingsExcluded').checked;
  const exclusionReason = document.getElementById('settingsExclusionReason').value.trim();
  if (excluded && !exclusionReason) throw new Error('Для исключения укажите причину');
  return {
    group: Number(document.getElementById('settingsGroup').value),
    subgroup: document.getElementById('settingsSubgroup').value.trim(),
    lagMonths: Number(document.getElementById('settingsLag').value),
    excluded,
    excludeFromScope: excluded,
    exclusionReason: excluded ? exclusionReason : '',
    exclusionSource: 'dashboard-state',
    note: document.getElementById('settingsNote').value.trim(),
    updatedAt: new Date().toISOString()
  };
}
function isDependencyRowVisible(row){
  const projectSection = row.closest('.project-section');
  const groupSection = row.closest('.group-section');
  return !row.classList.contains('row-hidden') &&
    !(projectSection && projectSection.classList.contains('project-hidden')) &&
    !(groupSection && groupSection.classList.contains('group-hidden'));
}
function selectedDependencyRows(){
  return Array.from(document.querySelectorAll('tr.dep-row')).filter(row => row.querySelector('.row-select')?.checked);
}
function updateSelectionControls(){
  // Selection must never outlive visibility: a row checked before the filter
  // changed stays checked while hidden, so a later "Исключить выбранные"
  // would silently apply a reason meant for the visible rows to packages the
  // user can no longer see. Dropping hidden rows keeps the counter and the
  // batch action describing the same set.
  document.querySelectorAll('tr.dep-row').forEach(row => {
    const input = row.querySelector('.row-select');
    if (input && input.checked && !isDependencyRowVisible(row)) input.checked = false;
  });
  const selected = selectedDependencyRows();
  const count = selected.length;
  const counter = document.getElementById('selectedRowsCounter');
  if (counter) counter.textContent = `Выбрано: ${count}`;
  ['excludeSelectedBtn','includeSelectedBtn','clearSelectionBtn'].forEach(id => {
    const button = document.getElementById(id);
    if (button) button.disabled = count === 0;
  });
  document.querySelectorAll('.table-select-visible').forEach(master => {
    const table = master.closest('table');
    const visible = Array.from(table?.querySelectorAll('tbody tr.dep-row') || []).filter(row => !row.classList.contains('row-hidden'));
    const checked = visible.filter(row => row.querySelector('.row-select')?.checked).length;
    master.checked = visible.length > 0 && checked === visible.length;
    master.indeterminate = checked > 0 && checked < visible.length;
  });
}
function clearDependencySelection(){
  document.querySelectorAll('.row-select').forEach(input => { input.checked = false; });
  updateSelectionControls();
}
function currentOverrideValueForRow(row){
  const existing = rowOverride(row) || {};
  return {
    ...existing,
    group: Number(row.dataset.group || row.dataset.originalGroup || 5),
    subgroup: row.dataset.subgroup || '',
    lagMonths: Number(row.dataset.lagThresholdMonths || 12),
    note: existing.note || '',
    updatedAt: new Date().toISOString()
  };
}
function setSelectedRowsExcluded(excluded){
  const rows = selectedDependencyRows();
  if (!rows.length) return;
  let reason = '';
  if (excluded) {
    reason = String(window.prompt(`Причина исключения для ${rows.length} зависимостей:`, 'Отложено на отдельную задачу.') || '').trim();
    if (!reason) return showToast('Исключение отменено: нужна причина.');
  }
  for (const row of rows) {
    const projectState = ensureProjectState(row.dataset.project);
    const value = currentOverrideValueForRow(row);
    value.excluded = excluded;
    value.excludeFromScope = excluded;
    value.exclusionReason = excluded ? reason : '';
    value.exclusionSource = excluded ? 'dashboard-state' : '';
    projectState[rowStateKey(row)] = value;
    applyOverrideToRow(row, value, false);
  }
  saveDashboardStateLocal(true);
  clearDependencySelection();
  applyFilters();
  showScopeChangeToast(excluded ? `Исключено зависимостей: ${rows.length}.` : `Возвращено в расчёт: ${rows.length}.`);
}
function saveActiveRowSettings(){
  if (!activeSettingsRow) return;
  let value;
  try { value = settingsFormValue(); }
  catch (e) { showToast(e.message); return; }
  const projectState = ensureProjectState(activeSettingsRow.dataset.project);
  projectState[rowStateKey(activeSettingsRow)] = value;
  applyOverrideToRow(activeSettingsRow, projectState[rowStateKey(activeSettingsRow)], true);
  applyFilters();
  closeModalById('settingsModal');
  showScopeChangeToast('Override применён.');
}
function applySettingsToVisibleRows(){
  let value;
  try { value = settingsFormValue(); }
  catch (e) { showToast(e.message); return; }
  const rows = Array.from(document.querySelectorAll('tr.dep-row')).filter(row => {
    const projectSection = row.closest('.project-section');
    const groupSection = row.closest('.group-section');
    return !row.classList.contains('row-hidden') &&
      !(projectSection && projectSection.classList.contains('project-hidden')) &&
      !(groupSection && groupSection.classList.contains('group-hidden'));
  });
  for (const row of rows) {
    const projectState = ensureProjectState(row.dataset.project);
    projectState[rowStateKey(row)] = {...value};
    applyOverrideToRow(row, projectState[rowStateKey(row)], false);
  }
  saveDashboardStateLocal(true);
  applyFilters();
  closeModalById('settingsModal');
  showScopeChangeToast(`Override применён к строкам: ${rows.length}.`);
}
function clearActiveRowSettings(){
  if (!activeSettingsRow) return;
  const project = activeSettingsRow.dataset.project;
  const key = rowStateKey(activeSettingsRow);
  if (dashboardState.packageOverrides?.[project]) {
    delete dashboardState.packageOverrides[project][key];
    delete dashboardState.packageOverrides[project][activeSettingsRow.dataset.name];
  }
  applyOverrideToRow(activeSettingsRow, null, true);
  applyFilters();
  closeModalById('settingsModal');
  showScopeChangeToast('Override сброшен.');
}
async function exportDashboardState(){
  const text = JSON.stringify(dashboardState, null, 2) + '\\n';
  const suggestedName = 'dashboard-state.json';
  if (window.showSaveFilePicker) {
    try {
      const handle = await window.showSaveFilePicker({suggestedName, types:[{description:'JSON',accept:{'application/json':['.json']}}]});
      const writable = await handle.createWritable();
      await writable.write(text);
      await writable.close();
      showToast('dashboard-state.json сохранён');
      return;
    } catch(e) {
      if (e && e.name === 'AbortError') return;
    }
  }
  const blob = new Blob([text], {type:'application/json;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href=url; a.download=suggestedName; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  showToast('State скачан — положите его в путь из settings');
}
async function importDashboardStateFile(file){
  try {
    const parsed = JSON.parse(await file.text());
    if (!parsed || typeof parsed !== 'object' || typeof parsed.packageOverrides !== 'object') throw new Error('packageOverrides missing');
    dashboardState = parsed;
    saveDashboardStateLocal(true);
    applyDashboardState();
    showToast('Настройки импортированы');
  } catch(e) { showToast('Не удалось импортировать state: ' + e.message); }
}
function openReleaseDetails(row){
  const mode = document.getElementById('targetMode')?.value || 'default';
  const selectedTarget = targetForRow(row, mode).value;
  const data = releaseForTarget(row, selectedTarget);
  document.getElementById('releaseModalTitle').textContent = `${data.package || row.dataset.name}: ${data.current || row.dataset.current} → ${data.target || selectedTarget || '—'}`;
  document.getElementById('releaseModalSummary').textContent = data.summary || row.dataset.releaseSummary || '—';
  const list = (title, items) => `<h3>${escapeHtmlText(title)}</h3>${items?.length ? `<ul class="detail-list">${items.map(x=>`<li>${escapeHtmlText(x)}</li>`).join('')}</ul>` : '<p class="muted">Нет найденных пунктов.</p>'}`;
  const sources = (data.sources || []).map(x => `<li><a href="${escapeHtmlText(safeExternalUrl(x.url))}" target="_blank" rel="noopener noreferrer">${escapeHtmlText(x.title || x.kind || x.url)}</a> <span class="muted">${escapeHtmlText(x.kind || '')}</span></li>`).join('');
  document.getElementById('releaseModalBody').innerHTML = `
    <p><strong>Статус:</strong> ${escapeHtmlText(data.status || row.dataset.releaseStatus || 'unknown')}</p>
    ${list('Breaking changes', data.breakingChanges)}
    ${list('Migration notes', data.migrationNotes)}
    ${list('Deprecations', data.deprecations)}
    ${list('Requirements / compatibility', data.requirements)}
    <p><strong>Покрытие:</strong> ${escapeHtmlText(data.coverage || 'не подтверждено')}</p>
    <h3>Источники</h3>${sources ? `<ul class="detail-list">${sources}</ul>` : '<p class="muted">Источники недоступны. Нельзя считать отсутствие breaking changes доказанным.</p>'}
    <div class="prompt-actions"><button type="button" class="secondary" onclick="closeModalById('releaseModal')">Закрыть</button></div>`;
  openModalById('releaseModal');
}
const DASHBOARD_SNAPSHOT_KEY = 'dependency-roadmap-dashboard-snapshots-v2:' + (REPORT_CONTEXT.dashboardStatePath || DEP_HISTORY_DIR || 'default');
let browserHistorySnapshots = [];

function parseJsonData(value, fallback={}){
  try { return JSON.parse(value || '{}'); } catch(e) { return fallback; }
}
function normalizeSnapshotRow(row){
  const release = row.release || row.release_intelligence || {};
  const lagThresholdMonths = Number(row.lagThresholdMonths || row.lag_threshold_months || 12);
  const lagPolicyTarget = row.lagPolicyTarget || row.lag_target || row[`min_lag_${lagThresholdMonths}m`] || row[`minLag${lagThresholdMonths}m`] || row.min_lag_12m || '—';
  return {
    name: row.name || row.package || '—',
    kind: row.kind || 'runtime',
    requestedSpec: row.requestedSpec || row.requested_spec || '—',
    current: row.current || row.current_version || '—',
    latest: row.latest || row.latest_version || '—',
    target: row.target || row.targetDefault || row.target_default || '—',
    targetReason: row.targetReason || row.targetDefaultReason || row.target_default_reason || row.reason || '—',
    targetDefault: row.targetDefault || row.target_default || row.target || '—',
    targetYellow: row.targetYellow || row.target_yellow || '—',
    targetGreen: row.targetGreen || row.target_green || '—',
    vulnerabilities: row.vulnerabilities || row.vulns || row.current_vulns || '0',
    group: Number(row.group || 0),
    subgroup: row.subgroup || '',
    lagThresholdMonths,
    lagPolicyTarget,
    minLag12m: row.minLag12m || row.min_lag_12m || '—',
    minLag9m: row.minLag9m || row.min_lag_9m || '—',
    minLag6m: row.minLag6m || row.min_lag_6m || '—',
    minLag3m: row.minLag3m || row.min_lag_3m || '—',
    releaseStatus: row.releaseStatus || row.release_status || release.status || 'not-checked-target',
    releaseSummary: row.releaseSummary || row.release_summary || release.summary || '—',
    reason: row.reason || '—',
    notes: row.notes || '—',
    vulnNote: row.vulnNote || row.vulnerability_work_note || '—',
    scopeExcluded: !!(row.scopeExcluded ?? row.scope_excluded),
    exclusionReason: row.exclusionReason || row.exclusion_reason || '',
    compatibilityCohort: row.compatibilityCohort || row.compatibility_cohort || '',
    compatibilityNote: row.compatibilityNote || row.compatibility_note || '',
    release
  };
}
function snapshotRowFromDom(row, mode){
  const base = rowToPromptData(row, mode);
  const releasePayload = parseJsonData(row.dataset.releaseJson, {});
  const registryArtifacts = parseJsonData(row.dataset.registryArtifactsJson, {});
  return {
    ...base,
    vulnerabilities: base.vulns,
    targetDefault: row.dataset.targetDefault || '—',
    targetYellow: row.dataset.targetYellow || '—',
    targetGreen: row.dataset.targetGreen || '—',
    targetDefaultReason: row.dataset.targetDefaultReason || '—',
    targetYellowReason: row.dataset.targetYellowReason || '—',
    targetGreenReason: row.dataset.targetGreenReason || '—',
    releaseByTarget: releasePayload.byTarget || {},
    registryArtifacts
  };
}
function currentDashboardSnapshot(label=''){
  const mode = document.getElementById('targetMode')?.value || 'default';
  const projectRows = {};
  for (const row of document.querySelectorAll('tr.dep-row')) {
    const project = row.dataset.project || 'unknown';
    if (!projectRows[project]) projectRows[project] = [];
    projectRows[project].push(snapshotRowFromDom(row, mode));
  }
  const projects = {};
  for (const [project, dependencies] of Object.entries(projectRows)) {
    projects[project] = {
      health: JSON.parse(JSON.stringify(liveProjectHealth?.[project] || REPORT_CONTEXT.projectHealth?.[project] || {})),
      baselineComparison: JSON.parse(JSON.stringify(REPORT_CONTEXT.baselineComparisons?.[project] || null)),
      git: JSON.parse(JSON.stringify(REPORT_CONTEXT.projectGit?.[project] || {})),
      suggestions: JSON.parse(JSON.stringify(REPORT_CONTEXT.suggestions?.byProject?.[project] || [])),
      dependencies
    };
  }
  const filters = {};
  for (const id of ['q','projectFilter','groupFilter','kindFilter','targetMode','statusFilter','promptScope','promptFormat','detailedCodeComments','onlyVuln','onlyCH','onlyResidual','onlyTargetRows','hideSatisfiedProjects']) {
    const el = document.getElementById(id);
    if (el) filters[id] = el.type === 'checkbox' ? !!el.checked : el.value;
  }
  filters.columns = Object.fromEntries(Array.from(document.querySelectorAll('[data-col]')).map(el => [el.dataset.col, !!el.checked]));
  const capturedAt = new Date().toISOString();
  return {
    schemaVersion: 2,
    type: 'dependency-roadmap-dashboard-snapshot',
    id: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `${capturedAt}-${Math.random().toString(16).slice(2)}`,
    capturedAt,
    label: label || '',
    source: 'browser',
    generatedAt: REPORT_CONTEXT.generatedAt || '',
    registry: REPORT_CONTEXT.registry || '',
    targetMode: mode,
    filters,
    dashboardState: JSON.parse(JSON.stringify(dashboardState || {})),
    globalSuggestions: JSON.parse(JSON.stringify(REPORT_CONTEXT.suggestions?.global || [])),
    summary: {projects:Object.keys(projects).length, dependencies:Object.values(projectRows).reduce((n, rows) => n + rows.filter(row => !row.scopeExcluded).length, 0)},
    projects
  };
}
function loadBrowserHistorySnapshots(){
  try {
    const parsed = JSON.parse(localStorage.getItem(DASHBOARD_SNAPSHOT_KEY) || '[]');
    browserHistorySnapshots = Array.isArray(parsed) ? parsed.filter(item => item && item.projects) : [];
  } catch(e) { browserHistorySnapshots = []; }
}
function saveBrowserHistorySnapshots(){
  const originalCount = browserHistorySnapshots.length;
  for (let keep = Math.min(30, originalCount); keep >= 1; keep--) {
    try {
      const retained = browserHistorySnapshots.slice(0, keep);
      localStorage.setItem(DASHBOARD_SNAPSHOT_KEY, JSON.stringify(retained));
      browserHistorySnapshots = retained;
      if (keep < originalCount) showToast(`Browser storage: сохранено ${keep} последних снимков, старые удалены по лимиту`);
      return true;
    } catch(e) {
      if (keep === 1) console.error(e);
    }
  }
  showToast('Снимок создан только в текущей вкладке — скачайте его JSON из истории');
  return false;
}
function allHistorySnapshots(){
  const embedded = (REPORT_CONTEXT.historySnapshots || []).map((item, index) => ({...item, _source:'file', _key:item.id || item._file || `embedded-${index}-${item.capturedAt || ''}`}));
  const browser = browserHistorySnapshots.map((item, index) => ({...item, _source:'browser', _key:item.id || `browser-${index}-${item.capturedAt || ''}`}));
  const result = [];
  const seen = new Set();
  for (const item of [...browser, ...embedded].sort((a,b) => String(b.capturedAt || '').localeCompare(String(a.capturedAt || '')))) {
    const key = item.id || `${item.capturedAt || ''}|${item.label || ''}|${item._file || ''}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(item);
  }
  return result;
}
function saveCurrentDashboardSnapshot(openHistory=false){
  const defaultLabel = `${document.getElementById('projectFilter')?.value || 'all-projects'} · ${targetModeTitle()}`;
  const label = window.prompt('Название снимка (например: Demo.App baseline, после группы 2)', defaultLabel);
  if (label === null) return;
  const snapshot = currentDashboardSnapshot(label.trim());
  browserHistorySnapshots.unshift(snapshot);
  browserHistorySnapshots = browserHistorySnapshots.slice(0, 30);
  saveBrowserHistorySnapshots();
  showToast('Полный снимок дашборда сохранён');
  if (openHistory) openHistoryBrowser(snapshot.id);
}
function dependencyMap(snapshot, project){
  const rows = snapshot?.projects?.[project]?.dependencies || [];
  return new Map(rows.map(raw => {
    const row = normalizeSnapshotRow(raw);
    return [`${row.name}|${row.kind}`, row];
  }));
}
function meaningfulSnapshotRow(row){
  if (!row) return null;
  return {
    current: row.current, latest: row.latest, target: row.target, targetReason: row.targetReason,
    vulnerabilities: row.vulnerabilities, group: row.group, subgroup: row.subgroup,
    lagThresholdMonths: row.lagThresholdMonths, lagPolicyTarget: row.lagPolicyTarget,
    releaseStatus: row.releaseStatus, releaseSummary: row.releaseSummary,
    scopeExcluded: row.scopeExcluded, exclusionReason: row.exclusionReason,
    reason: row.reason, notes: row.notes
  };
}
function snapshotDisplayName(snapshot, index){
  const source = snapshot._source === 'browser' ? 'browser' : 'history file';
  return `${snapshot.capturedAt || `snapshot ${index}`}${snapshot.label ? ' · ' + snapshot.label : ''} · ${source}`;
}
function historySnapshotHtml(snapshot, project){
  const projectData = snapshot?.projects?.[project];
  if (!projectData) return '<div class="snapshot-empty muted">В этом снимке выбранного проекта нет.</div>';
  const health = projectData.health || {};
  const rows = (projectData.dependencies || []).map(normalizeSnapshotRow).sort((a,b) => a.group-b.group || a.name.localeCompare(b.name));
  const actionCount = rows.filter(r => !r.scopeExcluded && semverParts(r.target)).length;
  const excludedCount = rows.filter(r => r.scopeExcluded).length;
  let html = `<div class="snapshot-summary">` +
    `<span class="badge">${escapeHtmlText(snapshot.capturedAt || '')}</span>` +
    `<span class="badge">${escapeHtmlText(snapshot.label || 'без названия')}</span>` +
    `<span class="badge ${snapshot._source === 'browser' ? 'snapshot-source-browser' : 'snapshot-source-file'}">${snapshot._source === 'browser' ? 'browser history' : 'history/snapshots'}</span>` +
    `<span class="badge">status ${escapeHtmlText(health.status || 'unknown')}</span>` +
    `<span class="badge">lag ${Number(health.lag_ok_pct ?? 0).toFixed(1)}%</span>` +
    `<span class="badge">C/H/M/L ${health.critical||0}/${health.high||0}/${health.moderate||0}/${health.low||0}</span>` +
    `<span class="badge">actions ${actionCount}, excluded ${excludedCount}</span></div>`;
  if (projectData.baselineComparison) {
    const cmp = projectData.baselineComparison;
    html += `<p class="muted">Baseline: ${escapeHtmlText(cmp.transition || '')}; ${escapeHtmlText(cmp.baselineCapturedAt || '')}</p>`;
  }
  html += '<div class="table-wrap"><table class="history-view-table"><thead><tr><th>Группа</th><th>Зависимость</th><th>Секция</th><th>Текущая</th><th>Target</th><th>Latest</th><th>Vuln</th><th>Lag policy</th><th>Release intel</th><th>Причина / заметка</th></tr></thead><tbody>';
  for (const row of rows) {
    const excluded = row.scopeExcluded ? `<div class="scope-excluded-badge">исключено: ${escapeHtmlText(row.exclusionReason || '—')}</div>` : '';
    html += `<tr><td>Г${row.group}${row.subgroup ? '<br><span class="muted">' + escapeHtmlText(row.subgroup) + '</span>' : ''}</td>` +
      `<td><code>${escapeHtmlText(row.name)}</code>${excluded}</td>` +
      `<td>${escapeHtmlText(sectionForKind(row.kind))}</td>` +
      `<td><code>${escapeHtmlText(row.current)}</code></td>` +
      `<td><code>${escapeHtmlText(row.target)}</code><div class="muted">${escapeHtmlText(row.targetReason)}</div></td>` +
      `<td><code>${escapeHtmlText(row.latest)}</code></td>` +
      `<td>${escapeHtmlText(row.vulnerabilities)}</td>` +
      `<td>≤${row.lagThresholdMonths}м → <code>${escapeHtmlText(row.lagPolicyTarget)}</code></td>` +
      `<td>${escapeHtmlText(releaseStatusLabel(row.releaseStatus))}<div class="muted">${escapeHtmlText(row.releaseSummary)}</div></td>` +
      `<td>${escapeHtmlText(row.reason)}<div class="muted">${escapeHtmlText(row.notes)}</div></td></tr>`;
  }
  return html + '</tbody></table></div>';
}
function historyComparisonHtml(snapshots, project, beforeIndex, afterIndex){
  const before = snapshots[beforeIndex];
  const after = snapshots[afterIndex];
  if (!before?.projects?.[project] || !after?.projects?.[project]) return '<p class="muted">Для выбранной пары нет данных проекта.</p>';
  const a = dependencyMap(after, project), b = dependencyMap(before, project);
  const keys = Array.from(new Set([...a.keys(), ...b.keys()])).sort();
  const changes = keys.map(key => ({key, before:b.get(key), after:a.get(key)}))
    .filter(x => JSON.stringify(meaningfulSnapshotRow(x.before)) !== JSON.stringify(meaningfulSnapshotRow(x.after)));
  const bh = before.projects[project].health || {}, ah = after.projects[project].health || {};
  let html = `<p><strong>${escapeHtmlText(snapshotDisplayName(before, beforeIndex))}</strong> → <strong>${escapeHtmlText(snapshotDisplayName(after, afterIndex))}</strong><br>` +
    `Status: ${escapeHtmlText(bh.status || '—')} → ${escapeHtmlText(ah.status || '—')}; ` +
    `Lag OK: ${Number(bh.lag_ok_pct ?? 0).toFixed(1)}% → ${Number(ah.lag_ok_pct ?? 0).toFixed(1)}%; ` +
    `C/H/M/L: ${bh.critical||0}/${bh.high||0}/${bh.moderate||0}/${bh.low||0} → ${ah.critical||0}/${ah.high||0}/${ah.moderate||0}/${ah.low||0}; ` +
    `изменённых строк: <strong>${changes.length}</strong>.</p>`;
  if (!changes.length) return html + '<p class="muted">Значимые dependency facts не изменились.</p>';
  html += '<div class="table-wrap"><table class="history-table"><thead><tr><th>Зависимость</th><th>Было</th><th>Стало</th><th>Изменение</th></tr></thead><tbody>';
  for (const change of changes) {
    const bv = change.before, av = change.after;
    const labels = [];
    if (!bv) labels.push('добавлена');
    else if (!av) labels.push('удалена');
    else {
      for (const [field, label] of [['current','версия'],['target','target'],['targetReason','причина target'],['vulnerabilities','vuln'],['group','группа'],['subgroup','подгруппа'],['lagThresholdMonths','lag policy'],['lagPolicyTarget','lag target'],['releaseStatus','release status'],['releaseSummary','release notes'],['scopeExcluded','scope'],['exclusionReason','причина исключения']]) {
        if (JSON.stringify(bv[field]) !== JSON.stringify(av[field])) labels.push(label);
      }
    }
    const describe = row => row ? `${row.current} → ${row.target}; ${row.vulnerabilities}; Г${row.group}${row.subgroup ? '/' + row.subgroup : ''}; ≤${row.lagThresholdMonths}м${row.scopeExcluded ? '; excluded' : ''}` : 'absent';
    html += `<tr><td><code>${escapeHtmlText((av||bv)?.name || change.key)}</code></td>` +
      `<td>${escapeHtmlText(describe(bv))}</td><td>${escapeHtmlText(describe(av))}</td>` +
      `<td>${escapeHtmlText(labels.join(', ') || 'metadata')}</td></tr>`;
  }
  return html + '</tbody></table></div>';
}
function renderHistory(selectedKey=''){
  const snapshots = allHistorySnapshots();
  if (!snapshots.length) return '<div class="snapshot-empty muted">Снимков пока нет. Нажмите «Сохранить снимок дашборда» или запустите generate с включённой history snapshots.</div>';
  const selectedProject = document.getElementById('projectFilter')?.value || '';
  const projects = Array.from(new Set(snapshots.flatMap(s => Object.keys(s.projects || {})))).sort();
  const project = projects.includes(selectedProject) ? selectedProject : projects[0];
  const optionForSnapshot = (snapshot, index) => `<option value="${index}" ${selectedKey && snapshot._key===selectedKey?'selected':''}>${escapeHtmlText(snapshotDisplayName(snapshot, index))}</option>`;
  let html = `<div class="history-controls form-grid">` +
    `<label>Проект<select id="historyProjectSelect">${projects.map(p=>`<option value="${escapeHtmlText(p)}" ${p===project?'selected':''}>${escapeHtmlText(p)}</option>`).join('')}</select></label>` +
    `<label>Просмотр снимка<select id="historyViewSelect">${snapshots.map(optionForSnapshot).join('')}</select></label>` +
    `<label>Сравнить: до<select id="historyBeforeSelect">${snapshots.map(optionForSnapshot).join('')}</select></label>` +
    `<label>Сравнить: после<select id="historyAfterSelect">${snapshots.map(optionForSnapshot).join('')}</select></label>` +
    `</div>`;
  html += '<div class="snapshot-section"><h3>Полный снимок</h3><div id="historySnapshotView"></div></div>';
  html += '<div class="snapshot-section"><h3>Сравнение</h3><div id="historyComparison"></div></div>';
  html += '<div class="snapshot-section"><h3>Все снимки</h3><div class="table-wrap"><table class="history-table"><thead><tr><th>Дата</th><th>Название</th><th>Источник</th><th>Проект</th><th>Статус</th><th>Lag OK</th><th>C/H/M/L</th><th>Файл</th></tr></thead><tbody>';
  for (const snapshot of snapshots) {
    for (const itemProject of Object.keys(snapshot.projects || {}).sort()) {
      const health = snapshot.projects?.[itemProject]?.health || {};
      html += `<tr><td>${escapeHtmlText(snapshot.capturedAt || '')}</td><td>${escapeHtmlText(snapshot.label || '—')}</td>` +
        `<td>${escapeHtmlText(snapshot._source || 'file')}</td><td>${escapeHtmlText(itemProject)}</td><td>${escapeHtmlText(health.status || '')}</td>` +
        `<td>${escapeHtmlText(Number(health.lag_ok_pct ?? 0).toFixed(1))}%</td><td>${health.critical||0}/${health.high||0}/${health.moderate||0}/${health.low||0}</td>` +
        `<td><code>${escapeHtmlText(snapshot._file || (snapshot._source === 'browser' ? 'browser localStorage' : ''))}</code></td></tr>`;
    }
  }
  return html + '</tbody></table></div></div>';
}
function selectedHistorySnapshot(){
  const snapshots = allHistorySnapshots();
  return snapshots[Number(document.getElementById('historyViewSelect')?.value || 0)];
}
function wireHistoryControls(preferredId=''){
  const snapshots = allHistorySnapshots();
  const projectSelect = document.getElementById('historyProjectSelect');
  const viewSelect = document.getElementById('historyViewSelect');
  const beforeSelect = document.getElementById('historyBeforeSelect');
  const afterSelect = document.getElementById('historyAfterSelect');
  if (!projectSelect || !viewSelect || !beforeSelect || !afterSelect || !snapshots.length) return;
  const preferredIndex = preferredId ? snapshots.findIndex(item => item.id === preferredId || item._key === preferredId) : -1;
  if (preferredIndex >= 0) viewSelect.value = String(preferredIndex);
  afterSelect.value = preferredIndex >= 0 ? String(preferredIndex) : '0';
  beforeSelect.value = snapshots.length > 1 ? String(Math.min(1, snapshots.length - 1)) : '0';
  const refreshView = () => {
    document.getElementById('historySnapshotView').innerHTML = historySnapshotHtml(snapshots[Number(viewSelect.value || 0)], projectSelect.value);
  };
  const refreshComparison = () => {
    document.getElementById('historyComparison').innerHTML = historyComparisonHtml(snapshots, projectSelect.value, Number(beforeSelect.value || 0), Number(afterSelect.value || 0));
  };
  projectSelect.addEventListener('change', () => { refreshView(); refreshComparison(); });
  viewSelect.addEventListener('change', refreshView);
  beforeSelect.addEventListener('change', refreshComparison);
  afterSelect.addEventListener('change', refreshComparison);
  refreshView(); refreshComparison();
}
function openHistoryBrowser(preferredId=''){
  const snapshots = allHistorySnapshots();
  const selectedKey = preferredId ? (snapshots.find(s => s.id === preferredId)?._key || '') : '';
  document.getElementById('historyModalBody').innerHTML = renderHistory(selectedKey);
  wireHistoryControls(preferredId);
  openModalById('historyModal');
}
function downloadJsonFile(value, filename){
  const blob = new Blob([JSON.stringify(value, null, 2) + '\\n'], {type:'application/json;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
}
function exportSelectedSnapshot(){
  const snapshot = selectedHistorySnapshot();
  if (!snapshot) return showToast('Снимок не выбран');
  const clean = JSON.parse(JSON.stringify(snapshot));
  delete clean._source; delete clean._key; delete clean._file;
  const safe = `${snapshot.capturedAt || 'snapshot'}-${snapshot.label || ''}`.replace(/[^a-zA-Z0-9а-яА-Я._-]+/g,'-').replace(/-+/g,'-');
  downloadJsonFile(clean, `dependency-dashboard-snapshot-${safe}.json`);
  showToast('Снимок скачан');
}
async function importHistorySnapshotFile(file){
  try {
    const parsed = JSON.parse(await file.text());
    const items = Array.isArray(parsed) ? parsed : [parsed];
    let added = 0;
    let firstAddedId = '';
    for (const item of items) {
      if (!item || typeof item !== 'object' || !item.projects || !Object.keys(item.projects).length) throw new Error('projects missing');
      const copy = JSON.parse(JSON.stringify(item));
      copy.id = copy.id || ((window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
      copy.source = 'browser-import';
      if (!firstAddedId) firstAddedId = copy.id;
      browserHistorySnapshots.unshift(copy); added++;
    }
    browserHistorySnapshots = browserHistorySnapshots.slice(0, 30);
    saveBrowserHistorySnapshots();
    showToast(`Импортировано снимков: ${added}`);
    openHistoryBrowser(firstAddedId);
  } catch(e) { showToast('Не удалось импортировать снимок: ' + e.message); }
}
function deleteSelectedBrowserSnapshot(){
  const snapshot = selectedHistorySnapshot();
  if (!snapshot) return;
  if (snapshot._source !== 'browser') return showToast('Файловый snapshot удаляется только из history/snapshots на диске');
  browserHistorySnapshots = browserHistorySnapshots.filter(item => item.id !== snapshot.id);
  saveBrowserHistorySnapshots();
  showToast('Локальный снимок удалён');
  openHistoryBrowser();
}
function initDashboardInteractions(){
  if (!new URLSearchParams(window.location.search).has('desktop-export')) loadDashboardStateLocal();
  loadBrowserHistorySnapshots();
  applyDashboardState();
  document.getElementById('detailedCodeComments')?.addEventListener('change', savePromptOptionsToDashboardState);
  document.querySelectorAll('.row-settings-btn').forEach(btn => btn.addEventListener('click', () => openRowSettings(btn.closest('tr.dep-row'))));
  document.querySelectorAll('.row-select').forEach(input => input.addEventListener('change', updateSelectionControls));
  document.querySelectorAll('.table-select-visible').forEach(master => master.addEventListener('change', () => {
    const table = master.closest('table');
    Array.from(table?.querySelectorAll('tbody tr.dep-row') || []).filter(row => !row.classList.contains('row-hidden')).forEach(row => {
      const input = row.querySelector('.row-select'); if (input) input.checked = master.checked;
    });
    updateSelectionControls();
  }));
  document.getElementById('excludeSelectedBtn')?.addEventListener('click', () => setSelectedRowsExcluded(true));
  document.getElementById('includeSelectedBtn')?.addEventListener('click', () => setSelectedRowsExcluded(false));
  document.getElementById('clearSelectionBtn')?.addEventListener('click', clearDependencySelection);
  document.querySelectorAll('.release-details-btn').forEach(btn => btn.addEventListener('click', () => openReleaseDetails(btn.closest('tr.dep-row'))));
  document.getElementById('saveRowSettingsBtn')?.addEventListener('click', saveActiveRowSettings);
  document.getElementById('applyVisibleSettingsBtn')?.addEventListener('click', applySettingsToVisibleRows);
  document.getElementById('clearRowSettingsBtn')?.addEventListener('click', clearActiveRowSettings);
  document.getElementById('closeSettingsBtn')?.addEventListener('click', () => closeModalById('settingsModal'));
  document.getElementById('exportStateBtn')?.addEventListener('click', exportDashboardState);
  document.getElementById('importStateBtn')?.addEventListener('click', () => document.getElementById('importStateFile')?.click());
  document.getElementById('importStateFile')?.addEventListener('change', event => { const file = event.target.files?.[0]; if (file) importDashboardStateFile(file); event.target.value=''; });
  document.getElementById('saveSnapshotBtn')?.addEventListener('click', () => saveCurrentDashboardSnapshot(false));
  document.getElementById('historyBtn')?.addEventListener('click', () => openHistoryBrowser());
  document.getElementById('saveSnapshotFromHistoryBtn')?.addEventListener('click', () => saveCurrentDashboardSnapshot(true));
  document.getElementById('exportSelectedSnapshotBtn')?.addEventListener('click', exportSelectedSnapshot);
  document.getElementById('importSnapshotBtn')?.addEventListener('click', () => document.getElementById('importSnapshotFile')?.click());
  document.getElementById('importSnapshotFile')?.addEventListener('change', event => { const file = event.target.files?.[0]; if (file) importHistorySnapshotFile(file); event.target.value=''; });
  document.getElementById('deleteSelectedSnapshotBtn')?.addEventListener('click', deleteSelectedBrowserSnapshot);
  document.getElementById('closeHistoryBtn')?.addEventListener('click', () => closeModalById('historyModal'));
  ['releaseModal','settingsModal','historyModal'].forEach(id => document.getElementById(id)?.addEventListener('click', e => { if (e.target.id === id) closeModalById(id); }));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') ['releaseModal','settingsModal','historyModal'].forEach(closeModalById); });
}

initThemeControl();
initDetailsExpandControl();
initProjectGroupToggles();
initPromptExport();
initDashboardInteractions();
const requestedProject = new URLSearchParams(window.location.search).get('project') || '';
if (requestedProject) {
  const projectFilter = document.getElementById('projectFilter');
  if (projectFilter && Array.from(projectFilter.options || []).some(option => option.value === requestedProject)) {
    projectFilter.value = requestedProject;
  }
}
applyFilters();
</script>
</body>
</html>
""")

def row_json(r: DependencyRow) -> Dict[str, Any]:
    item = dataclasses.asdict(r)
    item["vulnerability_work_note"] = vulnerability_work_note(r)
    item["yellow_effort_score"] = row_update_effort_score(r, r.target_yellow) if target_is_action(r.target_yellow) else None
    item["green_effort_score"] = row_update_effort_score(r, r.target_green) if target_is_action(r.target_green) else None
    item["default_effort_score"] = row_update_effort_score(r, r.target_default) if target_is_action(r.target_default) else None
    item["yellow_breaking_risk_note"] = row_breaking_risk_note(r, r.target_yellow) if target_is_action(r.target_yellow) else None
    item["green_breaking_risk_note"] = row_breaking_risk_note(r, r.target_green) if target_is_action(r.target_green) else None
    item["default_breaking_risk_note"] = row_breaking_risk_note(r, r.target_default) if target_is_action(r.target_default) else None
    return item


def write_json(
    rows_by_project: Dict[str, List[DependencyRow]],
    out: Path,
    baseline_comparisons: Optional[Dict[str, Dict[str, Any]]] = None,
    project_specs: Optional[Dict[str, ProjectSpec]] = None,
    health_by_project: Optional[Dict[str, ProjectHealth]] = None,
) -> None:
    health_by_project = health_by_project or enrich_project_targets(rows_by_project)
    suggestions_by_project, global_suggestions = build_all_suggestions(rows_by_project)
    data = {
        "project_health": {project: dataclasses.asdict(h) for project, h in health_by_project.items()},
        "baseline_comparisons": baseline_comparisons or {},
        "project_git": {
            name: {
                "remote": spec.git_remote,
                "sourceBranch": spec.source_branch,
                "sourceCheckout": spec.source_checkout,
            }
            for name, spec in (project_specs or {}).items()
        },
        "project_lockfiles": {
            name: spec.lockfile_state
            for name, spec in (project_specs or {}).items()
        },
        "project_manual_audit": {
            name: spec.current_audit
            for name, spec in (project_specs or {}).items()
        },
        "projects": {project: [row_json(r) for r in rows] for project, rows in rows_by_project.items()},
        "suggestions": {
            "global": [dataclasses.asdict(s) for s in global_suggestions],
            "by_project": {project: [dataclasses.asdict(s) for s in suggestions] for project, suggestions in suggestions_by_project.items()},
        },
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    configure_utf8_stdio()
    ap = argparse.ArgumentParser(description="Generate live dependency roadmap by project directories with settings.json, project-aware grouping, work-batch suggestions, HTML dashboard, and agent prompt export.")
    ap.add_argument("--settings", help="Legacy/base JSON settings file. If passed, auto project/local discovery is disabled.")
    ap.add_argument("--project-settings", help="Tracked project settings JSON. Default discovery: .dependency-roadmap/settings.project.json, settings.project.json, settings.json.")
    ap.add_argument("--local-settings", help="Ignored local settings JSON that overrides project settings. Default discovery: .dependency-roadmap/settings.local.json, settings.local.json.")
    ap.add_argument("--projects-file", help="Legacy text file with project directories, one per line. Overrides settings.projects/projects-file.")
    ap.add_argument("--root", help="Base directory for relative project paths. Overrides settings.root.")
    ap.add_argument("--input", help="Backward-compatible mode: scan directory recursively for package.json. Overrides settings.projects.")
    ap.add_argument("--out", help="Output Markdown file. Overrides settings.out. Default: artifacts/dependency-roadmap.md")
    ap.add_argument("--json-out", help="Optional JSON output. Overrides settings.json-out/jsonOut.")
    ap.add_argument("--html-out", help="Optional interactive HTML output. Overrides settings.html-out/htmlOut.")
    ap.add_argument("--artifacts-dir", help="Directory for generated reports when explicit outputs are not set. Overrides settings.artifacts-dir/artifactsDir.")
    ap.add_argument("--events-log", help="Append-only JSONL migration history path. Overrides settings.events-log/eventsLog.")
    ap.add_argument("--runs-dir", help="Directory for per-run migration reports. Overrides settings.runs-dir/runsDir.")
    ap.add_argument("--index-file", help="Append-only human-readable history index. Overrides settings.index-file/indexFile.")
    ap.add_argument("--groups-config", help="Optional JSON package group/subgroup overrides. Overrides settings.groups-config/groupsConfig.")
    ap.add_argument("--dashboard-state", help="Tracked dashboard state JSON with UI package overrides. Overrides settings.dashboardState.")
    ap.add_argument("--residual-stability-file", help="Ephemeral JSON from Desktop replan with previously approved package targets. Used only to minimize residual target churn.")
    ap.add_argument("--compatibility-evidence-file", help="Ephemeral deterministic post-Executor compatibility evidence. Reproduced/localized before it can become a session-local solver nogood.")
    ap.add_argument(
        "--exclude-dependency",
        action="append",
        help=(
            "Exclude a dependency from this generation scope with a mandatory reason. "
            "Repeatable forms: package|reason, project|package|reason, "
            "project|kind|package|reason (kind=runtime/dev/optional/peer)."
        ),
    )
    ap.add_argument("--knowledge-log", help="Append-only, revisioned package migration knowledge JSON. Overrides settings.knowledgeLog.")
    ap.add_argument("--only-project", action="append", help="Analyze only the named project from settings/projects. Can be passed multiple times.")
    ap.add_argument("--capture-baseline", action="store_true", help="Store the current project state as a migration baseline in history/baselines after generating the report.")
    ap.add_argument("--baseline-label", default="", help="Optional label stored with --capture-baseline, for example ticket or branch name.")
    ap.add_argument("--registry", help="npm registry URL; can point to corporate registry. Overrides settings.registry.")
    ap.add_argument("--use-system-proxy", action="store_true", help="Allow requests to inherit OS/env proxy settings. Default is disabled to avoid stale local proxies for corporate registries.")
    ap.add_argument("--internal-scope", action="append", help="Corporate/internal npm scope prefix, e.g. @your-company/. Can be passed multiple times. Overrides settings.internal-scope/internalScopes.")
    ap.add_argument("--include-prerelease", action="store_true", help="Consider prerelease versions. Overrides false default only when passed.")
    ap.add_argument("--max-candidates", type=int, help="Limit candidate versions after current; 0 = all. Overrides settings.max-candidates/maxCandidates.")
    ap.add_argument("--timeout", type=int, help="HTTP timeout seconds. Overrides settings.timeout.")
    ap.add_argument("--skip-release-intel", action="store_true", help="Do not fetch GitHub changelog/release notes.")
    ap.add_argument("--release-intel-max-packages", type=int, help="Maximum action packages to enrich with release notes; 0 = unlimited.")
    ap.add_argument("--no-history-snapshot", action="store_true", help="Do not store an automatic full dashboard snapshot in history/snapshots.")
    ap.add_argument("--history-snapshot-label", default="", help="Optional human label for the automatic dashboard snapshot (ticket, milestone, before/after note).")
    ap.add_argument(
        "--skip-source-checkout-guard",
        action="store_true",
        help="Do not require each configured project to be a clean checkout at the exact fetched remote/sourceBranch commit.",
    )
    ap.add_argument(
        "--skip-audit-bootstrap",
        action="store_true",
        help="Deprecated compatibility flag. generate never creates npm audit package-locks; run manual_dependency_audit.py explicitly.",
    )
    ap.add_argument(
        "--post-update",
        action="store_true",
        help="Backward-compatible alias. Current-checkout analysis is now the default whenever --capture-baseline is not used.",
    )
    ap.add_argument(
        "--lockfile-mode",
        choices=("validate", "update", "off"),
        help="Validate, refresh when stale, or disable project lockfile sync. Default: validate for --capture-baseline, update for current-checkout generation.",
    )
    args = ap.parse_args()

    residual_targets_by_project: Dict[str, Dict[str, str]] = {}
    if args.residual_stability_file:
        residual_path = Path(args.residual_stability_file).expanduser()
        try:
            payload = json.loads(residual_path.read_text(encoding="utf-8"))
            projects_payload = payload.get("projects", {}) if isinstance(payload, dict) else {}
            if isinstance(projects_payload, dict):
                for project_name, project_payload in projects_payload.items():
                    targets = project_payload.get("targets", {}) if isinstance(project_payload, dict) else {}
                    if isinstance(targets, dict):
                        residual_targets_by_project[str(project_name)] = {
                            str(name): str(version)
                            for name, version in targets.items()
                            if str(name).strip() and safe_version(str(version)) is not None
                        }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            ap.error(f"invalid --residual-stability-file: {exc}")

    external_evidence_by_project: Dict[str, CompatibilityEvidence] = {}
    if args.compatibility_evidence_file:
        try:
            evidence = load_compatibility_evidence(Path(args.compatibility_evidence_file).expanduser())
            external_evidence_by_project[evidence.project] = evidence
        except CompatibilityEvidenceError as exc:
            ap.error(f"invalid --compatibility-evidence-file: {exc}")

    settings_path_arg = Path(args.settings).expanduser() if args.settings else None
    project_settings_path_arg = Path(args.project_settings).expanduser() if args.project_settings else None
    local_settings_path_arg = Path(args.local_settings).expanduser() if args.local_settings else None
    settings, settings_path, settings_sources = read_merged_settings(
        settings_path_arg,
        project_settings_path_arg,
        local_settings_path_arg,
    )
    settings_base = settings_workspace_base(settings_path)

    # Path resolution rules:
    # - when settings live in .dependency-roadmap/settings.project.json, all
    #   relative project/output/history/config paths are resolved from the
    #   workspace root, not from the .dependency-roadmap directory;
    # - this prevents duplicated paths like
    #   .dependency-roadmap/.dependency-roadmap/artifacts;
    # - settings.root is also relative to the workspace root;
    # - project paths are relative to root if root exists, otherwise to the
    #   workspace root.
    root_value = args.root or settings_get(settings, "root")
    root = resolve_config_path(root_value, settings_base, None) if root_value else None

    artifacts_dir_value = args.artifacts_dir or settings_get(settings, "artifacts-dir", "artifactsDir", default="artifacts")
    artifacts_dir = resolve_config_path(str(artifacts_dir_value), settings_base, None) if artifacts_dir_value else settings_base / "artifacts"
    assert artifacts_dir is not None

    out_value = args.out or settings_get(settings, "out")
    json_out_value = args.json_out or settings_get(settings, "json-out", "jsonOut")
    html_out_value = args.html_out or settings_get(settings, "html-out", "htmlOut")

    out_path = resolve_config_path(str(out_value), settings_base, None) if out_value else (artifacts_dir / "dependency-roadmap.md")
    json_out_path = resolve_config_path(str(json_out_value), settings_base, None) if json_out_value else (artifacts_dir / "dependency-roadmap.json")
    html_out_path = resolve_config_path(str(html_out_value), settings_base, None) if html_out_value else (artifacts_dir / "dependency-roadmap.html")

    history_dir_value = settings_get(settings, "history-dir", "historyDir", default=".dependency-update-history")
    history_dir = resolve_config_path(str(history_dir_value), settings_base, None)
    assert history_dir is not None
    events_log_value = args.events_log or settings_get(settings, "events-log", "eventsLog")
    runs_dir_value = args.runs_dir or settings_get(settings, "runs-dir", "runsDir")
    index_file_value = args.index_file or settings_get(settings, "index-file", "indexFile")
    events_log = resolve_config_path(str(events_log_value), settings_base, None) if events_log_value else (history_dir / "events.jsonl")
    runs_dir = resolve_config_path(str(runs_dir_value), settings_base, None) if runs_dir_value else (history_dir / "runs")
    index_file = resolve_config_path(str(index_file_value), settings_base, None) if index_file_value else (history_dir / "index.md")

    groups_config_value = args.groups_config or settings_get(settings, "groups-config", "groupsConfig")
    groups_config_path = resolve_config_path(str(groups_config_value), settings_base, None) if groups_config_value else None
    dashboard_state_value = args.dashboard_state or settings_get(settings, "dashboard-state", "dashboardState", default=".dependency-roadmap/state/dashboard-state.json")
    dashboard_state_path = resolve_config_path(str(dashboard_state_value), settings_base, None) if dashboard_state_value else None
    knowledge_log_value = args.knowledge_log or settings_get(settings, "knowledge-log", "knowledgeLog")
    knowledge_log_path = resolve_config_path(str(knowledge_log_value), settings_base, None) if knowledge_log_value else None
    knowledge_entries = load_dependency_knowledge(knowledge_log_path)

    registry = args.registry or settings_get(settings, "registry", default=NPM_REGISTRY)
    timeout = args.timeout if args.timeout is not None else as_int(settings_get(settings, "timeout"), REQUEST_TIMEOUT)
    max_candidates = args.max_candidates if args.max_candidates is not None else as_int(settings_get(settings, "max-candidates", "maxCandidates"), 0)
    include_prerelease = args.include_prerelease or as_bool(settings_get(settings, "include-prerelease", "includePrerelease"), False)
    use_system_proxy = args.use_system_proxy or as_bool(settings_get(settings, "use-system-proxy", "useSystemProxy"), False)
    release_intel_enabled = not args.skip_release_intel and as_bool(settings_get(settings, "release-intel-enabled", "releaseIntelEnabled"), True)
    release_intel_max = args.release_intel_max_packages if args.release_intel_max_packages is not None else as_int(settings_get(settings, "release-intel-max-packages", "releaseIntelMaxPackages"), 0)
    history_snapshot_enabled = not args.no_history_snapshot and as_bool(settings_get(settings, "history-snapshots-enabled", "historySnapshotsEnabled"), True)
    source_checkout_guard_enabled = (
        not args.skip_source_checkout_guard
        and as_bool(settings_get(settings, "source-checkout-guard", "sourceCheckoutGuard"), False)
    )
    global_audit_bootstrap_raw = settings_get(settings, "audit-bootstrap", "auditBootstrap", default={})
    global_audit_bootstrap = dict(global_audit_bootstrap_raw) if isinstance(global_audit_bootstrap_raw, dict) else {}
    global_git_hooks_raw = settings_get(settings, "git-hooks", "gitHooks", default={})
    global_git_hooks = dict(global_git_hooks_raw) if isinstance(global_git_hooks_raw, dict) else {}
    global_release_raw = settings_get(settings, "release", default={})
    global_release = dict(global_release_raw) if isinstance(global_release_raw, dict) else {}
    global_migration_raw = settings_get(settings, "migration", default={})
    global_migration = dict(global_migration_raw) if isinstance(global_migration_raw, dict) else {}
    global_lockfile_raw = settings_get(settings, "lockfile-sync", "lockfileSync", default={})
    global_lockfile_sync = dict(global_lockfile_raw) if isinstance(global_lockfile_raw, dict) else {}
    global_constraint_verify_raw = settings_get(settings, "constraint-verification", "constraintVerification", default={})
    global_constraint_verify = dict(global_constraint_verify_raw) if isinstance(global_constraint_verify_raw, dict) else {}
    default_constraint_cache_value = settings_get(
        settings, "constraint-cache", "constraintCache",
        default=".dependency-roadmap/state/constraint-cache.json",
    )
    default_constraint_cache_path = resolve_config_path(
        str(default_constraint_cache_value), settings_base, None
    ) if default_constraint_cache_value else None

    global INTERNAL_SCOPES
    if args.internal_scope:
        INTERNAL_SCOPES = normalize_internal_scopes(args.internal_scope)
    else:
        configured_scopes = settings_get(settings, "internal-scope", "internalScopes", "internal_scopes")
        if configured_scopes:
            INTERNAL_SCOPES = normalize_internal_scopes(configured_scopes)

    # Projects: CLI overrides settings. Settings support either projects list or projects-file.
    if args.projects_file:
        projects = parse_projects_file(resolve_config_path(args.projects_file, settings_base, None) or Path(args.projects_file), root)
    elif args.input:
        projects = discover_projects(resolve_config_path(args.input, settings_base, None) or Path(args.input).expanduser().resolve())
    elif settings_get(settings, "projects") is not None:
        projects = parse_project_entries(settings_get(settings, "projects"), settings_base, root, global_git_hooks, global_release, global_migration, global_lockfile_sync, global_constraint_verify)
    elif settings_get(settings, "projects-file", "projectsFile"):
        pf = resolve_config_path(str(settings_get(settings, "projects-file", "projectsFile")), settings_base, None)
        assert pf is not None
        projects = parse_projects_file(pf, root)
    else:
        ap.error("Use --projects-file, --input, or settings.json with projects/projects-file")

    only_project_values = args.only_project or settings_get(settings, "only-project", "onlyProject")
    if only_project_values:
        if isinstance(only_project_values, str):
            wanted_values = [only_project_values]
        else:
            wanted_values = [str(x) for x in only_project_values]
        wanted = {x.lower() for x in wanted_values}
        projects = [p for p in projects if p.name.lower() in wanted or p.path.name.lower() in wanted]
        if not projects:
            ap.error(f"--only-project did not match any configured project: {', '.join(wanted_values)}")

    baseline_progress_path = settings_base / ".dependency-roadmap" / "state" / "baseline-verification-progress.json"
    baseline_resume_store = BaselineLocalizationCheckpointStore(baseline_progress_path)

    for project in projects:
        cache_value = project.constraint_verify_config.get("constraintCachePath")
        if cache_value:
            project.constraint_cache_path = resolve_config_path(str(cache_value), settings_base, None)
        else:
            project.constraint_cache_path = default_constraint_cache_path

        try:
            project.git_hooks = normalize_git_hook_policy(project.git_hooks)
            project.release_config = normalize_release_policy(project.release_config)
        except ValueError as exc:
            eprint(f"[error] {project.name}: {exc}")
            raise SystemExit(2) from None

        # Legacy auditBootstrap settings are accepted for compatibility but are
        # deliberately ignored: generate does not run vulnerability audit. It is an explicit user action
        # (manual_dependency_audit.py); generate never creates an npm package-lock
        # for a Yarn project.
        audit_cfg = dict(global_audit_bootstrap)
        audit_cfg.update(project.audit_bootstrap_config)
        project.audit_bootstrap_config = audit_cfg
        if as_bool(audit_cfg.get("enabled"), False):
            eprint(
                f"[warn] {project.name}: auditBootstrap is deprecated and ignored; "
                "run manual_dependency_audit.py explicitly when a manual vulnerability cross-check is needed"
            )

        configured_guard = (
            source_checkout_guard_enabled
            if project.source_checkout_guard is None
            else project.source_checkout_guard and not args.skip_source_checkout_guard
        )
        guard_enabled = bool(args.capture_baseline and configured_guard)
        if not guard_enabled:
            project.source_checkout = {
                "verified": False,
                "remote": project.git_remote or "origin",
                "sourceBranch": project.source_branch,
                "reason": "source checkout guard disabled",
            }
        else:
            current_branch_probe = _git_command(project, ["branch", "--show-current"], check=False)
            current_branch = (current_branch_probe.stdout or "").strip()
            if current_branch and project.source_branch and current_branch != project.source_branch:
                eprint(
                    f"[warn] {project.name}: current branch {current_branch!r} will NOT be analyzed; "
                    f"baseline capture will switch to fetched {project.git_remote or 'origin'}/{project.source_branch}."
                )
            try:
                metadata = ensure_source_checkout(
                    project,
                    allow_checkpoint_resume=baseline_resume_store.has_project_checkpoint(project.name),
                )
            except SourceCheckoutGuardError as exc:
                eprint(f"[error] {exc}")
                raise SystemExit(2) from None
            eprint(
                f"[info] source checkout verified: {project.name} "
                f"{metadata['remote']}/{metadata['sourceBranch']}@{metadata['sourceCommit'][:12]}"
            )

        baseline_mode = str(project.lockfile_sync_config.get("baselineMode") or project.lockfile_sync_config.get("mode") or "validate").strip().lower()
        current_mode = str(project.lockfile_sync_config.get("currentMode") or "update").strip().lower()
        lock_mode = args.lockfile_mode or (baseline_mode if args.capture_baseline else current_mode)
        try:
            lock_state = ensure_lockfile_consistency(
                project.path,
                str(registry),
                project.lockfile_sync_config,
                mode=lock_mode,
                allow_update=not guard_enabled,
            )
        except LockfileConsistencyError as exc:
            eprint(f"[error] {exc}")
            if not args.capture_baseline:
                eprint("[hint] current-checkout generation refreshes the project's own lockfile before dashboard analysis")
            raise SystemExit(2) from None
        project.lockfile_state = lock_state.as_dict()
        if lock_state.updated:
            eprint(
                f"[info] project lockfile refreshed: {project.name} manager={lock_state.manager} "
                f"file={lock_state.lockfile.name}; deduplication={lock_state.deduplication_status}"
            )
        else:
            eprint(
                f"[info] project lockfile verified: {project.name} manager={lock_state.manager} "
                f"file={lock_state.lockfile.name} mode={lock_mode}"
            )

        project.current_audit = {
            "mode": "manual-only",
            "prepared": False,
            "dashboardLockfile": lock_state.lockfile.name,
            "dashboardPackageManager": lock_state.manager,
            "toolPath": str(Path(__file__).resolve().parent / "manual_dependency_audit.py"),
            "command": "python manual_dependency_audit.py",
            "workspaceTemplate": ".dependency-roadmap/artifacts/manual-audit-<project>-workspace",
            "note": "for Yarn, an isolated package-lock bridge is created only by the explicitly invoked bundled manual audit tool",
        }
        eprint(
            f"[info] vulnerability audit: manual-only for {project.name}; "
            f"dashboard uses {lock_state.lockfile.name}, never a cross-manager audit lock"
        )

    ensure_output_parent(out_path)
    ensure_output_parent(json_out_path)
    ensure_output_parent(html_out_path)
    if artifacts_dir:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        write_if_missing(artifacts_dir / ".gitkeep", "")
    ensure_history_layout(events_log, runs_dir, index_file)
    if dashboard_state_path:
        dashboard_state_path.parent.mkdir(parents=True, exist_ok=True)
        if not dashboard_state_path.exists():
            dashboard_state_path.write_text(json.dumps({"schemaVersion": 1, "packageOverrides": {}}, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        cli_exclusions = parse_cli_exclusions(args.exclude_dependency)
    except ValueError as exc:
        ap.error(str(exc))
    overrides = merge_override_maps(
        load_group_overrides(groups_config_path),
        load_dashboard_state(dashboard_state_path),
        cli_exclusions,
    )
    client = LiveDataClient(str(registry), timeout, OSV_BATCH_SIZE, RATE_SLEEP_SEC, use_system_proxy=use_system_proxy)

    rows_by_project: Dict[str, List[DependencyRow]] = defaultdict(list)
    projects_by_name = {p.name: p for p in projects}
    if settings_sources:
        eprint(f"[info] settings: {', '.join(str(p) for p in settings_sources)}")
    else:
        eprint("[info] settings: not used")
    eprint(f"[info] projects: {len(projects)}")
    eprint(f"[info] out: {out_path}")
    eprint(f"[info] json-out: {json_out_path}")
    eprint(f"[info] html-out: {html_out_path}")
    eprint(f"[info] knowledge-log: {knowledge_log_path or 'not configured'} ({len(knowledge_entries)} active entries)")
    eprint(f"[info] history: {history_dir}")
    eprint(f"[info] dashboard state: {dashboard_state_path}")
    eprint(f"[info] release intelligence: {'enabled' if release_intel_enabled else 'disabled'}")
    eprint(f"[info] system proxy: {'enabled' if use_system_proxy else 'disabled'}")
    if args.capture_baseline:
        eprint("[info] analysis mode: fetched source-branch baseline")
    else:
        eprint("[info] analysis mode: current checkout compared with the saved baseline; the project-manager lockfile is refreshed before analysis")
    generation_started = time.perf_counter()
    for i, project in enumerate(projects, start=1):
        project_prefix = f"[{i}/{len(projects)}]"
        eprint(f"[info] {project_prefix} {project.name}: {project.path}")
        rows_by_project[project.name].extend(
            analyze_project(
                project,
                client,
                overrides,
                include_prerelease,
                max_candidates,
                progress_prefix=project_prefix,
            )
        )
        eprint(
            f"[info] {project_prefix} {project.name}: project dependency scan complete; "
            f"rows={len(rows_by_project[project.name])}"
        )

    baselines_dir = history_dir / "baselines"
    planning_baselines: Dict[str, Dict[str, Any]] = {}
    if not args.capture_baseline:
        planning_baselines = {
            project: baseline
            for project in rows_by_project
            if (baseline := load_latest_baseline(baselines_dir, project)) is not None
        }

    target_started = time.perf_counter()
    eprint("[info] target planning started")
    _apply_baseline_intent_scope(rows_by_project)
    health_by_project = enrich_project_targets(rows_by_project, planning_baselines)
    apply_supervisor_scope_expansions(rows_by_project)
    enforce_storybook_cohort(rows_by_project, client)
    apply_planner_deferrals(rows_by_project)
    # BLOCK_W_P0_P1_TYPES_NESTED_FIX_V1
    # Executable-action feasibility is part of planning, not a post-proof rewrite.
    # A deprecated @types/* target is either already proven removable against
    # the exact planned runtime target or conservatively deferred before the
    # expensive resolver/project proof begins.
    plan_executable_actions(
        rows_by_project,
        client,
        immutable_targets=False,
    )
    # Freeze the executable policy intent before compatibility resolution.
    # Registry evidence is applied first so peer solving never relies on a
    # metadata-only target; the solver may then choose registry-backed
    # fallbacks/companions without resurrecting an infeasible type-stub action.
    capture_desired_targets(rows_by_project)
    enrich_registry_target_evidence(rows_by_project, client)
    if residual_targets_by_project:
        count = sum(len(targets) for targets in residual_targets_by_project.values())
        eprint(f"[info] residual stability: loaded {count} previously approved target(s); merged matches are hard-fixed, pending matches are preferred")
    proven_dependency_envelopes: Dict[str, Dict[str, Dict[str, Any]]] = {}
    proven_assignments = resolve_peer_compatibility_with_verification(
        rows_by_project, projects_by_name, client,
        residual_targets_by_project=residual_targets_by_project,
        external_evidence_by_project=external_evidence_by_project,
        progress_path=baseline_progress_path,
        proof_envelopes_out=proven_dependency_envelopes,
    )
    # Everything below this line is a consumer of the proven assignment.
    enrich_registry_target_evidence(
        rows_by_project,
        client,
        allow_target_mutation=False,
    )
    # A late @types action decision may fail the handoff, but may not mutate a
    # package-manager-proven dependency target.
    plan_executable_actions(
        rows_by_project,
        client,
        immutable_targets=True,
    )
    assert_proven_assignment_conformance(rows_by_project, proven_assignments)
    final_peer_issues = validate_final_peer_assignment(rows_by_project, client)
    if final_peer_issues:
        raise RuntimeError(
            "FINAL_BASELINE_COMPATIBILITY_INVALID: " + " | ".join(final_peer_issues[:20])
        )

    proven_dependency_state_path = (
        settings_base / ".dependency-roadmap" / "state" / "proven-dependency-state.json"
    )
    proven_dependency_state = write_proven_dependency_state(
        proven_dependency_state_path,
        proven_dependency_envelopes,
    )
    eprint(
        f"[info] persisted content-addressed ProofEnvelope state: "
        f"{proven_dependency_state_path}"
    )
    # Health status is based on installed versions, but lag_blockers also carry
    # plannedTargetYellow/Green. Recompute only after every Supervisor, peer and
    # registry pass has finalized targets; otherwise target closure can claim
    # that one action remains while projecting zero possible improvement.
    health_by_project = {
        project: compute_project_health(rows, project, planning_baselines.get(project))
        for project, rows in rows_by_project.items()
    }
    eprint(f"[info] target planning completed in {time.perf_counter() - target_started:.1f}s")
    enrich_release_intelligence(rows_by_project, client, enabled=release_intel_enabled, max_packages=release_intel_max)
    captured_baselines: Dict[str, Dict[str, Any]] = {}
    if args.capture_baseline:
        for project, rows in rows_by_project.items():
            spec = projects_by_name.get(project)
            if not spec:
                continue
            snapshot = build_baseline_snapshot(
                project,
                spec.path,
                rows,
                health_by_project[project],
                args.baseline_label,
                source_checkout=spec.source_checkout,
            )
            baseline_path = write_baseline_snapshot(baselines_dir, snapshot)
            captured_baselines[project] = snapshot
            eprint(f"[info] baseline captured: {baseline_path}")

    baseline_comparisons: Dict[str, Dict[str, Any]] = {}
    for project, rows in rows_by_project.items():
        spec = projects_by_name.get(project)
        if not spec:
            continue
        baseline = captured_baselines.get(project) or planning_baselines.get(project)
        comparison = build_baseline_comparison(
            project,
            rows,
            health_by_project[project],
            baseline,
            spec.path,
            captured_this_run=project in captured_baselines,
        )
        if comparison:
            baseline_comparisons[project] = comparison

    if history_snapshot_enabled:
        snapshot_label = args.history_snapshot_label or args.baseline_label
        snapshot = compact_history_snapshot(
            rows_by_project,
            health_by_project,
            label=snapshot_label,
            baseline_comparisons=baseline_comparisons,
            project_specs=projects_by_name,
            dashboard_state=(read_json(dashboard_state_path) if dashboard_state_path and dashboard_state_path.exists() else {"schemaVersion": 1, "packageOverrides": {}}),
        )
        snapshot_path = write_history_snapshot(history_dir, snapshot)
        eprint(f"[info] history snapshot: {snapshot_path}")
    history_snapshots = load_history_snapshots(history_dir)

    eprint("[info] writing roadmap artifacts")
    write_markdown(
        rows_by_project, out_path,
        baseline_comparisons=baseline_comparisons,
        project_specs=projects_by_name,
        health_by_project=health_by_project,
    )
    if json_out_path:
        write_json(
            rows_by_project,
            json_out_path,
            baseline_comparisons=baseline_comparisons,
            project_specs=projects_by_name,
            health_by_project=health_by_project,
        )
    if html_out_path:
        write_html(
            rows_by_project,
            html_out_path,
            history_dir=history_dir,
            registry=str(registry),
            settings_sources=settings_sources,
            baseline_comparisons=baseline_comparisons,
            project_specs=projects_by_name,
            health_by_project=health_by_project,
            dashboard_state_path=dashboard_state_path,
            history_snapshots=history_snapshots,
            roadmap_json_path=json_out_path,
            knowledge_entries=knowledge_entries,
            knowledge_log_path=knowledge_log_path,
            proven_dependency_state=proven_dependency_state,
            proven_dependency_state_path=proven_dependency_state_path,
        )
    eprint(
        f"[done] wrote {out_path} and {html_out_path}; "
        f"total elapsed={time.perf_counter() - generation_started:.1f}s"
    )
    eprint(f"[info] history store: events={events_log}, runs={runs_dir}, index={index_file}")


if __name__ == "__main__":
    try:
        main()
    except BaselineConstraintVerificationError as exc:
        message = str(exc)
        if message.startswith(BASELINE_DECISION_MARKER):
            # Expected product control-flow boundary, not a crash.
            eprint(message)
            raise SystemExit(3)
        raise
