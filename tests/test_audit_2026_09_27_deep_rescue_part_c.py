"""Part C of the libjs deep rescue (2026-09-27): the verified path stops
aborting on a single UNFINISHED component and delivers the first useful result.

Production coverage beyond Part A's T1/T2:
  C1 - a fully pinned component is short-circuited BEFORE the model/solve and
       its report is honest: status optimal, assignment from the residual
       targets, pinned=True / proof=queued-fallback, zero attempts, and the
       solver is NEVER invoked for it.
  C2 - the short-circuit boundary: a PARTIALLY pinned component still goes
       through the exact solve (the remaining free packages still need one).
  C3 - the strict default contract is preserved: a call WITHOUT
       partial_on_incomplete still fails closed (EXACT_SOLVER_UNKNOWN) so the
       non-Draft roadmap/proof path keeps its guarantee; tolerance is opt-in at
       the call sites that need it (verified bootstrap, Draft).
"""
from __future__ import annotations

import unittest
from unittest import mock

import dependency_live_roadmap_generator as roadmap
from peer_solver_model import ExactSolveResult  # noqa: E402

REGISTRY = "https://nexus.example/repository/npm-group"


def _row(name: str, current: str = "1.0.0", desired: str = "2.0.0") -> roadmap.DependencyRow:
    return roadmap.DependencyRow(
        project="Demo",
        package_dir=".",
        name=name,
        kind="dev",
        requested_spec="*",
        current_version=current,
        current_source="lockfile",
        latest_version=desired,
        current_vulns="0",
        min_no_critical=current,
        min_no_high=current,
        min_no_vuln=current,
        min_lag_12m=current,
        min_lag_9m=current,
        min_lag_6m=current,
        min_lag_3m=current,
        group=1,
        reason="deep rescue part C",
        notes="",
        target_default=desired,
        target_yellow=desired,
        target_green=desired,
        target_default_reason="desired",
        target_yellow_reason="desired",
        target_green_reason="desired",
    )


def _client(names, *, peer_edges=()):
    client = roadmap.LiveDataClient(REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
    edges = {(edge[0], edge[3]): (edge[1], edge[2]) for edge in peer_edges}
    for name in names:
        versions = {}
        for version in ("1.0.0", "1.5.0", "2.0.0"):
            meta = {
                "name": name,
                "version": version,
                "dist": {"tarball": f"{REGISTRY}/artifact/{name}/{version}.tgz"},
            }
            if (name, version) in edges:
                peer_name, spec = edges[(name, version)]
                meta["peerDependencies"] = {peer_name: f"^{spec}"}
                meta.setdefault("peerDependenciesMeta", {})[peer_name] = {}
            versions[version] = meta
        client.npm_cache[name] = {"versions": versions}
        for version in ("1.0.0", "1.5.0", "2.0.0"):
            client.registry_artifact_cache[(name, version)] = {
                "status": "available",
                "tarballUrl": f"{REGISTRY}/artifact/{name}/{version}.tgz",
            }
    return client


def _pkg_names(model):
    return sorted(getattr(p, "name", str(p)) for p in model.packages)


def _optimal(model, version_by_name):
    assignment = {name: version_by_name[name] for name in _pkg_names(model)}
    return ExactSolveResult(
        backend="z3", status="optimal", assignment=assignment,
        score=model.assignment_score(assignment), elapsed_ms=12,
    )


class C1FullyPinnedComponentShortCircuit(unittest.TestCase):
    """A component whose EVERY package is pinned by residual targets is decided
    from the queued/prepared fallback WITHOUT a fresh global solve, and the
    report is honest about it (no attempts, pinned flag, proof=queued-fallback)."""

    def _component_ctx(self):
        client = _client(("a",))
        row = _row("a")
        by_project = {"Demo": [row]}
        roadmap.capture_desired_targets(by_project)
        rows_by_name = {r.name: r for r in by_project["Demo"]}
        domains = {"a": roadmap._candidate_domain(row, "default", client)}
        return client, rows_by_name, domains

    def test_fully_pinned_report_is_honest_and_solver_is_never_called(self):
        client, rows_by_name, domains = self._component_ctx()
        with mock.patch.object(
            roadmap, "solve_z3_exact", side_effect=AssertionError("pinned component must not solve")
        ):
            report = roadmap._run_z3_peer_component(
                ["a"], rows_by_name, domains, client, "default", [], None, None,
                residual_targets={"a": "1.0.0"},
            )
        self.assertEqual("optimal", report["status"])
        self.assertEqual({"a": "1.0.0"}, report["assignment"])
        self.assertTrue(report["pinned"])
        self.assertEqual("queued-fallback", report["proof"])
        self.assertEqual([], report["attempts"])
        self.assertEqual(0, report["attemptCount"])
        self.assertEqual(0, report["elapsedMs"])
        self.assertEqual(1, report["variables"])
        # the report reflects the REAL built model, not a fabricated shape
        self.assertEqual(3, report["candidates"])
        self.assertEqual(0, report["hardConstraints"])
        self.assertGreaterEqual(int(report["stateUpperBound"]), 1)
        self.assertEqual({"a": "1.0.0"}, report["residualTargets"])

    def test_pinned_component_delivered_through_resolve_without_solve(self):
        client = _client(("big-a", "big-b"),
                         peer_edges=(("big-a", "big-b", "2.0.0", "2.0.0"),
                                     ("big-b", "big-a", "2.0.0", "2.0.0")))
        by_project = {"Demo": [_row("big-a"), _row("big-b")]}
        roadmap.capture_desired_targets(by_project)
        solved = []

        def forbid(model, timeout_ms=30_000, operation_id=None):
            solved.append(_pkg_names(model))
            raise AssertionError("pinned component must not need a fresh global solve")

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=forbid):
            result = roadmap.resolve_peer_compatibility(
                by_project, client, modes=("default",), apply_results=False,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                residual_targets_by_project={"Demo": {"big-a": "1.0.0", "big-b": "1.0.0"}},
            )
        self.assertEqual({"big-a": "1.0.0", "big-b": "1.0.0"}, result["Demo"]["default"])
        self.assertEqual([], solved)


