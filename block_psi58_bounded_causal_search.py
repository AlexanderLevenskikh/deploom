"""Ψ.5.8 bounded causal-search experiment economics.

This module reduces diagnostic physical experiments only. It never removes
versions from the authoritative solver domain, creates a clause, or upgrades
metadata/signature similarity into compatibility proof.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from typing import Mapping, Sequence

AUTHORITY = "DIAGNOSTIC_HINT"


def _bounded_int(value: object, *, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


@dataclasses.dataclass(frozen=True)
class CausalSearchPolicy:
    per_package_probe_budget: int = 2
    family_probe_budget: int = 6
    max_parents: int = 3
    representative_limit: int = 2

    @classmethod
    def from_sources(
        cls,
        config: Mapping[str, object] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> "CausalSearchPolicy":
        raw = dict(config or {})
        env = environment if environment is not None else os.environ
        per_package = _bounded_int(
            raw.get("causalProbeBudgetPerPackage", 2),
            minimum=1, maximum=4, default=2,
        )
        family = _bounded_int(
            raw.get("causalProbeBudgetPerFamily", 6),
            minimum=2, maximum=16, default=6,
        )
        max_parents = _bounded_int(
            raw.get("causalMaxParents", 3),
            minimum=1, maximum=8, default=3,
        )
        representative_limit = _bounded_int(
            raw.get("causalRepresentativeLimit", 2),
            minimum=1, maximum=4, default=2,
        )
        if env.get("DEPLOOM_CAUSAL_PROBE_BUDGET_PER_PACKAGE"):
            per_package = _bounded_int(
                env.get("DEPLOOM_CAUSAL_PROBE_BUDGET_PER_PACKAGE"),
                minimum=1, maximum=4, default=per_package,
            )
        if env.get("DEPLOOM_CAUSAL_PROBE_BUDGET_PER_FAMILY"):
            family = _bounded_int(
                env.get("DEPLOOM_CAUSAL_PROBE_BUDGET_PER_FAMILY"),
                minimum=2, maximum=16, default=family,
            )
        if env.get("DEPLOOM_CAUSAL_MAX_PARENTS"):
            max_parents = _bounded_int(
                env.get("DEPLOOM_CAUSAL_MAX_PARENTS"),
                minimum=1, maximum=8, default=max_parents,
            )
        return cls(
            per_package_probe_budget=per_package,
            family_probe_budget=max(family, per_package),
            max_parents=max_parents,
            representative_limit=min(representative_limit, per_package),
        )


def _semver_key(value: str) -> tuple[int, int, int, int, str]:
    text = str(value or "").strip()
    match = re.match(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:-([^+]+))?(?:\+.*)?$", text)
    if not match:
        return (0, 0, 0, -1, text)
    major, minor, patch = (int(match.group(i)) for i in range(1, 4))
    prerelease = match.group(4)
    return (major, minor, patch, 1 if prerelease is None else 0, prerelease or "")


def causal_version_signature(
    metadata: Mapping[str, object],
    *,
    version: str,
    relevant_packages: Sequence[str],
) -> str:
    """Hash only registry relations relevant to the observed causal neighborhood."""
    versions = metadata.get("versions")
    payload = versions.get(version) if isinstance(versions, Mapping) else None
    record = payload if isinstance(payload, Mapping) else {}
    relevant = {str(name).strip().lower() for name in relevant_packages if str(name).strip()}
    relations: list[tuple[str, str, str]] = []
    for section in ("dependencies", "peerDependencies", "optionalDependencies"):
        values = record.get(section)
        if not isinstance(values, Mapping):
            continue
        for raw_name, raw_range in values.items():
            name = str(raw_name or "").strip()
            # Peer surface is itself compatibility structure, so keep all peers.
            # Ordinary/optional dependencies stay restricted to the known causal
            # neighborhood to avoid signatures changing for unrelated metadata.
            if section != "peerDependencies" and name.lower() not in relevant:
                continue
            relations.append((section, name.lower(), str(raw_range or "").strip()))
    body = json.dumps(sorted(relations), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class BoundedCausalDomain:
    versions: tuple[str, ...]
    attempted_count: int
    distinct_signatures: int
    exhausted: bool
    reason: str
    authority: str = AUTHORITY


def bounded_causal_probe_domain(
    *,
    metadata: Mapping[str, object],
    versions: Sequence[str],
    relevant_packages: Sequence[str],
    attempted_versions: Sequence[str],
    policy: CausalSearchPolicy,
) -> BoundedCausalDomain:
    """Choose representative diagnostic points without pruning solver versions."""
    ordered = sorted({str(v) for v in versions if str(v)}, key=_semver_key, reverse=True)
    attempts = {str(v) for v in attempted_versions if str(v)}
    attempted_count = len(attempts)
    if attempted_count >= policy.per_package_probe_budget:
        return BoundedCausalDomain(
            (), attempted_count, 0, True, "per-package-physical-budget-exhausted"
        )

    attempted_signatures = {
        causal_version_signature(
            metadata, version=version, relevant_packages=relevant_packages
        )
        for version in attempts
    }
    by_signature: dict[str, list[str]] = {}
    for version in ordered:
        signature = causal_version_signature(
            metadata, version=version, relevant_packages=relevant_packages
        )
        by_signature.setdefault(signature, []).append(version)

    remaining_budget = max(0, policy.per_package_probe_budget - attempted_count)
    limit = min(policy.representative_limit, remaining_budget)
    representatives: list[str] = []
    for signature, grouped in sorted(
        by_signature.items(),
        key=lambda item: _semver_key(item[1][0]),
        reverse=True,
    ):
        if signature in attempted_signatures:
            continue
        candidate = next((v for v in grouped if v not in attempts), "")
        if candidate:
            representatives.append(candidate)
        if len(representatives) >= limit:
            break

    exhausted = not representatives
    reason = "all-causal-signatures-observed" if exhausted else "representative-causal-signatures"
    return BoundedCausalDomain(
        tuple(representatives),
        attempted_count,
        len(by_signature),
        exhausted,
        reason,
    )


def family_budget_exhausted(
    attempted_counts: Mapping[str, int],
    *,
    policy: CausalSearchPolicy,
) -> bool:
    return sum(max(0, int(value)) for value in attempted_counts.values()) >= (
        policy.family_probe_budget
    )
