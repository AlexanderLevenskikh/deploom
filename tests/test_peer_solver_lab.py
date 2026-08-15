from __future__ import annotations

import itertools
import random
import time
import unittest
from unittest import mock

import dependency_live_roadmap_generator as roadmap


class PeerSolverLabTests(unittest.TestCase):
    """Small synthetic graphs that exercise solver correctness and termination.

    These tests intentionally call the large-component solver directly even for
    tiny graphs. That keeps failures cheap and reproducible while covering the
    same conflict-directed algorithm that can hit the 100k-state production cap.
    """

    REGISTRY = "https://nexus.example/repository/npm-group"

    def make_client(self) -> roadmap.LiveDataClient:
        return roadmap.LiveDataClient(self.REGISTRY, timeout=1, batch_size=10, sleep_sec=0)

    def add_package(self, client, name, versions, *, unavailable=()):
        records = {}
        unavailable = set(unavailable)
        for version, extra in versions.items():
            records[version] = {
                **extra,
                "dist": {
                    "tarball": (
                        f"{self.REGISTRY}/{name.replace('@', '').replace('/', '-')}/-/"
                        f"{name.split('/')[-1]}-{version}.tgz"
                    )
                },
            }
            client.registry_artifact_cache[(name, version)] = {
                "status": "unavailable" if version in unavailable else "available",
                "tarballUrl": f"{self.REGISTRY}/artifact/{name}/{version}",
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
            reason="solver lab",
            notes="",
            target_default=desired,
            target_yellow=desired,
            target_green=desired,
            target_default_reason="solver lab desired",
            target_yellow_reason="solver lab desired",
            target_green_reason="solver lab desired",
        )

    def prepare(self, rows, client):
        by_project = {"Demo": rows}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        rows_by_name = {row.name: row for row in rows}
        domains = {
            name: roadmap._candidate_domain(row, "default", client)
            for name, row in rows_by_name.items()
        }
        graph = roadmap._potential_peer_graph(rows_by_name, domains, client)
        return rows_by_name, domains, graph

    def solve_large(self, rows, client):
        rows_by_name, domains, graph = self.prepare(rows, client)
        component = sorted(rows_by_name)
        assignment = roadmap._solve_large_peer_component(
            component,
            rows_by_name,
            domains,
            graph,
            client,
            "default",
        )
        return assignment, rows_by_name, domains, graph

    def brute_force_best(self, rows, client):
        rows_by_name, domains, _graph = self.prepare(rows, client)
        component = sorted(rows_by_name)
        # This oracle must enumerate the *complete* model domain.  Reusing the
        # production large-search cap here would make the oracle share exactly
        # the same blind spot as the solver it is supposed to validate.
        search_domains = domains
        domain_rank = {
            name: {version: index for index, version in enumerate(search_domains[name])}
            for name in component
        }
        best = None
        best_score = None
        best_key = None
        for values in itertools.product(*(search_domains[name] for name in component)):
            assignment = dict(zip(component, values))
            issue, _involved = roadmap._complete_assignment_constraint_detail(
                component, assignment, rows_by_name, client
            )
            if issue:
                continue
            if any(
                version != rows_by_name[name].current_version
                and not roadmap._candidate_registry_installable(
                    rows_by_name[name],
                    version,
                    client,
                    trusted_target=rows_by_name[name].target_default,
                )
                for name, version in assignment.items()
            ):
                continue
            score = roadmap._assignment_score(
                component, assignment, rows_by_name, "default", domain_rank
            )
            key = tuple(sorted(assignment.items()))
            if best is None or score > best_score or (score == best_score and key < best_key):
                best = dict(assignment)
                best_score = score
                best_key = key
        self.assertIsNotNone(best, "current assignment should always keep the synthetic graph satisfiable")
        return best

    def test_large_solver_trivial_independent_packages_finish_immediately(self):
        client = self.make_client()
        rows = []
        for index in range(6):
            name = f"pkg-{index}"
            self.add_package(client, name, {"1.0.0": {}, "2.0.0": {}})
            rows.append(self.row(name))

        visited = []
        original = roadmap._complete_assignment_constraint_detail

        def record(component, assignment, rows_by_name, wrapped_client, learned_nogoods=None):
            visited.append(tuple(sorted(assignment.items())))
            return original(component, assignment, rows_by_name, wrapped_client, learned_nogoods)

        with mock.patch.object(roadmap, "_complete_assignment_constraint_detail", side_effect=record):
            assignment, *_ = self.solve_large(rows, client)

        self.assertEqual({row.name: "2.0.0" for row in rows}, assignment)
        self.assertEqual(1, len(visited))

    def test_large_solver_peer_chain_moves_as_one_compatible_cohort(self):
        client = self.make_client()
        rows = []
        size = 6
        for index in range(size):
            name = f"chain-{index}"
            next_name = f"chain-{index + 1}"
            target = {"peerDependencies": {next_name: ">=2"}} if index + 1 < size else {}
            self.add_package(client, name, {"1.0.0": {}, "1.5.0": {}, "2.0.0": target})
            rows.append(self.row(name))

        assignment, rows_by_name, *_ = self.solve_large(rows, client)

        self.assertEqual({row.name: "2.0.0" for row in rows}, assignment)
        issue, _ = roadmap._complete_assignment_constraint_detail(
            sorted(rows_by_name), assignment, rows_by_name, client
        )
        self.assertEqual("", issue)

    def test_large_solver_cycle_terminates_and_moves_both_members(self):
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

        started = time.perf_counter()
        assignment, *_ = self.solve_large(rows, client)
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual({"a": "2.0.0", "b": "2.0.0"}, assignment)

    def test_large_solver_missing_required_peer_falls_back_without_spinning(self):
        client = self.make_client()
        self.add_package(client, "plugin", {
            "1.0.0": {},
            "1.5.0": {},
            "2.0.0": {"peerDependencies": {"missing-host": ">=2"}},
        })
        rows = [self.row("plugin")]
        visited = []
        original = roadmap._complete_assignment_constraint_detail

        def record(component, assignment, rows_by_name, wrapped_client, learned_nogoods=None):
            visited.append(tuple(sorted(assignment.items())))
            return original(component, assignment, rows_by_name, wrapped_client, learned_nogoods)

        with mock.patch.object(roadmap, "_complete_assignment_constraint_detail", side_effect=record):
            assignment, *_ = self.solve_large(rows, client)

        self.assertNotEqual("2.0.0", assignment["plugin"])
        self.assertLessEqual(len(visited), 3)

    def test_large_solver_never_expands_the_same_complete_state_twice(self):
        client = self.make_client()
        self.add_package(client, "a", {
            "1.0.0": {},
            "1.5.0": {"peerDependencies": {"host": "^1"}},
            "2.0.0": {"peerDependencies": {"host": "^1"}},
        })
        self.add_package(client, "b", {
            "1.0.0": {},
            "1.5.0": {"peerDependencies": {"host": "^2"}},
            "2.0.0": {"peerDependencies": {"host": "^2"}},
        })
        self.add_package(client, "host", {"1.0.0": {}, "1.5.0": {}, "2.0.0": {}})
        rows = [self.row("a"), self.row("b"), self.row("host")]
        visited = []
        original = roadmap._complete_assignment_constraint_detail

        def record(component, assignment, rows_by_name, wrapped_client, learned_nogoods=None):
            visited.append(tuple(sorted(assignment.items())))
            return original(component, assignment, rows_by_name, wrapped_client, learned_nogoods)

        with mock.patch.object(roadmap, "_complete_assignment_constraint_detail", side_effect=record):
            self.solve_large(rows, client)

        self.assertGreater(len(visited), 1)
        self.assertEqual(len(visited), len(set(visited)), "a complete assignment was expanded twice")

    def test_large_solver_budget_exhaustion_is_bounded_and_returns_current(self):
        client = self.make_client()
        self.add_package(client, "plugin", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"host": "^1"}},
        })
        self.add_package(client, "host", {"1.0.0": {}, "2.0.0": {}})
        rows = [self.row("plugin"), self.row("host")]
        messages = []

        with (
            mock.patch.object(roadmap, "PEER_SOLVER_LARGE_MAX_VISITS", 1),
            mock.patch.object(roadmap, "eprint", side_effect=lambda *args: messages.append(" ".join(map(str, args)))),
        ):
            assignment, *_ = self.solve_large(rows, client)

        self.assertEqual({"plugin": "1.0.0", "host": "1.0.0"}, assignment)
        self.assertTrue(any("UNKNOWN_BUDGET after 1 conflict-directed states" in message for message in messages))

    def test_budget_exhaustion_is_not_reported_as_proven_peer_deferred(self):
        client = self.make_client()
        rows = []
        for index in range(8):
            name = f"plugin-{index}"
            host_range = "^1" if index < 4 else "^2"
            self.add_package(client, name, {
                "1.0.0": {},
                "2.0.0": {"peerDependencies": {"host": host_range}},
            })
            rows.append(self.row(name))
        self.add_package(client, "host", {"1.0.0": {}, "2.0.0": {}})
        rows.append(self.row("host"))

        by_project = {"Demo": rows}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        statuses = {}
        with mock.patch.object(roadmap, "PEER_SOLVER_LARGE_MAX_VISITS", 1):
            roadmap.resolve_peer_compatibility(
                by_project,
                client,
                modes=("default",),
                solver_statuses_out=statuses,
                shadow_solver_config_by_project={
                    "Demo": {"solverBackend": "custom", "shadowSolver": "off", "referenceOnly": True}
                },
            )

        self.assertTrue(all(status == "unknown_budget" for status in statuses["Demo"]["default"].values()))
        reasons = [row.resolution_reason_default for row in rows if row.target_default == roadmap.NO_ACTION]
        self.assertTrue(reasons)
        self.assertTrue(all("PEER_SOLVER_UNKNOWN_BUDGET" in reason for reason in reasons))
        self.assertTrue(all("PEER_RESOLUTION_DEFERRED" not in reason for reason in reasons))

    def test_large_solver_can_reach_interior_candidate_outside_compact_seed(self):
        client = self.make_client()
        rows = []
        b_versions = {f"1.{minor}.0": {} for minor in range(70)}
        self.add_package(client, "a", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"b": "=1.50.0"}},
        })
        rows.append(self.row("a"))
        self.add_package(client, "b", b_versions)
        rows.append(self.row("b", desired="1.69.0"))

        # Pad to the production large-component path.  Each pad package points
        # at the next one so the component stays connected without constraining
        # a/b's interior-version requirement.
        previous = "b"
        for index in range(7):
            name = f"pad-{index}"
            self.add_package(client, name, {
                "1.0.0": {},
                "2.0.0": {"peerDependencies": {previous: ">=1.0.0"}},
            })
            rows.append(self.row(name))
            previous = name

        actual, *_ = self.solve_large(rows, client)

        self.assertEqual("2.0.0", actual["a"])
        self.assertEqual("1.50.0", actual["b"])

    def test_conflict_heavy_star_stays_far_below_production_state_budget(self):
        client = self.make_client()
        rows = []
        # 8 plugins + host => 9 packages, i.e. the production large-component path.
        # Half of the desired plugins want host ^1 and half want host ^2. A valid
        # optimum requires coordinated fallbacks, but should still be tiny vs 100k.
        for index in range(8):
            name = f"plugin-{index}"
            desired_host = "^1" if index < 4 else "^2"
            self.add_package(client, name, {
                "1.0.0": {},
                "1.5.0": {},
                "2.0.0": {"peerDependencies": {"host": desired_host}},
            })
            rows.append(self.row(name))
        self.add_package(client, "host", {"1.0.0": {}, "1.5.0": {}, "2.0.0": {}})
        rows.append(self.row("host"))

        visited = []
        original = roadmap._complete_assignment_constraint_detail

        def record(component, assignment, rows_by_name, wrapped_client, learned_nogoods=None):
            visited.append(tuple(sorted(assignment.items())))
            return original(component, assignment, rows_by_name, wrapped_client, learned_nogoods)

        started = time.perf_counter()
        with mock.patch.object(roadmap, "_complete_assignment_constraint_detail", side_effect=record):
            assignment, rows_by_name, *_ = self.solve_large(rows, client)
        elapsed = time.perf_counter() - started

        issue, _ = original(sorted(rows_by_name), assignment, rows_by_name, client)
        self.assertEqual("", issue)
        self.assertLess(elapsed, 2.0)
        self.assertLess(len(visited), 2_000)
        self.assertEqual(len(visited), len(set(visited)))

    def test_large_solver_matches_bruteforce_oracle_on_named_conflict_cases(self):
        cases = []

        # Latest host conflicts with latest plugin; one side must fall back.
        client = self.make_client()
        self.add_package(client, "plugin", {
            "1.0.0": {}, "1.5.0": {"peerDependencies": {"host": "^1"}},
            "2.0.0": {"peerDependencies": {"host": "^1"}},
        })
        self.add_package(client, "host", {"1.0.0": {}, "1.5.0": {}, "2.0.0": {}})
        cases.append((client, [self.row("plugin"), self.row("host")]))

        # Triangle with two constraints on the same host.
        client = self.make_client()
        self.add_package(client, "a", {
            "1.0.0": {}, "1.5.0": {"peerDependencies": {"host": "^1"}},
            "2.0.0": {"peerDependencies": {"host": "^2"}},
        })
        self.add_package(client, "b", {
            "1.0.0": {}, "1.5.0": {"peerDependencies": {"host": "^1"}},
            "2.0.0": {"peerDependencies": {"host": "^1"}},
        })
        self.add_package(client, "host", {"1.0.0": {}, "1.5.0": {}, "2.0.0": {}})
        cases.append((client, [self.row("a"), self.row("b"), self.row("host")]))

        for client, rows in cases:
            with self.subTest(packages=[row.name for row in rows]):
                expected = self.brute_force_best(rows, client)
                actual, *_ = self.solve_large(rows, client)
                self.assertEqual(expected, actual)

    def test_large_solver_matches_bruteforce_on_deterministic_generated_micrographs(self):
        rng = random.Random(0xC0FFEE)
        peer_specs = ("^1", "^2")

        for case_index in range(40):
            client = self.make_client()
            package_count = rng.randint(2, 5)
            names = [f"p{i}" for i in range(package_count)]
            rows = []
            for source_index, name in enumerate(names):
                target_peers = {}
                fallback_peers = {}
                for peer_index, peer_name in enumerate(names):
                    if source_index == peer_index:
                        continue
                    if rng.random() < 0.28:
                        target_peers[peer_name] = rng.choice(peer_specs)
                    if rng.random() < 0.16:
                        fallback_peers[peer_name] = rng.choice(peer_specs)
                self.add_package(client, name, {
                    "1.0.0": {},
                    "1.5.0": {"peerDependencies": fallback_peers} if fallback_peers else {},
                    "2.0.0": {"peerDependencies": target_peers} if target_peers else {},
                })
                rows.append(self.row(name))

            with self.subTest(case=case_index, packages=package_count):
                expected = self.brute_force_best(rows, client)
                actual, *_ = self.solve_large(rows, client)
                self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
