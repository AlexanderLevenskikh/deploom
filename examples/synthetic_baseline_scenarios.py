#!/usr/bin/env python3
"""Small deterministic DepLoom Solver/Baseline acceptance scenarios.

No registry or package-manager process is required. The scenarios intentionally
exercise the neutral exact model, the real DependencyRow -> model extraction,
and Baseline liveness/terminal semantics added by Block S.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as roadmap
from constraint_verify import GlobalExactExclusionError, RankedComponentAlternative, coordinate_global_exact_exclusions
from peer_solver_model import PackageVariable, PeerOptimizationModel, RequiresAny, forbidden, solve_reference_exact


def _package(name: str, versions: tuple[str, ...], preferred: str) -> PackageVariable:
    return PackageVariable(
        name=name,
        current_version=versions[-1],
        domain=versions,
        scores=tuple((version, (10 if version == preferred else 0,)) for version in versions),
    )


def _terminal_for_reference(status: str) -> str:
    if status == "optimal":
        return roadmap.BaselineTerminalStatus.SAT_PROVEN.value
    if status == "unsat":
        return roadmap.BaselineTerminalStatus.UNSAT_PROVEN.value
    if status == "unknown_budget":
        return roadmap.BaselineTerminalStatus.BUDGET_EXHAUSTED.value
    return roadmap.BaselineTerminalStatus.SOLVER_UNKNOWN.value


def scenario_sat_peer_chain() -> Dict[str, Any]:
    model = PeerOptimizationModel(
        packages=(
            _package("plugin", ("2.0.0", "1.0.0"), "2.0.0"),
            _package("host", ("2.0.0", "1.0.0"), "2.0.0"),
            _package("tool", ("2.0.0", "1.0.0"), "2.0.0"),
        ),
        requirements=(
            RequiresAny(("plugin", "2.0.0"), "host", ("2.0.0",), reason="plugin@2 needs host@2"),
            RequiresAny(("tool", "2.0.0"), "plugin", ("2.0.0",), reason="tool@2 needs plugin@2"),
        ),
        objective_width=1,
    )
    result = solve_reference_exact(model)
    return {
        "scenario": "sat-peer-chain",
        "description": "3 packages; peer chain has one best compatible upgraded tuple",
        "terminalStatus": _terminal_for_reference(result.status),
        "solverStatus": result.status,
        "assignment": result.assignment,
        "states": result.states,
    }


def scenario_unsat_peer_contradiction() -> Dict[str, Any]:
    model = PeerOptimizationModel(
        packages=(
            _package("plugin-a", ("2.0.0",), "2.0.0"),
            _package("plugin-b", ("2.0.0",), "2.0.0"),
            _package("core", ("2.0.0", "1.0.0"), "2.0.0"),
        ),
        requirements=(
            RequiresAny(("plugin-a", "2.0.0"), "core", ("1.0.0",), reason="plugin-a@2 needs core@1"),
            RequiresAny(("plugin-b", "2.0.0"), "core", ("2.0.0",), reason="plugin-b@2 needs core@2"),
        ),
        objective_width=1,
    )
    result = solve_reference_exact(model)
    return {
        "scenario": "unsat-peer-contradiction",
        "description": "3 packages; two fixed upgraded plugins require mutually exclusive core versions",
        "terminalStatus": _terminal_for_reference(result.status),
        "solverStatus": result.status,
        "assignment": result.assignment,
        "states": result.states,
    }


def scenario_exact_exclusion_followup() -> Dict[str, Any]:
    """Direct F-02 reproducer: exact authority learned on the last soft iteration."""
    model = PeerOptimizationModel(
        packages=tuple(_package(name, ("2.0.0", "1.0.0"), "2.0.0") for name in ("a", "b", "c")),
        objective_width=1,
    )
    budget = roadmap.BaselineLivenessBudget(base_iterations=1, max_learning_extensions=2)
    first = solve_reference_exact(model)
    if first.status != "optimal" or first.assignment is None:
        raise RuntimeError(f"unexpected first solve: {first.status}")
    before = budget.allowed_iterations
    exact = dict(first.assignment)
    extended = budget.record_exact_exclusion()
    repaired_model = model.with_constraints((
        forbidden(exact.items(), reason="synthetic real-PM failure", provenance="fresh-exact-verification"),
    ))
    second = solve_reference_exact(repaired_model)
    return {
        "scenario": "exact-exclusion-followup",
        "description": "base budget=1; first best tuple is freshly rejected; exact exclusion must buy solve #2",
        "firstAssignment": first.assignment,
        "exactExclusion": exact,
        "extensionGranted": extended,
        "allowedIterationsBefore": before,
        "allowedIterationsAfter": budget.allowed_iterations,
        "hardIterations": budget.hard_iterations,
        "secondAssignment": second.assignment,
        "terminalStatus": _terminal_for_reference(second.status),
        "solverStatus": second.status,
    }


def scenario_plateau_without_authority() -> Dict[str, Any]:
    budget = roadmap.BaselineLivenessBudget(base_iterations=1, max_learning_extensions=2)
    return {
        "scenario": "plateau-without-authority",
        "description": "one soft iteration completes without any new authoritative formula change",
        "allowedIterations": budget.allowed_iterations,
        "hardIterations": budget.hard_iterations,
        "terminalStatus": roadmap.BaselineTerminalStatus.PLATEAU.value,
    }


def scenario_hard_safety_limit() -> Dict[str, Any]:
    budget = roadmap.BaselineLivenessBudget(base_iterations=1, max_learning_extensions=2)
    grants = [budget.record_exact_exclusion() for _ in range(3)]
    return {
        "scenario": "hard-safety-limit",
        "description": "fresh exact exclusions can extend liveness only to the independent hard ceiling",
        "extensionGranted": grants,
        "allowedIterations": budget.allowed_iterations,
        "hardIterations": budget.hard_iterations,
        "terminalStatus": roadmap.BaselineTerminalStatus.HARD_SAFETY_LIMIT.value,
    }


def scenario_global_exact_unsat() -> Dict[str, Any]:
    initial = (
        RankedComponentAlternative({"a": "2.0.0"}, (10,)),
        RankedComponentAlternative({"b": "2.0.0"}, (10,)),
    )
    try:
        coordinate_global_exact_exclusions(
            initial,
            ({"a": "2.0.0", "b": "2.0.0"},),
            lambda _index, _existing: None,
        )
    except GlobalExactExclusionError as exc:
        terminal = (
            roadmap.BaselineTerminalStatus.UNSAT_PROVEN.value
            if exc.reason == "unsat-proven"
            else roadmap.BaselineTerminalStatus.SOLVER_UNKNOWN.value
        )
        return {
            "scenario": "global-exact-unsat",
            "description": "2 independent components have one tuple and fresh exact evidence excludes it",
            "coordinatorReason": exc.reason,
            "terminalStatus": terminal,
        }
    raise RuntimeError("global exact exclusion scenario unexpectedly found an assignment")


def _row(name: str, *, current: str = "1.0.0", target: str = "2.0.0") -> roadmap.DependencyRow:
    return roadmap.DependencyRow(
        project="SyntheticRows", package_dir=".", name=name, kind="dev",
        requested_spec="*", current_version=current, current_source="synthetic",
        latest_version=target, current_vulns="0", min_no_critical=current,
        min_no_high=current, min_no_vuln=current, min_lag_12m=current,
        min_lag_9m=current, min_lag_6m=current, min_lag_3m=current,
        group=1, reason="synthetic", notes="", target_default=target,
        target_yellow=target, target_green=target,
        desired_target_default=target, desired_target_yellow=target,
        desired_target_green=target,
    )


def scenario_dependency_rows_model() -> Dict[str, Any]:
    """Exercise real DepLoom DependencyRow -> exact neutral IR extraction."""
    rows = {name: _row(name) for name in ("plugin", "host", "tool")}
    client = roadmap.LiveDataClient("https://registry.example.invalid/npm", timeout=1, batch_size=10, sleep_sec=0)
    client.npm_cache.update({
        "plugin": {"versions": {"1.0.0": {}, "2.0.0": {"peerDependencies": {"host": "=2.0.0"}}}},
        "host": {"versions": {"1.0.0": {}, "2.0.0": {}}},
        "tool": {"versions": {"1.0.0": {}, "2.0.0": {"peerDependencies": {"plugin": "=2.0.0"}}}},
    })
    for row in rows.values():
        row.registry_artifacts["2.0.0"] = {"status": "available"}
    domains = {name: ["2.0.0", "1.0.0"] for name in rows}
    model = roadmap._build_peer_optimization_model(
        ["plugin", "host", "tool"], rows, domains, client, "yellow", [], None,
    )
    result = solve_reference_exact(model)
    return {
        "scenario": "dependency-rows-model",
        "description": "real DependencyRow/registry metadata extraction for 3 packages, solved by independent reference oracle",
        "terminalStatus": _terminal_for_reference(result.status),
        "solverStatus": result.status,
        "assignment": result.assignment,
        "constraints": len(model.constraints) + len(model.requirements),
        "states": result.states,
    }


SCENARIOS = {
    "sat-peer-chain": scenario_sat_peer_chain,
    "unsat-peer-contradiction": scenario_unsat_peer_contradiction,
    "exact-exclusion-followup": scenario_exact_exclusion_followup,
    "plateau-without-authority": scenario_plateau_without_authority,
    "hard-safety-limit": scenario_hard_safety_limit,
    "global-exact-unsat": scenario_global_exact_unsat,
    "dependency-rows-model": scenario_dependency_rows_model,
}


def run_all() -> list[Dict[str, Any]]:
    return [SCENARIOS[name]() for name in SCENARIOS]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), action="append")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    names = args.scenario or list(SCENARIOS)
    results = [SCENARIOS[name]() for name in names]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        for item in results:
            print(f"[{item['terminalStatus']}] {item['scenario']}: {item['description']}")
            for key, value in item.items():
                if key not in {"terminalStatus", "scenario", "description"}:
                    print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