class C2PartialPinStillSolves(unittest.TestCase):
    """The short-circuit boundary: a partially pinned component keeps its free
    packages in the exact solve (pinning must not silence the solver for the
    rest of the component)."""

    def test_partially_pinned_component_still_solves(self):
        client = _client(("big-a", "big-b"), peer_edges=(("big-a", "big-b", "2.0.0", "2.0.0"),
                                                         ("big-b", "big-a", "2.0.0", "2.0.0")))
        by_project = {"Demo": [_row("big-a"), _row("big-b")]}
        roadmap.capture_desired_targets(by_project)
        solved = []

        def exact_target(model, timeout_ms=30_000, operation_id=None):
            names = _pkg_names(model)
            solved.append(names)
            return _optimal(model, {"big-a": "1.0.0", "big-b": "2.0.0"})

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target):
            result = roadmap.resolve_peer_compatibility(
                by_project, client, modes=("default",), apply_results=False,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                residual_targets_by_project={"Demo": {"big-a": "1.0.0"}},
            )
        self.assertEqual({"big-a": "1.0.0", "big-b": "2.0.0"}, result["Demo"]["default"])
        self.assertEqual([["big-a", "big-b"]], solved)


class C3StrictDefaultContractPreserved(unittest.TestCase):
    """Tolerance is opt-in. A default (non-partial) call still fails closed on
    an UNFINISHED component, so the non-Draft roadmap/proof path keeps its
    guarantee that an unresolved exact proof is never silently accepted."""

    def test_default_call_still_raises_exact_solver_unknown(self):
        client = _client(("a",))
        by_project = {"Demo": [_row("a")]}
        roadmap.capture_desired_targets(by_project)
        with mock.patch.object(
            roadmap, "solve_z3_exact",
            return_value=ExactSolveResult(backend="z3", status="unknown", detail="timeout", elapsed_ms=90),
        ):
            with self.assertRaises(roadmap.BaselineConstraintVerificationError) as raised:
                roadmap.resolve_peer_compatibility(
                    by_project, client, modes=("default",), apply_results=False,
                    shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                )
        self.assertEqual("SOLVER_UNKNOWN", raised.exception.terminal_status)
        self.assertEqual("EXACT_SOLVER_UNKNOWN", raised.exception.stop_code)


if __name__ == "__main__":
    unittest.main()
