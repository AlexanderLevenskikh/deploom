#!/usr/bin/env python3
"""Block V predicate-guided active verification utilities.

The ranker returns *all* still-unobserved exact version points.  It cannot prune
an authoritative solver candidate, create a solver clause, or convert an
external hint / predicate observation into authority.  Hints and point evidence
may affect experiment ordering and a soft solver preference only.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Mapping, Optional, Sequence

HINT_AUTHORITY = "DIAGNOSTIC_HINT"
OBSERVATION_AUTHORITY = "POINT_EVIDENCE"
PREDICATE_SEARCH_SCHEMA = 1


@dataclasses.dataclass(frozen=True)
class DiagnosticHint:
    package: str
    predicate: str
    version: str
    confidence: str = "medium"
    source: str = ""
    summary: str = ""


@dataclasses.dataclass(frozen=True)
class PredicateObservation:
    package: str
    version: str
    predicate: str
    present: bool
    assignment_fingerprint: str = ""
    other_predicates: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ProbeCandidate:
    package: str
    version: str
    predicate: str
    score: tuple[int, int, int, int, tuple[int, int, int, int, str]]
    reasons: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class PredicateProbePolicy:
    """Bounded diagnostic exploration policy.

    This policy controls only experiment *cost*.  It never changes the solver
    domain or proof authority.  The defaults are intentionally conservative:
    active probing starts only after the same package/predicate has been freshly
    observed at two distinct failing direct versions.
    """

    enabled: bool = True
    repeat_threshold: int = 2
    probe_budget: int = 3

    @classmethod
    def from_sources(
        cls,
        config: Optional[Mapping[str, object]] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> "PredicateProbePolicy":
        raw = dict(config or {})
        env = environment if environment is not None else os.environ

        enabled = _coerce_bool(raw.get("predicateActiveSearch"), True)
        if "DEPLOOM_PREDICATE_ACTIVE_SEARCH" in env:
            enabled = _coerce_bool(env.get("DEPLOOM_PREDICATE_ACTIVE_SEARCH"), enabled)

        repeat_threshold = _bounded_int(
            raw.get("predicateProbeRepeatThreshold", 2), minimum=2, maximum=8, default=2
        )
        if env.get("DEPLOOM_PREDICATE_REPEAT_THRESHOLD"):
            repeat_threshold = _bounded_int(
                env.get("DEPLOOM_PREDICATE_REPEAT_THRESHOLD"),
                minimum=2,
                maximum=8,
                default=repeat_threshold,
            )

        probe_budget = _bounded_int(
            raw.get("predicateProbeBudget", 3), minimum=1, maximum=8, default=3
        )
        if env.get("DEPLOOM_PREDICATE_PROBE_BUDGET"):
            probe_budget = _bounded_int(
                env.get("DEPLOOM_PREDICATE_PROBE_BUDGET"),
                minimum=1,
                maximum=8,
                default=probe_budget,
            )
        return cls(
            enabled=bool(enabled),
            repeat_threshold=repeat_threshold,
            probe_budget=probe_budget,
        )


def _coerce_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    return default


def _bounded_int(value: object, *, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _semver_key(value: str) -> tuple[int, int, int, int, str]:
    text = str(value or "").strip()
    # npm versions can include pre-release/build metadata.  We need a stable
    # order for probe selection, not a replacement for the solver's semver.
    match = re.match(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:-([^+]+))?(?:\+.*)?$", text)
    if not match:
        return (0, 0, 0, -1, text)
    major, minor, patch = (int(match.group(i)) for i in range(1, 4))
    prerelease = match.group(4)
    # Stable releases sort after prereleases with the same core.
    return (major, minor, patch, 1 if prerelease is None else 0, prerelease or "")


def load_hint_snapshot(path: str | Path | None) -> tuple[DiagnosticHint, ...]:
    """Fail-open local snapshot loader. Malformed records are ignored, not authority."""
    if not path:
        return ()
    source = Path(path)
    if not source.is_file():
        return ()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ()
    if not isinstance(payload, dict) or payload.get("schemaVersion") != PREDICATE_SEARCH_SCHEMA:
        return ()
    records = payload.get("hints")
    if not isinstance(records, list):
        return ()
    hints: list[DiagnosticHint] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        package = str(item.get("package") or "").strip()
        predicate = str(item.get("predicate") or "").strip()
        version = str(item.get("version") or "").strip()
        if not package or not predicate or not version:
            continue
        hints.append(DiagnosticHint(
            package=package,
            predicate=predicate,
            version=version,
            confidence=str(item.get("confidence") or "medium"),
            source=str(item.get("source") or ""),
            summary=str(item.get("summary") or ""),
        ))
    return tuple(sorted(hints, key=lambda item: (
        item.package.lower(), item.predicate, _semver_key(item.version), item.source
    )))


def _matching_hint_score(
    package: str,
    predicate: str,
    version: str,
    hints: Sequence[DiagnosticHint],
) -> tuple[int, tuple[str, ...]]:
    confidence_score = {"high": 3, "strong": 3, "medium": 2, "low": 1}
    score = 0
    reasons: list[str] = []
    for hint in hints:
        if hint.package.lower() != package.lower():
            continue
        if hint.predicate != predicate:
            continue
        if hint.version != version:
            continue
        value = confidence_score.get(hint.confidence.lower(), 1)
        score = max(score, value)
        reasons.append(
            "external-hint:" + (hint.source or hint.summary or hint.version)
        )
    return score, tuple(sorted(set(reasons)))


def _stable_observed_status(
    *,
    package: str,
    predicate: str,
    observations: Sequence[PredicateObservation],
) -> dict[str, bool]:
    """Return only exact version points whose observations do not disagree.

    Conflicting observations can occur because the same direct version was
    probed under different surrounding assignments or because project behavior
    is nondeterministic.  Such a point must provide *no* boundary implication.
    It remains eligible for a future diagnostic probe.  This fixes the old
    first-observation-wins behavior while preserving the no-pruning invariant.
    """
    values: dict[str, set[bool]] = {}
    for item in observations:
        if item.package.lower() != package.lower() or item.predicate != predicate:
            continue
        values.setdefault(item.version, set()).add(bool(item.present))
    return {
        version: next(iter(statuses))
        for version, statuses in values.items()
        if len(statuses) == 1
    }


def predicate_repeat_count(
    *,
    package: str,
    predicate: str,
    observations: Sequence[PredicateObservation],
) -> int:
    """Count distinct, non-conflicting exact versions where the predicate was present."""
    status = _stable_observed_status(
        package=package, predicate=predicate, observations=observations
    )
    return sum(1 for present in status.values() if present)


def _boundary_score(
    version: str,
    ordered: Sequence[str],
    observations: Mapping[str, bool],
) -> tuple[int, str]:
    """Information heuristic only; never infers status for unobserved versions."""
    if not observations or version not in ordered:
        return 0, ""
    index = ordered.index(version)
    known = sorted(
        (ordered.index(candidate), status)
        for candidate, status in observations.items()
        if candidate in ordered
    )
    best = 0
    reason = ""
    for left_pos, left_status in known:
        for right_pos, right_status in known:
            if left_pos >= right_pos or left_status == right_status:
                continue
            if not (left_pos < index < right_pos):
                continue
            midpoint = (left_pos + right_pos) / 2.0
            span = right_pos - left_pos
            closeness = max(1, int(1000 - abs(index - midpoint) * 1000 / max(1, span)))
            if closeness > best:
                best = closeness
                reason = "predicate-status-bracket-midpoint"
    return best, reason


def _exploration_score(version: str, ordered: Sequence[str], observed: set[str]) -> int:
    """Prefer points far from already tested points to avoid neighbour crawling."""
    if version not in ordered:
        return 0
    index = ordered.index(version)
    observed_positions = [ordered.index(item) for item in observed if item in ordered]
    if not observed_positions:
        # First probe: favour newest exact version because the solver normally
        # optimizes toward useful/newer updates. This is only ordering.
        return index
    return min(abs(index - position) for position in observed_positions)


def rank_version_probes(
    *,
    package: str,
    predicate: str,
    versions: Sequence[str],
    observations: Sequence[PredicateObservation] = (),
    hints: Sequence[DiagnosticHint] = (),
) -> tuple[ProbeCandidate, ...]:
    """Return a deterministic permutation of every non-stably-observed version.

    Completeness invariant: a heuristic never removes an authoritative solver
    candidate.  Exact points with contradictory observations are intentionally
    considered ambiguous and therefore remain probe-eligible.
    """
    unique = sorted({str(item) for item in versions if str(item)}, key=_semver_key)
    observed_status = _stable_observed_status(
        package=package, predicate=predicate, observations=observations
    )
    observed_versions = set(observed_status)
    result: list[ProbeCandidate] = []
    for version in unique:
        if version in observed_versions:
            continue
        hint_score, hint_reasons = _matching_hint_score(
            package, predicate, version, hints
        )
        boundary, boundary_reason = _boundary_score(
            version, unique, observed_status
        )
        exploration = _exploration_score(version, unique, observed_versions)
        semver = _semver_key(version)
        reasons = list(hint_reasons)
        if boundary_reason:
            reasons.append(boundary_reason)
        if exploration:
            reasons.append("diversity-from-observed-points")
        # Larger tuple wins. Final semver tie-break remains deterministic.
        score = (hint_score, boundary, exploration, semver[3], semver)
        result.append(ProbeCandidate(
            package=package,
            version=version,
            predicate=predicate,
            score=score,
            reasons=tuple(sorted(set(reasons))) or ("deterministic-fallback",),
        ))
    result.sort(key=lambda item: item.score, reverse=True)

    expected = set(unique) - observed_versions
    actual = {item.version for item in result}
    if expected != actual or len(result) != len(actual):
        raise AssertionError("PREDICATE_PROBE_RANKER_PRUNED_CANDIDATES")
    return tuple(result)


def controlled_probe_assignment(
    base_assignment: Mapping[str, str],
    *,
    package: str,
    version: str,
) -> dict[str, str]:
    """Change exactly one solver-owned direct dimension for a diagnostic intervention."""
    if package not in base_assignment:
        raise KeyError(f"PREDICATE_PROBE_PACKAGE_NOT_IN_ASSIGNMENT: {package}")
    result = dict(base_assignment)
    result[package] = str(version)
    changed = [name for name in result if result[name] != base_assignment.get(name)]
    if changed != [package]:
        raise AssertionError("PREDICATE_PROBE_CHANGED_MULTIPLE_DIMENSIONS")
    return dict(sorted(result.items()))


def predicate_package(predicate: str) -> str:
    """Extract a package only from predicate families that explicitly carry one."""
    text = str(predicate or "")
    for prefix in (
        "ts-module-resolution:",
        "esm-cjs:",
        "duplicate-type-universe:",
    ):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


def prioritize_probe_preference(
    versions: Sequence[str],
    *,
    preferred_version: str,
    current_version: str,
) -> list[str]:
    """Move one diagnostic fallback directly behind the canonical first choice.

    The first domain element (normally the desired/exact policy target) is never
    displaced.  The current/source version is never promoted by diagnostic
    evidence.  The output is a permutation of the exact same version set.
    """
    original = list(dict.fromkeys(str(item) for item in versions if str(item)))
    preferred = str(preferred_version or "")
    if (
        not original
        or not preferred
        or preferred == str(current_version or "")
        or preferred not in original
        or preferred == original[0]
    ):
        return original
    reordered = [original[0], preferred] + [
        item for item in original[1:] if item != preferred
    ]
    if len(reordered) != len(original) or set(reordered) != set(original):
        raise AssertionError("PREDICATE_PROBE_PREFERENCE_PRUNED_DOMAIN")
    return reordered
