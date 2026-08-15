from __future__ import annotations

import copy
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import dependency_live_roadmap_generator as roadmap


class PeerCompatibilitySolverTests(unittest.TestCase):
    REGISTRY = "https://nexus.example/repository/npm-group"

    def make_client(self) -> roadmap.LiveDataClient:
        return roadmap.LiveDataClient(self.REGISTRY, timeout=1, batch_size=10, sleep_sec=0)

    def add_package(self, client, name, versions, *, unavailable=()):
        records = {}
        unavailable = set(unavailable)
        for version, extra in versions.items():
            records[version] = {
                **extra,
                "dist": {"tarball": f"{self.REGISTRY}/{name.replace('@', '').replace('/', '-')}/-/{name.split('/')[-1]}-{version}.tgz"},
            }
            client.registry_artifact_cache[(name, version)] = {
                "status": "unavailable" if version in unavailable else "available",
                "tarballUrl": f"{self.REGISTRY}/artifact/{name}/{version}",
            }
        client.npm_cache[name] = {"versions": records}

    @staticmethod
    def row(name, current, desired=roadmap.NO_ACTION, *, group=5, current_vulns="0", min_lag=None, scope_excluded=False):
        lag = min_lag or current
        return roadmap.DependencyRow(
            project="Demo",
            package_dir=".",
            name=name,
            kind="dev",
            requested_spec="*",
            current_version=current,
            current_source="lockfile",
            latest_version=desired if roadmap.target_is_action(desired) else current,
            current_vulns=current_vulns,
            min_no_critical=current,
            min_no_high=current,
            min_no_vuln=current,
            min_lag_12m=lag,
            min_lag_9m=lag,
            min_lag_6m=lag,
            min_lag_3m=lag,
            group=group,
            reason="display only",
            notes="",
            target_default=desired,
            target_yellow=desired,
            target_green=desired,
            target_default_reason="policy desired",
            target_yellow_reason="policy desired",
            target_green_reason="policy desired",
            scope_excluded=scope_excluded,
        )

    def resolve(self, rows, client, modes=("default",)):
        by_project = {"Demo": rows}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        roadmap.resolve_peer_compatibility(by_project, client, modes=modes)
        return by_project

    def test_residual_pending_target_is_preserved_over_newer_policy_target(self):
        client = self.make_client()
        self.add_package(client, "tool", {"1.0.0": {}, "2.0.0": {}, "3.0.0": {}})
        row = self.row("tool", "1.0.0", "3.0.0")
        by_project = {"Demo": [row]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)

        roadmap.resolve_peer_compatibility(
            by_project,
            client,
            modes=("default",),
            residual_targets_by_project={"Demo": {"tool": "2.0.0"}},
        )

        self.assertEqual("2.0.0", row.target_default)

    def test_residual_pending_target_moves_only_when_new_hard_constraint_requires_it(self):
        client = self.make_client()
        self.add_package(client, "plugin", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"core": "=2.0.0"}},
            "3.0.0": {"peerDependencies": {"core": "=3.0.0"}},
        })
        self.add_package(client, "core", {"2.0.0": {}, "3.0.0": {}})
        plugin = self.row("plugin", "1.0.0", "3.0.0")
        core = self.row("core", "3.0.0", roadmap.NO_ACTION)
        by_project = {"Demo": [plugin, core]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)

        roadmap.resolve_peer_compatibility(
            by_project,
            client,
            modes=("default",),
            residual_targets_by_project={"Demo": {"plugin": "2.0.0", "core": "3.0.0"}},
        )

        self.assertEqual("3.0.0", plugin.target_default)
        self.assertEqual(roadmap.NO_ACTION, core.target_default)

    def test_residual_merged_target_is_hard_fixed_to_cumulative_current(self):
        client = self.make_client()
        self.add_package(client, "tool", {"1.0.0": {}, "2.0.0": {}, "3.0.0": {}})
        row = self.row("tool", "2.0.0", "3.0.0")
        by_project = {"Demo": [row]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)

        assignments = roadmap.resolve_peer_compatibility(
            by_project,
            client,
            modes=("default",),
            residual_targets_by_project={"Demo": {"tool": "2.0.0"}},
        )

        self.assertEqual("2.0.0", assignments["Demo"]["default"]["tool"])
        self.assertEqual(roadmap.NO_ACTION, row.target_default)

    def test_vitest_vite_cross_group_companion_is_raised(self):
        client = self.make_client()
        self.add_package(client, "vitest", {
            "0.30.1": {},
            "3.2.6": {"peerDependencies": {"vite": ">=5.0.0"}},
        })
        self.add_package(client, "vite", {"4.3.9": {}, "5.4.0": {}, "6.0.0": {}})
        vitest = self.row("vitest", "0.30.1", "3.2.6", group=37)
        vite = self.row("vite", "4.3.9", roadmap.NO_ACTION, group=2)

        self.resolve([vitest, vite], client)

        self.assertEqual("3.2.6", vitest.target_default)
        self.assertEqual("5.4.0", vite.target_default)
        self.assertEqual(37, vitest.group)
        self.assertEqual(2, vite.group)
        self.assertEqual(vitest.compatibility_cohort, vite.compatibility_cohort)
        self.assertEqual("", vitest.resolution_reason_default)
        self.assertIn("PEER_COMPANION", vite.resolution_reason_default)
        self.assertIn("vitest@3.2.6 requires vite@>=5.0.0", vite.resolution_reason_default)

    def test_node_engine_constraint_is_solved_before_branch_execution(self):
        client = self.make_client()
        self.add_package(client, "tool", {
            "1.0.0": {"engines": {"node": ">=18"}},
            "1.9.0": {"engines": {"node": ">=18"}},
            "2.0.0": {"engines": {"node": ">=20.19"}},
        })
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"volta":{"node":"18.20.4"}}', encoding="utf-8")
            row = self.row("tool", "1.0.0", "2.0.0")
            row.package_dir = str(root)

            self.resolve([row], client)

        self.assertEqual("1.9.0", row.target_default)
        self.assertIn("PROJECT_NODE_ENGINE_CONFLICT", row.resolution_reason_default)
        self.assertTrue(row.target_default_dynamic_locked)

    def test_transition_safety_couples_package_that_drops_old_peer_with_moving_provider(self):
        client = self.make_client()
        self.add_package(client, "plugin", {
            "1.0.0": {"peerDependencies": {"core": "^1.0.0"}},
            "2.0.0": {},
        })
        self.add_package(client, "core", {"1.0.0": {}, "2.0.0": {}})
        plugin = self.row("plugin", "1.0.0", "2.0.0")
        core = self.row("core", "1.0.0", "2.0.0")

        self.resolve([plugin, core], client)

        self.assertEqual("2.0.0", plugin.target_default)
        self.assertEqual("2.0.0", core.target_default)
        self.assertTrue(plugin.compatibility_cohort)
        self.assertEqual(plugin.compatibility_cohort, core.compatibility_cohort)
        self.assertIn("TRANSITION_COHORT", plugin.compatibility_note + core.compatibility_note)

    def test_transition_safety_does_not_create_cohort_when_old_peer_provider_does_not_move(self):
        client = self.make_client()
        self.add_package(client, "plugin", {
            "1.0.0": {"peerDependencies": {"core": "^1.0.0"}},
            "2.0.0": {},
        })
        self.add_package(client, "core", {"1.0.0": {}, "2.0.0": {}})
        plugin = self.row("plugin", "1.0.0", "2.0.0")
        core = self.row("core", "1.0.0", roadmap.NO_ACTION)

        self.resolve([plugin, core], client)

        self.assertEqual("2.0.0", plugin.target_default)
        self.assertEqual(roadmap.NO_ACTION, core.target_default)
        self.assertFalse(plugin.compatibility_cohort)
        self.assertFalse(core.compatibility_cohort)

    def test_vitest_falls_back_when_vite_5_is_unavailable(self):
        client = self.make_client()
        self.add_package(client, "vitest", {
            "0.30.1": {},
            "2.1.9": {"peerDependencies": {"vite": "^4.0.0"}},
            "3.2.6": {"peerDependencies": {"vite": ">=5.0.0"}},
        })
        self.add_package(client, "vite", {"4.3.9": {}, "5.4.0": {}}, unavailable={"5.4.0"})
        vitest = self.row("vitest", "0.30.1", "3.2.6")
        vite = self.row("vite", "4.3.9")

        self.resolve([vitest, vite], client)

        self.assertEqual("2.1.9", vitest.target_default)
        self.assertEqual(roadmap.NO_ACTION, vite.target_default)
        self.assertIn("PEER_RESOLUTION_FALLBACK", vitest.resolution_reason_default)

    def test_stale_target_is_recomputed_after_registry_removes_companion(self):
        client = self.make_client()
        self.add_package(client, "a", {
            "1.0.0": {},
            "1.5.0": {"peerDependencies": {"b": "^1.0.0"}},
            "2.0.0": {"peerDependencies": {"b": ">=2.0.0"}},
        })
        self.add_package(client, "b", {"1.0.0": {}, "2.0.0": {}}, unavailable={"2.0.0"})
        a = self.row("a", "1.0.0", "2.0.0")
        b = self.row("b", "1.0.0", "2.0.0")

        self.resolve([a, b], client)

        self.assertNotEqual("2.0.0", a.target_default)
        self.assertEqual("1.5.0", a.target_default)
        self.assertEqual(roadmap.NO_ACTION, b.target_default)
        self.assertEqual([], roadmap.validate_final_peer_assignment({"Demo": [a, b]}, client, modes=("default",)))

    def test_cross_group_result_equals_same_group_result(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {"peerDependencies": {"b": ">=2"}}})
        self.add_package(client, "b", {"1.0.0": {}, "2.0.0": {}})

        same = [self.row("a", "1.0.0", "2.0.0", group=7), self.row("b", "1.0.0", group=7)]
        cross = [self.row("a", "1.0.0", "2.0.0", group=1), self.row("b", "1.0.0", group=99)]
        self.resolve(same, client)
        self.resolve(cross, client)

        self.assertEqual([r.target_default for r in same], [r.target_default for r in cross])

    def test_group_invariance_for_ids_names_and_boundaries(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {"peerDependencies": {"b": ">=2"}}})
        self.add_package(client, "b", {"1.0.0": {}, "2.0.0": {}})
        first = [self.row("a", "1.0.0", "2.0.0", group=1), self.row("b", "1.0.0", group=5)]
        second = [self.row("a", "1.0.0", "2.0.0", group=41), self.row("b", "1.0.0", group=777)]
        first[0].reason = "old group boundary/name"
        second[0].reason = "completely different display taxonomy"

        self.resolve(first, client)
        self.resolve(second, client)

        self.assertEqual({r.name: r.target_default for r in first}, {r.name: r.target_default for r in second})
        self.assertEqual({r.name: r.resolution_reason_default for r in first}, {r.name: r.resolution_reason_default for r in second})

    def test_peer_chain_propagates_to_the_end(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {"peerDependencies": {"b": ">=2"}}})
        self.add_package(client, "b", {"1.0.0": {}, "2.0.0": {"peerDependencies": {"c": ">=2"}}})
        self.add_package(client, "c", {"1.0.0": {}, "2.0.0": {}})
        a = self.row("a", "1.0.0", "2.0.0")
        b = self.row("b", "1.0.0")
        c = self.row("c", "1.0.0")

        self.resolve([a, b, c], client)

        self.assertEqual(("2.0.0", "2.0.0", "2.0.0"), (a.target_default, b.target_default, c.target_default))
        self.assertEqual(a.compatibility_cohort, c.compatibility_cohort)

    def test_multiple_peer_constraints_use_registry_intersection(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {"peerDependencies": {"b": ">=3 <5"}}})
        self.add_package(client, "c", {"1.0.0": {}, "2.0.0": {"peerDependencies": {"b": ">=4 <6"}}})
        self.add_package(client, "b", {"2.0.0": {}, "3.5.0": {}, "4.2.0": {}, "5.2.0": {}})
        a = self.row("a", "1.0.0", "2.0.0")
        c = self.row("c", "1.0.0", "2.0.0")
        b = self.row("b", "2.0.0")

        self.resolve([a, b, c], client)

        self.assertEqual("4.2.0", b.target_default)
        self.assertEqual("2.0.0", a.target_default)
        self.assertEqual("2.0.0", c.target_default)

    def test_cycle_terminates_with_valid_assignment(self):
        client = self.make_client()
        self.add_package(client, "a", {"1.0.0": {}, "2.0.0": {"peerDependencies": {"b": ">=2"}}})
        self.add_package(client, "b", {"1.0.0": {}, "2.0.0": {"peerDependencies": {"a": ">=2"}}})
        a = self.row("a", "1.0.0", "2.0.0")
        b = self.row("b", "1.0.0")

        self.resolve([a, b], client)

        self.assertEqual(("2.0.0", "2.0.0"), (a.target_default, b.target_default))
        self.assertEqual([], roadmap.validate_final_peer_assignment({"Demo": [a, b]}, client, modes=("default",)))

    def test_missing_required_peer_uses_fallback_when_new_names_are_not_allowed(self):
        client = self.make_client()
        self.add_package(client, "a", {
            "1.0.0": {},
            "1.5.0": {},
            "2.0.0": {"peerDependencies": {"missing-host": ">=2"}},
        })
        a = self.row("a", "1.0.0", "2.0.0")

        self.resolve([a], client)

        self.assertEqual("1.5.0", a.target_default)
        self.assertIn("missing peer missing-host", a.resolution_reason_default)

    def test_absent_optional_peer_does_not_block_target(self):
        client = self.make_client()
        self.add_package(client, "a", {
            "1.0.0": {},
            "2.0.0": {
                "peerDependencies": {"optional-host": ">=2"},
                "peerDependenciesMeta": {"optional-host": {"optional": True}},
            },
        })
        a = self.row("a", "1.0.0", "2.0.0")

        self.resolve([a], client)

        self.assertEqual("2.0.0", a.target_default)

    def test_conflicting_peers_choose_best_compatible_fallback(self):
        client = self.make_client()
        self.add_package(client, "a", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"host": "^1.0.0"}},
        })
        self.add_package(client, "c", {
            "1.0.0": {},
            "1.5.0": {"peerDependencies": {"host": "^1.0.0"}},
            "2.0.0": {"peerDependencies": {"host": "^2.0.0"}},
        })
        self.add_package(client, "host", {"1.0.0": {}, "2.0.0": {}})
        a = self.row("a", "1.0.0", "2.0.0")
        c = self.row("c", "1.0.0", "2.0.0")
        host = self.row("host", "1.0.0")

        self.resolve([a, c, host], client)

        self.assertEqual("2.0.0", a.target_default)
        self.assertEqual("1.5.0", c.target_default)
        self.assertEqual(roadmap.NO_ACTION, host.target_default)
        self.assertIn("PEER_RESOLUTION_FALLBACK", c.resolution_reason_default)

    def test_registry_narrowing_recalculates_dependents(self):
        client = self.make_client()
        self.add_package(client, "a", {
            "1.0.0": {},
            "1.8.0": {"peerDependencies": {"b": "^1"}},
            "2.0.0": {"peerDependencies": {"b": "^2"}},
        })
        self.add_package(client, "b", {"1.0.0": {}, "2.0.0": {}}, unavailable={"2.0.0"})
        a = self.row("a", "1.0.0", "2.0.0")
        b = self.row("b", "1.0.0")

        self.resolve([a, b], client)

        self.assertEqual("1.8.0", a.target_default)
        self.assertEqual(roadmap.NO_ACTION, b.target_default)

    def test_projection_counts_only_final_resolved_targets(self):
        client = self.make_client()
        self.add_package(client, "blocked", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"missing": ">=2"}},
        })
        self.add_package(client, "ok", {"1.0.0": {}, "2.0.0": {}})
        blocked = self.row("blocked", "1.0.0", "2.0.0", min_lag="2.0.0")
        ok = self.row("ok", "1.0.0", "2.0.0", min_lag="2.0.0")

        self.resolve([blocked, ok], client, modes=("yellow",))

        self.assertEqual(roadmap.NO_ACTION, blocked.target_yellow)
        self.assertEqual("2.0.0", ok.target_yellow)
        self.assertEqual(1, roadmap.projected_lag_ok_count([blocked, ok], "yellow"))

    def test_determinism_is_independent_of_row_and_metadata_order(self):
        def build(reverse=False):
            client = self.make_client()
            a_versions = [("1.0.0", {}), ("2.0.0", {"peerDependencies": {"b": ">=2"}})]
            b_versions = [("1.0.0", {}), ("2.0.0", {}), ("3.0.0", {})]
            if reverse:
                a_versions.reverse()
                b_versions.reverse()
            self.add_package(client, "a", dict(a_versions))
            self.add_package(client, "b", dict(b_versions))
            rows = [self.row("a", "1.0.0", "2.0.0", group=11), self.row("b", "1.0.0", group=42)]
            if reverse:
                rows.reverse()
            self.resolve(rows, client)
            return {
                row.name: (row.target_default, row.resolution_reason_default, row.compatibility_cohort)
                for row in rows
            }

        self.assertEqual(build(False), build(True))


    def test_full_yellow_target_planning_is_display_group_invariant(self):
        def build(groups):
            rows = []
            for index, group in enumerate(groups):
                row = self.row(f"pkg-{index:02d}", "1.0.0", roadmap.NO_ACTION, group=group, min_lag="2.0.0")
                row.latest_version = "2.0.0"
                row.reason = f"display taxonomy only: group={group}; " + ("blocked отдельная задача" if group % 2 else "simple")
                rows.append(row)
            by_project = {"Demo": rows}
            health = roadmap.enrich_project_targets(by_project)
            roadmap.minimize_yellow_plan_after_compatibility(by_project, self.make_client(), health)
            return {row.name: (row.target_yellow, row.target_default) for row in rows}

        ordinary = build(list(range(1, 11)))
        arbitrary = build([101, 3, 777, 42, 8, 9001, 6, 73, 12, 5])
        self.assertEqual(ordinary, arbitrary)

    def test_branch_and_bound_does_not_probe_cartesian_product(self):
        client = self.make_client()
        self.add_package(client, "a", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"b": ">=2", "c": ">=2"}},
        })
        many = {"1.0.0": {}}
        many.update({f"{major}.0.0": {} for major in range(2, 41)})
        self.add_package(client, "b", many)
        self.add_package(client, "c", many)
        a = self.row("a", "1.0.0", "2.0.0")
        b = self.row("b", "1.0.0")
        c = self.row("c", "1.0.0")

        original = roadmap._candidate_registry_installable
        with mock.patch.object(roadmap, "_candidate_registry_installable", wraps=original) as probe:
            self.resolve([a, b, c], client)

        self.assertEqual(("2.0.0", "2.0.0", "2.0.0"), (a.target_default, b.target_default, c.target_default))
        # Naive search would consider ~40*40 companion combinations. The
        # optimistic bound should reject inferior branches before registry IO.
        self.assertLess(probe.call_count, 20)

    def test_update_effort_and_breaking_policy_are_group_invariant(self):
        first = self.row("demo-runtime", "1.0.0", "2.0.0", group=1)
        second = self.row("demo-runtime", "1.0.0", "2.0.0", group=987)
        first.kind = second.kind = "runtime"
        # Display explanations may change together with group taxonomy and must
        # not leak into executable effort/risk scoring.
        first.reason = "blocked / отдельная задача / display group 4"
        second.reason = "cosmetic display group 987"

        self.assertEqual(
            roadmap.row_update_effort_score(first, "2.0.0"),
            roadmap.row_update_effort_score(second, "2.0.0"),
        )
        self.assertEqual(
            roadmap.row_breaking_risk_note(first, "2.0.0"),
            roadmap.row_breaking_risk_note(second, "2.0.0"),
        )

    def test_critical_remediation_outranks_conflicting_high(self):
        client = self.make_client()
        self.add_package(client, "critical", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"host": "^2"}},
        })
        self.add_package(client, "high", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"host": "^1"}},
        })
        self.add_package(client, "host", {"1.0.0": {}, "2.0.0": {}})
        critical = self.row("critical", "1.0.0", "2.0.0", current_vulns="C:1")
        critical.min_no_critical = critical.min_no_high = "2.0.0"
        high = self.row("high", "1.0.0", "2.0.0", current_vulns="H:1")
        high.min_no_high = "2.0.0"
        host = self.row("host", "1.0.0")

        self.resolve([critical, high, host], client)

        self.assertEqual("2.0.0", critical.target_default)
        self.assertEqual(roadmap.NO_ACTION, high.target_default)
        self.assertEqual("2.0.0", host.target_default)

    def test_duplicate_package_rows_preserve_strictest_intent(self):
        client = self.make_client()
        self.add_package(client, "duplicate", {"1.0.0": {}, "1.5.0": {}, "2.0.0": {}})
        runtime = self.row("duplicate", "1.0.0", "2.0.0")
        runtime.kind = "runtime"
        development = self.row("duplicate", "1.0.0", "1.5.0")
        development.kind = "dev"

        self.resolve([runtime, development], client)

        self.assertEqual("2.0.0", runtime.target_default)
        self.assertEqual("2.0.0", development.target_default)

    def test_large_connected_component_uses_bounded_solver(self):
        client = self.make_client()
        rows = []
        size = roadmap.PEER_SOLVER_EXACT_COMPONENT_SIZE + 12
        for index in range(size):
            name = f"chain-{index:02d}"
            next_name = f"chain-{index + 1:02d}"
            target_meta = {"peerDependencies": {next_name: ">=2"}} if index + 1 < size else {}
            self.add_package(client, name, {
                "1.0.0": {},
                "1.5.0": {},
                "2.0.0": target_meta,
                "3.0.0": target_meta,
            })
            rows.append(self.row(name, "1.0.0", "2.0.0"))

        started = __import__("time").perf_counter()
        self.resolve(rows, client)
        elapsed = __import__("time").perf_counter() - started

        self.assertLess(elapsed, 3.0)
        self.assertEqual(["2.0.0"] * size, [row.target_default for row in rows])
        self.assertEqual([], roadmap.validate_final_peer_assignment({"Demo": rows}, client, modes=("default",)))
    def test_candidate_domain_keeps_complete_registry_history_for_correctness(self):
        client = self.make_client()
        versions = {f"1.{minor}.0": {} for minor in range(100)}
        self.add_package(client, "wide", versions)
        row = self.row("wide", "1.0.0", "1.80.0")
        roadmap.capture_desired_targets({"Demo": [row]})

        domain = roadmap._candidate_domain(row, "default", client)

        self.assertEqual(100, len(domain))
        self.assertIn("1.0.0", domain)
        self.assertIn("1.80.0", domain)

    def test_interior_peer_candidate_beyond_old_domain_cap_is_not_lost(self):
        client = self.make_client()
        b_versions = {f"1.{minor}.0": {} for minor in range(70)}
        self.add_package(client, "a", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"b": "=1.50.0"}},
        })
        self.add_package(client, "b", b_versions)
        a = self.row("a", "1.0.0", "2.0.0")
        b = self.row("b", "1.0.0")

        self.resolve([a, b], client)

        self.assertEqual("2.0.0", a.target_default)
        self.assertEqual("1.50.0", b.target_default)

    def test_learned_three_way_nogood_is_retained_in_cohort_topology(self):
        client = self.make_client()
        rows = []
        for name in ("a", "b", "c"):
            self.add_package(client, name, {"1.0.0": {}, "2.0.0": {}})
            rows.append(self.row(name, "1.0.0", "2.0.0"))
        by_project = {"Demo": rows}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)

        roadmap.resolve_peer_compatibility(
            by_project,
            client,
            modes=("default",),
            learned_nogoods_by_project_mode={
                "Demo": {
                    "default": [
                        {"a": "2.0.0", "b": "2.0.0", "c": "1.0.0"},
                    ]
                }
            },
        )

        cohorts = {row.compatibility_cohort for row in rows if row.compatibility_cohort}
        self.assertEqual(1, len(cohorts))
        self.assertTrue(all(row.compatibility_cohort for row in rows))

    def test_release_rollback_stops_after_branch_is_published(self):
        root = Path(roadmap.__file__).parent
        release = (root / "push-branch-and-tag.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$BranchPublished = $true", release)
        self.assertIn("if ($VersionCommitCreated -and -not $BranchPublished)", release)
        self.assertLess(release.index("git push -u $Remote $Branch"), release.index("$BranchPublished = $true"))
        self.assertLess(release.index("$BranchPublished = $true"), release.index("git push $Remote \"refs/tags/$Tag\""))

    def test_baseline_confirmation_describes_a_new_cycle(self):
        flow = (Path(roadmap.__file__).parent / "desktop/src/data/flow.ts").read_text(encoding="utf-8")
        self.assertNotIn("После начала обновления переснимать его нельзя", flow)
        self.assertIn("Baseline начнёт новый цикл планирования", flow)

    def test_dashboard_and_branch_plan_do_not_assume_five_groups(self):
        source = Path(roadmap.__file__).read_text(encoding="utf-8")
        self.assertNotIn("range(1, 6)", source)
        self.assertNotIn("group4Value", source)
        self.assertNotIn("group5Value", source)
        self.assertNotIn("[1,2,3,4,5].includes(group)", source)
        self.assertNotIn('<select id="settingsGroup"><option value="1">', source)
        self.assertIn('id="settingsGroup" type="number"', source)
        self.assertNotIn("row.compatibilityCohort || row.subgroup || `group-${row.group}`", source)
        self.assertNotIn("`package-${slugifyBranchPart(row.name)}`", source)
        self.assertIn("automaticBranchBuckets(projectRows, config.branchBatchSize)", source)
        self.assertIn("stableBranchBucketKey(family, chunk)", source)
        self.assertIn("const limit = Math.max(1, Math.min(8, Number(maxBatchSize) || 4))", source)
        self.assertIn("pinned-package-scope", source)
        migration_source = (Path(roadmap.__file__).parent / "desktop/electron/migration-progress.ts").read_text(encoding="utf-8")
        self.assertNotIn("inferGroupBranchStem", migration_source)
        self.assertIn("branchPrefix?: string", migration_source)
        self.assertIn("inferWorkBranchPrefix", migration_source)


if __name__ == "__main__":
    unittest.main()
