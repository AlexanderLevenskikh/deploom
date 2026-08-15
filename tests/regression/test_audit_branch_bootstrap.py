from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_audit_branch as branch


@unittest.skipUnless(shutil.which("git"), "git is required")
class AuditBranchBootstrapRegressionTests(unittest.TestCase):
    def git(self, project: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(project), *args], text=True, encoding="utf-8", capture_output=True, check=False)
        if result.returncode != 0:
            self.fail(result.stdout + result.stderr)
        return result.stdout.strip()

    def fake_report(self, **kwargs):
        workspace = Path(kwargs["audit_workspace"])
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".dependency-roadmap-audit-workspace.json").write_text(json.dumps({"managedBy": "dependency-roadmap-tool"}), encoding="utf-8")
        (workspace / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {}}), encoding="utf-8")
        (workspace / "npm-audit.json").write_text(json.dumps({"metadata": {"vulnerabilities": {}}}), encoding="utf-8")
        return {
            "schemaVersion": 2,
            "generatedAt": "2026-07-13T00:00:00+00:00",
            "projectName": kwargs["project_name"],
            "projectDir": ".",
            "registry": kwargs["registry"],
            "packageManager": "yarn",
            "directDeclarations": 0,
            "audit": {
                "engine": "npm-lock-bridge",
                "effectiveRegistry": kwargs["registry"],
                "requestedRegistry": kwargs["registry"],
                "complete": True,
                "packages": {},
                "totals": {"critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0},
                "command": ["npm", "audit"],
                "exitCode": 0,
                "notes": [],
            },
            "lag": [],
            "complete": True,
        }

    def test_prepare_commits_isolated_evidence_and_restores_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
            self.git(project, "config", "user.name", "Roadmap Test")
            self.git(project, "config", "user.email", "roadmap@example.test")
            self.git(project, "checkout", "-b", "master")
            (project / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
            (project / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
            self.git(project, "add", "package.json", "yarn.lock")
            self.git(project, "commit", "-m", "initial")
            source_commit = self.git(project, "rev-parse", "HEAD")

            with patch.object(branch, "build_report", side_effect=self.fake_report):
                result = branch.prepare_audit_branch(
                    project=project,
                    project_name="Demo",
                    registry="https://nexus.example/repository/npm",
                    source_remote="origin",
                    source_branch="master",
                    source_commit=source_commit,
                    audit_branch="libs-audit",
                )

            self.assertTrue(result["prepared"])
            self.assertEqual("master", self.git(project, "branch", "--show-current"))
            self.assertEqual(source_commit, self.git(project, "rev-parse", "HEAD"))
            self.assertFalse((project / "package-lock.json").exists())
            manifest = json.loads(self.git(project, "show", "libs-audit:.dependency-roadmap-audit/branch-manifest.json"))
            self.assertEqual("npm-lock-bridge", manifest["auditEngine"])
            self.assertEqual(source_commit, manifest["sourceCommit"])
            self.assertEqual(result["commit"], self.git(project, "rev-parse", "libs-audit"))

    def test_prepare_skips_failing_project_pre_commit_hook_and_restores_clean_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
            self.git(project, "config", "user.name", "Roadmap Test")
            self.git(project, "config", "user.email", "roadmap@example.test")
            self.git(project, "checkout", "-b", "master")
            (project / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
            (project / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
            self.git(project, "add", "package.json", "yarn.lock")
            self.git(project, "commit", "-m", "initial")
            source_commit = self.git(project, "rev-parse", "HEAD")

            hook = project / ".git" / "hooks" / "pre-commit"
            hook.write_text("#!/bin/sh\necho should-not-run > \"$(git rev-parse --git-dir)/audit-hook-ran.txt\"\nexit 77\n", encoding="utf-8", newline="\n")
            hook.chmod(0o755)

            with patch.object(branch, "build_report", side_effect=self.fake_report):
                result = branch.prepare_audit_branch(
                    project=project,
                    project_name="Demo",
                    registry="https://nexus.example/repository/npm",
                    source_remote="origin",
                    source_branch="master",
                    source_commit=source_commit,
                    audit_branch="libs",
                )

            self.assertTrue(result["intermediateHooksSkipped"])
            self.assertFalse((project / ".git" / "audit-hook-ran.txt").exists())
            self.assertEqual("master", self.git(project, "branch", "--show-current"))
            self.assertEqual("", self.git(project, "status", "--porcelain"))


    def test_final_capture_then_cleanup_removes_workspace_from_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
            self.git(project, "config", "user.name", "Roadmap Test")
            self.git(project, "config", "user.email", "roadmap@example.test")
            self.git(project, "checkout", "-b", "merged")
            (project / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
            (project / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
            self.git(project, "add", "package.json", "yarn.lock")
            self.git(project, "commit", "-m", "initial")

            with patch.object(branch, "build_report", side_effect=self.fake_report):
                captured = branch.capture_current_branch(project, "Demo", "https://nexus.example/repository/npm")
            self.assertTrue((project / ".dependency-roadmap-audit" / "package-lock.json").exists())
            cleaned = branch.cleanup_current_branch(project)
            self.assertTrue(cleaned["cleaned"])
            self.assertFalse((project / ".dependency-roadmap-audit").exists())
            self.assertNotEqual(captured["commit"], cleaned["commit"])
            self.assertEqual("", self.git(project, "status", "--porcelain"))

    def test_prepare_reuses_completed_audit_for_same_source_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
            self.git(project, "config", "user.name", "Roadmap Test")
            self.git(project, "config", "user.email", "roadmap@example.test")
            self.git(project, "checkout", "-b", "master")
            (project / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
            (project / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
            self.git(project, "add", "package.json", "yarn.lock")
            self.git(project, "commit", "-m", "initial")
            source_commit = self.git(project, "rev-parse", "HEAD")

            with patch.object(branch, "build_report", side_effect=self.fake_report) as report:
                first = branch.prepare_audit_branch(
                    project=project,
                    project_name="Demo",
                    registry="https://nexus.example/repository/npm",
                    source_remote="origin",
                    source_branch="master",
                    source_commit=source_commit,
                    audit_branch="libs-audit",
                )
                second = branch.prepare_audit_branch(
                    project=project,
                    project_name="Demo",
                    registry="https://nexus.example/repository/npm",
                    source_remote="origin",
                    source_branch="master",
                    source_commit=source_commit,
                    audit_branch="libs-audit",
                )

            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["commit"], second["commit"])
            self.assertEqual(1, report.call_count)
            self.assertEqual("master", self.git(project, "branch", "--show-current"))

    def test_existing_clean_base_branch_can_be_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
            self.git(project, "config", "user.name", "Roadmap Test")
            self.git(project, "config", "user.email", "roadmap@example.test")
            self.git(project, "checkout", "-b", "master")
            (project / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
            (project / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
            self.git(project, "add", "package.json", "yarn.lock")
            self.git(project, "commit", "-m", "initial")
            self.git(project, "branch", "libs")
            source_commit = self.git(project, "rev-parse", "HEAD")

            with patch.object(branch, "build_report", side_effect=self.fake_report):
                result = branch.prepare_audit_branch(
                    project=project,
                    project_name="Demo",
                    registry="https://nexus.example/repository/npm",
                    source_remote="origin",
                    source_branch="master",
                    source_commit=source_commit,
                    audit_branch="libs",
                )

            self.assertEqual("libs", result["branch"])
            self.assertEqual("master", self.git(project, "branch", "--show-current"))
            self.assertEqual(result["commit"], self.git(project, "rev-parse", "libs"))


if __name__ == "__main__":
    unittest.main()
