#!/usr/bin/env python3
"""Authoritative Z3 Optimize backend for the finite-domain constraint IR."""
from __future__ import annotations

import time
from typing import Dict, Tuple

from peer_solver_model import ExactSolveResult, PeerOptimizationModel
from verification_observability import (
    emit_observability_event,
    new_observability_id,
    process_resource_snapshot,
)

# BLOCK_Y_FULL_OBSERVABILITY_V1


def _objective_bounds_proven(optimize: object, handles: list[object]) -> tuple[bool, str]:
    """Return true only when every Optimize objective has a closed exact bound."""
    for index, handle in enumerate(handles):
        try:
            lower = str(optimize.lower(handle))  # type: ignore[attr-defined]
            upper = str(optimize.upper(handle))  # type: ignore[attr-defined]
        except Exception as exc:
            return False, f"objective[{index}] bounds unavailable: {exc}"
        if lower != upper:
            return False, f"objective[{index}] lower={lower} upper={upper}"
    return True, f"objectives={len(handles)} bounds closed"


def _solve_z3_exact_impl(
    model: PeerOptimizationModel,
    *,
    timeout_ms: int = 30_000,
) -> ExactSolveResult:
    """Solve one IR model exactly with lexicographic objectives.

    Production installs bundle z3-solver. Returning ``unavailable`` remains a
    defensive diagnostic for broken/incomplete environments; callers must not
    silently fall back to the heuristic solver when Z3 is authoritative.
    """
    started = time.perf_counter()
    model_issue = model.validation_issue()
    if model_issue:
        return ExactSolveResult(
            backend="z3",
            status="error",
            detail=f"INVALID_MODEL: {model_issue}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    try:
        import z3  # type: ignore
    except Exception as exc:  # pragma: no cover - environment-dependent
        return ExactSolveResult(
            backend="z3",
            status="unavailable",
            detail=f"z3-solver is not installed: {exc}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    try:
        optimize = z3.Optimize()
        optimize.set(priority="lex")
        objective_handles: list[object] = []
        if timeout_ms > 0:
            optimize.set(timeout=int(timeout_ms))

        variables: Dict[Tuple[str, str], object] = {}
        for package in model.packages:
            package_vars = []
            for index, version in enumerate(package.domain):
                variable = z3.Bool(f"pkg__{len(variables)}__{index}")
                variables[(package.name, version)] = variable
                package_vars.append(variable)
            optimize.add(z3.PbEq([(variable, 1) for variable in package_vars], 1))

        for constraint in model.constraints:
            clause_vars = [variables[(name, version)] for name, version in constraint.literals]
            if len(clause_vars) == 1:
                optimize.add(z3.Not(clause_vars[0]))
            else:
                optimize.add(z3.Not(z3.And(*clause_vars)))

        for requirement in model.requirements:
            trigger = variables[requirement.trigger]
            allowed = [
                variables[(requirement.provider, version)]
                for version in requirement.allowed_versions
            ]
            if allowed:
                optimize.add(z3.Or(z3.Not(trigger), *allowed))
            else:
                optimize.add(z3.Not(trigger))

        for objective_index in range(model.objective_width):
            terms = []
            for package in model.packages:
                for version in package.domain:
                    score = package.score_for(version)[objective_index]
                    if score:
                        terms.append(z3.If(variables[(package.name, version)], int(score), 0))
            objective_handles.append(optimize.maximize(z3.Sum(terms) if terms else z3.IntVal(0)))

        # Match the production/reference deterministic tie-break: among equal
        # objective vectors, choose the lexicographically smallest assignment by
        # package name then version string.
        for package in sorted(model.packages, key=lambda item: item.name):
            lexical_versions = sorted(package.domain)
            lexical_rank = {version: index for index, version in enumerate(lexical_versions)}
            objective_handles.append(optimize.minimize(
                z3.Sum([
                    z3.If(variables[(package.name, version)], lexical_rank[version], 0)
                    for version in package.domain
                ])
            ))

        check = optimize.check()
        elapsed = int((time.perf_counter() - started) * 1000)
        if check == z3.unsat:
            return ExactSolveResult(backend="z3", status="unsat", elapsed_ms=elapsed)
        if check != z3.sat:
            reason = ""
            try:
                reason = str(optimize.reason_unknown())
            except Exception:
                reason = str(check)
            return ExactSolveResult(
                backend="z3",
                status="unknown",
                detail=reason or "Z3 returned unknown",
                elapsed_ms=elapsed,
            )

        bounds_proven, bounds_detail = _objective_bounds_proven(optimize, objective_handles)
        if not bounds_proven:
            return ExactSolveResult(
                backend="z3",
                status="sat_unproven",
                detail=f"Z3 returned a satisfiable model without closed optimization bounds: {bounds_detail}",
                elapsed_ms=elapsed,
            )

        solved = optimize.model()
        assignment: Dict[str, str] = {}
        for package in model.packages:
            selected = [
                version
                for version in package.domain
                if z3.is_true(solved.eval(variables[(package.name, version)], model_completion=True))
            ]
            if len(selected) != 1:
                return ExactSolveResult(
                    backend="z3",
                    status="error",
                    detail=f"expected one selected version for {package.name}, got {selected}",
                    elapsed_ms=elapsed,
                )
            assignment[package.name] = selected[0]

        issue = model.assignment_issue(assignment)
        if issue:
            return ExactSolveResult(
                backend="z3",
                status="error",
                assignment=assignment,
                detail=f"backend returned assignment violating IR: {issue}",
                elapsed_ms=elapsed,
            )
        return ExactSolveResult(
            backend="z3",
            status="optimal",
            assignment=assignment,
            score=model.assignment_score(assignment),
            elapsed_ms=elapsed,
        )
    except Exception as exc:  # pragma: no cover - backend/runtime-specific
        return ExactSolveResult(
            backend="z3",
            status="error",
            detail=f"Z3 shadow solver failed: {exc}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

def _safe_model_metric(model: object, name: str, default: object = 0) -> object:
    try:
        value = getattr(model, name)
        return value() if callable(value) else value
    except Exception:
        return default


def solve_z3_exact(
    model: PeerOptimizationModel,
    *,
    timeout_ms: int = 30_000,
) -> ExactSolveResult:
    """Instrumented authoritative Z3 entry point.

    Telemetry is observational only; the wrapped implementation and returned
    ExactSolveResult are unchanged.
    """
    operation_id = new_observability_id("z3")
    started = time.perf_counter()
    before = process_resource_snapshot()
    packages = tuple(getattr(model, "packages", ()) or ())
    constraints = tuple(getattr(model, "constraints", ()) or ())
    requirements = tuple(getattr(model, "requirements", ()) or ())
    candidate_count = sum(len(getattr(package, "domain", ()) or ()) for package in packages)
    objective_width = int(_safe_model_metric(model, "objective_width", 0) or 0)
    state_upper_bound = str(_safe_model_metric(model, "state_count_upper_bound", ""))

    emit_observability_event(
        "solver.z3.start",
        operationId=operation_id,
        backend="z3",
        packageCount=len(packages),
        candidateCount=candidate_count,
        hardConstraintCount=len(constraints),
        requirementCount=len(requirements),
        objectiveWidth=objective_width,
        deterministicTieBreakObjectives=len(packages),
        stateUpperBound=state_upper_bound,
        timeoutMs=int(timeout_ms),
    )
    try:
        result = _solve_z3_exact_impl(model, timeout_ms=timeout_ms)
    except BaseException as exc:
        after = process_resource_snapshot()
        emit_observability_event(
            "solver.z3.finish",
            operationId=operation_id,
            backend="z3",
            status="exception",
            packageCount=len(packages),
            candidateCount=candidate_count,
            hardConstraintCount=len(constraints),
            requirementCount=len(requirements),
            objectiveWidth=objective_width,
            deterministicTieBreakObjectives=len(packages),
            stateUpperBound=state_upper_bound,
            timeoutMs=int(timeout_ms),
            durationMs=max(0, int((time.perf_counter() - started) * 1000)),
            cpuMs=max(0, after.cpu_ms - before.cpu_ms),
            rssBytes=after.rss_bytes,
            peakRssBytes=after.peak_rss_bytes,
            errorType=type(exc).__name__,
        )
        raise

    after = process_resource_snapshot()
    emit_observability_event(
        "solver.z3.finish",
        operationId=operation_id,
        backend="z3",
        status=str(result.status),
        packageCount=len(packages),
        candidateCount=candidate_count,
        hardConstraintCount=len(constraints),
        requirementCount=len(requirements),
        objectiveWidth=objective_width,
        deterministicTieBreakObjectives=len(packages),
        stateUpperBound=state_upper_bound,
        timeoutMs=int(timeout_ms),
        solverElapsedMs=int(result.elapsed_ms),
        assignmentCount=len(result.assignment or {}),
        durationMs=max(0, int((time.perf_counter() - started) * 1000)),
        cpuMs=max(0, after.cpu_ms - before.cpu_ms),
        rssBytes=after.rss_bytes,
        peakRssBytes=after.peak_rss_bytes,
    )
    return result

