#!/usr/bin/env python3
"""Block V-C predicate-guided active verification utilities.

The module ranks *all* still-unobserved exact version points.  It cannot prune a
candidate, create a solver clause, or convert an external hint into authority.
That type/API boundary is intentional: hints may affect cost, never correctness.
"""
from __future__ import annotations

import dataclasses
import json
import math
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

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
    distance = min(abs(index - position) for position in observed_positions)
    return distance


def rank_version_probes(
    *,
    package: str,
    predicate: str,
    versions: Sequence[str],
    observations: Sequence[PredicateObservation] = (),
    hints: Sequence[DiagnosticHint] = (),
) -> tuple[ProbeCandidate, ...]:
    """Return a deterministic permutation of every unobserved exact version.

    Completeness invariant: the returned version set is exactly
    ``versions - already_observed_versions``.  No heuristic can remove a point.
    """
    unique = sorted({str(item) for item in versions if str(item)}, key=_semver_key)
    relevant = [
        item for item in observations
        if item.package.lower() == package.lower() and item.predicate == predicate
    ]
    observed_status: dict[str, bool] = {}
    for item in relevant:
        # If repeated exact point observations disagree, treat it as observed but
        # give it no range implication. The caller should flag nondeterminism.
        if item.version in observed_status and observed_status[item.version] != item.present:
            continue
        observed_status[item.version] = item.present
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
    """Change exactly one solver-owned dimension for a diagnostic intervention."""
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
