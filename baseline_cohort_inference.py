"""Proof-neutral dynamic cohort inference for iterative DepLoom Baseline.

A cohort is a navigation/search neighborhood, never Solver or proof authority.
The only authoritative dependency facts remain exact package-manager/project
verification and independently certified constraints.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import deque
from typing import Iterable, Mapping, Optional, Sequence

AUTHORITY = "DIAGNOSTIC_HINT"


@dataclasses.dataclass(frozen=True)
class CohortSuggestion:
    cohort_id: str
    label: str
    predicate: str
    subjects: tuple[str, ...]
    packages: tuple[str, ...]
    blocked_packages: tuple[str, ...]
    warning_packages: tuple[str, ...]
    boundary_packages: tuple[str, ...]
    confidence: float
    reasons: tuple[str, ...]
    decision_id: str
    expanded_from: str = ""
    authority: str = AUTHORITY

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.cohort_id,
            "label": self.label,
            "predicate": self.predicate,
            "subjects": list(self.subjects),
            "packages": list(self.packages),
            "blockedPackages": list(self.blocked_packages),
            "warningPackages": list(self.warning_packages),
            "boundaryPackages": list(self.boundary_packages),
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "decisionId": self.decision_id,
            "expandedFrom": self.expanded_from or None,
            "authority": self.authority,
        }


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _base(value: object) -> str:
    return _norm(value).rsplit("/", 1)[-1]


def _vite(name: str) -> bool:
    value = _norm(name)
    return (
        value in {"vite", "vitest", "rollup"}
        or value.startswith("@vitejs/")
        or value.startswith("vite-")
        or value.startswith("@sentry/vite")
        or value.startswith("@storybook/builder-vite")
        or value.startswith("@storybook/react-vite")
        or value.startswith("unplugin")
    )


def _storybook(name: str) -> bool:
    value = _norm(name)
    return value == "storybook" or value.startswith("@storybook/") or "storybook" in value


def _eslint(name: str) -> bool:
    value = _norm(name)
    return value == "eslint" or value.startswith("eslint-") or value.startswith("@typescript-eslint/") or value == "@babel/eslint-parser"


def _stylelint(name: str) -> bool:
    value = _norm(name)
    return value == "stylelint" or value.startswith("stylelint-") or value.startswith("@stylelint/")


def _webpack(name: str) -> bool:
    value = _norm(name)
    base = _base(name)
    return value == "webpack" or value.startswith("webpack-") or value.startswith("@storybook/builder-webpack") or base.endswith("-loader")


def _typescript(name: str) -> bool:
    value = _norm(name)
    return value == "typescript" or value.startswith("@typescript-eslint/") or value in {"ts-node", "ts-jest", "typescript-eslint"}


def _react(name: str) -> bool:
    value = _norm(name)
    return value in {"react", "react-dom", "@types/react", "@types/react-dom"} or value.startswith("@vitejs/plugin-react") or value.startswith("@storybook/react")


_RULES = (
    ("vite-build", "Vite / build tooling", {"rollup", "vite", "vitest"}, _vite),
    ("storybook", "Storybook", {"storybook"}, _storybook),
    ("eslint", "ESLint", {"eslint"}, _eslint),
    ("stylelint", "Stylelint", {"stylelint"}, _stylelint),
    ("webpack", "Webpack / build tooling", {"webpack"}, _webpack),
    ("typescript", "TypeScript tooling", {"typescript", "tsc"}, _typescript),
    ("react", "React", {"react", "react-dom"}, _react),
)


def _subjects(predicate: str) -> tuple[str, ...]:
    result: list[str] = []
    for family in str(predicate or "").split("|"):
        _head, separator, subject = family.partition(":")
        value = _norm(subject if separator else "")
        if value and value not in result:
            result.append(value)
    return tuple(result)


def _rules_for(subjects: Sequence[str], focus_package: str) -> list[tuple[str, str, object]]:
    probes = list(subjects)
    if focus_package:
        probes.append(_norm(focus_package))
    found: list[tuple[str, str, object]] = []
    for cohort_id, label, rule_subjects, matcher in _RULES:
        if any(subject in rule_subjects or _base(subject) in rule_subjects or matcher(subject) for subject in probes):
            found.append((cohort_id, label, matcher))
    return found


def _neighbors(graph: Mapping[str, Iterable[str]], name: str) -> tuple[str, ...]:
    return tuple(sorted({str(item) for item in graph.get(name, ()) if str(item)}))


def _distance_from_seeds(graph: Mapping[str, Iterable[str]], seeds: set[str], max_depth: int = 2) -> dict[str, int]:
    distance = {name: 0 for name in seeds}
    queue = deque(sorted(seeds))
    while queue:
        current = queue.popleft()
        depth = distance[current]
        if depth >= max_depth:
            continue
        for neighbor in _neighbors(graph, current):
            if neighbor in distance:
                continue
            distance[neighbor] = depth + 1
            queue.append(neighbor)
    return distance


def _priority(value: str) -> str:
    normalized = _norm(value)
    return normalized if normalized in {"required", "critical", "high", "runtime", "normal", "dev"} else "normal"


def infer_baseline_cohort(
    *,
    predicate: str,
    direct_packages: Iterable[str],
    focus_package: str = "",
    subject_consumers: Optional[Mapping[str, Iterable[str]]] = None,
    interaction_graph: Optional[Mapping[str, Iterable[str]]] = None,
    policy_by_package: Optional[Mapping[str, str]] = None,
    package_priority: Optional[Mapping[str, str]] = None,
    previous_deferred: Sequence[Mapping[str, object]] = (),
    repeated_count: int = 1,
    max_packages: int = 14,
) -> Optional[CohortSuggestion]:
    """Infer an actionable direct-package neighborhood without creating authority."""
    direct = tuple(sorted({str(name) for name in direct_packages if str(name).strip()}))
    if not direct:
        return None
    direct_by_norm = {_norm(name): name for name in direct}
    graph = {str(name): set(map(str, values)) for name, values in (interaction_graph or {}).items()}
    # Accept either adjacency direction from callers; cohort navigation uses an
    # undirected interaction neighborhood even when metadata originated from a
    # directed dependency edge.
    for left, neighbors in list(graph.items()):
        for right in tuple(neighbors):
            graph.setdefault(right, set()).add(left)
    consumers = {str(key).lower(): {str(v) for v in values} for key, values in (subject_consumers or {}).items()}
    policies = {str(k): _norm(v) for k, v in (policy_by_package or {}).items()}
    priorities = {str(k): _priority(str(v)) for k, v in (package_priority or {}).items()}
    subjects = _subjects(predicate)
    focus = str(focus_package or "").strip()
    matched_rules = _rules_for(subjects, focus)

    seeds: set[str] = set()
    reasons: list[str] = []
    for subject in subjects:
        direct_name = direct_by_norm.get(subject)
        if direct_name:
            seeds.add(direct_name)
            reasons.append("predicate-direct-package")
        for consumer in consumers.get(subject, ()):
            if consumer in direct:
                seeds.add(consumer)
                reasons.append("reverse-transitive-consumer")
    if focus and focus in direct:
        seeds.add(focus)
        reasons.append("confirmed-direct-focus")

    if len(matched_rules) == 1:
        cohort_id, label, matcher = matched_rules[0]
    elif len(matched_rules) > 1:
        cohort_id, label, matcher = "cross-ecosystem", "Cross-ecosystem compatibility region", None
        reasons.append("multiple-ecosystem-priors")
    else:
        cohort_id, label, matcher = "interaction", "Связанная compatibility-группа", None

    prior_matches = {name for name in direct if matcher is not None and matcher(name)}
    if matched_rules:
        reasons.append("ecosystem-prior")

    # Prefer graph/reverse-dependency evidence. Static ecosystem membership is a
    # fallback/expansion prior, not sufficient proof that every package belongs.
    if not seeds and prior_matches:
        seeds.update(sorted(prior_matches)[:4])
        reasons.append("ecosystem-seed-fallback")
    if not seeds:
        return None

    distance = _distance_from_seeds(graph, seeds, max_depth=2)
    candidates = set(seeds)
    for name in prior_matches:
        if name in distance and distance[name] <= 1:
            candidates.add(name)
    for seed in tuple(seeds):
        for neighbor in _neighbors(graph, seed):
            if neighbor in direct and (matcher is None or matcher(neighbor) or neighbor in prior_matches):
                candidates.add(neighbor)
                reasons.append("interaction-neighbor")
    if len(candidates) < 2 and prior_matches:
        candidates.update(prior_matches)
        reasons.append("ecosystem-small-neighborhood-fallback")

    expanded_from = ""
    previous_packages: set[str] = set()
    for item in previous_deferred:
        previous_id = str(item.get("id") or "")
        previous_predicate = str(item.get("predicate") or "")
        if previous_id != cohort_id and previous_predicate != str(predicate or ""):
            continue
        values = item.get("packages")
        if isinstance(values, (list, tuple)):
            previous_packages.update(str(value) for value in values if str(value) in direct)
        expanded_from = previous_id or cohort_id
        break
    if previous_packages:
        candidates.update(previous_packages)
        if int(repeated_count or 0) >= 2:
            boundary = set()
            for name in previous_packages:
                boundary.update(_neighbors(graph, name))
            expandable = [name for name in sorted(boundary - previous_packages) if name in direct]
            # Expand only a bounded shell. It remains diagnostic and every final
            # assignment is still globally verified.
            for name in expandable[:4]:
                candidates.add(name)
            if expandable:
                reasons.append("repeated-boundary-expansion")

    blocked: list[str] = []
    warnings: list[str] = []
    deferrable: list[str] = []
    for name in sorted(candidates):
        policy = policies.get(name, "auto")
        priority = priorities.get(name, "normal")
        if policy == "required" or priority == "critical":
            blocked.append(name)
            continue
        if priority == "high":
            warnings.append(name)
        if policy == "auto":
            deferrable.append(name)
    if not deferrable:
        return None

    def rank(name: str) -> tuple[int, int, str]:
        subject_rank = 0 if _norm(name) in subjects else 1
        graph_rank = distance.get(name, 99)
        return (subject_rank, graph_rank, _norm(name))

    deferrable = sorted(deferrable, key=rank)[: max(1, int(max_packages))]
    chosen = set(deferrable) | set(blocked)
    boundary_packages = sorted({
        neighbor
        for name in chosen
        for neighbor in _neighbors(graph, name)
        if neighbor in direct and neighbor not in chosen
    })[:8]

    confidence = 0.42
    if "predicate-direct-package" in reasons:
        confidence += 0.15
    if "reverse-transitive-consumer" in reasons:
        confidence += 0.22
    if "confirmed-direct-focus" in reasons:
        confidence += 0.10
    if matched_rules:
        confidence += 0.12
    if "interaction-neighbor" in reasons:
        confidence += 0.06
    if int(repeated_count or 0) >= 2:
        confidence += 0.06
        reasons.append("repeated-predicate")
    if expanded_from:
        confidence += 0.03
    confidence = min(0.95, round(confidence, 2))

    identity = {
        "predicate": str(predicate or ""),
        "cohort": cohort_id,
        "packages": sorted(deferrable),
        "blocked": sorted(blocked),
        "expandedFrom": expanded_from,
    }
    decision_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:20]

    return CohortSuggestion(
        cohort_id=cohort_id,
        label=label,
        predicate=str(predicate or ""),
        subjects=subjects,
        packages=tuple(deferrable),
        blocked_packages=tuple(sorted(blocked)),
        warning_packages=tuple(sorted(warnings)),
        boundary_packages=tuple(boundary_packages),
        confidence=confidence,
        reasons=tuple(dict.fromkeys(reasons)),
        decision_id=decision_id,
        expanded_from=expanded_from,
    )
