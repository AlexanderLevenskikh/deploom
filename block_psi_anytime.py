"""Proof-neutral anytime policy for DepLoom Baseline (Block Psi)."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from enum import Enum
import time
from typing import Callable, Mapping, Optional

class BaselineSearchMode(str, Enum):
    AUTO = "AUTO"
    BOUNDED_IMPROVEMENT = "BOUNDED_IMPROVEMENT"
    EXHAUSTIVE = "EXHAUSTIVE"

class BaselineCompletionStatus(str, Enum):
    VERIFIED_GOOD_ENOUGH = "VERIFIED_GOOD_ENOUGH"
    VERIFIED_TARGET_COMPLETE = "VERIFIED_TARGET_COMPLETE"
    VERIFIED_PARTIAL_SCOPE = "VERIFIED_PARTIAL_SCOPE"
    SEARCH_BUDGET_EXHAUSTED_WITH_INCUMBENT = "SEARCH_BUDGET_EXHAUSTED_WITH_INCUMBENT"
    SEARCH_BUDGET_EXHAUSTED_NO_INCUMBENT = "SEARCH_BUDGET_EXHAUSTED_NO_INCUMBENT"
    USER_STOPPED_WITH_INCUMBENT = "USER_STOPPED_WITH_INCUMBENT"
    USER_STOPPED_NO_INCUMBENT = "USER_STOPPED_NO_INCUMBENT"

class ContinuationReason(str, Enum):
    AUTOMATIC_BUDGET_EXHAUSTED = "AUTOMATIC_BUDGET_EXHAUSTED"
    STAGNATION = "STAGNATION"
    BASE_ITERATION_LIMIT = "BASE_ITERATION_LIMIT"
    EXPENSIVE_DIAGNOSTIC = "EXPENSIVE_DIAGNOSTIC"

@dataclass(frozen=True)
class BestVerifiedIncumbent:
    assignment: dict[str, str]
    assignment_identity: str
    changed_dependency_count: int
    policy_score: float
    yellow_coverage: float
    distance_from_desired: int
    deferred_targets: tuple[str, ...]
    verification_evidence: str
    verified_at: str
    run_identity: str
    objective_rank: tuple[int, int, int]

    def to_json(self) -> dict[str, object]:
        value = asdict(self)
        value["deferred_targets"] = list(self.deferred_targets)
        value["objective_rank"] = list(self.objective_rank)
        return value

    @classmethod
    def from_json(cls, value: Mapping[str, object]) -> Optional["BestVerifiedIncumbent"]:
        try:
            raw = value.get("assignment")
            rank = tuple(int(v) for v in value.get("objective_rank", ()))
            if not isinstance(raw, Mapping) or len(rank) != 3:
                return None
            return cls(
                {str(k): str(v) for k, v in raw.items()},
                str(value.get("assignment_identity") or ""),
                max(0, int(value.get("changed_dependency_count") or 0)),
                float(value.get("policy_score") or 0),
                float(value.get("yellow_coverage") or 0),
                max(0, int(value.get("distance_from_desired") or 0)),
                tuple(str(v) for v in value.get("deferred_targets", ()) if str(v)),
                str(value.get("verification_evidence") or ""),
                str(value.get("verified_at") or ""),
                str(value.get("run_identity") or ""),
                rank,
            )
        except (TypeError, ValueError):
            return None

@dataclass(frozen=True)
class AutomaticBudgetPolicy:
    wall_clock_seconds: float = 1800
    max_expensive_attempts: int = 8
    max_attempts_without_improvement: int = 3
    stagnation_rejections: int = 3
    pre_incumbent_minimization_probes: int = 2
    pre_incumbent_minimization_seconds: float = 300

@dataclass
class BaselineAnytimeState:
    policy: AutomaticBudgetPolicy = field(default_factory=AutomaticBudgetPolicy)
    search_mode: BaselineSearchMode = BaselineSearchMode.AUTO
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    elapsed_before_resume_seconds: float = 0
    expensive_attempts: int = 0
    rejected_assignments: int = 0
    consecutive_without_improvement: int = 0
    learned_constraints_at_last_rejection: int = 0
    repeated_predicate: str = ""
    repeated_predicate_count: int = 0
    search_strategy: str = "exact-neighbor-refinement"
    candidate_duration_ewma_seconds: float = 0
    incumbent: Optional[BestVerifiedIncumbent] = None
    desired_assignment: dict[str, str] = field(default_factory=dict)
    desired_identity: str = ""
    continuation_reason: Optional[ContinuationReason] = None
    exhaustive_authorized: bool = False

    def __post_init__(self) -> None:
        self.started_at = self.clock()
        self.exhaustive_authorized = self.search_mode == BaselineSearchMode.EXHAUSTIVE

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.elapsed_before_resume_seconds + self.clock() - self.started_at)

    @property
    def stagnating(self) -> bool:
        n = self.policy.stagnation_rejections
        return self.repeated_predicate_count >= n and self.consecutive_without_improvement >= n

    def observe_candidate(self, *, duration_seconds: float, passed: bool,
                          predicate: str = "", learned_constraints: int = 0) -> bool:
        duration = max(0.0, float(duration_seconds))
        self.expensive_attempts += 1
        self.candidate_duration_ewma_seconds = duration if not self.candidate_duration_ewma_seconds else (
            .35 * duration + .65 * self.candidate_duration_ewma_seconds
        )
        if passed:
            self.consecutive_without_improvement = 0
            self.repeated_predicate = ""
            self.repeated_predicate_count = 0
            return False
        self.rejected_assignments += 1
        self.consecutive_without_improvement += 1
        predicate = str(predicate or "").strip()
        if predicate and predicate == self.repeated_predicate:
            self.repeated_predicate_count += 1
        elif predicate:
            self.repeated_predicate, self.repeated_predicate_count = predicate, 1
        else:
            self.repeated_predicate, self.repeated_predicate_count = "", 0
        delta = max(0, int(learned_constraints) - self.learned_constraints_at_last_rejection)
        self.learned_constraints_at_last_rejection = max(self.learned_constraints_at_last_rejection, int(learned_constraints))
        if self.stagnating and delta == 0:
            self.search_strategy = "target-cohort-relaxation"
            self.continuation_reason = ContinuationReason.STAGNATION
            return True
        return False

    def automatic_continuation_reason(self, *, base_iteration_limit_hit: bool = False) -> Optional[ContinuationReason]:
        if self.exhaustive_authorized:
            return None
        if self.stagnating:
            return ContinuationReason.STAGNATION
        if base_iteration_limit_hit:
            return ContinuationReason.BASE_ITERATION_LIMIT
        if self.elapsed_seconds >= self.policy.wall_clock_seconds:
            return ContinuationReason.AUTOMATIC_BUDGET_EXHAUSTED
        if self.expensive_attempts >= self.policy.max_expensive_attempts:
            return ContinuationReason.AUTOMATIC_BUDGET_EXHAUSTED
        if self.consecutive_without_improvement >= self.policy.max_attempts_without_improvement:
            return ContinuationReason.STAGNATION
        cost = self.candidate_duration_ewma_seconds
        if cost and self.elapsed_seconds + cost > self.policy.wall_clock_seconds:
            return ContinuationReason.AUTOMATIC_BUDGET_EXHAUSTED
        return None

    def update_incumbent(self, candidate: BestVerifiedIncumbent) -> bool:
        if self.incumbent and candidate.objective_rank >= self.incumbent.objective_rank:
            return False
        self.incumbent = candidate
        self.consecutive_without_improvement = 0
        return True

    def completion_status(self, desired_identity: str) -> Optional[BaselineCompletionStatus]:
        if not self.incumbent:
            return None
        return (BaselineCompletionStatus.VERIFIED_TARGET_COMPLETE
                if self.incumbent.assignment_identity == desired_identity
                else BaselineCompletionStatus.VERIFIED_GOOD_ENOUGH)

    def continuation_payload(self, *, project: str, mode: str, iteration: int,
                             reason: ContinuationReason) -> dict[str, object]:
        incumbent = self.incumbent.to_json() if self.incumbent else None
        return {
            "schemaVersion": 2, "event": "BASELINE_CONTINUATION_REQUIRED",
            "reason": reason.value, "project": project, "mode": mode,
            "iteration": max(0, int(iteration)), "elapsedSeconds": round(self.elapsed_seconds, 3),
            "bestIncumbent": incumbent,
            "incumbentQuality": list(self.incumbent.objective_rank) if self.incumbent else None,
            "rejectedAssignments": self.rejected_assignments,
            "repeatedPredicate": self.repeated_predicate or None,
            "observedCandidateDurationSeconds": round(self.candidate_duration_ewma_seconds, 3),
            "continuationCostClass": "hours" if self.candidate_duration_ewma_seconds >= 1800 else "minutes",
            "recommendedAction": "USE_CURRENT_RESULT" if incumbent else "FIND_GOOD_ENOUGH",
            "availableActions": (["USE_CURRENT_RESULT", "IMPROVE_BOUNDED", "IMPROVE_EXHAUSTIVE", "STOP_SAVE_PROGRESS"]
                if incumbent else ["FIND_GOOD_ENOUGH", "CONTINUE_BOUNDED", "CONTINUE_EXHAUSTIVE", "STOP_SAVE_PROGRESS"]),
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "searchMode": self.search_mode.value, "searchStrategy": self.search_strategy,
            "elapsedSeconds": round(self.elapsed_seconds, 3), "expensiveAttempts": self.expensive_attempts,
            "rejectedAssignments": self.rejected_assignments,
            "consecutiveWithoutImprovement": self.consecutive_without_improvement,
            "learnedConstraintsAtLastRejection": self.learned_constraints_at_last_rejection,
            "repeatedPredicate": self.repeated_predicate, "repeatedPredicateCount": self.repeated_predicate_count,
            "observedCandidateDurationSeconds": self.candidate_duration_ewma_seconds,
            "exhaustiveAuthorized": self.exhaustive_authorized,
            "continuationReason": self.continuation_reason.value if self.continuation_reason else "",
            "bestVerifiedIncumbent": self.incumbent.to_json() if self.incumbent else None,
            "desiredAssignment": dict(self.desired_assignment), "desiredAssignmentIdentity": self.desired_identity,
        }

    def restore(self, value: Mapping[str, object]) -> None:
        try:
            self.elapsed_before_resume_seconds = max(0.0, float(value.get("elapsedSeconds") or 0))
            for key, attr in (("expensiveAttempts", "expensive_attempts"), ("rejectedAssignments", "rejected_assignments"),
                              ("consecutiveWithoutImprovement", "consecutive_without_improvement"),
                              ("learnedConstraintsAtLastRejection", "learned_constraints_at_last_rejection"),
                              ("repeatedPredicateCount", "repeated_predicate_count")):
                setattr(self, attr, max(0, int(value.get(key) or 0)))
            self.repeated_predicate = str(value.get("repeatedPredicate") or "")
            self.candidate_duration_ewma_seconds = max(0.0, float(value.get("observedCandidateDurationSeconds") or 0))
            self.search_strategy = str(value.get("searchStrategy") or self.search_strategy)
            self.exhaustive_authorized = bool(value.get("exhaustiveAuthorized", False))
            desired = value.get("desiredAssignment")
            if isinstance(desired, Mapping):
                self.desired_assignment = {str(k): str(v) for k, v in desired.items()}
            self.desired_identity = str(value.get("desiredAssignmentIdentity") or "")
            raw = value.get("bestVerifiedIncumbent")
            if isinstance(raw, Mapping):
                self.incumbent = BestVerifiedIncumbent.from_json(raw)
            reason = str(value.get("continuationReason") or "")
            self.continuation_reason = ContinuationReason(reason) if reason else None
        except (TypeError, ValueError):
            return
