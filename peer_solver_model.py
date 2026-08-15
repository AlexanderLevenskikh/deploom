#!/usr/bin/env python3
"""Neutral finite-domain optimization IR for dependency peer solving.

The production roadmap generator owns model extraction because it understands npm
metadata and project policy.  Solver backends consume only this module: package
variables, forbidden conjunctions (clauses/nogoods), and lexicographic score
vectors.  This keeps the mathematical model independent from any one search
implementation.
"""
from __future__ import annotations

import dataclasses
import itertools
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ScoreVector = Tuple[int, ...]
Literal = Tuple[str, str]


@dataclasses.dataclass(frozen=True)
class PackageVariable:
    name: str
    current_version: str
    domain: Tuple[str, ...]
    scores: Tuple[Tuple[str, ScoreVector], ...]

    def score_for(self, version: str) -> ScoreVector:
        for candidate, score in self.scores:
            if candidate == version:
                return score
        raise KeyError(f"{self.name}@{version} is outside the model domain")


@dataclasses.dataclass(frozen=True)
class ForbiddenCombination:
    """A conjunction of exact package/version literals that must not all hold."""

    literals: Tuple[Literal, ...]
    reason: str = ""
    provenance: str = "static"

    def matches(self, assignment: Mapping[str, str]) -> bool:
        return bool(self.literals) and all(assignment.get(name) == version for name, version in self.literals)


@dataclasses.dataclass(frozen=True)
class RequiresAny:
    """If ``trigger`` is selected, provider must use one of ``allowed_versions``."""

    trigger: Literal
    provider: str
    allowed_versions: Tuple[str, ...]
    reason: str = ""
    provenance: str = "peer-range"

    def violated_by(self, assignment: Mapping[str, str]) -> bool:
        trigger_name, trigger_version = self.trigger
        return (
            assignment.get(trigger_name) == trigger_version
            and assignment.get(self.provider) not in self.allowed_versions
        )


@dataclasses.dataclass(frozen=True)
class PeerOptimizationModel:
    packages: Tuple[PackageVariable, ...]
    constraints: Tuple[ForbiddenCombination, ...] = ()
    requirements: Tuple[RequiresAny, ...] = ()
    objective_width: int = 0

    def package_map(self) -> Dict[str, PackageVariable]:
        return {package.name: package for package in self.packages}

    def candidate_count(self) -> int:
        return sum(len(package.domain) for package in self.packages)

    def state_count_upper_bound(self) -> int:
        total = 1
        for package in self.packages:
            total *= max(1, len(package.domain))
        return total

    def validation_issue(self) -> str:
        """Return a deterministic diagnostic for malformed finite-domain IR."""
        if self.objective_width < 0:
            return f"INVALID_OBJECTIVE_WIDTH: {self.objective_width}"
        package_map: Dict[str, PackageVariable] = {}
        for package in self.packages:
            if not package.name:
                return "EMPTY_PACKAGE_NAME"
            if package.name in package_map:
                return f"DUPLICATE_PACKAGE: {package.name}"
            package_map[package.name] = package
            if not package.domain:
                return f"EMPTY_DOMAIN: {package.name}"
            if len(set(package.domain)) != len(package.domain):
                return f"DUPLICATE_DOMAIN_VERSION: {package.name}"
            score_versions = [version for version, _score in package.scores]
            if len(set(score_versions)) != len(score_versions):
                return f"DUPLICATE_SCORE_VERSION: {package.name}"
            if set(score_versions) != set(package.domain):
                return f"SCORE_DOMAIN_MISMATCH: {package.name}"
            for version, score in package.scores:
                if len(score) != self.objective_width:
                    return (
                        f"OBJECTIVE_WIDTH_MISMATCH: {package.name}@{version}: "
                        f"expected {self.objective_width}, got {len(score)}"
                    )

        for constraint in self.constraints:
            for name, version in constraint.literals:
                package = package_map.get(name)
                if package is None:
                    return f"UNKNOWN_CONSTRAINT_PACKAGE: {name}"
                if version not in package.domain:
                    return f"CONSTRAINT_LITERAL_OUTSIDE_DOMAIN: {name}@{version}"

        for requirement in self.requirements:
            trigger_name, trigger_version = requirement.trigger
            trigger = package_map.get(trigger_name)
            if trigger is None:
                return f"UNKNOWN_REQUIREMENT_TRIGGER: {trigger_name}"
            if trigger_version not in trigger.domain:
                return f"REQUIREMENT_TRIGGER_OUTSIDE_DOMAIN: {trigger_name}@{trigger_version}"
            provider = package_map.get(requirement.provider)
            if provider is None:
                return f"UNKNOWN_REQUIREMENT_PROVIDER: {requirement.provider}"
            for version in requirement.allowed_versions:
                if version not in provider.domain:
                    return f"REQUIREMENT_VERSION_OUTSIDE_DOMAIN: {requirement.provider}@{version}"
        return ""

    def assignment_issue(self, assignment: Mapping[str, str]) -> str:
        package_map = self.package_map()
        for name, package in package_map.items():
            version = assignment.get(name)
            if version is None:
                return f"MISSING_ASSIGNMENT: {name}"
            if version not in package.domain:
                return f"OUTSIDE_DOMAIN: {name}@{version}"
        for constraint in self.constraints:
            if constraint.matches(assignment):
                return constraint.reason or "FORBIDDEN_COMBINATION"
        for requirement in self.requirements:
            if requirement.violated_by(assignment):
                return requirement.reason or "REQUIRES_ALLOWED_VERSION"
        return ""

    def assignment_score(self, assignment: Mapping[str, str]) -> ScoreVector:
        width = self.objective_width
        total = [0] * width
        for package in self.packages:
            contribution = package.score_for(assignment[package.name])
            if len(contribution) != width:
                raise ValueError(
                    f"objective width mismatch for {package.name}: expected {width}, got {len(contribution)}"
                )
            for index, value in enumerate(contribution):
                total[index] += int(value)
        return tuple(total)

    def assignment_key(self, assignment: Mapping[str, str]) -> Tuple[Literal, ...]:
        return tuple((package.name, assignment[package.name]) for package in sorted(self.packages, key=lambda item: item.name))

    def with_constraints(self, constraints: Iterable[ForbiddenCombination]) -> "PeerOptimizationModel":
        merged: List[ForbiddenCombination] = list(self.constraints)
        seen = {(constraint.literals, constraint.reason, constraint.provenance) for constraint in merged}
        for constraint in constraints:
            key = (constraint.literals, constraint.reason, constraint.provenance)
            if key not in seen:
                seen.add(key)
                merged.append(constraint)
        return dataclasses.replace(self, constraints=tuple(merged))


