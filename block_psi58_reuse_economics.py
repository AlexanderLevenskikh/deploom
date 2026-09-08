"""Ψ.5.8 proof-neutral ResolverSeed publication economics.

The module decides only whether an already-authoritative fresh resolver tree is
worth copying into the same-run ResolverSeed cache. It cannot create or reuse
ResolverProof/PreparationProof/ProjectProof and never affects solver authority.
"""
from __future__ import annotations

import dataclasses

AUTHORITY = "PERFORMANCE_ONLY"
RESOLVER_SEED_PUBLICATION_NONE = ""
RESOLVER_SEED_PUBLICATION_BRIDGE = "bridge-lifecycle"


@dataclasses.dataclass(frozen=True)
class ResolverSeedPublicationDecision:
    publish: bool
    reason: str
    authority: str = AUTHORITY


def resolver_seed_publication_decision(
    *,
    capability_enabled: bool,
    fresh_resolver: bool,
    run_project_checks: bool,
    publication_hint: str,
    verification_purpose: str,
) -> ResolverSeedPublicationDecision:
    """Publish only when a resolver-only pass intentionally bridges to lifecycle."""
    if not capability_enabled:
        return ResolverSeedPublicationDecision(False, "capability-disabled")
    if not fresh_resolver:
        return ResolverSeedPublicationDecision(False, "resolver-not-fresh")
    if run_project_checks:
        return ResolverSeedPublicationDecision(False, "inline-lifecycle-no-copy")
    purpose = str(verification_purpose or "").strip().lower()
    if purpose in {"baseline-control", "incumbent-promotion"}:
        return ResolverSeedPublicationDecision(False, "role-does-not-need-seed")
    hint = str(publication_hint or "").strip().lower()
    if hint != RESOLVER_SEED_PUBLICATION_BRIDGE:
        return ResolverSeedPublicationDecision(False, "no-expected-consumer")
    return ResolverSeedPublicationDecision(True, "expected-later-lifecycle")
