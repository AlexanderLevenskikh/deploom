from __future__ import annotations

import unittest

import dependency_live_roadmap_generator as roadmap
from dependency_interaction import DIRECT_SHADOWING, PEER_REQUIREMENT
from peer_solver_model import solve_reference_exact


class DependencyInteractionTests(unittest.TestCase):
    REGISTRY = "https://nexus.example/repository/npm-group"

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
    def row(name, current, desired):
        return roadmap.DependencyRow(
            project="Demo", package_dir=".", name=name, kind="dev", requested_spec="*",
            current_version=current, current_source="lockfile", latest_version=desired,
            current_vulns="0", min_no_critical=current, min_no_high=current,
            min_no_vuln=current, min_lag_12m=current, min_lag_9m=current,
            min_lag_6m=current, min_lag_3m=current, group=1, reason="interaction test", notes="",
            target_default=desired, target_yellow=desired, target_green=desired,
            target_default_reason="desired", target_yellow_reason="desired", target_green_reason="desired",
        )

    def test_normal_dependency_between_direct_packages_is_verification_interaction_not_peer(self):
        client = self.make_client()
        self.add_package(client, "vitest", {
            "0.30.1": {"dependencies": {"vite": "^4.0.0"}},
            "3.2.6": {"dependencies": {"vite": "^5.0.0 || ^6.0.0 || ^7.0.0"}},
        })
        self.add_package(client, "vite", {"4.3.9": {}, "5.4.0": {}})
        rows = [self.row("vitest", "0.30.1", "3.2.6"), self.row("vite", "4.3.9", roadmap.NO_ACTION)]
        rows_by_name = {row.name: row for row in rows}
        domains = {name: roadmap._candidate_domain(row, "default", client) for name, row in rows_by_name.items()}

        hard_graph = roadmap._potential_peer_graph(rows_by_name, domains, client)
        self.assertEqual(set(), hard_graph["vitest"])
        self.assertEqual(set(), hard_graph["vite"])

        edges = roadmap._potential_interaction_edges(rows_by_name, domains, client)
        direct = [edge for edge in edges if edge.kind == DIRECT_SHADOWING]
        self.assertTrue(direct)
        self.assertEqual({("vite", "vitest")}, {(edge.left, edge.right) for edge in direct})

    def test_peer_relation_is_also_exposed_in_interaction_ir(self):
        client = self.make_client()
        self.add_package(client, "plugin", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"host": "^2.0.0"}},
        })
        self.add_package(client, "host", {"1.0.0": {}, "2.0.0": {}})
        rows = [self.row("plugin", "1.0.0", "2.0.0"), self.row("host", "1.0.0", "2.0.0")]
        rows_by_name = {row.name: row for row in rows}
        domains = {name: roadmap._candidate_domain(row, "default", client) for name, row in rows_by_name.items()}
        edges = roadmap._potential_interaction_edges(rows_by_name, domains, client)
        self.assertTrue(any(edge.kind == PEER_REQUIREMENT for edge in edges))

    def test_changed_package_verification_unit_carries_current_direct_shadow_neighbor(self):
        client = self.make_client()
        self.add_package(client, "vitest", {
            "0.30.1": {"dependencies": {"vite": "^4.0.0"}},
            "3.2.6": {"dependencies": {"vite": "^5.0.0"}},
        })
        self.add_package(client, "vite", {"4.3.9": {}, "5.4.0": {}})
        rows = [self.row("vitest", "0.30.1", "3.2.6"), self.row("vite", "4.3.9", roadmap.NO_ACTION)]
        rows_by_name = {row.name: row for row in rows}
        assignment = {"vitest": "3.2.6", "vite": "4.3.9"}

        units = roadmap._verification_units_for_assignment(rows_by_name, assignment, "default", client, [])
        self.assertEqual(1, len(units))
        self.assertEqual(("vite", "vitest"), units[0].packages)

    def test_learned_direct_interaction_nogood_can_activate_companion_move(self):
        client = self.make_client()
        self.add_package(client, "vitest", {
            "0.30.1": {"dependencies": {"vite": "^4.0.0"}},
            "3.2.6": {"dependencies": {"vite": "^5.0.0"}},
        })
        self.add_package(client, "vite", {"4.3.9": {}, "5.4.0": {}})
        rows = [self.row("vitest", "0.30.1", "3.2.6"), self.row("vite", "4.3.9", roadmap.NO_ACTION)]
        roadmap.capture_desired_targets({"Demo": rows})
        roadmap.enrich_registry_target_evidence({"Demo": rows}, client)
        rows_by_name = {row.name: row for row in rows}
        domains = {name: roadmap._candidate_domain(row, "default", client) for name, row in rows_by_name.items()}
        learned = [{"vitest": "3.2.6", "vite": "4.3.9"}]
        hard_graph = roadmap._potential_peer_graph(rows_by_name, domains, client)
        from constraint_verify import merge_nogood_edges
        merge_nogood_edges(hard_graph, learned)
        self.assertEqual([["vite", "vitest"]], roadmap._graph_components(hard_graph))
        model = roadmap._build_peer_optimization_model(
            ["vite", "vitest"], rows_by_name, domains, client, "default", learned
        )
        solved = solve_reference_exact(model)
        self.assertEqual("optimal", solved.status)
        self.assertEqual("3.2.6", solved.assignment["vitest"])
        self.assertEqual("5.4.0", solved.assignment["vite"])

    def test_interaction_context_is_one_hop_not_transitive_giant_component(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {"dependencies": {"b": "^1"}}})
        self.add_package(client, "b", {"1.0.0": {"dependencies": {"c": "^1"}}})
        self.add_package(client, "c", {"1.0.0": {}})
        rows = [
            self.row("a", "1.0.0", "2.0.0"),
            self.row("b", "1.0.0", roadmap.NO_ACTION),
            self.row("c", "1.0.0", roadmap.NO_ACTION),
        ]
        rows_by_name = {row.name: row for row in rows}
        assignment = {"a": "2.0.0", "b": "1.0.0", "c": "1.0.0"}
        units = roadmap._verification_units_for_assignment(rows_by_name, assignment, "default", client, [])
        self.assertEqual(("a", "b"), units[0].packages)


if __name__ == "__main__":
    unittest.main()
