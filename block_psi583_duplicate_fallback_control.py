"""Ψ.5.8.3 duplicate cohort-fallback control-flow hotfix.

This block is orchestration-only. It does not add proof authority, infer new
incompatibility, or alter solver constraints.
"""
from __future__ import annotations

import dataclasses

from block_psi582_pre_run_closure import CohortQueueDecision

AUTHORITY = "DIAGNOSTIC_HINT"


@dataclasses.dataclass(frozen=True)
class CohortHandoffControl:
    queue_fallback: bool
    publish_current_exact_now: bool
    steer_outer_iteration: bool
    continue_predicate_loop: bool
    reason: str
    authority: str = AUTHORITY


def cohort_handoff_control(
    decision: CohortQueueDecision,
) -> CohortHandoffControl:
    """Translate queue dedup into explicit nested-loop ownership."""
    if decision.queue:
        return CohortHandoffControl(
            queue_fallback=True,
            publish_current_exact_now=True,
            steer_outer_iteration=True,
            continue_predicate_loop=False,
            reason="queue-new-fallback",
        )
    return CohortHandoffControl(
        queue_fallback=False,
        publish_current_exact_now=False,
        steer_outer_iteration=False,
        continue_predicate_loop=True,
        reason=f"skip-known-fallback:{decision.reason}",
    )
