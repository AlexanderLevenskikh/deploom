"""Git/filesystem delivery boundaries; no real migrated project or agent used."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import iterative_delivery as d
import iterative_migration as core
from iterative_task import _targets_satisfied


def report(critical=0):
    return {"generatedAt": datetime.now(timezone.utc).isoformat(), "dependencyInputHash": "fixture",
            "auditComplete": True, "lagComplete": True,
            "policy": {"targetLevel": "yellow", "maxKnownCritical": 0, "maxKnownHigh": 1, "minLagOkPct": 80, "lagPolicyMonths": 12},
            "audit": {"engine": "yarn-inventory", "trusted": True, "canonicalInventory": {"complete": True},
                      "lagOkPct": 90, "lagOk": 9, "lagTotal": 10, "lagUnknown": 0,
                      "packageTotals": {"critical": critical, "high": 0, "moderate": 0, "low": 0, "unknown": 0},
                      "advisoryTotals": {"unknown": 0}}}


class SelectedGoalTests(unittest.TestCase):
    def test_auto_proposals_do_not_create_false_goals_required_versions_do(self):
        config = {"goalMode": "audit-policy", "targets": {"auto": "9.0.0", "required": "2.0.0"},
                  "packagePolicies": {"auto": "auto", "required": "required"}}
        checkpoint = {"fullAssignment": {"auto": "1.0.0", "required": "2.0.0"}, "audit": {"status": "PASS"}}
        self.assertTrue(core._policy_satisfied(config, checkpoint))
        self.assertTrue(_targets_satisfied(config, checkpoint)[0])
        checkpoint["fullAssignment"]["required"] = "1.0.0"
        self.assertFalse(core._policy_satisfied(config, checkpoint))
        self.assertFalse(_targets_satisfied(config, checkpoint)[0])
        checkpoint["audit"]["status"] = "UNKNOWN"
        self.assertFalse(core._policy_satisfied(config, checkpoint))

    def test_keep_current_guard_runs_before_checkpoint_capture(self):
        with patch.object(core, "capture_durable_source_snapshot") as capture:
            with self.assertRaisesRegex(core.InvalidInputError, "KEEP_CURRENT_CHANGED"):
                core._accept_checkpoint(Path("unused"), {}, {"packagePolicies": {"frozen": "keep-current"}},
                                        {"fullAssignment": {"frozen": "2.0.0"}},
                                        {"fullAssignment": {"frozen": "1.0.0"}}, None)
            capture.assert_not_called()


@unittest.skipUnless(shutil.which("git"), "Git required")
class GitDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deploom-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "original"; self.repo.mkdir()
        self.run_dir = self.root / "run"; self.run_dir.mkdir()
        (self.repo / "package.json").write_text('{"name":"delivery-fixture","version":"1.0.0","private":true}', encoding="utf-8")
        (self.repo / "package-lock.json").write_text('{"name":"delivery-fixture","version":"1.0.0","lockfileVersion":3,"packages":{"":{"name":"delivery-fixture","version":"1.0.0"}}}', encoding="utf-8")
        (self.repo / ".gitignore").write_text("node_modules/\n.dependency-roadmap/\n", encoding="utf-8")
        (self.repo / "check.cjs").write_text("if (require('fs').readFileSync('source.txt','utf8') !== 'upgraded') process.exit(1)", encoding="utf-8")
        (self.repo / "source.txt").write_text("original", encoding="utf-8")
        d.git(self.repo, "init", "-b", "fixture-base")
        d.git(self.repo, "config", "user.email", "fixture@example.invalid")
        d.git(self.repo, "config", "user.name", "Delivery Fixture")
        d.git(self.repo, "add", "."); d.git(self.repo, "commit", "-m", "fixture base")
        self.head = d.git(self.repo, "rev-parse", "HEAD")
        self.source = self.root / "snapshot"
        shutil.copytree(self.repo, self.source)
        (self.source / "source.txt").write_text("upgraded", encoding="utf-8")
        (self.source / "fixtures").mkdir()
        (self.source / "fixtures" / "important.txt").write_text("preserved fixture", encoding="utf-8")
        (self.source / "scratch.log").write_text("exclude scratch", encoding="utf-8")
        self.run = {"schemaVersion": core.SCHEMA_VERSION, "runId": "delivery-fixture", "phase": "TERMINAL", "activeCheckpointId": "C1", "activeCandidateId": None}
        self.config = {"schemaVersion": core.SCHEMA_VERSION, "projectDir": str(self.repo), "projectName": "fixture", "runtime": {},
                       "verifyConfig": {"commands": ["node check.cjs"], "projectChecks": "strict"}, "goalMode": "audit-policy", "targets": {}, "auditPolicy": {}}
        self.checkpoint = {"schemaVersion": core.SCHEMA_VERSION, "checkpointId": "C1", "seq": 1, "status": "VERIFIED", "sourceSnapshotKey": "fixture-key", "sourceHead": self.head, "projectRelative": ".", "fullAssignment": {}, "audit": {"status": "FAIL"}}
        core.save_run(self.run_dir, self.run); core.save_config(self.run_dir, self.config)
        core.save_checkpoint(self.run_dir, self.checkpoint)
        (self.run_dir / "reports").mkdir()
        for name in ("MIGRATION_REPORT.md", "DEVELOPER_UPGRADE_GUIDE.md"):
            (self.run_dir / "reports" / name).write_text("fixture deliverable", encoding="utf-8")
        self.snapshot = patch.object(core, "_open_checkpoint_source", return_value=SimpleNamespace(root=self.source))
        self.snapshot.start(); self.addCleanup(self.snapshot.stop)

    def security_fixture(self):
        root = self.run_dir / "security" / "attempt-1" / "workspace"
        root.parent.mkdir(parents=True)
        shutil.copytree(self.source, root)
        self.checkpoint["audit"] = d.summary(report(critical=1), self.run_dir / "audit" / "C1", "C1")
        core.save_checkpoint(self.run_dir, self.checkpoint)
        state = {"schemaVersion": 1, "runId": self.run["runId"], "baseCheckpointId": "C1", "attempt": 1, "status": "ready",
                 "sourceSnapshotKey": "fixture-key", "workspaceRoot": str(root), "projectRelative": ".",
                 "before": d.inventory(root), "sourceHead": self.head, "scripts": {}, "beforeAudit": self.checkpoint["audit"],
                 "originalInput": list(core.manifest_and_lock_hashes(self.repo)[:2])}
        core._write_json_atomic(self.run_dir / "security-state.json", state)
        return root

    def test_security_cannot_change_check_scripts_or_direct_scope(self):
        root = self.security_fixture()
        manifest = d.read(root / "package.json"); manifest["scripts"] = {"test": "echo skipped"}
        (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        with patch.object(core, "verify_assignment") as verify:
            with self.assertRaisesRegex(RuntimeError, "CHECK_COMMANDS_CHANGED"):
                d.security_verify(self.run_dir, {})
            verify.assert_not_called()
        manifest.pop("scripts"); manifest["dependencies"] = {"shell-quote": "1.11.0"}
        (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "DIRECT_SCOPE_CHANGED"):
            d.security_verify(self.run_dir, {})

    def test_security_original_input_guard_precedes_verification(self):
        self.security_fixture()
        (self.repo / "package.json").write_text('{"changed":true}', encoding="utf-8")
        with patch.object(core, "verify_assignment") as verify:
            with self.assertRaisesRegex(RuntimeError, "ORIGINAL_INPUT_CHANGED"):
                d.security_verify(self.run_dir, {})
            verify.assert_not_called()

    def test_security_unknown_or_worse_audit_never_replaces_checkpoint(self):
        self.security_fixture()
        for evidence in ({**report(), "auditComplete": False}, report(critical=2)):
            state = d.read(self.run_dir / "security-state.json"); state["status"] = "ready"
            core._write_json_atomic(self.run_dir / "security-state.json", state)
            with patch.object(core, "verify_assignment", return_value=SimpleNamespace(ok=True)), patch.object(d, "collect_audit", return_value=evidence):
                result = d.security_verify(self.run_dir, {})
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(core.load_run(self.run_dir)["activeCheckpointId"], "C1")
        self.assert_original_preserved()

    @unittest.skipUnless(shutil.which("node") and (shutil.which("npm") or shutil.which("npm.cmd")), "Node/npm required")
    def test_security_acceptance_recovery_closes_same_real_snapshot_without_paid_repair(self):
        self.security_fixture()
        real_save = core.save_checkpoint
        def interrupt_audit_save(run_dir, checkpoint):
            if checkpoint.get("checkpointId") == "C2" and checkpoint.get("audit", {}).get("status") == "PASS":
                raise OSError("crash after pointer adoption")
            return real_save(run_dir, checkpoint)
        with patch.object(d, "collect_audit", return_value=report()), patch.object(core, "save_checkpoint", side_effect=interrupt_audit_save):
            with self.assertRaisesRegex(OSError, "pointer adoption"):
                d.security_verify(self.run_dir, {})
        self.assertEqual(core.load_run(self.run_dir)["activeCheckpointId"], "C2")
        self.assertEqual(d.read(self.run_dir / "security-state.json")["status"], "accepting")
        self.snapshot.stop()  # recovery opens the REAL newly sealed C2 snapshot
        with patch.object(core, "verify_assignment") as verifier, patch.object(core, "_finish_locked"):
            recovered = d.security_verify(self.run_dir, {})
            verifier.assert_not_called()
        self.assertEqual(recovered["status"], "accepted")
        self.assertTrue(recovered["goalSatisfied"])
        self.assertEqual(recovered["checkpointId"], "C2")
        self.assertFalse((self.run_dir / "checkpoints" / "C3.json").exists())
        self.assert_original_preserved()

    @unittest.skipUnless(shutil.which("node") and (shutil.which("npm") or shutil.which("npm.cmd")), "Node/npm required")
    def test_security_crash_after_sealing_preserves_orphan_and_reverifies_same_attempt(self):
        self.security_fixture()
        real_save = core.save_checkpoint
        def interrupt_checkpoint_save(run_dir, checkpoint):
            if checkpoint.get("checkpointId") == "C2":
                raise OSError("crash after sealing")
            return real_save(run_dir, checkpoint)
        with patch.object(d, "collect_audit", side_effect=lambda *args: report()), patch.object(core, "save_checkpoint", side_effect=interrupt_checkpoint_save):
            with self.assertRaisesRegex(OSError, "after sealing"):
                d.security_verify(self.run_dir, {})
        self.assertEqual(core.load_run(self.run_dir)["activeCheckpointId"], "C1")
        self.assertTrue((self.run_dir / core.SOURCE_DIR / "C2").is_dir())
        with patch.object(d, "collect_audit", side_effect=lambda *args: report()), patch.object(core, "_finish_locked"):
            result = d.security_verify(self.run_dir, {})
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(result["checkpointId"], "C2")
        self.assertEqual(len(result["sourceOrphans"]), 1)
        self.assertTrue(Path(result["sourceOrphans"][0]).is_dir())
        self.assert_original_preserved()

    def prepare(self):
        return d.delivery_prepare(self.run_dir, {"branch": "codex/fixture-delivery"})

    def assert_original_preserved(self):
        self.assertEqual(d.git(self.repo, "branch", "--show-current"), "fixture-base")
        self.assertEqual(d.git(self.repo, "rev-parse", "HEAD"), self.head)
        self.assertEqual((self.repo / "source.txt").read_text(), "original")
        self.assertEqual(d.git(self.repo, "status", "--porcelain"), "")

    def test_registered_branch_preserves_original_and_text_fixtures(self):
        state = self.prepare(); root = Path(state["workspaceRoot"])
        self.assertEqual(state["status"], "ready")
        self.assertEqual((root / "fixtures" / "important.txt").read_text(), "preserved fixture")
        self.assertFalse((root / "scratch.log").exists())
        self.assert_original_preserved()
        self.assertEqual(self.prepare(), state)
        self.assertIn("branch refs/heads/codex/fixture-delivery", d.git(self.repo, "worktree", "list", "--porcelain"))

    def test_selection_audit_cache_tracks_dependencies_policy_and_resolved_runtime(self):
        inputs = {"projectDir": str(self.repo), "projectName": "fixture", "intent": {}, "requestedNode": "22"}
        runtime = SimpleNamespace(found=True, node_path="fixture-node", effective_version="22.18.0")
        with patch("project_runtime.resolve_requested_node", return_value=runtime), patch.object(d, "collect_audit", side_effect=lambda *args: report()) as collect:
            first = d.current_audit(self.run_dir, inputs)
            self.assertEqual(d.current_audit(self.run_dir, inputs), first)
            self.assertEqual(collect.call_count, 1)
            runtime.effective_version = "22.20.0"
            d.current_audit(self.run_dir, inputs)
            self.assertEqual(collect.call_count, 2)
            inputs["intent"] = {"acceptancePolicy": {"maxKnownHigh": 0}}
            d.current_audit(self.run_dir, inputs)
            self.assertEqual(collect.call_count, 3)
            (self.repo / "package-lock.json").write_text('{"changed":true}', encoding="utf-8")
            d.current_audit(self.run_dir, inputs)
            self.assertEqual(collect.call_count, 4)

    def test_unknown_selection_evidence_is_not_reused_as_a_success_cache(self):
        inputs = {"projectDir": str(self.repo), "projectName": "fixture", "intent": {}}
        with patch.object(d, "collect_audit", return_value={**report(), "lagComplete": False}) as collect:
            self.assertEqual(d.current_audit(self.run_dir, inputs)["status"], "UNKNOWN")
            self.assertEqual(d.current_audit(self.run_dir, inputs)["status"], "UNKNOWN")
            self.assertEqual(collect.call_count, 2)

    def test_saved_intent_thresholds_and_lag_overrides_are_pinned(self):
        intent = self.root / "intent.json"
        targets = self.root / "targets.json"
        dashboard = self.root / "dashboard.json"
        intent.write_text(json.dumps({"policies": {"frozen": "keep-current", "required": "required"},
                                     "acceptancePolicy": {"lagPolicyMonths": 6, "minLagOkPct": 95, "maxKnownHigh": 0, "maxKnownModerate": 2}}), encoding="utf-8")
        targets.write_text(json.dumps({"frozen": "2.0.0", "required": "2.0.0"}), encoding="utf-8")
        dashboard.write_text(json.dumps({"packageOverrides": {"fixture": {"required": {"lagMonths": 3}}}}), encoding="utf-8")
        args = core.build_parser().parse_args(["--run-dir", str(self.run_dir), "begin", "--project-dir", str(self.repo),
                    "--project-name", "fixture", "--intent-file", str(intent), "--targets-file", str(targets), "--dashboard-state", str(dashboard)])
        config = core.build_run_config(self.run_dir, args, discover=False)
        self.assertEqual(config["goalMode"], "audit-policy")
        self.assertNotIn("frozen", config["targets"])
        self.assertEqual(config["auditPolicy"], {"lagMonths": 6, "minLagOkPct": 95, "maxKnownHigh": 0, "maxKnownModerate": 2})
        dashboard.write_text("{}", encoding="utf-8")
        self.assertEqual(d.read(config["dashboardState"])["packageOverrides"]["fixture"]["required"]["lagMonths"], 3)

    def test_restart_archives_state_without_moving_registered_worktree(self):
        from iterative_restart import archive_for_restart
        state = self.prepare(); root = Path(state["workspaceRoot"])
        archived = archive_for_restart(self.run_dir, core.LOCK_FILENAME, core._write_json_atomic)
        self.assertTrue(archived["archived"])
        self.assertTrue(root.is_dir())
        self.assertEqual(d.git(root, "branch", "--show-current"), "codex/fixture-delivery")
        self.assertIn(f"worktree {root.resolve().as_posix()}", d.git(self.repo, "worktree", "list", "--porcelain"))
        self.assert_original_preserved()

    def test_restart_preserves_inflight_final_session(self):
        from iterative_restart import archive_for_restart, RestartArchiveError
        state = self.prepare()
        core._write_json_atomic(self.run_dir / "delivery-agent.json", {"status": "running", "sessionId": "pending"})
        with self.assertRaisesRegex(RestartArchiveError, "RESTART_AGENT_PENDING"):
            archive_for_restart(self.run_dir, core.LOCK_FILENAME, core._write_json_atomic)
        self.assertEqual(d.read(self.run_dir / "delivery-state.json"), state)
        self.assertTrue((self.run_dir / "run.json").is_file())

    def test_existing_unused_branch_at_base_is_reused(self):
        d.git(self.repo, "branch", "codex/fixture-delivery")
        state = self.prepare()
        self.assertTrue(state["reuseBranch"])
        self.assertEqual(state["branch"], "codex/fixture-delivery")
        self.assertEqual(d.git(self.repo, "rev-parse", "codex/fixture-delivery"), self.head)
        self.assertEqual(self.prepare()["workspaceRoot"], state["workspaceRoot"])
        self.assert_original_preserved()

    def test_existing_branch_with_other_commits_gets_automatic_suffix(self):
        d.git(self.repo, "branch", "codex/fixture-delivery")
        other = self.root / "other"
        d.git(self.repo, "worktree", "add", str(other), "codex/fixture-delivery")
        (other / "user.txt").write_text("user work", encoding="utf-8")
        d.git(other, "add", "."); d.git(other, "commit", "-m", "unrelated user work")
        user_head = d.git(other, "rev-parse", "HEAD")
        state = self.prepare()
        self.assertNotEqual(state["branch"], state["requestedBranch"])
        self.assertEqual(state["branchResolution"], "configured-branch-preserved")
        self.assertEqual(d.git(other, "rev-parse", "HEAD"), user_head)
        self.assertEqual((other / "user.txt").read_text(), "user work")
        self.assertEqual(self.prepare()["branch"], state["branch"])
        self.assert_original_preserved()

    def test_checked_out_base_branch_gets_suffix_and_collisions_are_skipped(self):
        requested = "fixture-base"
        first, reuse, reason = d.select_delivery_branch(self.repo, requested, self.head, self.run["runId"])
        self.assertFalse(reuse); self.assertEqual(reason, "configured-branch-preserved")
        d.git(self.repo, "branch", first)
        second, reuse, _ = d.select_delivery_branch(self.repo, requested, self.head, self.run["runId"])
        self.assertEqual(second, first + "-2")
        self.assertEqual(d.git(self.repo, "branch", "--show-current"), requested)
        d.git(self.repo, "branch", "namespace")
        third, _, _ = d.select_delivery_branch(self.repo, "namespace/result", self.head, self.run["runId"])
        self.assertFalse(third.startswith("namespace/"))
        d.git(self.repo, "check-ref-format", "--branch", third)

    def test_reused_branch_moving_before_worktree_creation_is_preserved(self):
        d.git(self.repo, "branch", "codex/fixture-delivery")
        with patch.object(d, "resume_delivery_preparation", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError): self.prepare()
        tree = d.git(self.repo, "rev-parse", "HEAD^{tree}")
        result = __import__("subprocess").run(["git", "-C", str(self.repo), "commit-tree", tree, "-p", self.head, "-m", "concurrent work"], capture_output=True, text=True, check=True)
        moved = result.stdout.strip()
        d.git(self.repo, "update-ref", "refs/heads/codex/fixture-delivery", moved, self.head)
        with self.assertRaisesRegex(RuntimeError, "DELIVERY_BRANCH_CHANGED"):
            self.prepare()
        self.assertEqual(d.git(self.repo, "rev-parse", "codex/fixture-delivery"), moved)
        self.assert_original_preserved()

    def test_ignored_source_inputs_stop_before_worktree_creation(self):
        (self.source / ".gitignore").write_text("node_modules/\n.private-config.json\n", encoding="utf-8")
        (self.source / ".private-config.json").write_text('{"checkInput":true}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "DELIVERY_IGNORED_SOURCE_INPUTS"):
            self.prepare()
        self.assertFalse(d.delivery_root(self.run_dir, self.run["runId"]).exists())
        self.assertFalse((self.run_dir / "delivery-state.json").exists())
        self.assert_original_preserved()

    def test_interrupted_copy_resumes_without_recreating_branch(self):
        original_copy = d.shutil.copy2
        count = 0
        def fail_copy(src, dst):
            nonlocal count
            original_copy(src, dst); count += 1
            if count == 2: raise OSError("injected copy interruption")
        with patch.object(d.shutil, "copy2", side_effect=fail_copy):
            with self.assertRaisesRegex(OSError, "injected"):
                self.prepare()
        self.assertEqual(d.read(self.run_dir / "delivery-state.json")["status"], "preparing")
        state = self.prepare()
        self.assertEqual(state["status"], "ready")
        self.assert_original_preserved()

    def test_interrupted_copy_preserves_unrelated_new_edits(self):
        with patch.object(d.shutil, "copy2", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError): self.prepare()
        state = d.read(self.run_dir / "delivery-state.json")
        target = Path(state["workspaceRoot"]) / "source.txt"
        target.write_text("user edit", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "PREPARATION_FILES_CHANGED"):
            self.prepare()
        self.assertEqual(target.read_text(), "user edit")

    def test_agent_commit_cannot_change_verified_bytes(self):
        state = self.prepare(); root = Path(state["workspaceRoot"])
        (root / "source.txt").write_text("agent silently changed it", encoding="utf-8")
        d.git(root, "add", "."); d.git(root, "commit", "-m", "incorrect grouped changes")
        with patch.object(core, "verify_assignment") as verifier:
            with self.assertRaisesRegex(RuntimeError, "DELIVERY_SOURCE_CHANGED"):
                d.delivery_verify(self.run_dir, {})
            verifier.assert_not_called()
        self.assert_original_preserved()

    @unittest.skipUnless(shutil.which("node") and (shutil.which("npm") or shutil.which("npm.cmd")), "Node/npm required")
    def test_committed_result_runs_real_configured_check_and_repeat_audit(self):
        state = self.prepare(); root = Path(state["workspaceRoot"])
        guide = root / "docs" / "dependency-migration" / self.run["runId"] / "DEVELOPER_UPGRADE_GUIDE.md"
        guide.write_text("Actual installed upgrades: this fixture has no dependencies. Source adaptation and the configured Node check are verified; advisory collection uses deterministic fixture evidence.", encoding="utf-8")
        d.git(root, "add", "."); d.git(root, "commit", "-m", "chore(deps): coherent upgrade and adaptation")
        with patch.object(d, "collect_audit", return_value=report(critical=1)) as audit, patch.object(core, "_finish_locked") as finish:
            result = d.delivery_verify(self.run_dir, {})
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["audit"]["status"], "FAIL")
        self.assertEqual(core.load_checkpoint(self.run_dir, "C1")["audit"]["status"], "FAIL")
        self.assertEqual(audit.call_args.args[0], Path(result["verificationWorkspace"]))
        self.assertEqual(d.git(Path(result["verificationWorkspace"]), "rev-parse", "HEAD"), result["head"])
        self.assertNotIn("terminalOutcome", finish.call_args.args[1])
        self.assertEqual(len(result["commits"]), 1)
        self.assertEqual(len(result["documentationHashes"]), 2)
        self.assert_original_preserved()


if __name__ == "__main__":
    unittest.main()
