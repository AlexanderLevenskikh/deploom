from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dependency_live_roadmap_generator as roadmap
from draft_decision_contract import LATEST_APPROVED, normalize_decision_contract
from draft_input_identity import capture_draft_semantic_inputs


class DraftDecisionContractTests(unittest.TestCase):
    REGISTRY = "https://registry.example.invalid"

    def setUp(self):
        self.environment = patch.dict(os.environ, {"DEPLOOM_BASELINE_INTENT_JSON": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        roadmap._BASELINE_INTENT_CACHE_RAW = "<unset>"
        self.client = roadmap.LiveDataClient(self.REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
        # A missed fixture must fail, never reach the network.
        self.network = patch.object(self.client.session, "request", side_effect=AssertionError("unexpected network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def intent(self, **values):
        os.environ["DEPLOOM_BASELINE_INTENT_JSON"] = json.dumps(values)

    def row(self, name="sample-library", current="1.0.0", target=roadmap.NO_ACTION):
        return roadmap.DependencyRow(
            project="synthetic", package_dir=".", name=name, kind="dev", requested_spec="*",
            current_version=current, current_source="lockfile", latest_version=current, current_vulns="0",
            min_no_critical=current, min_no_high=current, min_no_vuln=current,
            min_lag_12m=current, min_lag_9m=current, min_lag_6m=current, min_lag_3m=current,
            group=1, reason="synthetic fixture", notes="", target_default=target,
            target_yellow=target, target_green=target, draft_scan_state="done",
        )

    def package(self, name, versions):
        metadata = {}
        for version, extra in versions.items():
            metadata[version] = {**extra, "dist": {"tarball": f"{self.REGISTRY}/artifact/{name}/{version}.tgz"}}
            self.client.registry_artifact_cache[(name, version)] = {"status": "available", "tarballUrl": metadata[version]["dist"]["tarball"]}
        self.client.npm_cache[name] = {"versions": metadata}

    def test_contract_defaults_and_rejected_bounds_are_not_cached_as_valid(self):
        self.assertEqual("pinned", normalize_decision_contract()["targetMode"])
        self.assertEqual("minimum-change-to-policy", normalize_decision_contract()["objective"])
        self.intent(mutablePackages=[], policies={"sample-library": "required"})
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "outside mutablePackages"):
                roadmap._baseline_intent_payload()
        for value in ({"approvedVersionLines": {"sample-library": [True]}}, {"targetMode": "optional"}, {"objective": "whatever"}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "DECISION_CONTRACT_INVALID"):
                normalize_decision_contract(value)

    def test_latest_objective_updates_healthy_package_only_inside_approved_major(self):
        row = self.row()
        self.package(row.name, {"1.0.0": {}, "1.5.0": {}, "2.1.0": {}})
        rows = {"synthetic": [row]}
        roadmap.enrich_project_targets(rows, {})
        self.assertFalse(roadmap.target_is_action(row.target_default), "healthy minimum-change goal should remain a no-op")
        self.intent(objective=LATEST_APPROVED)
        roadmap.apply_decision_contract_targets(rows, self.client)
        self.assertEqual("1.5.0", row.target_default)
        self.assertNotIn("2.1.0", roadmap._candidate_domain(row, "default", self.client))
        # Freshness selection cannot invent OSV evidence for the chosen version.
        self.assertFalse(roadmap._projected_row_security_full(row, "1.5.0")[-1])
        self.intent(objective=LATEST_APPROVED, approvedVersionLines={row.name: [2]})
        roadmap.apply_decision_contract_targets(rows, self.client)
        self.assertEqual("2.1.0", row.target_default)
        domain = roadmap._candidate_domain(row, "default", self.client)
        self.assertEqual({"1.0.0", "2.1.0"}, set(domain))
        self.intent(objective=LATEST_APPROVED, approvedVersionLines={row.name: []})
        self.assertEqual(["1.0.0"], roadmap._candidate_domain(row, "default", self.client))
        self.intent(objective=LATEST_APPROVED, approvedVersionLines={row.name: [2]})
        self.assertIn("USER_APPROVED_VERSION_LINE", roadmap._assignment_constraint_issue({row.name}, {row.name: "1.5.0"}, {row.name: row}, self.client, partial=False))

    def test_latest_final_projection_uses_exact_target_security_evidence(self):
        row = self.row()
        self.package(row.name, {"1.0.0": {}, "1.5.0": {}})
        rows = {"synthetic": [row]}
        health = roadmap.enrich_project_targets(rows, {})
        self.intent(objective=LATEST_APPROVED)
        roadmap.apply_decision_contract_targets(rows, self.client)
        self.assertEqual("1.5.0", row.target_default)
        for summary, expected in ((None, "unknown"), ("unknown", "unknown"), ("C:1", "blocked"), ("0", "feasible")):
            with self.subTest(summary=summary):
                row.vuln_evidence_by_version = {"1.0.0": "0"}
                if summary is not None:
                    row.vuln_evidence_by_version["1.5.0"] = summary
                plan = roadmap.build_draft_plan(rows, {}, health)
                projection = plan["proposals"][0]["health"]
                self.assertEqual(expected, projection["postPlanGoal"])
                if summary == "C:1":
                    self.assertEqual(1, projection["postPlanCritical"])
                if expected == "unknown":
                    self.assertEqual(1, projection["postPlanSecurityUnknown"])

    def test_latest_metadata_unknown_is_not_policy_satisfied_or_safe(self):
        row = self.row()
        self.intent(objective=LATEST_APPROVED)
        roadmap.apply_decision_contract_targets({"synthetic": [row]}, self.client)
        self.assertFalse(roadmap.target_is_action(row.target_default))
        self.assertIn("LATEST_CANDIDATE_UNKNOWN", row.objective_note)
        entry = roadmap.build_draft_plan({"synthetic": [row]}, {}, {"synthetic": roadmap.compute_project_health([row], "synthetic", None)})["proposals"][0]["rows"][0]
        self.assertIsNone(entry["objectiveCandidate"])
        self.assertIn("UNKNOWN", entry["objectiveNote"])

    def test_mutable_scope_keeps_provider_as_peer_constraint(self):
        consumer = self.row("sample-consumer", target="2.0.0")
        provider = self.row("sample-provider")
        self.package(consumer.name, {"1.0.0": {}, "2.0.0": {"peerDependencies": {provider.name: "^2.0.0"}}})
        self.package(provider.name, {"1.0.0": {}, "2.0.0": {}})
        self.intent(mutablePackages=[consumer.name])
        rows = {"synthetic": [consumer, provider]}
        roadmap._apply_baseline_intent_scope(rows)
        self.assertTrue(provider.planner_deferred)
        self.assertFalse(provider.scope_excluded)
        self.assertEqual(["1.0.0"], roadmap._candidate_domain(provider, "default", self.client))
        issue = roadmap._assignment_constraint_issue({consumer.name, provider.name}, {consumer.name: "2.0.0", provider.name: "1.0.0"}, {consumer.name: consumer, provider.name: provider}, self.client, partial=False)
        self.assertIn("PEER_CONFLICT", issue)
        self.intent(mutablePackages=[])
        self.assertEqual(["1.0.0"], roadmap._candidate_domain(consumer, "default", self.client))

    def test_storybook_compiler_uses_independent_release_and_keeps_peer_constraint(self):
        storybook = self.row("storybook", "10.0.0", "10.1.0")
        framework = self.row("@storybook/react-vite", "10.0.0", "10.1.0")
        compiler = self.row("@storybook/addon-webpack5-compiler-babel", "4.0.0", "4.1.0")
        self.package(storybook.name, {"10.0.0": {}, "10.1.0": {}})
        self.package(framework.name, {"10.0.0": {}, "10.1.0": {"peerDependencies": {"storybook": "^10.0.0"}}})
        self.package(compiler.name, {"4.0.0": {}, "4.1.0": {"peerDependencies": {"storybook": "^10.0.0"}}})
        rows = {"synthetic": [storybook, framework, compiler]}
        roadmap.enforce_storybook_cohort(rows, self.client)
        self.assertEqual(["10.1.0", "10.1.0", "4.1.0"], [r.target_default for r in rows["synthetic"]])
        self.assertFalse(compiler.compatibility_cohort)
        self.assertFalse(roadmap.is_storybook_core_package("@storybook/independently-published-addon"))
        assignment = {r.name: r.target_default for r in rows["synthetic"]}
        row_map = {r.name: r for r in rows["synthetic"]}
        self.assertEqual("", roadmap._assignment_constraint_issue(set(row_map), assignment, row_map, self.client, partial=False))
        self.client.npm_cache[compiler.name]["versions"]["4.1.0"]["peerDependencies"] = {"storybook": "^9.0.0"}
        self.assertIn("PEER_CONFLICT", roadmap._assignment_constraint_issue(set(row_map), assignment, row_map, self.client, partial=False))
        roadmap.capture_desired_targets(rows)
        roadmap.enrich_registry_target_evidence(rows, self.client)
        roadmap.resolve_peer_compatibility(rows, self.client, modes=("default",), apply_results=True, partial_on_incomplete=True)
        self.assertFalse(roadmap.target_is_action(compiler.target_default), "a real compiler peer conflict must still block that upgrade")

    def test_ru_and_en_prompts_use_same_authority_and_keep_migration_deliverables(self):
        plan = {"proposals": [], "unknowns": [], "conflicts": []}
        for language in ("ru", "en"):
            for mode in ("pinned", "advisory"):
                with self.subTest(language=language, mode=mode):
                    prompt = roadmap.build_draft_prompt("synthetic-run", "workspace", "project", "draft", "hash", plan, {}, language=language, snapshot={"intentJson": json.dumps({"targetMode": mode, "objective": LATEST_APPROVED})})
                    self.assertIn(f'"targetMode": "{mode}"', prompt)
                    self.assertIn("POLICY_SATISFIED", prompt)
                    self.assertIn("PLAN_ONLY", prompt)
                    self.assertIn("EXECUTE", prompt)
                    if language == "ru":
                        self.assertIn("без install, выполнения проверок проекта, изменений проекта", prompt)
                        self.assertIn("применяются только к уже разрешённому пользователем EXECUTE", prompt)
                        self.assertIn("не требует механического повтора идентичного MCP-запроса", prompt)
                        self.assertIn("не являются пользовательским pin", prompt)
                    else:
                        self.assertIn("without install, project checks, project edits", prompt)
                        self.assertIn("instructions below apply only to user-authorized EXECUTE", prompt)
                        self.assertIn("does not require mechanically repeating an identical MCP request", prompt)
                        self.assertIn("are not user pins", prompt)
                    self.assertIn("UNKNOWN", prompt)
                    self.assertIn("manual_dependency_audit.py", prompt)
                    self.assertIn("DEVELOPER_UPGRADE_GUIDE.md", prompt)
                    self.assertIn("MIGRATION_REPORT.md", prompt)
                    self.assertNotIn("only as a starting point", prompt)
                    self.assertNotIn("только как отправную точку", prompt)
                    if mode == "advisory":
                        self.assertIn("decision diff", prompt)
                        self.assertIn("advisory:", prompt)

    def test_manifest_echo_and_semantic_identity_use_captured_inputs(self):
        previous = list(roadmap._ARTIFACTS_BASE_FOR_DRAFT)
        self.addCleanup(lambda: setattr(roadmap, "_ARTIFACTS_BASE_FOR_DRAFT", previous))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "package.json").write_text('{"name":"synthetic","packageManager":"npm@10.0.0"}', encoding="utf-8")
            (path / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}', encoding="utf-8")
            spec = roadmap.ProjectSpec("synthetic", path)
            spec.draft_semantic_inputs = capture_draft_semantic_inputs(path)
            captured = spec.draft_semantic_inputs["semanticInputHash"]
            (path / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{"changed":{}}}', encoding="utf-8")
            roadmap.set_draft_artifacts_base(path / "artifacts")
            snapshot = {"intentJson": json.dumps({"targetMode": "advisory", "mutablePackages": [], "objective": LATEST_APPROVED}), "draftMaxCandidates": "32"}
            manifest = roadmap.publish_draft_result(run_id="synthetic-run", workspace_id="workspace", project_id="synthetic", mode="draft", rows_by_project={"synthetic": [self.row()]}, projects_by_name={"synthetic": spec}, health_by_project={"synthetic": roadmap.compute_project_health([self.row()], "synthetic", None)}, status="DRAFT_READY", partial_reason=None, deadline=roadmap.DeadlineClock(None), input_hashes={"synthetic": "legacy-preserved"}, settings_snapshot=snapshot)
            self.assertEqual({"synthetic": "legacy-preserved"}, manifest["inputHashes"])
            self.assertEqual(captured, manifest["semanticInputsByProject"]["synthetic"]["semanticInputHash"])
            self.assertEqual(normalize_decision_contract(json.loads(snapshot["intentJson"])), manifest["decisionContract"])
            self.assertEqual("NOT_PROVEN", manifest["objectiveEvidence"]["freshnessOptimality"])
            self.assertEqual(32, manifest["planningOptions"]["draftMaxCandidates"])
            self.assertIsNone(manifest["planningOptions"]["staticSolverDomainCap"])
            self.assertEqual("PLANNING_ONLY", manifest["authority"])
            persisted_plan = json.loads(Path(manifest["artifacts"]["plan"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["decisionContract"], persisted_plan["decisionContract"])


if __name__ == "__main__":
    unittest.main()
