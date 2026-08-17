from __future__ import annotations

import random
import unittest
from unittest import mock

import dependency_live_roadmap_generator as roadmap
from peer_solver_model import ExactSolveResult, PackageVariable, PeerOptimizationModel, forbidden, solve_reference_exact
from peer_solver_z3 import solve_z3_exact


class PeerSolverModelTests(unittest.TestCase):
    REGISTRY = "https://nexus.example/repository/npm-group"

    def test_custom_solver_setting_cannot_restore_production_authority(self):
        production = roadmap._peer_solver_backend_options({"solverBackend": "custom"})
        reference = roadmap._peer_solver_backend_options({"solverBackend": "custom", "referenceOnly": True})
        self.assertEqual("z3", production["authoritative"])
        self.assertEqual("custom->z3", production["authorityOverride"])
        self.assertEqual("custom", reference["authoritative"])

    def make_client(self) -> roadmap.LiveDataClient:
        return roadmap.LiveDataClient(self.REGISTRY, timeout=1, batch_size=10, sleep_sec=0)

    def add_package(self, client, name, versions):
        records = {}
        for version, extra in versions.items():
            records[version] = {
                **extra,
                "dist": {"tarball": f"{self.REGISTRY}/artifact/{name}/{version}.tgz"},
            }
            client.registry_artifact_cache[(name, version)] = {
                "status": "available",
                "tarballUrl": f"{self.REGISTRY}/artifact/{name}/{version}.tgz",
            }
        client.npm_cache[name] = {"versions": records}

    @staticmethod
    def row(name, current="1.0.0", desired="2.0.0"):
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
            reason="model lab",
            notes="",
            target_default=desired,
            target_yellow=desired,
            target_green=desired,
            target_default_reason="desired",
            target_yellow_reason="desired",
            target_green_reason="desired",
        )

    def prepare_model(self, rows, client, learned=None):
        by_project = {"Demo": rows}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        rows_by_name = {row.name: row for row in rows}
        domains = {name: roadmap._candidate_domain(row, "default", client) for name, row in rows_by_name.items()}
        component = sorted(rows_by_name)
        model = roadmap._build_peer_optimization_model(
            component,
            rows_by_name,
            domains,
            client,
            "default",
            learned or [],
        )
        return model, rows_by_name, domains

    def test_reference_exact_uses_lexicographic_objective_and_deterministic_tie_break(self):
        model = PeerOptimizationModel(
            packages=(
                PackageVariable(
                    name="a",
                    current_version="1",
                    domain=("2", "1"),
                    scores=(("2", (1, 0)), ("1", (0, 100))),
                ),
                PackageVariable(
                    name="b",
                    current_version="1",
                    domain=("2", "1"),
                    scores=(("2", (0, 0)), ("1", (0, 0))),
                ),
            ),
            objective_width=2,
        )
        result = solve_reference_exact(model)
        self.assertEqual("optimal", result.status)
        self.assertEqual({"a": "2", "b": "1"}, result.assignment)
        self.assertEqual((1, 0), result.score)

    def test_reference_exact_respects_three_way_nogood(self):
        packages = tuple(
            PackageVariable(
                name=name,
                current_version="1",
                domain=("2", "1"),
                scores=(("2", (1,)), ("1", (0,))),
            )
            for name in ("a", "b", "c")
        )
        model = PeerOptimizationModel(
            packages=packages,
            constraints=(
                forbidden(
                    [("a", "2"), ("b", "2"), ("c", "2")],
                    reason="three-way",
                    provenance="test",
                ),
            ),
            objective_width=1,
        )
        result = solve_reference_exact(model)
        self.assertEqual("optimal", result.status)
        self.assertEqual(2, sum(value == "2" for value in result.assignment.values()))
        self.assertEqual("", model.assignment_issue(result.assignment))

    def test_extracted_ir_matches_custom_solver_on_small_peer_cycle(self):
        client = self.make_client()
        self.add_package(client, "a", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"b": ">=2"}},
        })
        self.add_package(client, "b", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"a": ">=2"}},
        })
        rows = [self.row("a"), self.row("b")]
        model, rows_by_name, domains = self.prepare_model(rows, client)
        graph = roadmap._potential_peer_graph(rows_by_name, domains, client)
        diagnostics = {}
        custom = roadmap._solve_peer_component(
            ["a", "b"], rows_by_name, domains, graph, client, "default", diagnostics=diagnostics
        )
        reference = solve_reference_exact(model)
        self.assertEqual("optimal", reference.status)
        self.assertEqual(custom, reference.assignment)
        self.assertEqual({"a": "2.0.0", "b": "2.0.0"}, reference.assignment)

    def test_extracted_ir_matches_custom_solver_on_deterministic_micrographs(self):
        rng = random.Random(20260814)
        for case_index in range(30):
            with self.subTest(case=case_index):
                client = self.make_client()
                package_count = rng.randint(2, 4)
                names = [f"p{case_index}-{index}" for index in range(package_count)]
                rows = []
                for index, name in enumerate(names):
                    versions = {"1.0.0": {}, "2.0.0": {}}
                    peer_candidates = [other for other in names if other != name]
                    if peer_candidates and rng.random() < 0.75:
                        peer = rng.choice(peer_candidates)
                        required_major = rng.choice((1, 2))
                        versions["2.0.0"] = {
                            "peerDependencies": {peer: f"^{required_major}.0.0"}
                        }
                    self.add_package(client, name, versions)
                    rows.append(self.row(name))

                model, rows_by_name, domains = self.prepare_model(rows, client)
                graph = roadmap._potential_peer_graph(rows_by_name, domains, client)
                component = sorted(rows_by_name)
                diagnostics = {}
                custom = roadmap._solve_peer_component(
                    component,
                    rows_by_name,
                    domains,
                    graph,
                    client,
                    "default",
                    diagnostics=diagnostics,
                )
                reference = solve_reference_exact(model)
                self.assertEqual("optimal", reference.status)
                self.assertEqual(custom, reference.assignment)

    def test_ir_preserves_learned_n_way_constraint_as_one_clause(self):
        client = self.make_client()
        rows = []
        for name in ("a", "b", "c"):
            self.add_package(client, name, {"1.0.0": {}, "2.0.0": {}})
            rows.append(self.row(name))
        model, _rows_by_name, _domains = self.prepare_model(
            rows,
            client,
            learned=[{"a": "2.0.0", "b": "2.0.0", "c": "2.0.0"}],
        )
        learned = [constraint for constraint in model.constraints if constraint.provenance == "learned-nogood"]
        self.assertEqual(1, len(learned))
        self.assertEqual(3, len(learned[0].literals))
        reference = solve_reference_exact(model)
        self.assertEqual("optimal", reference.status)
        self.assertEqual(2, sum(version == "2.0.0" for version in reference.assignment.values()))

    def test_z3_backend_is_optional_and_matches_reference_when_installed(self):
        model = PeerOptimizationModel(
            packages=(
                PackageVariable("a", "1", ("2", "1"), (("2", (1,)), ("1", (0,)))),
                PackageVariable("b", "1", ("2", "1"), (("2", (1,)), ("1", (0,)))),
            ),
            constraints=(forbidden([("a", "2"), ("b", "2")], reason="pair"),),
            objective_width=1,
        )
        reference = solve_reference_exact(model)
        z3_result = solve_z3_exact(model, timeout_ms=5_000)
        self.assertIn(z3_result.status, {"optimal", "unavailable"})
        if z3_result.status == "optimal":
            self.assertEqual(reference.assignment, z3_result.assignment)
            self.assertEqual(reference.score, z3_result.score)

    def test_invalid_empty_domain_is_reported_before_solver_backend(self):
        model = PeerOptimizationModel(
            packages=(PackageVariable("empty", "1", (), ()),),
            objective_width=0,
        )
        reference = solve_reference_exact(model)
        z3_result = solve_z3_exact(model)
        self.assertEqual("error", reference.status)
        self.assertIn("INVALID_MODEL: EMPTY_DOMAIN: empty", reference.detail)
        self.assertEqual("error", z3_result.status)
        self.assertIn("INVALID_MODEL: EMPTY_DOMAIN: empty", z3_result.detail)

    def test_constraint_literal_outside_domain_is_diagnostic_not_keyerror(self):
        model = PeerOptimizationModel(
            packages=(PackageVariable("a", "1", ("1",), (("1", (0,)),)),),
            constraints=(forbidden([("a", "2")], reason="invalid fixture"),),
            objective_width=1,
        )
        result = solve_z3_exact(model)
        self.assertEqual("error", result.status)
        self.assertIn("CONSTRAINT_LITERAL_OUTSIDE_DOMAIN: a@2", result.detail)


    def test_shadow_result_is_non_authoritative_even_when_it_is_better(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {}})
        row = self.row("a")
        by_project = {"Demo": [row]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        reports = {}

        def production_current(component, rows_by_name, domains, graph, wrapped_client, mode, learned_nogoods=None, diagnostics=None):
            if diagnostics is not None:
                diagnostics.update(status="unknown_budget", states=1)
            return {name: rows_by_name[name].current_version for name in component}

        def exact_target(model, timeout_ms=30_000):
            assignment = {package.name: "2.0.0" for package in model.packages}
            return ExactSolveResult(
                backend="z3",
                status="optimal",
                assignment=assignment,
                score=model.assignment_score(assignment),
            )

        with mock.patch.object(roadmap, "_solve_peer_component", side_effect=production_current), mock.patch.object(
            roadmap, "solve_z3_exact", side_effect=exact_target
        ):
            assignments = roadmap.resolve_peer_compatibility(
                by_project,
                client,
                modes=("default",),
                apply_results=False,
                shadow_solver_config_by_project={
                    "Demo": {
                        "solverBackend": "custom",
                        "referenceOnly": True,
                        "shadowSolver": "z3",
                        "shadowSolverMinComponentSize": 1,
                    }
                },
                shadow_reports_out=reports,
            )

        self.assertEqual("1.0.0", assignments["Demo"]["default"]["a"])
        report = reports["Demo"]["default"][0]
        self.assertEqual("better", report["objectiveRelation"])
        self.assertEqual("2.0.0", report["assignment"]["a"])
        self.assertFalse(report["sameAssignment"])

    def test_authoritative_z3_bypasses_legacy_and_uses_exact_assignment(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {}})
        row = self.row("a")
        by_project = {"Demo": [row]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)

        def exact_target(model, timeout_ms=30_000):
            assignment = {package.name: "2.0.0" for package in model.packages}
            return ExactSolveResult(
                backend="z3", status="optimal", assignment=assignment,
                score=model.assignment_score(assignment),
            )

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target), mock.patch.object(
            roadmap, "_solve_peer_component", side_effect=AssertionError("legacy solver must not be authoritative fallback")
        ):
            assignments = roadmap.resolve_peer_compatibility(
                by_project,
                client,
                modes=("default",),
                apply_results=False,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
            )

        self.assertEqual("2.0.0", assignments["Demo"]["default"]["a"])

    def test_authoritative_z3_unknown_fails_closed_without_legacy_fallback(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {}})
        row = self.row("a")
        by_project = {"Demo": [row]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)

        with mock.patch.object(
            roadmap, "solve_z3_exact",
            return_value=ExactSolveResult(backend="z3", status="unknown", detail="timeout"),
        ), mock.patch.object(
            roadmap, "_solve_peer_component", side_effect=AssertionError("legacy fallback is forbidden")
        ):
            with self.assertRaises(
                roadmap.BaselineConstraintVerificationError
            ) as raised:
                roadmap.resolve_peer_compatibility(
                    by_project,
                    client,
                    modes=("default",),
                    apply_results=False,
                    shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                )

        error = raised.exception
        self.assertEqual("SOLVER_UNKNOWN", error.terminal_status)
        self.assertEqual("z3", error.terminal_source)
        self.assertEqual("EXACT_SOLVER_UNKNOWN", error.stop_code)
        self.assertIn("EXACT_SOLVER_UNKNOWN", str(error))
        self.assertIn("unfinished exact proof is not a dependency decision", str(error))

    def test_shadow_z3_runs_before_legacy_search(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {}})
        row = self.row("a")
        by_project = {"Demo": [row]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        order = []

        def exact_target(model, timeout_ms=30_000):
            order.append("z3")
            assignment = {package.name: "2.0.0" for package in model.packages}
            return ExactSolveResult(
                backend="z3", status="optimal", assignment=assignment,
                score=model.assignment_score(assignment),
            )

        def legacy(component, rows_by_name, domains, graph, wrapped_client, mode, learned_nogoods=None, diagnostics=None):
            order.append("legacy")
            if diagnostics is not None:
                diagnostics.update(status="optimal")
            return {name: rows_by_name[name].current_version for name in component}

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target), mock.patch.object(
            roadmap, "_solve_peer_component", side_effect=legacy
        ):
            roadmap.resolve_peer_compatibility(
                by_project,
                client,
                modes=("default",),
                apply_results=False,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "custom", "shadowSolver": "z3", "referenceOnly": True}},
            )

        self.assertGreaterEqual(len(order), 2)
        self.assertEqual(["z3", "legacy"], order[:2])


if __name__ == "__main__":
    unittest.main()
