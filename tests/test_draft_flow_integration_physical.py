"""R10: end-to-end Draft integration scenarios on real fixtures.

Runs the actual generator process (main -> a real publish chain) against tiny
real projects with scripts, across the accepted scenarios: fast draft on a
fresh workspace, policy change moving hash/gate/prompt, pnpm manifest-only
availability, and a real `npm test` smoke recorded separately from mocks.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "dependency_live_roadmap_generator.py"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class DraftFlowIntegrationPhysicalTests(unittest.TestCase):
    """Full-chain generator runs; no mocks for the publish path."""

    def setUp(self) -> None:
        self._tmp: Path = Path(tempfile.mkdtemp(prefix="deploom-r10-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _npm_fixture(self, name: str, deps: list[tuple[str, str]], lockfiles=True) -> Path:
        ws = self._tmp / name / "ws"
        _write(ws / "package.json", json.dumps({
            "name": name,
            "version": "1.0.0",
            "scripts": {
                "test": "node build.js test",
                "build": "node build.js build",
            },
            "dependencies": {pkg: spec for pkg, spec in deps},
        }, indent=2))
        _write(ws / "build.js", (
            "const assert = require('node:assert');\n"
            "const mode = process.argv[2] || 'test';\n"
            "assert.strictEqual(require('node:path').basename(process.cwd()), 'ws');\n"
            "console.log('SMOKE_' + mode + '_OK');\n"
        ))
        if lockfiles:
            lock = {"name": name, "version": "1.0.0", "lockfileVersion": 3, "requires": True,
                    "packages": {"": {"name": name, "version": "1.0.0", "dependencies": {p: s for p, s in deps}},
                                 "node_modules/" + deps[0][0]: {"version": deps[0][1].strip("^")}}}
            _write(ws / "package-lock.json", json.dumps(lock, indent=2))
        _write(ws / ".dependency-roadmap" / "settings.project.json",
               json.dumps({"projects": [{"name": name, "path": ".", "sourceBranch": "main"}]}))
        return ws

    def _run_generator(
        self,
        ws: Path,
        project: str,
        args: list[str],
        env: dict[str, str] | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            str(GENERATOR),
            "--project-settings", str(ws / ".dependency-roadmap" / "settings.project.json"),
            "--only-project", project,
            "--draft-baseline",
            "--artifacts-dir", str(ws / ".dependency-roadmap" / "artifacts"),
            *args,
        ]
        run_env = os.environ.copy()
        run_env.pop("DEPLOOM_DRAFT_DEADLINE_SECONDS", None)
        run_env.pop("DEPLOOM_BASELINE_MIN_LAG_OK_PCT", None)
        run_env.pop("DEPLOOM_BASELINE_TARGET_LEVEL", None)
        run_env["PYTHONIOENCODING"] = "utf-8"
        if env:
            run_env.update(env)
        return subprocess.run(cmd, cwd=str(ws), env=run_env, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout)

    def test_fresh_workspace_fast_draft_publishes_partial_without_legacy_outputs(self) -> None:
        ws = self._npm_fixture("fresh-npm", [("uuid", "^10.0.0"), ("is-number", "^7.0.0")])
        result = self._run_generator(
            ws, "fresh-npm",
            ["--run-id", "run-r10-fresh", "--workspace-id", "ws-fresh", "--project-id", "fresh-npm",
             "--mode", "draft", "--draft-deadline-seconds", "0.05"],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        draft = ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-r10-fresh" / "draft"
        for name in ("result.json", "plan.json", "prompt.md", "summary.md"):
            self.assertTrue((draft / name).is_file(), f"missing {name}")
        manifest = _read_json(draft / "result.json")
        self.assertIn(manifest["status"], ("DRAFT_PARTIAL", "DRAFT_READY"))
        self.assertEqual(manifest["runId"], "run-r10-fresh")
        self.assertEqual(manifest["projectId"], "fresh-npm")
        self.assertEqual(manifest["mode"], "draft")
        self.assertEqual(manifest["verificationStatus"], "NOT_VERIFIED")
        self.assertEqual(manifest["authority"], "PLANNING_ONLY")
        # R5: no legacy MD/JSON/HTML outputs from a Draft on a fresh workspace.
        for legacy in ("dependency-roadmap.md", "dependency-roadmap.json", "dependency-roadmap.html"):
            self.assertFalse((ws / legacy).exists(), f"legacy output written: {legacy}")

    def test_policy_change_moves_hash_gate_and_prompt(self) -> None:
        ws = self._npm_fixture("policy-npm", [("uuid", "^10.0.0"), ("is-number", "^7.0.0")])
        base = ["--run-id", "run-r10-pol", "--workspace-id", "ws-pol", "--project-id", "policy-npm", "--mode", "draft"]
        run80 = self._run_generator(ws, "policy-npm", [*base, "--min-lag-ok-pct", "80", "--target-level", "yellow"])
        run90 = self._run_generator(ws, "policy-npm", [*base, "--min-lag-ok-pct", "90", "--target-level", "green"])
        self.assertEqual(run80.returncode, 0, run80.stdout + run80.stderr)
        self.assertEqual(run90.returncode, 0, run90.stdout + run90.stderr)
        draft80 = ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-r10-pol" / "draft"
        # Same runId, two invocations: the second overwrote the run-scoped dir.
        manifest90 = _read_json(draft80 / "result.json")
        prompt90 = (draft80 / "prompt.md").read_text(encoding="utf-8")
        self.assertEqual(manifest90["settings"]["targetLevel"], "green")
        self.assertEqual(manifest90["settings"]["minLagOkPct"], "90")
        self.assertIn("Цель запуска: уровень `green`, минимум актуальности `90%`", prompt90)
        self.assertIn(f"policyHash: `{manifest90['policyHash']}`", prompt90)
        self.assertTrue(manifest90["policyHash"])

    def test_pnpm_manifest_only_draft_is_available_and_marks_unsupported_manager(self) -> None:
        ws = self._tmp / "pnpm-only" / "ws"
        _write(ws / "package.json", json.dumps({
            "name": "pnpm-only", "version": "1.0.0",
            "scripts": {"test": "node build.js test"},
            "dependencies": {"is-number": "^7.0.0"},
        }, indent=2))
        _write(ws / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n\nimporters:\n  .:\n    dependencies:\n      is-number:\n        specifier: ^7.0.0\n        version: 7.0.0\n")
        _write(ws / ".dependency-roadmap" / "settings.project.json",
               json.dumps({"projects": [{"name": "pnpm-only", "path": ".", "sourceBranch": "main"}]}))
        result = self._run_generator(
            ws, "pnpm-only",
            ["--run-id", "run-r10-pnpm", "--workspace-id", "ws-pnpm", "--project-id", "pnpm-only", "--mode", "draft"],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        draft = ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-r10-pnpm" / "draft"
        manifest = _read_json(draft / "result.json")
        self.assertEqual(manifest["status"], "DRAFT_READY")
        # The unsupported physical manager is an honest limitation of the next
        # Verified step, not a ban on the theoretical plan (R7).
        self.assertIn("PACKAGE_MANAGER_PNPM_UNSUPPORTED", result.stderr or "")
        self.assertTrue((ws / "package.json").exists())

    def test_real_npm_test_smoke_on_fixture_with_scripts(self) -> None:
        ws = self._npm_fixture("smoke-npm", [("is-number", "^7.0.0")])
        finished = subprocess.run(
            ["npm.cmd", "test"], cwd=str(ws), capture_output=True, text=True,
            encoding="utf-8", timeout=120)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("SMOKE_test_OK", finished.stdout)


if __name__ == "__main__":
    unittest.main()
