#!/usr/bin/env python3
"""Ψ.5.6 proof-neutral causal anytime search primitives.

The module translates observed structural predicates (which may name transitive
resolved packages) into ranked *direct* dependency decision variables.  The
projection changes experiment order only: it never creates a solver clause,
prunes a domain, or upgrades diagnostic evidence to proof authority.

PromisingAssignment is same-run scheduling state.  It preserves the exact
physical point that improved a diagnostic predicate so the ordinary Baseline
pipeline can verify that exact point next instead of asking the solver for a
merely similar assignment.
"""
from __future__ import annotations

import dataclasses
from typing import Iterable, Mapping, Sequence

from baseline_cohort_inference import infer_baseline_cohort
from block_v_predicate_search import predicate_package

AUTHORITY = "DIAGNOSTIC_HINT"


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _subjects(predicate: str) -> tuple[str, ...]:
    result: list[str] = []
    for family in str(predicate or "").split("|"):
        package = predicate_package(family)
        normalized = _norm(package)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _undirected_graph(
    graph: Mapping[str, Iterable[str]] | None,
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for raw_left, raw_neighbors in (graph or {}).items():
        left = str(raw_left or "").strip()
        if not left:
            continue
        result.setdefault(left, set())
        for raw_right in raw_neighbors:
            right = str(raw_right or "").strip()
            if not right or right == left:
                continue
            result[left].add(right)
            result.setdefault(right, set()).add(left)
    return result


@dataclasses.dataclass(frozen=True)
class CausalDecisionProjection:
    predicate: str
    subjects: tuple[str, ...]
    direct_packages: tuple[str, ...]
    ranked_packages: tuple[str, ...]
    reasons: tuple[tuple[str, tuple[str, ...]], ...]
    graph_backed_packages: tuple[str, ...]
    prior_only_packages: tuple[str, ...]
    confidence: float
    authority: str = AUTHORITY

    def reasons_for(self, package: str) -> tuple[str, ...]:
        for name, reasons in self.reasons:
            if name == package:
                return reasons
        return ()

    def reason_map(self) -> dict[str, list[str]]:
        return {name: list(reasons) for name, reasons in self.reasons}


@dataclasses.dataclass
class PromisingAssignmentQueue:
    """Same-run exact-point scheduler; persisted preferences cannot recreate it."""

    _items: dict[tuple[str, str], "PromisingAssignment"] = dataclasses.field(
        default_factory=dict
    )

    def offer(self, project: str, mode: str, candidate: "PromisingAssignment") -> None:
        self._items[(str(project), str(mode))] = candidate

    def pop(self, project: str, mode: str) -> "PromisingAssignment | None":
        return self._items.pop((str(project), str(mode)), None)

    def peek(self, project: str, mode: str) -> "PromisingAssignment | None":
        return self._items.get((str(project), str(mode)))

    def clear(self) -> None:
        self._items.clear()


@dataclasses.dataclass(frozen=True)
class PromisingAssignment:
    assignment: tuple[tuple[str, str], ...]
    assignment_fingerprint: str
    originating_predicate: str
    removed_predicates: tuple[str, ...]
    remaining_predicates: tuple[str, ...]
    preparation_proof_key: str = ""
    authority: str = AUTHORITY

    @property
    def assignment_dict(self) -> dict[str, str]:
        return dict(self.assignment)


def build_promising_assignment(
    *,
    assignment: Mapping[str, str],
    assignment_fingerprint: str,
    originating_predicate: str,
    removed_predicates: Sequence[str] = (),
    remaining_predicates: Sequence[str] = (),
    preparation_proof_key: str = "",
) -> PromisingAssignment:
    """Capture an exact same-run diagnostic point without promoting authority."""
    normalized_assignment = tuple(sorted(
        (str(name), str(version))
        for name, version in assignment.items()
        if str(name).strip() and str(version).strip()
    ))
    return PromisingAssignment(
        assignment=normalized_assignment,
        assignment_fingerprint=str(assignment_fingerprint or ""),
        originating_predicate=str(originating_predicate or ""),
        removed_predicates=tuple(sorted({str(item) for item in removed_predicates if str(item)})),
        remaining_predicates=tuple(sorted({str(item) for item in remaining_predicates if str(item)})),
        preparation_proof_key=str(preparation_proof_key or ""),
    )


def causal_decision_projection(
    *,
    predicate: str,
    direct_packages: Iterable[str],
    subject_consumers: Mapping[str, Iterable[str]] | None = None,
    interaction_graph: Mapping[str, Iterable[str]] | None = None,
    visited_packages: Sequence[str] = (),
    max_packages: int = 8,
) -> CausalDecisionProjection:
    """Project a resolved predicate onto direct decision variables.

    Ranking is deliberately conservative:
      0. predicate subject is itself direct;
      1. a direct package is a concrete reverse consumer of the subject;
      2. a direct interaction neighbor of such a consumer;
      3. existing cohort/ecosystem inference fallback.

    The result is navigation evidence only and is intentionally useful even when
    the transitive subject is absent from the solver assignment.
    """
    direct = tuple(sorted({str(name) for name in direct_packages if str(name).strip()}))
    direct_by_norm = {_norm(name): name for name in direct}
    visited = {_norm(name) for name in visited_packages if str(name).strip()}
    subjects = _subjects(predicate)
    consumers = {
        _norm(subject): {str(name) for name in names if str(name).strip()}
        for subject, names in (subject_consumers or {}).items()
    }
    graph = _undirected_graph(interaction_graph)

    # package -> (rank, reasons, graph_backed)
    candidates: dict[str, tuple[int, set[str], bool]] = {}

    def add(package: str, rank: int, reason: str, *, graph_backed: bool) -> None:
        canonical = direct_by_norm.get(_norm(package))
        if not canonical or _norm(canonical) in visited:
            return
        current = candidates.get(canonical)
        if current is None:
            candidates[canonical] = (rank, {reason}, graph_backed)
            return
        old_rank, reasons, old_graph_backed = current
        reasons = set(reasons)
        reasons.add(reason)
        candidates[canonical] = (
            min(old_rank, rank), reasons, old_graph_backed or graph_backed
        )

    for subject in subjects:
        direct_subject = direct_by_norm.get(subject)
        if direct_subject:
            add(direct_subject, 0, "predicate-direct-package", graph_backed=True)

        concrete_consumers = sorted(consumers.get(subject, ()), key=_norm)
        for consumer in concrete_consumers:
            canonical_consumer = direct_by_norm.get(_norm(consumer))
            if canonical_consumer:
                add(
                    canonical_consumer,
                    1,
                    "reverse-transitive-consumer",
                    graph_backed=True,
                )
                for neighbor in sorted(graph.get(canonical_consumer, ()), key=_norm):
                    if _norm(neighbor) in direct_by_norm:
                        add(
                            neighbor,
                            2,
                            "interaction-neighbor",
                            graph_backed=True,
                        )

    # Reuse the existing proof-neutral cohort inference as a bounded fallback.
    # This intentionally does not manufacture causality when concrete graph
    # evidence exists; prior-only packages always rank after graph-backed ones.
    cohort = infer_baseline_cohort(
        predicate=predicate,
        direct_packages=direct,
        subject_consumers=subject_consumers,
        interaction_graph=interaction_graph,
        repeated_count=1,
        max_packages=max(1, int(max_packages)),
    )
    if cohort is not None:
        for package in cohort.packages:
            add(package, 3, "cohort-navigation-prior", graph_backed=False)

    ordered = sorted(
        candidates,
        key=lambda name: (
            candidates[name][0],
            0 if candidates[name][2] else 1,
            _norm(name),
        ),
    )[: max(1, int(max_packages))]
    graph_backed = tuple(name for name in ordered if candidates[name][2])
    prior_only = tuple(name for name in ordered if not candidates[name][2])

    confidence = 0.0
    if ordered:
        confidence = 0.45
    if any(candidates[name][0] == 0 for name in ordered):
        confidence += 0.35
    elif any(candidates[name][0] == 1 for name in ordered):
        confidence += 0.30
    elif graph_backed:
        confidence += 0.18
    if prior_only and not graph_backed:
        confidence = max(confidence, 0.42)
    confidence = min(0.95, round(confidence, 2))

    return CausalDecisionProjection(
        predicate=str(predicate or ""),
        subjects=subjects,
        direct_packages=direct,
        ranked_packages=tuple(ordered),
        reasons=tuple(
            (name, tuple(sorted(candidates[name][1])))
            for name in ordered
        ),
        graph_backed_packages=graph_backed,
        prior_only_packages=prior_only,
        confidence=confidence,
    )


def select_projected_followup(
    *,
    predicates: Sequence[str],
    assignment: Mapping[str, str],
    subject_consumers: Mapping[str, Iterable[str]] | None = None,
    interaction_graph: Mapping[str, Iterable[str]] | None = None,
    visited_packages: Sequence[str] = (),
) -> tuple[str, str, CausalDecisionProjection] | None:
    """Choose the first deterministic causal direct package for a predicate."""
    for predicate in sorted({str(item).strip() for item in predicates if str(item).strip()}):
        projection = causal_decision_projection(
            predicate=predicate,
            direct_packages=assignment.keys(),
            subject_consumers=subject_consumers,
            interaction_graph=interaction_graph,
            visited_packages=visited_packages,
        )
        if projection.ranked_packages:
            return predicate, projection.ranked_packages[0], projection
    return None