@dataclasses.dataclass(frozen=True)
class ExactSolveResult:
    backend: str
    status: str
    assignment: Optional[Dict[str, str]] = None
    score: Optional[ScoreVector] = None
    states: int = 0
    detail: str = ""
    elapsed_ms: int = 0


def normalize_forbidden_literals(literals: Iterable[Literal]) -> Tuple[Literal, ...]:
    return tuple(sorted(set((str(name), str(version)) for name, version in literals)))


def forbidden(
    literals: Iterable[Literal],
    *,
    reason: str = "",
    provenance: str = "static",
) -> ForbiddenCombination:
    normalized = normalize_forbidden_literals(literals)
    if not normalized:
        raise ValueError("forbidden combination must contain at least one literal")
    return ForbiddenCombination(normalized, reason=reason, provenance=provenance)


def solve_reference_exact(
    model: PeerOptimizationModel,
    *,
    max_states: int = 1_000_000,
) -> ExactSolveResult:
    """Deterministic exhaustive oracle for small synthetic models.

    This is deliberately not a production backend.  It exists so Solver Lab can
    validate model extraction and optimization semantics independently from both
    the custom search and Z3.
    """
    import time

    started = time.perf_counter()
    model_issue = model.validation_issue()
    if model_issue:
        return ExactSolveResult(
            backend="reference",
            status="error",
            detail=f"INVALID_MODEL: {model_issue}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    packages = tuple(sorted(model.packages, key=lambda package: package.name))
    domains: Sequence[Tuple[str, ...]] = [package.domain for package in packages]
    best_assignment: Optional[Dict[str, str]] = None
    best_score: Optional[ScoreVector] = None
    best_key: Optional[Tuple[Literal, ...]] = None
    states = 0

    for values in itertools.product(*domains):
        states += 1
        if states > max_states:
            elapsed = int((time.perf_counter() - started) * 1000)
            return ExactSolveResult(
                backend="reference",
                status="unknown_budget",
                assignment=best_assignment,
                score=best_score,
                states=states - 1,
                detail=f"reference state budget {max_states} exhausted",
                elapsed_ms=elapsed,
            )
        assignment = {package.name: version for package, version in zip(packages, values)}
        if model.assignment_issue(assignment):
            continue
        score = model.assignment_score(assignment)
        key = model.assignment_key(assignment)
        if best_score is None or score > best_score or (score == best_score and (best_key is None or key < best_key)):
            best_assignment = assignment
            best_score = score
            best_key = key

    elapsed = int((time.perf_counter() - started) * 1000)
    if best_assignment is None:
        return ExactSolveResult(
            backend="reference",
            status="unsat",
            states=states,
            elapsed_ms=elapsed,
        )
    return ExactSolveResult(
        backend="reference",
        status="optimal",
        assignment=best_assignment,
        score=best_score,
        states=states,
        elapsed_ms=elapsed,
    )
