from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import dependency_live_roadmap_generator as roadmap


class DependencyRoadmapTests(unittest.TestCase):
    def test_cli_exclusions_require_reason_and_support_scoped_forms(self) -> None:
        parsed = roadmap.parse_cli_exclusions([
            "oidc-client|backend migration is blocked",
            "Demo.App|react|separate React 19 initiative",
            "Demo.App|dev|eslint|shared config is not ready",
        ])
        self.assertTrue(parsed["oidc-client"]["excluded"])
        self.assertEqual(
            "separate React 19 initiative",
            parsed["Demo.App:react"]["exclusionReason"],
        )
        self.assertEqual("cli", parsed["Demo.App:dev:eslint"]["exclusionSource"])
        with self.assertRaisesRegex(ValueError, "requires a reason"):
            roadmap.parse_cli_exclusions(["oidc-client|"])

    def test_excluded_dependency_is_visible_but_not_counted_in_health_or_plan(self) -> None:
        excluded = self.make_row(
            name="@company/icons",
            current_version="1.13.1",
            current_vulns="C:2, H:3, M:4, L:5",
            min_lag_3m="2.0.9",
            target_default="2.0.9",
            target_yellow="2.0.9",
            target_green="2.0.9",
            scope_excluded=True,
            exclusion_reason="blocked by product migration",
            exclusion_source="dashboard-state",
        )
        active = self.make_row(
            name="foo",
            current_version="2.0.0",
            latest_version="2.0.0",
            current_vulns="0",
            min_lag_12m="2.0.0",
            min_lag_9m="2.0.0",
            min_lag_6m="2.0.0",
            min_lag_3m="2.0.0",
            target_default=roadmap.NO_ACTION,
            target_yellow=roadmap.NO_ACTION,
            target_green=roadmap.NO_ACTION,
        )
        health = roadmap.compute_project_health([excluded, active], "Demo")
        self.assertEqual(1, health.excluded)
        self.assertEqual(1, health.total)
        self.assertEqual(1, health.lag_ok_12m)
        self.assertEqual("green", health.status)
        self.assertEqual(0, health.critical)
        self.assertEqual(0, health.high)
        self.assertEqual(0, health.moderate)
        self.assertEqual(0, health.low)
        self.assertFalse(roadmap.dependency_needs_lag_update(excluded))
        self.assertFalse(roadmap.dependency_is_lag_ok_after_planned_target(excluded, "yellow"))

    def test_yellow_projection_does_not_count_excluded_dependency_as_success(self) -> None:
        fresh = [
            self.make_row(
                name=f"fresh-{index}",
                current_version="2.0.0",
                latest_version="2.0.0",
                current_vulns="0",
                min_lag_12m="2.0.0",
                min_lag_9m="2.0.0",
                min_lag_6m="2.0.0",
                min_lag_3m="2.0.0",
                lag_threshold_months=12,
                group=5,
            )
            for index in range(7)
        ]
        stale = [
            self.make_row(
                name=f"stale-{index}",
                current_version="1.0.0",
                latest_version="2.0.0",
                current_vulns="0",
                min_lag_12m="2.0.0",
                min_lag_9m="2.0.0",
                min_lag_6m="2.0.0",
                min_lag_3m="2.0.0",
                lag_threshold_months=12,
                group=5,
            )
            for index in range(2)
        ]
        excluded = self.make_row(
            name="excluded",
            current_version="1.0.0",
            latest_version="2.0.0",
            current_vulns="C:1, H:1",
            min_lag_12m="2.0.0",
            min_lag_9m="2.0.0",
            min_lag_6m="2.0.0",
            min_lag_3m="2.0.0",
            lag_threshold_months=12,
            group=5,
            scope_excluded=True,
            exclusion_reason="handled outside this migration",
            exclusion_source="dashboard-state",
        )
        rows = [*fresh, *stale, excluded]

        health = roadmap.enrich_project_targets({"Demo": rows})["Demo"]
        # Candidate selection is intentionally complete before compatibility.
        self.assertEqual(2, sum(1 for row in rows if roadmap.target_is_action(row.target_yellow)))
        client = roadmap.LiveDataClient("https://nexus.example/repository/npm-group", timeout=1, batch_size=10, sleep_sec=0)
        roadmap.minimize_yellow_plan_after_compatibility({"Demo": rows}, client, {"Demo": health})

        self.assertEqual(9, health.total)
        self.assertEqual(7, health.lag_ok_12m)
        self.assertAlmostEqual(77.8, health.lag_ok_pct, places=1)
        yellow_actions = [row for row in rows if roadmap.target_is_action(row.target_yellow)]
        self.assertEqual(1, len(yellow_actions))
        self.assertIn(yellow_actions[0].name, {"stale-0", "stale-1"})
        self.assertEqual(roadmap.NO_ACTION, excluded.target_yellow)

    def test_planned_update_target_always_reaches_the_live_compliance_boundary(self) -> None:
        # Compliance is judged against the live registry window while the
        # remediation target stays anchored to the captured baseline. Once a
        # baseline is old enough the live boundary overtakes the frozen
        # buffered one, and planning the baseline value would tell the agent
        # to install a version that still fails the policy: the work lands,
        # the percentage does not move, and the goal looks stuck for no
        # visible reason.
        stale_baseline = self.make_row(
            name="stale-baseline", current_version="1.0.0", latest_version="4.0.0", current_vulns="0",
            min_lag_12m="3.0.0", min_lag_9m="3.5.0", min_lag_6m="3.5.0", min_lag_3m="4.0.0",
            planning_min_lag_12m="1.5.0", planning_min_lag_9m="2.0.0",
            planning_min_lag_6m="2.0.0", planning_min_lag_3m="2.5.0",
            lag_threshold_months=12, group=5,
        )
        compliance = roadmap.lag_compliance_target_for_row(stale_baseline)
        planned = roadmap.lag_update_target_for_row(stale_baseline)
        self.assertEqual("3.0.0", compliance)
        self.assertTrue(
            roadmap.current_meets_target(planned, compliance),
            f"planned target {planned} must satisfy the live boundary {compliance}",
        )

        # A fresh baseline keeps the buffered target: the +3 month buffer is
        # the whole point of anchoring remediation, and must not be lost.
        fresh_baseline = self.make_row(
            name="fresh-baseline", current_version="1.0.0", latest_version="4.0.0", current_vulns="0",
            min_lag_12m="2.0.0", min_lag_9m="2.5.0", min_lag_6m="3.0.0", min_lag_3m="3.5.0",
            planning_min_lag_12m="2.0.0", planning_min_lag_9m="2.5.0",
            planning_min_lag_6m="3.0.0", planning_min_lag_3m="3.5.0",
            lag_threshold_months=12, group=5,
        )
        self.assertEqual("2.5.0", roadmap.lag_update_target_for_row(fresh_baseline))

    def test_live_compliance_does_not_treat_old_baseline_boundary_as_current_failure(self) -> None:
        fresh = [
            self.make_row(
                name=f"fresh-live-{index}", current_version="2.0.0", current_vulns="0",
                min_lag_12m="2.0.0", min_lag_9m="2.0.0", min_lag_6m="2.0.0", min_lag_3m="2.0.0",
                lag_threshold_months=12, group=5,
            )
            for index in range(6)
        ]
        baseline_drift_but_live_ok = self.make_row(
            name="storybook-live-ok", current_version="1.0.0", current_vulns="0",
            min_lag_12m="1.0.0", min_lag_9m="1.0.0", min_lag_6m="1.0.0", min_lag_3m="1.0.0",
            planning_min_lag_12m="2.0.0", planning_min_lag_9m="3.0.0",
            planning_min_lag_6m="3.0.0", planning_min_lag_3m="3.0.0",
            lag_threshold_months=12, group=5,
        )
        stale = [
            self.make_row(
                name=f"stale-live-{index}", current_version="1.0.0", current_vulns="0",
                min_lag_12m="2.0.0", min_lag_9m="2.5.0", min_lag_6m="3.0.0", min_lag_3m="4.0.0",
                planning_min_lag_12m="2.0.0", planning_min_lag_9m="2.5.0",
                planning_min_lag_6m="3.0.0", planning_min_lag_3m="4.0.0",
                lag_threshold_months=12, group=5,
            )
            for index in range(3)
        ]
        rows = [*fresh, baseline_drift_but_live_ok, *stale]

        health = roadmap.enrich_project_targets({"Demo": rows})["Demo"]
        client = roadmap.LiveDataClient("https://nexus.example/repository/npm-group", timeout=1, batch_size=10, sleep_sec=0)
        roadmap.minimize_yellow_plan_after_compatibility({"Demo": rows}, client, {"Demo": health})

        self.assertEqual(10, health.total)
        self.assertEqual(7, health.lag_ok_12m)
        self.assertAlmostEqual(70.0, health.lag_ok_pct, places=1)
        self.assertTrue(roadmap.dependency_is_lag_ok(baseline_drift_but_live_ok))
        self.assertEqual(roadmap.NO_ACTION, baseline_drift_but_live_ok.target_yellow)
        yellow = [row for row in stale if roadmap.target_is_action(row.target_yellow)]
        self.assertEqual(2, len(yellow))
        self.assertTrue(all(row.target_yellow == "2.5.0" for row in yellow))

    def test_green_plan_excludes_excluded_dependency_from_every_criterion(self) -> None:
        fresh = [
            self.make_row(
                name=f"fresh-green-{index}",
                current_version="2.0.0",
                latest_version="2.0.0",
                current_vulns="0",
                min_lag_12m="2.0.0",
                min_lag_9m="2.0.0",
                min_lag_6m="2.0.0",
                min_lag_3m="2.0.0",
                lag_threshold_months=12,
                group=5,
            )
            for index in range(7)
        ]
        stale = [
            self.make_row(
                name=f"stale-green-{index}",
                current_version="1.0.0",
                latest_version="2.0.0",
                current_vulns="0",
                min_lag_12m="2.0.0",
                min_lag_9m="2.0.0",
                min_lag_6m="2.0.0",
                min_lag_3m="2.0.0",
                lag_threshold_months=12,
                group=5,
            )
            for index in range(2)
        ]
        excluded = self.make_row(
            name="excluded-green",
            current_version="1.0.0",
            latest_version="2.0.0",
            current_vulns="C:5, H:5, M:50, L:50",
            min_lag_12m="2.0.0",
            min_lag_9m="2.0.0",
            min_lag_6m="2.0.0",
            min_lag_3m="2.0.0",
            min_no_critical="2.0.0",
            min_no_high="2.0.0",
            min_no_vuln="2.0.0",
            lag_threshold_months=12,
            group=5,
            scope_excluded=True,
            exclusion_reason="handled outside this migration",
            exclusion_source="dashboard-state",
        )
        rows = [*fresh, *stale, excluded]

        health = roadmap.enrich_project_targets({"Demo": rows})["Demo"]

        self.assertEqual(9, health.total)
        self.assertEqual(0, health.critical)
        self.assertEqual(0, health.high)
        self.assertEqual(0, health.moderate)
        self.assertEqual(0, health.low)
        green_actions = [row for row in rows if roadmap.target_is_action(row.target_green)]
        self.assertEqual({"stale-green-0", "stale-green-1"}, {row.name for row in green_actions})
        self.assertEqual(roadmap.NO_ACTION, excluded.target_green)

    def test_compare_semver_returns_none_instead_of_equal_for_unparseable_input(self) -> None:
        self.assertEqual(roadmap.compare_semver("2.0.0", "1.0.0"), 1)
        self.assertEqual(roadmap.compare_semver("1.0.0", "1.0.0"), 0)
        self.assertIsNone(roadmap.compare_semver("2.0.0", "workspace:*"))
        self.assertIsNone(roadmap.compare_semver("not-a-version", "1.0.0"))

    def test_unknown_lag_target_is_not_counted_as_failed_lag_policy(self) -> None:
        known_ok_rows = [
            self.make_row(
                name=f"fresh-{index}",
                current_version="2.0.0",
                latest_version="2.0.0",
                current_vulns="0",
                min_lag_12m="2.0.0",
                min_lag_9m="2.0.0",
                min_lag_6m="2.0.0",
                min_lag_3m="2.0.0",
                lag_threshold_months=12,
            )
            for index in range(4)
        ]
        unknown_rows = [
            self.make_row(
                name=f"unknown-{index}",
                current_version="1.0.0",
                latest_version="registry unavailable",
                current_vulns="0",
                min_lag_12m="unknown",
                min_lag_9m="unknown",
                min_lag_6m="unknown",
                min_lag_3m="unknown",
                lag_threshold_months=12,
            )
            for index in range(2)
        ]

        health = roadmap.enrich_project_targets({"Demo": [*known_ok_rows, *unknown_rows]})["Demo"]

        self.assertEqual("yellow", health.status)
        self.assertEqual(4, health.total)
        self.assertEqual(4, health.lag_ok_12m)
        self.assertEqual(100.0, health.lag_ok_pct)
        self.assertEqual(2, health.lag_unknown)
        self.assertEqual(
            [],
            [row.name for row in known_ok_rows + unknown_rows if roadmap.target_is_action(row.target_yellow)],
        )

    def test_dependency_knowledge_is_revisioned_and_superseded_entries_are_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "knowledge.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "type": "dependency-roadmap-knowledge-log",
                "entries": [
                    {
                        "id": "sonar-old", "recordedAt": "2026-07-13", "packages": ["eslint-plugin-sonarjs"],
                        "title": "old", "symptom": "lint fails", "cause": "unknown", "guidance": "guess",
                        "verification": ["run lint"],
                    },
                    {
                        "id": "sonar-legacy", "recordedAt": "2026-07-14", "packages": ["eslint-plugin-sonarjs"],
                        "title": "legacy preset", "symptom": "name rejected", "cause": "flat preset",
                        "guidance": "use recommended-legacy", "verification": ["run lint"], "supersedes": ["sonar-old"],
                    },
                ],
            }), encoding="utf-8")
            entries = roadmap.load_dependency_knowledge(path)
            self.assertEqual(["sonar-legacy"], [entry["id"] for entry in entries])

    def test_project_scoped_overrides_are_loaded(self) -> None:
        overrides = roadmap.normalize_override_document({
            "packages": {"global-pkg": {"group": 2}},
            "projects": {"Demo.App": {"packages": {"react-router-dom": {"group": 4, "lagMonths": 6}}}},
        })
        self.assertEqual(2, overrides["global-pkg"]["group"])
        self.assertEqual(4, overrides["Demo.App:react-router-dom"]["group"])
        self.assertEqual(6, overrides["Demo.App:react-router-dom"]["lagMonths"])

    def test_subgroup_expands_to_package_overrides(self) -> None:
        overrides = roadmap.normalize_override_document({
            "subgroups": {
                "team-ui-maintenance": {
                    "packages": ["@company/react-ui", "@company/icons"],
                    "group": 4,
                    "lagMonths": 3,
                }
            }
        })
        self.assertEqual("team-ui-maintenance", overrides["@company/icons"]["subgroup"])
        self.assertEqual(3, overrides["@company/react-ui"]["lagMonths"])

    def test_generic_global_subgroup_is_not_silently_ignored(self) -> None:
        overrides = roadmap.normalize_override_document({
            "subgroups": {
                "ui-maintenance": {
                    "packages": ["react", "react-dom"],
                    "group": 4,
                    "lagMonths": 3,
                    "reason": "example UI maintenance policy",
                }
            }
        })
        self.assertEqual(4, overrides["react"]["group"])
        self.assertEqual(3, overrides["react-dom"]["lagMonths"])

    def test_shipped_override_example_has_no_team_specific_subgroups(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        example = json.loads(
            (tool_root / "templates" / "groups.override.example.json").read_text(encoding="utf-8")
        )
        self.assertFalse(example.get("subgroups"))

    def test_kind_specific_override_wins_and_name_override_is_fallback(self) -> None:
        profile = roadmap.ProjectProfile(name="Demo.App", path="/tmp/Demo.App")
        overrides = {
            "Demo.App:runtime:react": {"group": 4},
            "Demo.App:react": {"group": 3},
        }
        self.assertEqual(4, roadmap.override_for_package(overrides, profile, "react", "runtime")["group"])
        self.assertEqual(3, roadmap.override_for_package(overrides, profile, "react", "dev")["group"])

    def test_section_specific_override_is_supported(self) -> None:
        profile = roadmap.ProjectProfile(name="Demo", path="/tmp/Demo")
        overrides = roadmap.normalize_override_document({
            "projects": {
                "Demo": {
                    "packages": {
                        "dependencies:react": {"group": 1},
                        "peerDependencies:react": {"group": 4},
                    }
                }
            }
        })
        self.assertEqual(1, roadmap.override_for_package(overrides, profile, "react", "runtime")["group"])
        self.assertEqual(4, roadmap.override_for_package(overrides, profile, "react", "peer")["group"])

    def test_dashboard_note_is_reflected_in_generated_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            (project_dir / "package.json").write_text(json.dumps({
                "name": "demo",
                "dependencies": {"foo": "file:vendor/foo"},
            }), encoding="utf-8")
            overrides = {
                "Demo:runtime:foo": {
                    "group": 2, "subgroup": "custom", "lagMonths": 3, "note": "обновлять вместе с UI",
                }
            }
            rows = roadmap.analyze_project(
                roadmap.ProjectSpec("Demo", project_dir),
                object(),
                overrides,
                include_prerelease=False,
                max_candidates=0,
            )
            self.assertEqual(1, len(rows))
            self.assertIn("комментарий команды: обновлять вместе с UI", rows[0].notes)

    def test_collect_direct_dependencies_includes_optional_peer_and_duplicates(self) -> None:
        rows = roadmap.collect_direct_dependencies({
            "dependencies": {"react": "18.2.0"},
            "devDependencies": {"react": "18.2.0", "vitest": "1.0.0"},
            "optionalDependencies": {"fsevents": "2.3.3"},
            "peerDependencies": {"react": ">=18.0.0"},
        })
        self.assertIn(("react", "runtime", "18.2.0"), rows)
        self.assertIn(("react", "dev", "18.2.0"), rows)
        self.assertIn(("react", "peer", ">=18.0.0"), rows)
        self.assertIn(("fsevents", "optional", "2.3.3"), rows)
        self.assertEqual(5, len(rows))

    def test_action_target_must_be_semver(self) -> None:
        self.assertTrue(roadmap.target_is_action("19.1.0"))
        self.assertTrue(roadmap.target_is_action("^19.1.0"))
        self.assertFalse(roadmap.target_is_action("нет safe target из-за peer dependency"))
        self.assertFalse(roadmap.target_is_action("—"))

    def test_lag_policy_uses_three_month_target(self) -> None:
        row = self.make_row(lag_threshold_months=3)
        self.assertEqual("4.0.0", roadmap.lag_target_for_row(row))
        self.assertEqual("4.0.0", roadmap.lag_update_target_for_row(row))
        self.assertTrue(roadmap.dependency_needs_lag_update(row))

    def test_baseline_applies_buffer_once_and_freezes_lag_targets(self) -> None:
        captured = self.make_row(
            current_version="1.0.0",
            lag_threshold_months=12,
            min_lag_12m="1.1.0",
            min_lag_9m="1.5.0",
            min_lag_6m="2.0.0",
            min_lag_3m="3.0.0",
            current_vulns="0",
        )
        roadmap.enrich_project_targets({"Demo": [captured]})
        captured_payload = roadmap.row_json(captured)
        # Simulate the baseline already created by the previous kit version.
        for field in (
            "planning_min_lag_12m",
            "planning_min_lag_9m",
            "planning_min_lag_6m",
            "planning_min_lag_3m",
            "lag_planning_source",
        ):
            captured_payload.pop(field, None)
        baseline = {"schemaVersion": 1, "rows": [captured_payload]}

        repeated = self.make_row(
            current_version="1.0.0",
            lag_threshold_months=12,
            # Live registry metadata moved after baseline capture.
            min_lag_12m="1.2.0",
            min_lag_9m="1.6.0",
            min_lag_6m="2.1.0",
            min_lag_3m="3.1.0",
            current_vulns="0",
        )
        roadmap.enrich_project_targets({"Demo": [repeated]}, {"Demo": baseline})

        self.assertEqual("baseline", repeated.lag_planning_source)
        self.assertEqual("1.1.0", roadmap.lag_target_for_row(repeated, 12))
        self.assertEqual("1.5.0", roadmap.lag_update_target_for_row(repeated, 12))
        self.assertEqual("1.5.0", repeated.target_green)

        # Calling planning again must be idempotent: it reads the same baseline
        # 9m target and never treats 1.5.0 as a new boundary to buffer again.
        roadmap.enrich_project_targets({"Demo": [repeated]}, {"Demo": baseline})
        self.assertEqual("1.5.0", roadmap.lag_update_target_for_row(repeated, 12))
        self.assertEqual("1.5.0", repeated.target_green)

    def test_dependency_updated_to_buffered_baseline_target_stays_fresh_next_day(self) -> None:
        baseline_row = self.make_row(
            current_version="1.0.0",
            lag_threshold_months=12,
            min_lag_12m="1.1.0",
            min_lag_9m="1.5.0",
            current_vulns="0",
        )
        roadmap.enrich_project_targets({"Demo": [baseline_row]})
        baseline = {"rows": [roadmap.row_json(baseline_row)]}

        next_day = self.make_row(
            current_version="1.5.0",
            lag_threshold_months=12,
            min_lag_12m="1.2.0",
            min_lag_9m="1.6.0",
            current_vulns="0",
        )
        health = roadmap.enrich_project_targets(
            {"Demo": [next_day]},
            {"Demo": baseline},
        )["Demo"]

        self.assertTrue(roadmap.dependency_is_lag_ok(next_day))
        self.assertEqual("green", health.status)
        self.assertEqual(roadmap.NO_ACTION, next_day.target_default)

    def test_release_analysis_is_honest_and_extracts_evidence(self) -> None:
        text = """
        ## 2.0.0
        BREAKING CHANGE: removed legacy createClient API.
        Migration: rename oldOption to newOption.
        Requires Node 20 and new peer dependencies.
        oldOption is deprecated.
        """
        intel = roadmap.analyze_release_text(text, "1.0.0", "2.0.0", source_count=1, coverage_note="release 2.0.0")
        self.assertEqual("breaking-confirmed", intel.status)
        self.assertTrue(intel.breaking_changes)
        self.assertTrue(intel.migration_notes)
        self.assertTrue(intel.deprecations)
        self.assertTrue(intel.requirements)
        self.assertEqual("release 2.0.0", intel.coverage)

        unavailable = roadmap.analyze_release_text("", "1.0.0", "1.1.0", source_count=0)
        self.assertEqual("unavailable", unavailable.status)

    def test_changelog_range_keeps_nested_breaking_and_migration_sections(self) -> None:
        text = """# Changelog

## 3.0.0
### Breaking
Removed old API.
### Migration
Rename oldOption to newOption.

## 2.0.0
Patch only.

## 1.0.0
Initial.
"""
        relevant, versions, fallback = roadmap.extract_relevant_markdown_details(text, "2.0.0", "3.0.0")
        self.assertFalse(fallback)
        self.assertEqual(["3.0.0"], versions)
        self.assertIn("### Breaking", relevant)
        self.assertIn("Removed old API", relevant)
        self.assertIn("### Migration", relevant)
        self.assertNotIn("## 2.0.0", relevant)

    def test_negated_breaking_change_text_is_not_confirmed(self) -> None:
        intel = roadmap.analyze_release_text(
            "No breaking changes in this release.\nThis is a non-breaking refactor.",
            "1.0.0", "1.1.0", source_count=1, coverage_note="1/1", coverage_complete=True,
        )
        self.assertEqual("no-breaking-found", intel.status)
        self.assertFalse(intel.breaking_changes)

    def test_extracts_changelog_from_npm_tarball(self) -> None:
        buffer = io.BytesIO()
        content = b"# Changelog\n\n## 2.0.0\nBREAKING: removed old API.\n"
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("package/CHANGELOG.md")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        text, names = roadmap.extract_release_docs_from_tarball(buffer.getvalue(), "1.0.0", "2.0.0")
        self.assertIn("BREAKING", text)
        self.assertEqual(["package/CHANGELOG.md"], names)

    @staticmethod
    def make_npm_tarball(package_json: dict, extra_files: dict[str, bytes] | None = None) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            content = json.dumps(package_json).encode("utf-8")
            info = tarfile.TarInfo("package/package.json")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
            for name, data in (extra_files or {}).items():
                member = tarfile.TarInfo(f"package/{name}")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return buffer.getvalue()

    def test_declared_types_missing_from_tarball_is_flagged(self) -> None:
        # Reproduces the real failure that blocked a live migration: an
        # internal registry mirror repacked date-fns@4.2.0 with 2887 files
        # but only one stray .d.ts under docs/, missing every declared entry
        # point. npm/yarn install succeeds -- the tarball is reachable and
        # non-empty -- so the cheap reachability probe cannot see this; only
        # actually reading the tarball's own package.json against its
        # contents catches it.
        data = self.make_npm_tarball({"name": "date-fns", "version": "4.2.0", "types": "./date-fns.d.ts"})
        reason = roadmap.registry_tarball_missing_declared_types(data)
        self.assertIsNotNone(reason)
        self.assertIn("date-fns.d.ts", reason)

    def test_missing_runtime_main_entrypoint_is_flagged_before_executor(self) -> None:
        data = self.make_npm_tarball(
            {"name": "eslint-plugin-css-modules", "version": "2.11.2", "main": "build/index.js"},
            {"packages/gonzales-primitives.js": b"module.exports = {};\n"},
        )
        reason = roadmap.registry_tarball_missing_declared_runtime_entrypoint(data)
        self.assertIsNotNone(reason)
        self.assertIn("build/index.js", reason)

    def test_present_runtime_main_entrypoint_passes(self) -> None:
        data = self.make_npm_tarball(
            {"name": "demo-plugin", "version": "1.0.0", "main": "build/index"},
            {"build/index.js": b"module.exports = {};\n"},
        )
        self.assertIsNone(roadmap.registry_tarball_missing_declared_runtime_entrypoint(data))

    def test_runtime_tarball_self_types_requires_declared_file_to_exist(self) -> None:
        good = self.make_npm_tarball(
            {"name": "uuid", "version": "11.1.1", "types": "./dist/index.d.ts"},
            {"dist/index.d.ts": b"export declare const v4: () => string;\n"},
        )
        bad = self.make_npm_tarball(
            {"name": "uuid", "version": "11.1.1", "types": "./dist/index.d.ts"},
        )
        self.assertTrue(roadmap.registry_tarball_provides_own_types(good))
        self.assertFalse(roadmap.registry_tarball_provides_own_types(bad))

    def test_declared_types_present_in_tarball_pass(self) -> None:
        data = self.make_npm_tarball(
            {"name": "demo-pkg", "version": "1.0.0", "types": "./index.d.ts"},
            {"index.d.ts": b"export {};\n"},
        )
        self.assertIsNone(roadmap.registry_tarball_missing_declared_types(data))

    def test_no_declared_types_is_not_flagged(self) -> None:
        # A package that never promised TypeScript declarations (plain JS, or
        # types shipped separately via @types/*) must not be penalized for
        # lacking something it never claimed.
        data = self.make_npm_tarball({"name": "demo-pkg", "version": "1.0.0"})
        self.assertIsNone(roadmap.registry_tarball_missing_declared_types(data))

    def test_exports_types_condition_is_also_checked(self) -> None:
        data = self.make_npm_tarball({
            "name": "demo-pkg", "version": "1.0.0",
            "exports": {".": {"types": "./dist/index.d.ts", "default": "./dist/index.js"}},
        })
        reason = roadmap.registry_tarball_missing_declared_types(data)
        self.assertIsNotNone(reason)
        self.assertIn("dist/index.d.ts", reason)

    def test_nested_conditional_exports_types_is_checked(self) -> None:
        # The actual shape that slipped past the first version of this check:
        # date-fns@4.2.0 is a dual ESM/CJS package, so `types` is nested one
        # level deeper under `import`/`require`, not a direct key of
        # exports["."]. A flat `.get("types")` finds nothing and silently
        # treats the package as making no declaration promise at all.
        data = self.make_npm_tarball({
            "name": "date-fns", "version": "4.2.0",
            "exports": {".": {
                "import": {"types": "./index.d.ts", "default": "./index.js"},
                "require": {"types": "./index.d.cts", "default": "./index.cjs"},
            }},
        })
        reason = roadmap.registry_tarball_missing_declared_types(data)
        self.assertIsNotNone(reason)
        self.assertIn("index.d.ts", reason)
        self.assertIn("index.d.cts", reason)

    def test_nested_conditional_exports_types_present_passes(self) -> None:
        data = self.make_npm_tarball(
            {
                "name": "date-fns", "version": "4.2.0",
                "exports": {".": {
                    "import": {"types": "./index.d.ts", "default": "./index.js"},
                    "require": {"types": "./index.d.cts", "default": "./index.cjs"},
                }},
            },
            {"index.d.ts": b"export {};\n", "index.d.cts": b"export {};\n"},
        )
        self.assertIsNone(roadmap.registry_tarball_missing_declared_types(data))

    def test_cli_only_project_generates_exactly_one_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("Alpha", "Beta"):
                project = root / name
                (project / "vendor").mkdir(parents=True)
                (project / "package.json").write_text(json.dumps({
                    "name": name.lower(),
                    "packageManager": "yarn@1.22.22",
                    "dependencies": {"local-demo": "file:vendor"},
                }), encoding="utf-8")
                (project / "yarn.lock").write_text(
                    'local-demo@file:vendor:\n  version "1.0.0"\n',
                    encoding="utf-8",
                )
                (project / "vendor" / "package.json").write_text(json.dumps({
                    "name": "local-demo", "version": "1.0.0",
                }), encoding="utf-8")
            settings = root / "settings.project.json"
            settings.write_text(json.dumps({
                "root": str(root),
                "projects": [{"name": "Alpha", "path": "Alpha"}, {"name": "Beta", "path": "Beta"}],
                "out": "artifacts/report.md",
                "jsonOut": "artifacts/report.json",
                "htmlOut": "artifacts/report.html",
                "historyDir": "history",
                "dashboardState": "state.json",
                "releaseIntelEnabled": False,
            }), encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(Path(roadmap.__file__)),
                "--project-settings", str(settings),
                "--only-project", "Beta",
                "--skip-release-intel",
                "--no-history-snapshot",
            ], cwd=root, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            report = json.loads((root / "artifacts" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(["Beta"], list(report["projects"]))

    def test_release_intelligence_is_collected_for_each_distinct_target_mode(self) -> None:
        row = self.make_row(
            target_default="2.0.0", target_yellow="2.0.0", target_green="3.0.0",
            target_default_reason="default", target_yellow_reason="yellow", target_green_reason="green",
        )

        class Client:
            def __init__(self): self.targets = []
            def fetch_npm_metadata(self, _name): return {"versions": {"2.0.0": {}, "3.0.0": {}}}
            def fetch_release_intelligence(self, _name, _meta, _current, target):
                self.targets.append(target)
                return roadmap.ReleaseIntelligence(
                    target=target, status="breaking-confirmed" if target == "3.0.0" else "no-breaking-found",
                    summary=f"analysis for {target}", coverage=f"coverage {target}",
                )

        client = Client()
        roadmap.enrich_release_intelligence({"Demo": [row]}, client)
        self.assertEqual(["2.0.0", "3.0.0"], client.targets)
        self.assertEqual({"2.0.0", "3.0.0"}, set(row.release_by_target))
        self.assertEqual("no-breaking-found", row.release_by_target["2.0.0"].status)
        self.assertEqual("breaking-confirmed", row.release_by_target["3.0.0"].status)
        self.assertEqual("3.0.0", row.release_target)

    def test_stable_release_coverage_ignores_prerelease_versions(self) -> None:
        class Client:
            registry = "https://registry.example"
            def fetch_json_url(self, _url): return None
            def fetch_text(self, _url): return None
            def fetch_bytes(self, _url): return None

        meta = {
            "versions": {
                "1.1.0-beta.1": {},
                "1.1.0": {},
            }
        }
        intel = roadmap.build_release_intelligence(Client(), "demo", meta, "1.0.0", "1.1.0")
        self.assertIn("версии покрыты: 0/1", intel.coverage)
        self.assertNotIn("0/2", intel.coverage)

    def test_html_contains_combined_lag_release_modal_state_and_prompt_contract(self) -> None:
        row = self.make_row(
            release_status="breaking-confirmed",
            release_summary="breaking found",
            release_coverage="tarball changelog",
            breaking_changes=["removed API"],
            release_requirements=["Requires Node 20"],
            release_by_target={
                "2.0.0": roadmap.ReleaseIntelligence(target="2.0.0", status="no-breaking-found", summary="safe 2", coverage="2/2"),
                "4.0.0": roadmap.ReleaseIntelligence(target="4.0.0", status="breaking-confirmed", summary="breaking found", coverage="4/4", breaking_changes=["removed API"], requirements=["Requires Node 20"]),
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps({
                "schemaVersion": 1,
                "packageOverrides": {"Demo": {"runtime:demo-pkg": {"group": 3, "subgroup": "saved", "note": "</script><script>alert(1)</script>"}}},
            }), encoding="utf-8")
            roadmap.write_html(
                {"Demo": [row]}, out,
                history_dir=Path(tmp) / "history",
                project_specs={"Demo": roadmap.ProjectSpec("Demo", Path(tmp) / "demo", source_branch="main")},
                dashboard_state_path=state_path,
                roadmap_json_path=Path(tmp) / "roadmap.json",
                history_snapshots=[],
            )
            html = out.read_text(encoding="utf-8")
            self.assertIn("≤3м", html)
            self.assertIn("releaseModal", html)
            self.assertIn("Requirements / compatibility", html)
            self.assertIn("Автономный Git-цикл", html)
            self.assertIn("scope-manifest", html)
            self.assertIn("scopeManifestRowFor", html)
            self.assertIn("function rowNeedsAction(row)", html)
            self.assertIn("const actionRows = rows.filter(r => rowNeedsAction(r));", html)
            self.assertIn("shouldUpdate: rowNeedsAction(r)", html)
            self.assertIn("focused-check-required", html)
            self.assertIn("docs/dependency-update-review-notes.md", html)
            self.assertIn("docs/dependency-upgrades.md", html)
            self.assertIn("docs/dependency-update-summary.md", html)
            self.assertIn("критичные баги/уязвимости", html)
            self.assertIn("CHANGE_RATIONALE_REQUIRED", html)
            self.assertNotIn("--execute-regression", html)
            self.assertIn("historyBeforeSelect", html)
            self.assertIn("rowStateKey", html)
            self.assertIn('"subgroup": "saved"', html)
            self.assertIn("REPORT_CONTEXT.dashboardState", html)
            self.assertIn("recalculateDashboardHealth", html)
            self.assertIn("liveProjectHealth", html)
            self.assertIn('id="totalDependenciesValue"', html)
            self.assertIn("Полностью не учитывать зависимость", html)
            self.assertIn("не входит в проценты, числа уязвимостей, цвет проекта", html)
            self.assertNotIn("</script><script>alert(1)</script>", html)
            self.assertIn("\\u003c/script\\u003e", html)
            self.assertIn("safeExternalUrl", html)
            self.assertIn("releaseForTarget", html)
            self.assertIn("byTarget", html)
            self.assertIn("2.0.0", html)
            self.assertIn("Release notes для точного target не анализировались", html)
            self.assertIn("name !== 'release-cell'", html)
            self.assertIn('id="promptFormat"', html)
            self.assertIn('id="detailedCodeComments"', html)
            self.assertIn("detailed-why-comments", html)
            self.assertIn("minimal-comments", html)
            self.assertIn("codeCommentPolicy", html)
            self.assertIn("compact-v1", html)
            self.assertIn("buildCompactPromptFromCurrentView", html)
            self.assertIn("desktop-export", html)
            self.assertIn("!new URLSearchParams(window.location.search).has('desktop-export')", html)
            self.assertIn("CONTINUE_REQUIRED", html)
            self.assertIn("promptMeta", html)
            self.assertIn("compactCriticalReleaseDossierForRows", html)
            # The compact prompt must call the truncated dossier builder, not the
            # unbounded one shared with the full/diagnostic prompt: embedding every
            # breakingChanges/migrationNotes/sources entry per package made real
            # scopes hundreds of KB, pushing the branch plan (near the end of the
            # document) out of a small-context agent model's reach entirely.
            self.assertIn("criticalReleaseDossier = compactCriticalReleaseDossierForRows(actions)", html)
            # Every work branch used to write the same flat docs/*.md file,
            # created fresh from the same base commit on every sibling
            # branch, so merging any two of them produced an add/add
            # conflict on all three docs every time (observed for real,
            # twice). The compact prompt must instruct a per-branch shard
            # path instead and explicitly forbid writing the flat file
            # directly, which is what the release tool assembles from
            # afterward.
            self.assertIn("docs/dependency-upgrades/<branch>.md", html)
            self.assertIn("docs/dependency-update-summary/<branch>.md", html)
            self.assertIn("docs/dependency-update-review-notes/<branch>.md", html)
            self.assertIn("Never write to the flat", html)
            # buildGroupScopedCompactPrompt scopes one branch's own manifest
            # rows/dossier/branch-plan entry to a fresh, short-lived agent
            # session instead of handing over the whole multi-branch plan.
            # It depends on live DOM/filter state (document.querySelectorAll,
            # applyFilters), so its row-isolation behavior was verified by
            # generating a real two-branch dashboard and calling it in an
            # actual browser -- confirmed each branch's prompt contains only
            # its own packages, the orchestrator-owns-branches text replaces
            # the autonomous git-cycle rule, "## Final validation" and any
            # merge/release command are absent, and an unknown branch raises
            # GROUP_NOT_FOUND. These assertions cover the static template
            # text a plain source scan can verify.
            self.assertIn("function buildGroupScopedCompactPrompt(project, branch, packageSubset){", html)
            self.assertIn("function projectTargetPromptRowsForProject(project){", html)
            self.assertIn("## Orchestrator owns branches and merge", html)
            self.assertIn("GROUP_NOT_FOUND", html)
            self.assertIn("BATCH_SCOPE_DRIFT", html)
            self.assertIn("packageSubset", html)
            self.assertIn("execution batch=", html)
            self.assertIn("execution batch inside an already-running branch", html)
            self.assertIn("preserve all entries/evidence written by earlier batches", html)
            self.assertIn("compact single-branch task", html)
            self.assertNotIn("## Final validation\\n\\nFor each project", html)

            scripts = []
            import re
            scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
            js = Path(tmp) / "report.js"
            js.write_text("\n".join(scripts), encoding="utf-8")
            node = subprocess.run(["node", "--check", str(js)], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
            self.assertEqual(0, node.returncode, node.stderr)

    def test_dashboard_and_prompt_read_digit_lag_attributes_without_desync(self) -> None:
        row = self.make_row(
            name="@company/icons",
            current_version="1.13.1",
            min_lag_12m="1.13.1",
            min_lag_9m="1.20.2",
            min_lag_6m="2.0.0",
            min_lag_3m="2.0.9",
            lag_threshold_months=3,
            target_default=roadmap.NO_ACTION,
            target_yellow=roadmap.NO_ACTION,
            target_green="2.0.9",
            target_default_reason=roadmap.NO_ACTION,
            target_yellow_reason=roadmap.NO_ACTION,
            target_green_reason="lag",
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.html"
            roadmap.write_html(
                {"Demo": [row]}, out,
                history_dir=Path(tmp) / "history",
                project_specs={"Demo": roadmap.ProjectSpec("Demo", Path(tmp) / "demo", source_branch="main")},
                roadmap_json_path=Path(tmp) / "roadmap.json",
                history_snapshots=[],
            )
            html = out.read_text(encoding="utf-8")
            start = html.index("function semverParts(value){")
            end = html.index("function releaseForTarget(row, target){", start)
            functions = html[start:end]
            script = Path(tmp) / "lag-target.js"
            script.write_text(functions + r'''
const attrs = {
  'data-min-lag-12m': '1.13.1',
  'data-min-lag-9m': '1.20.2',
  'data-min-lag-6m': '2.0.0',
  'data-min-lag-3m': '2.0.9'
};
const row = {
  dataset: {
    lagThresholdMonths: '3', current: '1.13.1',
    targetDefault: '2.0.9', targetDefaultReason: 'lag-policy ≤3м', hasTargetDefault: '1',
    targetDefaultNonLag: '—', targetDefaultNonLagReason: '—',
    targetDefaultHasLag: '1', targetDefaultDynamicLocked: '0',
    targetYellow: '—', targetYellowReason: '—', hasTargetYellow: '0',
    targetGreen: '2.0.9', targetGreenReason: 'lag', hasTargetGreen: '1'
  },
  getAttribute: name => attrs[name] || null
};
const targetAt3m = targetForRow(row, 'default');
row.dataset.lagThresholdMonths = '12';
const targetAt12m = targetForRow(row, 'default');
const staleQuarterlyRow = {
  dataset: {
    lagThresholdMonths: '12', current: '1.0.0',
    targetDefault: '2.0.9', targetDefaultReason: 'lag-policy ≤3м', hasTargetDefault: '1',
    targetDefaultNonLag: '—', targetDefaultNonLagReason: '—',
    targetDefaultHasLag: '1', targetDefaultDynamicLocked: '0',
    targetYellow: '—', targetYellowReason: '—', hasTargetYellow: '0',
    targetGreen: '2.0.9', targetGreenReason: 'lag', hasTargetGreen: '1'
  },
  getAttribute: name => ({
    'data-min-lag-12m': '1.20.2',
    'data-min-lag-9m': '2.0.0'
  })[name] || null
};
const securityAndLagRow = {
  dataset: {
    lagThresholdMonths: '12', current: '1.0.0',
    targetDefault: '2.0.9', targetDefaultReason: 'убрать Critical; lag-policy ≤3м', hasTargetDefault: '1',
    targetDefaultNonLag: '1.50.0', targetDefaultNonLagReason: 'убрать Critical',
    targetDefaultHasLag: '1', targetDefaultDynamicLocked: '0'
  },
  getAttribute: name => ({
    'data-min-lag-12m': '1.20.2',
    'data-min-lag-9m': '2.0.0'
  })[name] || null
};
process.stdout.write(JSON.stringify({
  targetAt3m,
  targetAt12m,
  recalculated12m: targetForRow(staleQuarterlyRow, 'default'),
  securityPreserved: targetForRow(securityAndLagRow, 'default'),
  m3: lagTargetForDomRow(row, 3),
  m12: lagTargetForDomRow(row, 12)
}));
''', encoding="utf-8")
            result = subprocess.run(["node", str(script)], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("2.0.9", payload["targetAt3m"]["value"])
            self.assertTrue(payload["targetAt3m"]["has"])
            self.assertIn("lag-policy ≤3м", payload["targetAt3m"]["reason"])
            self.assertEqual("—", payload["targetAt12m"]["value"])
            self.assertFalse(payload["targetAt12m"]["has"])
            self.assertEqual("2.0.0", payload["recalculated12m"]["value"])
            self.assertTrue(payload["recalculated12m"]["has"])
            self.assertIn("lag-policy ≤12м", payload["recalculated12m"]["reason"])
            self.assertEqual("2.0.0", payload["securityPreserved"]["value"])
            self.assertIn("убрать Critical", payload["securityPreserved"]["reason"])
            self.assertEqual("2.0.9", payload["m3"])
            self.assertEqual("1.13.1", payload["m12"])
            self.assertIn("ROADMAP_TARGET_DESYNC", html)
            self.assertIn("lagPolicyTarget", html)

    def test_yellow_target_is_calculated_against_baseline_universe(self) -> None:
        baseline = {
            "directDependencies": {
                f"dependencies:dep-{index}": {
                    "section": "dependencies",
                    "name": f"dep-{index}",
                    "spec": "1.0.0",
                }
                for index in range(100)
            }
        }
        rows = [
            self.make_row(
                name=f"dep-{index}",
                current_version="2.0.0" if index < 77 else "1.0.0",
                min_lag_12m="2.0.0",
                min_lag_9m="2.0.0",
                min_lag_6m="2.0.0",
                min_lag_3m="2.0.0",
                lag_threshold_months=12,
                current_vulns="",
            )
            for index in range(98)
        ]

        health = roadmap.enrich_project_targets(
            {"Demo": rows},
            {"Demo": baseline},
        )["Demo"]

        self.assertEqual(100, health.total)
        self.assertEqual(79, health.lag_ok_12m)
        self.assertEqual(79.0, health.lag_ok_pct)
        self.assertEqual(
            21,
            sum(1 for row in rows if roadmap.target_is_action(row.target_yellow)),
        )
        client = roadmap.LiveDataClient("https://nexus.example/repository/npm-group", timeout=1, batch_size=10, sleep_sec=0)
        roadmap.minimize_yellow_plan_after_compatibility({"Demo": rows}, client, {"Demo": health})
        # 79 are already compliant; post-compat greedy keeps six more to retain
        # the deliberate 85% reserve instead of targeting the exact 80% gate.
        self.assertEqual(
            6,
            sum(1 for row in rows if roadmap.target_is_action(row.target_yellow)),
        )

    def test_post_compatibility_greedy_keeps_full_plan_when_yellow_is_unreachable(self) -> None:
        rows = [
            self.make_row(
                name=f"dep-{index}",
                current_version="1.0.0",
                latest_version="2.0.0",
                min_lag_12m="2.0.0",
                min_lag_9m="2.0.0",
                min_lag_6m="2.0.0",
                min_lag_3m="2.0.0",
                lag_threshold_months=12,
                current_vulns="0",
            )
            for index in range(10)
        ]
        health = roadmap.enrich_project_targets({"Demo": rows})["Demo"]
        # Model compatibility/registry narrowing that proved three candidates
        # impossible. Seven useful updates remain: below Yellow, but still work
        # that must be executed and preserved.
        for row in rows[-3:]:
            row.target_yellow = roadmap.NO_ACTION
            row.target_yellow_has_lag = False
        client = roadmap.LiveDataClient("https://nexus.example/repository/npm-group", timeout=1, batch_size=10, sleep_sec=0)
        roadmap.minimize_yellow_plan_after_compatibility({"Demo": rows}, client, {"Demo": health})
        self.assertEqual(7, sum(1 for row in rows if roadmap.target_is_action(row.target_yellow)))

    def test_strict_package_lag_policy_is_not_skipped_by_yellow_80_percent_rule(self) -> None:
        rows = [
            self.make_row(
                name=f"fresh-{index}",
                current_version="2.0.0",
                min_lag_12m="2.0.0",
                min_lag_9m="2.0.0",
                min_lag_6m="2.0.0",
                min_lag_3m="2.0.0",
                lag_threshold_months=12,
                current_vulns="0",
            )
            for index in range(7)
        ]
        strict = self.make_row(
            name="@company/icons",
            current_version="1.13.1",
            min_lag_12m="1.13.1",
            min_lag_9m="1.20.0",
            min_lag_6m="2.0.0",
            min_lag_3m="2.0.9",
            lag_threshold_months=3,
            current_vulns="0",
            group=4,
        )
        ordinary_lag_rows = [
            self.make_row(
                name=f"ordinary-lag-{index}",
                current_version="1.0.0",
                min_lag_12m="1.1.0",
                min_lag_9m="1.1.0",
                min_lag_6m="1.1.0",
                min_lag_3m="1.1.0",
                lag_threshold_months=12,
                current_vulns="0",
                group=5,
            )
            for index in range(2)
        ]
        rows.extend([strict, *ordinary_lag_rows])

        health = roadmap.enrich_project_targets({"Demo": rows})["Demo"]
        client = roadmap.LiveDataClient("https://nexus.example/repository/npm-group", timeout=1, batch_size=10, sleep_sec=0)
        roadmap.minimize_yellow_plan_after_compatibility({"Demo": rows}, client, {"Demo": health})

        self.assertEqual("red", health.status)
        self.assertEqual("2.0.9", strict.target_yellow)
        self.assertEqual("2.0.9", strict.target_default)
        self.assertIn("не исключается правилом 80%", strict.target_default_reason)
        self.assertEqual(1, sum(1 for row in ordinary_lag_rows if roadmap.target_is_action(row.target_yellow)))

    def test_registry_target_uses_readable_nexus_tarball_not_metadata_only_latest(self) -> None:
        registry = "https://nexus.example/repository/npm-group"
        client = roadmap.LiveDataClient(registry, timeout=1, batch_size=10, sleep_sec=0)
        meta = {
            "dist-tags": {"latest": "7.7.4"},
            "versions": {
                "7.6.24": {"dist": {"tarball": f"{registry}/@storybook/builder-vite/-/builder-vite-7.6.24.tgz"}},
                "7.7.4": {"dist": {"tarball": f"{registry}/@storybook/builder-vite/-/builder-vite-7.7.4.tgz"}},
            },
            "time": {
                "7.6.24": "2026-06-01T00:00:00Z",
                "7.7.4": "2026-07-01T00:00:00Z",
            },
        }
        client.registry_artifact_cache[("@storybook/builder-vite", "7.7.4")] = {
            "status": "unavailable", "tarballUrl": meta["versions"]["7.7.4"]["dist"]["tarball"]
        }
        client.registry_artifact_cache[("@storybook/builder-vite", "7.6.24")] = {
            "status": "available", "tarballUrl": meta["versions"]["7.6.24"]["dist"]["tarball"]
        }
        latest = client.latest_installable_version("@storybook/builder-vite", meta)
        self.assertEqual("7.6.24", latest)
        target = roadmap.min_by_lag(
            meta,
            ["7.6.24", "7.7.4"],
            12,
            latest_override=latest,
            is_available=lambda version: client.registry_version_is_installable(
                "@storybook/builder-vite", meta, version
            ),
        )
        self.assertEqual("7.6.24", target)

    def test_target_walks_up_past_a_version_with_broken_type_declarations(self) -> None:
        # A registry-reachable tarball that's missing declared TypeScript
        # types must be treated the same as an unreachable one for target
        # selection: min_by_lag/min_by_vuln already return the first
        # candidate satisfying both the policy and availability, walking
        # `candidates` from `current` upward -- so this alone makes target
        # selection skip a broken version and land on the next one that
        # actually works, with no separate retry loop required.
        registry = "https://nexus.example/repository/npm-group"
        client = roadmap.LiveDataClient(registry, timeout=1, batch_size=10, sleep_sec=0)
        meta = {
            "dist-tags": {"latest": "4.3.0"},
            "versions": {
                "4.2.0": {"dist": {"tarball": f"{registry}/date-fns/-/date-fns-4.2.0.tgz"}},
                "4.3.0": {"dist": {"tarball": f"{registry}/date-fns/-/date-fns-4.3.0.tgz"}},
            },
            "time": {
                "4.2.0": "2026-06-01T00:00:00Z",
                "4.3.0": "2026-07-01T00:00:00Z",
            },
        }
        client.registry_artifact_cache[("date-fns", "4.2.0")] = {"status": "available", "tarballUrl": meta["versions"]["4.2.0"]["dist"]["tarball"]}
        client.registry_artifact_cache[("date-fns", "4.3.0")] = {"status": "available", "tarballUrl": meta["versions"]["4.3.0"]["dist"]["tarball"]}
        # Reachable (status=available) but broken: package.json promises types
        # that the packed tarball does not actually contain.
        client.registry_types_cache[("date-fns", "4.2.0")] = "package.json declares TypeScript types at ./date-fns.d.ts, none present in the published tarball"
        client.registry_types_cache[("date-fns", "4.3.0")] = None

        def is_available(version: str) -> bool:
            if not client.registry_version_is_installable("date-fns", meta, version):
                return False
            return client.registry_version_type_declarations_ok("date-fns", meta, version) is None

        target = roadmap.min_by_lag(meta, ["4.2.0", "4.3.0"], 12, latest_override="4.3.0", is_available=is_available)
        self.assertEqual("4.3.0", target)

        # And if every candidate is broken, the row must not silently pick a
        # non-actionable target -- it stays deferred with a reason, exactly
        # like the pre-existing "not reachable" case.
        client.registry_types_cache[("date-fns", "4.3.0")] = "package.json declares TypeScript types at ./date-fns.d.ts, none present in the published tarball"
        blocked = roadmap.min_by_lag(meta, ["4.2.0", "4.3.0"], 12, latest_override="4.3.0", is_available=is_available)
        self.assertFalse(roadmap.target_is_action(blocked))

    def test_registry_blocks_public_tarball_url_even_when_nexus_metadata_contains_it(self) -> None:
        client = roadmap.LiveDataClient(
            "https://nexus.example/repository/npm-group",
            timeout=1,
            batch_size=10,
            sleep_sec=0,
        )
        meta = {
            "versions": {
                "1.0.0": {"dist": {"tarball": "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz"}}
            }
        }
        candidates, notes = client.registry_structural_candidates(meta, ["1.0.0"])
        self.assertEqual([], candidates)
        self.assertIn("outside configured registry", notes[0])
        evidence = client.registry_version_artifact("demo", meta, "1.0.0")
        self.assertEqual("foreign-registry-url", evidence["status"])

    def test_storybook_cohort_aligns_core_and_blocks_incompatible_plugin_without_tarball(self) -> None:
        registry = "https://nexus.example/repository/npm-group"
        client = roadmap.LiveDataClient(registry, timeout=1, batch_size=10, sleep_sec=0)

        def metadata(name, versions):
            return {
                "versions": {
                    version: {"dist": {"tarball": f"{registry}/{name}/-/{name.split('/')[-1]}-{version}.tgz"}}
                    for version in versions
                },
                "dist-tags": {"latest": versions[-1]},
            }

        client.npm_cache = {
            "@storybook/react": metadata("@storybook/react", ["7.6.21", "7.6.24"]),
            "@storybook/builder-vite": metadata("@storybook/builder-vite", ["7.6.24"]),
            "eslint-plugin-storybook": metadata("eslint-plugin-storybook", ["7.6.24", "9.0.17"]),
        }
        for package in ("@storybook/react", "@storybook/builder-vite"):
            client.registry_artifact_cache[(package, "7.6.24")] = {
                "status": "available",
                "tarballUrl": client.registry_tarball_url(client.npm_cache[package], "7.6.24"),
            }
        client.registry_artifact_cache[("eslint-plugin-storybook", "7.6.24")] = {
            "status": "unavailable",
            "tarballUrl": client.registry_tarball_url(client.npm_cache["eslint-plugin-storybook"], "7.6.24"),
        }
        client.registry_artifact_cache[("eslint-plugin-storybook", "9.0.17")] = {
            "status": "available",
            "tarballUrl": client.registry_tarball_url(client.npm_cache["eslint-plugin-storybook"], "9.0.17"),
        }
        rows = [
            self.make_row(name="@storybook/react", kind="dev", current_version="6.5.9", target_default="7.6.21", target_yellow="7.6.21", target_green="7.6.21"),
            self.make_row(name="@storybook/builder-vite", kind="dev", current_version="0.2.2", target_default="7.6.24", target_yellow="7.6.24", target_green="7.6.24"),
            self.make_row(name="eslint-plugin-storybook", kind="dev", current_version="0.5.12", target_default="9.0.17", target_yellow="9.0.17", target_green="9.0.17"),
        ]
        roadmap.enforce_storybook_cohort({"Demo": rows}, client)
        self.assertEqual("7.6.24", rows[0].target_default)
        self.assertEqual("7.6.24", rows[1].target_default)
        self.assertEqual(roadmap.NO_ACTION, rows[2].target_default)
        self.assertIn("нет installable версии 7.x", rows[2].target_default_reason)
        self.assertNotEqual("9.0.17", rows[2].target_default)

    def test_dashboard_history_snapshot_preserves_full_roadmap_state(self) -> None:
        row = self.make_row(
            project="Demo.App",
            name="@company/icons",
            current_version="1.13.1",
            target_default="2.0.9",
            target_default_reason="quarterly lag policy",
            scope_excluded=True,
            exclusion_reason="known product blocker",
        )
        health = roadmap.ProjectHealth(
            project="Demo.App", status="yellow", status_rank=1, total=1,
            lag_ok_12m=1, lag_bad_12m=0, lag_ok_pct=100.0,
            critical=0, high=0, moderate=1, low=0, unknown=0, reason="test", excluded=1,
        )
        snapshot = roadmap.compact_history_snapshot(
            {"Demo.App": [row]},
            {"Demo.App": health},
            label="before group 4",
            dashboard_state={"schemaVersion": 1, "packageOverrides": {"Demo.App": {}}},
        )
        self.assertEqual(2, snapshot["schemaVersion"])
        self.assertEqual("dependency-roadmap-dashboard-snapshot", snapshot["type"])
        self.assertEqual("before group 4", snapshot["label"])
        saved = snapshot["projects"]["Demo.App"]["dependencies"][0]
        self.assertEqual("2.0.9", saved["target_default"])
        self.assertTrue(saved["scope_excluded"])
        self.assertEqual("known product blocker", saved["exclusion_reason"])
        self.assertIn("dashboardState", snapshot)

    def test_html_exposes_full_snapshot_history_and_task_spec_export(self) -> None:
        row = self.make_row(project="Demo.App", name="demo")
        health = roadmap.ProjectHealth(
            project="Demo.App", status="red", status_rank=0, total=1,
            lag_ok_12m=0, lag_bad_12m=1, lag_ok_pct=0.0,
            critical=0, high=1, moderate=0, low=0, unknown=0, reason="test",
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "roadmap.html"
            roadmap.write_html({"Demo.App": [row]}, out, health_by_project={"Demo.App": health})
            html = out.read_text(encoding="utf-8")
        self.assertIn('id="saveSnapshotBtn"', html)
        self.assertIn('id="exportTaskSpecBtn"', html)
        self.assertIn("class='row-select'", html)
        self.assertIn('id="excludeSelectedBtn"', html)
        self.assertIn('id="includeSelectedBtn"', html)
        self.assertIn("function setSelectedRowsExcluded", html)
        self.assertIn("function lagComplianceTargetForDomRow", html)
        self.assertIn("dashboard-state-recalculate.json", html)
        self.assertIn("function currentDashboardSnapshot", html)
        self.assertIn("function historyComparisonHtml", html)
        self.assertIn("function buildTaskSpecification", html)
        self.assertIn("Полный снимок", html)

    def test_desktop_queues_roadmap_recalculation_after_planning_state_save(self) -> None:
        root = Path(__file__).resolve().parents[1]
        main = (root / "desktop/electron/main.ts").read_text(encoding="utf-8")
        hook = (root / "desktop/src/hooks/useDependencyFlow.ts").read_text(encoding="utf-8")
        # Asserted as behaviour rather than one exact line: the recalculation
        # flag must still be derived from the download filename, and the
        # projects to recalculate must come from the saved-state diff instead
        # of whichever project happens to be selected in the app -- the
        # dashboard edits every project at once.
        self.assertIn("dashboard-state-recalculate", main)
        self.assertIn("/dashboard-state-recalculate/i.test(suggested)", main)
        self.assertIn("changedOverrideProjects", main)
        self.assertIn("recalculateProjects", main)
        self.assertIn("pendingRoadmapRecalc", hook)
        self.assertIn("recalculateProjects", hook)
        self.assertIn("action: 'generate'", hook)
        self.assertIn("автоматический пересчёт после изменения scope", hook)

    def test_target_reason_join_does_not_repeat_reason_already_in_accumulated_text(self) -> None:
        existing = "first reason; AUTO_PEER_CLOSURE: react requires peer; third reason"
        merged = roadmap.target_reason_join([existing, "AUTO_PEER_CLOSURE: react requires peer"])
        self.assertEqual(merged.count("AUTO_PEER_CLOSURE: react requires peer"), 1)

    def test_auto_peer_closure_activates_existing_direct_companion_across_display_groups(self) -> None:
        registry = "https://nexus.example/repository/npm-group"
        client = roadmap.LiveDataClient(registry, timeout=1, batch_size=10, sleep_sec=0)
        client.npm_cache.update({
            "vitest": {
                "versions": {
                    "3.2.6": {
                        "peerDependencies": {"vite": ">=5.0.0"},
                        "dist": {"tarball": f"{registry}/vitest/-/vitest-3.2.6.tgz"},
                    },
                }
            },
            "vite": {
                "versions": {
                    "4.3.9": {"dist": {"tarball": f"{registry}/vite/-/vite-4.3.9.tgz"}},
                    "5.4.0": {"dist": {"tarball": f"{registry}/vite/-/vite-5.4.0.tgz"}},
                    "6.0.0": {"dist": {"tarball": f"{registry}/vite/-/vite-6.0.0.tgz"}},
                }
            },
        })
        client.registry_artifact_cache[("vite", "5.4.0")] = {"status": "available"}
        vitest = self.make_row(
            name="vitest", kind="dev", current_version="0.30.1", latest_version="3.2.6", group=5, subgroup="tests",
            target_default="3.2.6", target_yellow="3.2.6", target_green="3.2.6",
        )
        vite = self.make_row(
            name="vite", kind="dev", current_version="4.3.9", latest_version="6.0.0", group=4, subgroup="vite",
            target_default=roadmap.NO_ACTION, target_yellow=roadmap.NO_ACTION, target_green=roadmap.NO_ACTION,
        )

        roadmap.auto_expand_direct_peer_scope({"Demo": [vitest, vite]}, client)

        self.assertEqual("5.4.0", vite.target_default)
        self.assertEqual("5.4.0", vite.target_yellow)
        self.assertEqual("5.4.0", vite.target_green)
        self.assertTrue(vitest.compatibility_cohort.startswith("peer-"))
        self.assertEqual(vitest.compatibility_cohort, vite.compatibility_cohort)
        self.assertEqual(5, vitest.group)
        self.assertEqual(4, vite.group)
        self.assertIn("PEER_COMPANION", vite.compatibility_note)

    def test_auto_peer_closure_never_overrides_explicit_exclusion(self) -> None:
        registry = "https://nexus.example/repository/npm-group"
        client = roadmap.LiveDataClient(registry, timeout=1, batch_size=10, sleep_sec=0)
        client.npm_cache.update({
            "vitest": {"versions": {"3.2.6": {"peerDependencies": {"vite": ">=5"}, "dist": {"tarball": f"{registry}/vitest/-/vitest-3.2.6.tgz"}}}},
            "vite": {"versions": {"5.4.0": {"dist": {"tarball": f"{registry}/vite/-/vite-5.4.0.tgz"}}}},
        })
        client.registry_artifact_cache[("vite", "5.4.0")] = {"status": "available"}
        vitest = self.make_row(name="vitest", kind="dev", current_version="0.30.1", target_default="3.2.6", target_yellow="3.2.6", target_green="3.2.6")
        vite = self.make_row(
            name="vite", kind="dev", current_version="4.3.9", target_default=roadmap.NO_ACTION, target_yellow=roadmap.NO_ACTION, target_green=roadmap.NO_ACTION,
            scope_excluded=True, exclusion_reason="user chose coordinated migration later", exclusion_source="dashboard-state",
        )

        roadmap.auto_expand_direct_peer_scope({"Demo": [vitest, vite]}, client)

        self.assertEqual(roadmap.NO_ACTION, vite.target_default)
        self.assertFalse(vite.compatibility_cohort)

    def test_supervisor_deferral_keeps_package_in_health_but_removes_executable_cohort_targets(self) -> None:
        first = self.make_row(
            name="vitest", current_version="0.30.1", group=5, subgroup="tests",
            target_default="3.2.6", target_yellow="3.2.6", target_green="3.2.6",
            compatibility_cohort="test-toolchain", planner_deferred=True,
            planner_deferred_reason="needs a new direct companion outside autonomous scope",
        )
        companion = self.make_row(
            name="@vitejs/plugin-basic-ssl", current_version="0.1.1", group=5, subgroup="tests",
            target_default="2.1.0", target_yellow="2.1.0", target_green="2.1.0",
            compatibility_cohort="test-toolchain",
        )
        roadmap.apply_planner_deferrals({"Demo": [first, companion]})

        for row in (first, companion):
            self.assertFalse(row.scope_excluded)
            self.assertTrue(row.planner_deferred)
            self.assertEqual(roadmap.NO_ACTION, row.target_default)
            self.assertEqual(roadmap.NO_ACTION, row.target_yellow)
            self.assertEqual(roadmap.NO_ACTION, row.target_green)
            self.assertIn("PLANNER_DEFERRED", row.compatibility_note)

    def test_supervisor_scope_expansion_activates_existing_direct_target_without_bypassing_gates(self) -> None:
        row = self.make_row(
            name="postcss-scss", current_version="4.0.4", latest_version="4.0.7", group=5,
            target_default=roadmap.NO_ACTION, target_yellow=roadmap.NO_ACTION, target_green=roadmap.NO_ACTION,
            planner_target_default="4.0.7", planner_target_yellow="4.0.7",
        )
        roadmap.apply_supervisor_scope_expansions({"Demo": [row]})

        self.assertEqual("4.0.7", row.target_default)
        self.assertEqual("4.0.7", row.target_yellow)
        self.assertEqual(roadmap.NO_ACTION, row.target_green)
        self.assertIn("SUPERVISOR_SCOPE_EXPANSION", row.compatibility_note)
        self.assertTrue(row.target_default_dynamic_locked)

        row.lag_threshold_months = 12
        row.min_lag_12m = "4.0.7"
        health = roadmap.compute_project_health([row], "Demo")
        self.assertEqual("4.0.7", health.lag_blockers[0]["plannedTargetYellow"])

    def test_peer_scope_blocks_target_when_required_direct_peer_is_excluded(self) -> None:
        client = roadmap.LiveDataClient("https://nexus.example/repository/npm-group", timeout=1, batch_size=10, sleep_sec=0)
        client.npm_cache["vitest"] = {
            "versions": {
                "3.2.6": {"peerDependencies": {"vite": ">=5.0.0"}},
            }
        }
        vitest = self.make_row(
            name="vitest", current_version="0.30.1", latest_version="3.2.6", group=5, subgroup="test",
            target_default="3.2.6", target_yellow="3.2.6", target_green="3.2.6",
        )
        vite = self.make_row(
            name="vite", current_version="4.3.9", latest_version="7.0.0", group=4, subgroup="vite",
            target_default=roadmap.NO_ACTION, target_yellow=roadmap.NO_ACTION, target_green=roadmap.NO_ACTION,
            scope_excluded=True, exclusion_reason="runtime compatibility", exclusion_source="dashboard-state",
        )
        health = roadmap.ProjectHealth(
            project="Demo", status="yellow", status_rank=roadmap.TARGET_RANK["yellow"], total=1,
            lag_ok_12m=1, lag_bad_12m=0, lag_ok_pct=100.0, critical=0, high=0, moderate=0, low=0, unknown=0, reason="test", excluded=1,
        )

        roadmap.enforce_direct_peer_scope_compatibility({"Demo": [vitest, vite]}, client, {"Demo": health})

        self.assertEqual(roadmap.NO_ACTION, vitest.target_default)
        self.assertEqual(roadmap.NO_ACTION, vitest.target_yellow)
        self.assertEqual(roadmap.NO_ACTION, vitest.target_green)
        self.assertIn("PEER_RESOLUTION_DEFERRED", vitest.compatibility_note)
        self.assertIn("vite", vitest.target_default_reason)

    def test_peer_scope_uses_planned_peer_version_and_reverse_direct_constraints(self) -> None:
        registry = "https://nexus.example/repository/npm-group"
        client = roadmap.LiveDataClient(registry, timeout=1, batch_size=10, sleep_sec=0)
        client.npm_cache.update({
            "@company/react-ui": {
                "versions": {
                    "5.4.2": {
                        "peerDependencies": {"react": ">=16.9 <19"},
                        "dist": {"tarball": f"{registry}/@company/react-ui/-/react-ui-5.4.2.tgz"},
                    },
                }
            },
            "react": {
                "versions": {
                    "19.0.1": {"dist": {"tarball": f"{registry}/react/-/react-19.0.1.tgz"}},
                }
            },
            "react-dom": {
                "versions": {
                    "18.2.0": {
                        "peerDependencies": {"react": "^18.2.0"},
                        "dist": {"tarball": f"{registry}/react-dom/-/react-dom-18.2.0.tgz"},
                    },
                }
            },
        })
        react_ui = self.make_row(
            name="@company/react-ui", current_version="4.26.0", latest_version="5.4.2", group=4, subgroup="",
            target_default="5.4.2", target_yellow="5.4.2", target_green="5.4.2",
        )
        react = self.make_row(
            name="react", current_version="18.2.0", latest_version="19.0.1", group=4, subgroup="",
            target_default="19.0.1", target_yellow="19.0.1", target_green="19.0.1",
        )
        react_dom = self.make_row(
            name="react-dom", current_version="18.2.0", latest_version="19.0.1", group=4, subgroup="",
            target_default=roadmap.NO_ACTION, target_yellow=roadmap.NO_ACTION, target_green=roadmap.NO_ACTION,
        )
        health = roadmap.ProjectHealth(
            project="Demo", status="yellow", status_rank=roadmap.TARGET_RANK["yellow"], total=3,
            lag_ok_12m=3, lag_bad_12m=0, lag_ok_pct=100.0, critical=0, high=0, moderate=0, low=0, unknown=0, reason="test", excluded=0,
        )

        roadmap.enforce_direct_peer_scope_compatibility(
            {"Demo": [react_ui, react, react_dom]}, client, {"Demo": health}
        )

        self.assertEqual("5.4.2", react_ui.target_default)
        self.assertEqual(roadmap.NO_ACTION, react.target_default)
        self.assertEqual(react_ui.compatibility_cohort, react.compatibility_cohort)
        self.assertIn("react-dom@18.2.0", react.compatibility_note)

    def test_peer_scope_checks_provider_target_against_current_direct_plugin(self) -> None:
        registry = "https://nexus.example/repository/npm-group"
        client = roadmap.LiveDataClient(registry, timeout=1, batch_size=10, sleep_sec=0)
        client.npm_cache.update({
            "vite": {
                "versions": {
                    "6.4.2": {"dist": {"tarball": f"{registry}/vite/-/vite-6.4.2.tgz"}},
                }
            },
            "vite-plugin-pwa": {
                "versions": {
                    "0.12.3": {
                        "peerDependencies": {"vite": "^2.0.0 || ^3.0.0-0"},
                        "dist": {"tarball": f"{registry}/vite-plugin-pwa/-/vite-plugin-pwa-0.12.3.tgz"},
                    },
                }
            },
        })
        vite = self.make_row(
            name="vite", current_version="4.3.9", latest_version="6.4.2", group=4, subgroup="",
            target_default="6.4.2", target_yellow="6.4.2", target_green="6.4.2",
        )
        pwa = self.make_row(
            name="vite-plugin-pwa", current_version="0.12.3", latest_version="1.0.3", group=3, subgroup="",
            target_default="1.0.3", target_yellow="1.0.3", target_green="1.0.3",
        )
        health = roadmap.ProjectHealth(
            project="Demo", status="yellow", status_rank=roadmap.TARGET_RANK["yellow"], total=2,
            lag_ok_12m=2, lag_bad_12m=0, lag_ok_pct=100.0, critical=0, high=0, moderate=0, low=0, unknown=0, reason="test", excluded=0,
        )

        roadmap.enforce_direct_peer_scope_compatibility({"Demo": [vite, pwa]}, client, {"Demo": health})

        self.assertEqual(roadmap.NO_ACTION, vite.target_default)
        self.assertIn("vite-plugin-pwa@0.12.3", vite.compatibility_note)

    def test_peer_scope_allows_target_when_required_peer_is_planned_in_different_display_group(self) -> None:
        registry = "https://nexus.example/repository/npm-group"
        client = roadmap.LiveDataClient(registry, timeout=1, batch_size=10, sleep_sec=0)
        client.npm_cache.update({
            "vitest": {
                "versions": {
                    "3.2.6": {
                        "peerDependencies": {"vite": ">=5.0.0"},
                        "dist": {"tarball": f"{registry}/vitest/-/vitest-3.2.6.tgz"},
                    },
                }
            },
            "vite": {
                "versions": {
                    "4.3.9": {"dist": {"tarball": f"{registry}/vite/-/vite-4.3.9.tgz"}},
                    "6.0.0": {"dist": {"tarball": f"{registry}/vite/-/vite-6.0.0.tgz"}},
                }
            },
        })
        vitest = self.make_row(
            name="vitest", current_version="0.30.1", latest_version="3.2.6", group=5, subgroup="vite-stack",
            target_default="3.2.6", target_yellow="3.2.6", target_green="3.2.6",
        )
        vite = self.make_row(
            name="vite", current_version="4.3.9", latest_version="6.0.0", group=17, subgroup="other-display-bucket",
            target_default="6.0.0", target_yellow="6.0.0", target_green="6.0.0",
        )
        health = roadmap.ProjectHealth(
            project="Demo", status="yellow", status_rank=roadmap.TARGET_RANK["yellow"], total=2,
            lag_ok_12m=2, lag_bad_12m=0, lag_ok_pct=100.0, critical=0, high=0, moderate=0, low=0, unknown=0, reason="test", excluded=0,
        )

        roadmap.enforce_direct_peer_scope_compatibility({"Demo": [vitest, vite]}, client, {"Demo": health})

        self.assertEqual("3.2.6", vitest.target_default)
        self.assertEqual("3.2.6", vitest.target_yellow)
        self.assertEqual(5, vitest.group)
        self.assertEqual(17, vite.group)
        self.assertEqual(vitest.compatibility_cohort, vite.compatibility_cohort)
        self.assertNotIn("PEER_RESOLUTION_DEFERRED", vitest.compatibility_note)

    def test_types_stub_remove_is_atomic_with_runtime_upgrade(self) -> None:
        types_row = self.make_row(
            name="@types/uuid", kind="dev", current_version="8.3.4", requested_spec="^8.3.4",
            target_default="11.0.0", target_yellow="11.0.0", target_green="11.0.0",
        )
        runtime_row = self.make_row(
            name="uuid", kind="runtime", current_version="9.0.0", requested_spec="^9.0.0",
            target_default="11.1.1", target_yellow="11.1.1", target_green="11.1.1",
        )

        class FakeClient:
            def __init__(self):
                self.npm_cache = {
                    "@types/uuid": {
                        "dist-tags": {"latest": "11.0.0"},
                        "versions": {"11.0.0": {"deprecated": "This is a stub types definition. uuid provides its own types."}},
                    },
                    "uuid": {"versions": {"9.0.0": {}, "11.1.1": {}}},
                }

            def registry_version_provides_own_types(self, package, meta, version):
                return package == "uuid" and version == "11.1.1"

        roadmap.plan_executable_actions({"Demo": [types_row, runtime_row]}, FakeClient())

        self.assertEqual("remove", types_row.planned_action_default)
        self.assertEqual("update", runtime_row.planned_action_default)
        self.assertTrue(types_row.compatibility_cohort)
        self.assertEqual(types_row.compatibility_cohort, runtime_row.compatibility_cohort)
        self.assertIn("TYPE_STUB_REMOVE_PROVED", types_row.compatibility_note)

    def test_types_stub_remove_is_deferred_without_self_typed_runtime_proof(self) -> None:
        types_row = self.make_row(
            name="@types/uuid", kind="dev", current_version="8.3.4",
            target_default="11.0.0", target_yellow="11.0.0", target_green="11.0.0",
        )
        runtime_row = self.make_row(
            name="uuid", current_version="9.0.0",
            target_default="—", target_yellow="—", target_green="—",
        )

        class FakeClient:
            def __init__(self):
                self.npm_cache = {
                    "@types/uuid": {
                        "dist-tags": {"latest": "11.0.0"},
                        "versions": {"11.0.0": {"deprecated": "This is a stub types definition. uuid provides its own types."}},
                    },
                    "uuid": {"versions": {"9.0.0": {}}},
                }

            def registry_version_provides_own_types(self, package, meta, version):
                return False

        roadmap.plan_executable_actions({"Demo": [types_row, runtime_row]}, FakeClient())
        self.assertEqual("deferred", types_row.planned_action_default)
        self.assertEqual("—", types_row.target_default)
        self.assertIn("TYPE_STUB_REMOVE_DEFERRED", types_row.compatibility_note)

    @staticmethod
    def make_row(**changes):
        values = dict(
            project="Demo", package_dir=".", name="demo-pkg", kind="runtime", requested_spec="^1.0.0",
            current_version="1.0.0", current_source="package-lock.json", latest_version="4.0.0",
            current_vulns="H:1", min_no_critical="1.0.0", min_no_high="2.0.0", min_no_vuln="3.0.0",
            min_lag_12m="2.0.0", min_lag_9m="2.5.0", min_lag_6m="3.0.0", min_lag_3m="4.0.0",
            group=4, reason="test", notes="", subgroup="demo", lag_threshold_months=3,
            target_default="4.0.0", target_yellow="4.0.0", target_green="4.0.0",
            target_default_reason="3m", target_yellow_reason="3m", target_green_reason="3m",
        )
        values.update(changes)
        return roadmap.DependencyRow(**values)


if __name__ == "__main__":
    unittest.main()
