from __future__ import annotations

import unittest
from pathlib import Path

import dependency_live_roadmap_generator as roadmap


class DraftPeerDeferralExportTests(unittest.TestCase):
    """P0 regression: the Draft solver resolves ONLY the "default" variant,
    while the export reads the chosen level (yellow/green). A row the solver
    deferred/fallbacked in "default" must never resurrect its stale pre-solver
    target in the published plan (evidence: react-dom was exported PROPOSED
    @19.0.4 with needsReview=false while the solver proved PEER_RESOLUTION_DEFERRED
    to current 18.3.1)."""

    REGISTRY = "https://nexus.example/repository/npm-group"

    def make_client(self) -> roadmap.LiveDataClient:
        return roadmap.LiveDataClient(self.REGISTRY, timeout=1, batch_size=10, sleep_sec=0)

    def add_package(self, client, name, versions, *, unavailable=()):
        records = {}
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
    def row(name, current, desired=roadmap.NO_ACTION, *, scope_excluded=False):
        return roadmap.DependencyRow(
            project="Demo",
            package_dir=".",
            name=name,
            kind="dev",
            requested_spec="*",
            current_version=current,
            current_source="lockfile",
            latest_version=desired if roadmap.target_is_action(desired) else current,
            current_vulns="0",
            min_no_critical=current,
            min_no_high=current,
            min_no_vuln=current,
            min_lag_12m=current,
            min_lag_9m=current,
            min_lag_6m=current,
            min_lag_3m=current,
            group=5,
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

    def draft_solve(self, rows, client):
        """Mirror the Draft-branch sequence (main, draft_baseline path): solve
        the "default" variant, then mirror the solver resolution to the other
        variant attributes via _draft_mirror_solver_resolution."""
        by_project = {"Demo": rows}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        roadmap.resolve_peer_compatibility(
            by_project,
            client,
            modes=("default",),
            apply_results=True,
            partial_on_incomplete=True,
            run_context={"runId": "test-draft-peer-deferral-export"},
        )
        roadmap._draft_mirror_solver_resolution(by_project, solved_modes=("default",))
        return by_project

    def plan_entry(self, plan, package):
        for proposal in plan.get("proposals") or []:
            for row in proposal.get("rows") or []:
                if row.get("package") == package:
                    return row
        raise AssertionError(f"package {package} not found in plan proposals: {plan}")

    def test_p0_deferred_peer_target_is_never_exported_as_proposed(self):
        client = self.make_client()
        # react stays 18.3.1 (scope-excluded) -- exactly the proven production
        # state from the planning-run artifact (planning-run-484b5a710144.json).
        self.add_package(client, "react", {"18.3.1": {}})
        self.add_package(client, "react-dom", {
            "18.3.1": {"peerDependencies": {"react": "^18.3.1"}},
            "19.0.4": {"peerDependencies": {"react": "^19.0.4"}},
        })
        react = self.row("react", "18.3.1", roadmap.NO_ACTION, scope_excluded=True)
        react_dom = self.row("react-dom", "18.3.1", "19.0.4")

        # Solve "default" WITHOUT the mirror first to prove the bug vector.
        by_project = {"Demo": [react, react_dom]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        roadmap.resolve_peer_compatibility(
            by_project,
            client,
            modes=("default",),
            apply_results=True,
            partial_on_incomplete=True,
            run_context={"runId": "test"},
        )
        # The solver deferred react-dom to current 18.3.1 in the solved variant...
        self.assertEqual(roadmap.NO_ACTION, react_dom.target_default)
        self.assertIn("PEER_RESOLUTION_DEFERRED", react_dom.compatibility_note)
        # ...but the stale chosen-level target survives and _draft_target_for_major
        # would resurrect it as PROPOSED (the P0).
        self.assertEqual("19.0.4", react_dom.target_yellow)
        self.assertEqual("19.0.4", roadmap._draft_target_for_major(react_dom))

        # Now the Draft-branch mirror must kill the resurrection.
        roadmap._draft_mirror_solver_resolution(by_project, solved_modes=("default",))
        self.assertEqual(roadmap.NO_ACTION, react_dom.target_yellow)
        self.assertEqual(roadmap.NO_ACTION, react_dom.target_green)
        self.assertEqual(roadmap.NO_ACTION, roadmap._draft_target_for_major(react_dom))
        self.assertEqual(react_dom.resolution_reason_default, react_dom.resolution_reason_yellow)
        self.assertIn("PEER_RESOLUTION_DEFERRED", react_dom.target_yellow_reason)

        # End-to-end through the plan export.
        health = roadmap.compute_project_health([react, react_dom], "Demo", None)
        plan = roadmap.build_draft_plan({"Demo": [react, react_dom]}, {}, {"Demo": health})
        entry = self.plan_entry(plan, "react-dom")
        self.assertIsNone(entry["target"], entry)
        self.assertNotEqual("proposed", entry["status"], entry)
        self.assertNotEqual("no-change", entry["status"], entry)

    def test_fallback_target_is_mirrored_and_still_exported(self):
        client = self.make_client()
        self.add_package(client, "plugin", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"core": "=2.0.0"}},
            "3.0.0": {"peerDependencies": {"core": "=3.0.0"}},
        })
        self.add_package(client, "core", {"2.0.0": {}})
        plugin = self.row("plugin", "1.0.0", "3.0.0")
        core = self.row("core", "2.0.0", roadmap.NO_ACTION)

        self.draft_solve([plugin, core], client)

        # The solver fell back to the registry-backed 2.0.0; the mirror keeps
        # that version in EVERY variant and it MUST still be exported as an
        # actionable proposal (fallback is not deferral).
        self.assertEqual("2.0.0", plugin.target_default)
        self.assertEqual("2.0.0", plugin.target_yellow)
        self.assertIn("PEER_RESOLUTION_FALLBACK", plugin.compatibility_note)
        self.assertEqual("2.0.0", roadmap._draft_target_for_major(plugin))
        health = roadmap.compute_project_health([plugin, core], "Demo", None)
        plan = roadmap.build_draft_plan({"Demo": [plugin, core]}, {}, {"Demo": health})
        entry = self.plan_entry(plan, "plugin")
        self.assertEqual("2.0.0", entry["target"], entry)
        self.assertEqual("proposed", entry["status"], entry)

    def test_unadjusted_rows_keep_their_chosen_level_targets(self):
        client = self.make_client()
        self.add_package(client, "tool", {"1.0.0": {}, "2.0.0": {}, "3.0.0": {}})
        tool = self.row("tool", "1.0.0", "3.0.0")

        self.draft_solve([tool], client)

        # The solver had nothing to adjust: no resolution reason, no mirroring,
        # chosen-level target and export unchanged.
        self.assertEqual("", tool.resolution_reason_default)
        self.assertEqual("3.0.0", tool.target_yellow)
        self.assertEqual("3.0.0", roadmap._draft_target_for_major(tool))
        health = roadmap.compute_project_health([tool], "Demo", None)
        plan = roadmap.build_draft_plan({"Demo": [tool]}, {}, {"Demo": health})
        entry = self.plan_entry(plan, "tool")
        self.assertEqual("3.0.0", entry["target"], entry)
        self.assertEqual("proposed", entry["status"], entry)

    def test_mirror_is_noop_when_variant_already_matches_solver(self):
        client = self.make_client()
        self.add_package(client, "plugin", {
            "1.0.0": {},
            "2.0.0": {"peerDependencies": {"core": "=2.0.0"}},
            "3.0.0": {"peerDependencies": {"core": "=3.0.0"}},
        })
        self.add_package(client, "core", {"2.0.0": {}})
        plugin = self.row("plugin", "1.0.0", "3.0.0")
        core = self.row("core", "2.0.0", roadmap.NO_ACTION)

        by_project = {"Demo": [plugin, core]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        roadmap.resolve_peer_compatibility(
            by_project, client, modes=("default",), apply_results=True,
            partial_on_incomplete=True, run_context={"runId": "test-noop"},
        )
        # The yellow AND green variants ALREADY carry the solver-resolved
        # assignment (a consistent baseline -- e.g. re-run after a prior fix).
        plugin.target_yellow = plugin.target_default
        plugin.target_green = plugin.target_default
        plugin.target_yellow_reason = plugin.target_default_reason
        plugin.target_green_reason = plugin.target_default_reason
        before = plugin.target_default, plugin.target_yellow, plugin.target_green

        roadmap._draft_mirror_solver_resolution(by_project, solved_modes=("default",))

        # Idempotence: mirroring an already-consistent row changes nothing and
        # never invents a target that contradicts the solved assignment.
        self.assertEqual(before, (plugin.target_default, plugin.target_yellow, plugin.target_green))
        self.assertEqual(plugin.target_default, roadmap._draft_target_for_major(plugin))
        self.assertIn("PEER_RESOLUTION_FALLBACK", plugin.compatibility_note)

    def test_mirror_is_wired_into_the_draft_branch_after_the_solver(self):
        source = Path(roadmap.__file__).read_text(encoding="utf-8")
        assert "def _draft_mirror_solver_resolution" in source
        lines = source.splitlines()
        def_line = next(i for i, line in enumerate(lines) if "def _draft_mirror_solver_resolution" in line)
        solve_line = next(i for i, line in enumerate(lines) if "partial_on_incomplete=True" in line)
        call_line = next(i for i, line in enumerate(lines) if "_draft_mirror_solver_resolution(" in line and "def " not in line)
        # The mirror must be defined before the draft branch and called AFTER
        # the Draft's own resolve_peer_compatibility call.
        self.assertLess(def_line, call_line)
        self.assertGreater(call_line, solve_line)
        self.assertIn("solved_modes=(\"default\",)", "\n".join(lines[call_line:call_line + 3]))


if __name__ == "__main__":
    unittest.main()
