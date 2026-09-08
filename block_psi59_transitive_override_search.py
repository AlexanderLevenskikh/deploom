#!/usr/bin/env python3
"""Ψ.5.9 evidence-bounded transitive resolver override search.

This module is deliberately proof-neutral. It may propose a package-manager
control candidate only when an exact physically produced Yarn Classic lockfile
shows a duplicate transitive subject and all of that subject's parseable lock
selectors admit one exact common version. The ordinary resolver/lifecycle/project
pipeline remains the only authority that can accept the candidate.
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import os
import re
from pathlib import Path
from typing import Mapping, Optional, Sequence

from semantic_version import NpmSpec, Version

AUTHORITY = "DIAGNOSTIC_HINT"
DUPLICATE_TYPE_PREFIX = "duplicate-type-universe:"
SUPPORTED_MANAGER = "yarn"
ATTEMPT_STATE_PREFIX = "transitive-override:"


@dataclasses.dataclass(frozen=True)
class TransitiveOverridePolicy:
    enabled: bool = True
    per_assignment_candidates: int = 1
    family_physical_budget: int = 4

    @classmethod
    def from_sources(
        cls,
        config: Optional[Mapping[str, object]] = None,
        environment: Optional[Mapping[str, str]] = None,
    ) -> "TransitiveOverridePolicy":
        raw = dict(config or {})
        env = environment if environment is not None else os.environ

        enabled = _coerce_bool(raw.get("transitiveOverrideSearch"), True)
        if "DEPLOOM_TRANSITIVE_OVERRIDE_SEARCH" in env:
            enabled = _coerce_bool(
                env.get("DEPLOOM_TRANSITIVE_OVERRIDE_SEARCH"), enabled
            )

        per_assignment = _bounded_int(
            raw.get("transitiveOverrideCandidatesPerAssignment", 1),
            minimum=1,
            maximum=2,
            default=1,
        )
        if env.get("DEPLOOM_TRANSITIVE_OVERRIDE_CANDIDATES_PER_ASSIGNMENT"):
            per_assignment = _bounded_int(
                env.get("DEPLOOM_TRANSITIVE_OVERRIDE_CANDIDATES_PER_ASSIGNMENT"),
                minimum=1,
                maximum=2,
                default=per_assignment,
            )

        family_budget = _bounded_int(
            raw.get("transitiveOverrideFamilyBudget", 4),
            minimum=1,
            maximum=6,
            default=4,
        )
        if env.get("DEPLOOM_TRANSITIVE_OVERRIDE_FAMILY_BUDGET"):
            family_budget = _bounded_int(
                env.get("DEPLOOM_TRANSITIVE_OVERRIDE_FAMILY_BUDGET"),
                minimum=1,
                maximum=6,
                default=family_budget,
            )
        return cls(
            enabled=bool(enabled),
            per_assignment_candidates=per_assignment,
            family_physical_budget=family_budget,
        )


@dataclasses.dataclass(frozen=True)
class YarnSubjectPoint:
    selector: str
    requested_range: str
    resolved_version: str


@dataclasses.dataclass(frozen=True)
class TransitiveDuplicateEvidence:
    package: str
    predicate: str
    manager: str
    resolved_state_key: str
    lockfile_hash: str
    selectors: tuple[str, ...]
    requested_ranges: tuple[str, ...]
    observed_versions: tuple[str, ...]
    points: tuple[YarnSubjectPoint, ...]
    authority: str = AUTHORITY


@dataclasses.dataclass(frozen=True)
class TransitiveOverrideCandidate:
    package: str
    version: str
    predicate: str
    manager: str
    manifest_path: str
    candidate_source: str
    evidence: TransitiveDuplicateEvidence
    reason: str = "transitive-yarn-lock-range-intersection"
    authority: str = AUTHORITY

    def as_resolver_override_proposal(self):
        # Import lazily so the evidence parser remains independently testable.
        from block_psi55_resolver_overrides import ResolverOverrideProposal

        return ResolverOverrideProposal(
            package=self.package,
            version=self.version,
            predicate=self.predicate,
            manager=self.manager,
            manifest_path=self.manifest_path,
            reason=self.reason,
        )


@dataclasses.dataclass(frozen=True)
class TransitiveOverrideAttemptPermit:
    granted: bool
    reason: str
    used: int
    remaining: int
    token: str
    authority: str = AUTHORITY


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


def _bounded_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _duplicate_subject(signatures: Sequence[str]) -> str:
    subjects = sorted({
        str(signature)[len(DUPLICATE_TYPE_PREFIX):].strip().lower()
        for signature in signatures
        if str(signature).startswith(DUPLICATE_TYPE_PREFIX)
        and str(signature)[len(DUPLICATE_TYPE_PREFIX):].strip()
    })
    return subjects[0] if len(subjects) == 1 else ""


def transitive_duplicate_subject(
    *,
    signatures: Sequence[str],
    managed_packages: Sequence[str],
    manager: str,
) -> str:
    """Return one transitive duplicate subject, never a managed direct package."""
    if str(manager or "").strip().lower() != SUPPORTED_MANAGER:
        return ""
    subject = _duplicate_subject(signatures)
    if not subject:
        return ""
    managed = {str(item).strip().lower() for item in managed_packages if str(item)}
    if subject in managed:
        # Ψ.5.5 already owns exact managed-direct subjects.
        return ""
    return subject


def _split_yarn_selector(selector: str) -> tuple[str, str] | None:
    text = str(selector or "").strip().strip('"').strip("'")
    if not text:
        return None
    marker = text.rfind("@")
    if marker <= 0 or marker >= len(text) - 1:
        return None
    package = text[:marker].strip()
    requested = text[marker + 1 :].strip()
    if not package or not requested:
        return None
    if package.startswith("@") and "/" not in package:
        return None
    return package, requested


def _parse_selector_header(line: str) -> tuple[str, ...]:
    body = str(line or "").strip()
    if not body.endswith(":"):
        return ()
    body = body[:-1]
    try:
        row = next(csv.reader([body], skipinitialspace=True))
    except (csv.Error, StopIteration):
        return ()
    return tuple(str(item).strip() for item in row if str(item).strip())


def parse_yarn1_subject_points(
    lockfile_bytes: bytes,
    *,
    package: str,
) -> tuple[YarnSubjectPoint, ...]:
    """Extract exact selector->resolved points for one Yarn Classic subject.

    Unknown syntax is not interpreted. A caller that needs range intersection
    must fail closed if any returned requested range is not valid npm semver.
    """
    try:
        text = lockfile_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ()
    subject = str(package or "").strip().lower()
    if not subject:
        return ()

    active: list[tuple[str, str]] = []
    result: list[YarnSubjectPoint] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        if line and not line[0].isspace() and line.endswith(":"):
            active = []
            if line.lstrip().startswith("#"):
                continue
            for selector in _parse_selector_header(line):
                parsed = _split_yarn_selector(selector)
                if parsed is None:
                    continue
                selector_package, requested = parsed
                if selector_package.lower() == subject:
                    active.append((selector, requested))
            continue

        if not active:
            continue
        match = re.match(r'^\s+version\s+["\']?([^"\'\s]+)["\']?\s*$', line)
        if not match:
            continue
        resolved = match.group(1).strip()
        try:
            Version(resolved)
        except ValueError:
            active = []
            continue
        for selector, requested in active:
            point = YarnSubjectPoint(
                selector=selector,
                requested_range=requested,
                resolved_version=resolved,
            )
            if point not in result:
                result.append(point)
        active = []
    return tuple(result)


def _validated_lockfile_bytes(
    *,
    proof_cache_dir: str | Path | None,
    resolved_state_key: str,
    resolved_state_artifact: str,
    resolved_lockfile_path: str,
    resolved_lockfile_hash: str,
) -> bytes | None:
    """Read only the exact hash-bound resolver artifact carried by the result."""
    if len(str(resolved_state_key or "")) != 64:
        return None
    expected_hash = str(resolved_lockfile_hash or "").strip().lower()
    if len(expected_hash) != 64:
        return None
    if Path(str(resolved_lockfile_path or "")).name.lower() != "yarn.lock":
        return None
    artifact_relative = str(resolved_state_artifact or "").strip()
    if not artifact_relative or proof_cache_dir is None:
        return None
    try:
        root = Path(proof_cache_dir).resolve()
        artifact = (root / Path(artifact_relative)).resolve()
        artifact.relative_to(root)
        if not artifact.is_file():
            return None
        payload = artifact.read_bytes()
    except (OSError, TypeError, ValueError):
        return None
    if hashlib.sha256(payload).hexdigest().lower() != expected_hash:
        return None
    return payload


def _specs_for_points(
    points: Sequence[YarnSubjectPoint],
) -> tuple[NpmSpec, ...] | None:
    specs: list[NpmSpec] = []
    for requested in dict.fromkeys(point.requested_range for point in points):
        try:
            specs.append(NpmSpec(str(requested)))
        except ValueError:
            # git/file/npm-alias/tag/workspace or otherwise ambiguous selector:
            # no transitive override candidate is authorized.
            return None
    return tuple(specs)


def _exact_version(value: str) -> Version | None:
    try:
        return Version(str(value).strip())
    except ValueError:
        return None


def _compatible_versions(
    values: Sequence[str],
    specs: Sequence[NpmSpec],
) -> list[str]:
    parsed: list[tuple[Version, str]] = []
    for raw in dict.fromkeys(str(item).strip() for item in values if str(item).strip()):
        version = _exact_version(raw)
        if version is None or version.prerelease:
            continue
        if all(spec.match(version) for spec in specs):
            parsed.append((version, raw))
    parsed.sort(key=lambda item: item[0], reverse=True)
    return [raw for _, raw in parsed]


def propose_transitive_duplicate_override_candidates(
    *,
    signatures: Sequence[str],
    managed_packages: Sequence[str],
    manager: str,
    proof_cache_dir: str | Path | None,
    resolved_state_key: str,
    resolved_state_artifact: str,
    resolved_lockfile_path: str,
    resolved_lockfile_hash: str,
    published_versions: Sequence[str],
    limit: int = 1,
) -> tuple[TransitiveOverrideCandidate, ...]:
    """Propose exact transitive overrides only from physical lock evidence.

    Required evidence:
      * exactly one duplicate-type subject;
      * subject is transitive (not solver-managed direct);
      * Yarn Classic exact lockfile artifact is hash-bound to the result;
      * at least two distinct resolved subject versions are present;
      * every subject lock selector is parseable as npm semver;
      * proposed exact version satisfies the intersection of every selector.
    """
    subject = transitive_duplicate_subject(
        signatures=signatures,
        managed_packages=managed_packages,
        manager=manager,
    )
    if not subject:
        return ()
    payload = _validated_lockfile_bytes(
        proof_cache_dir=proof_cache_dir,
        resolved_state_key=resolved_state_key,
        resolved_state_artifact=resolved_state_artifact,
        resolved_lockfile_path=resolved_lockfile_path,
        resolved_lockfile_hash=resolved_lockfile_hash,
    )
    if payload is None:
        return ()
    points = parse_yarn1_subject_points(payload, package=subject)
    if not points:
        return ()
    observed = _compatible_versions(
        tuple(point.resolved_version for point in points),
        (),
    )
    if len(set(observed)) < 2:
        return ()

    specs = _specs_for_points(points)
    if not specs:
        return ()

    observed_compatible = _compatible_versions(observed, specs)
    published_compatible = [
        version
        for version in _compatible_versions(published_versions, specs)
        if version not in set(observed_compatible)
    ]
    ranked = [
        (version, "observed-resolved") for version in observed_compatible
    ] + [
        (version, "registry-range-intersection")
        for version in published_compatible
    ]
    bounded_limit = max(1, min(int(limit or 1), 2))
    if not ranked:
        return ()

    evidence = TransitiveDuplicateEvidence(
        package=subject,
        predicate=f"{DUPLICATE_TYPE_PREFIX}{subject}",
        manager=SUPPORTED_MANAGER,
        resolved_state_key=str(resolved_state_key),
        lockfile_hash=str(resolved_lockfile_hash),
        selectors=tuple(sorted({point.selector for point in points})),
        requested_ranges=tuple(sorted({point.requested_range for point in points})),
        observed_versions=tuple(observed),
        points=tuple(points),
    )
    return tuple(
        TransitiveOverrideCandidate(
            package=subject,
            version=version,
            predicate=evidence.predicate,
            manager=SUPPORTED_MANAGER,
            manifest_path="resolutions",
            candidate_source=source,
            evidence=evidence,
        )
        for version, source in ranked[:bounded_limit]
    )


def transitive_attempt_state_package(subject_package: str) -> str:
    return f"{ATTEMPT_STATE_PREFIX}{str(subject_package or '').strip().lower()}"


def transitive_attempt_token(
    direct_assignment_fingerprint: str,
    override_version: str,
) -> str:
    context = str(direct_assignment_fingerprint or "").strip()
    version = str(override_version or "").strip()
    return f"{context}:{version}"


def reserve_transitive_override_attempt(
    *,
    predicate_state_store: object,
    policy: TransitiveOverridePolicy,
    project: str,
    mode: str,
    run_identity: str,
    predicate: str,
    subject_package: str,
    direct_assignment_fingerprint: str,
    override_version: str,
) -> TransitiveOverrideAttemptPermit:
    """Persist family-budget reservation before any physical verifier starts."""
    state_package = transitive_attempt_state_package(subject_package)
    token = transitive_attempt_token(
        direct_assignment_fingerprint,
        override_version,
    )
    session = predicate_state_store.load_session(
        project,
        mode,
        run_identity=run_identity,
        package=state_package,
        predicate=predicate,
    )
    attempted = tuple(dict.fromkeys(str(item) for item in session.attempted_versions))
    used = len(attempted)
    budget = int(policy.family_physical_budget)
    if token in attempted:
        return TransitiveOverrideAttemptPermit(
            False,
            "exact-context-already-attempted",
            used,
            max(0, budget - used),
            token,
        )
    if used >= budget:
        return TransitiveOverrideAttemptPermit(
            False,
            "transitive-override-family-budget-exhausted",
            used,
            0,
            token,
        )
    try:
        predicate_state_store.mark_attempt(
            project,
            mode,
            run_identity=run_identity,
            package=state_package,
            predicate=predicate,
            version=token,
        )
    except Exception as exc:
        return TransitiveOverrideAttemptPermit(
            False,
            f"reservation-persist-failed:{type(exc).__name__}",
            used,
            max(0, budget - used),
            token,
        )
    return TransitiveOverrideAttemptPermit(
        True,
        "reserved-before-physical-verification",
        used + 1,
        max(0, budget - used - 1),
        token,
    )
