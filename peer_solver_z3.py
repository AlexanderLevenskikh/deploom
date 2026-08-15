#!/usr/bin/env python3
"""Authoritative Z3 Optimize backend for the finite-domain constraint IR."""
from __future__ import annotations

import time
from typing import Dict, Tuple

from peer_solver_model import ExactSolveResult, PeerOptimizationModel


def solve_z3_exact(
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
            optimize.maximize(z3.Sum(terms) if terms else z3.IntVal(0))

        # Match the production/reference deterministic tie-break: among equal
        # objective vectors, choose the lexicographically smallest assignment by
        # package name then version string.
        for package in sorted(model.packages, key=lambda item: item.name):
            lexical_versions = sorted(package.domain)
            lexical_rank = {version: index for index, version in enumerate(lexical_versions)}
            optimize.minimize(
                z3.Sum([
                    z3.If(variables[(package.name, version)], lexical_rank[version], 0)
                    for version in package.domain
                ])
            )

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
