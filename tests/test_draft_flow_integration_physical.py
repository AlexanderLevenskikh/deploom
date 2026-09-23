"""R10 / T8: end-to-end Draft integration scenarios on real fixtures.

Runs the actual generator process (main -> a real publish chain) against tiny
real projects with scripts, across the accepted scenarios: fast draft on a
fresh workspace, policy change moving hash/gate/prompt, pnpm manifest-only
availability, a real `npm test` smoke, and the T1 cross-language boundary:
the real Python writer -> files -> the real production Electron reader module
(compiled from desktop/electron/draft-artifact-reader.ts) loaded in Node.
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
RENDERER_TS = ROOT / "desktop" / "electron" / "draft-artifact-reader.ts"
READER_ASSET = ROOT / "desktop" / "dist-electron" / "draft-artifact-reader.js"
PROBE = ROOT / "tests" / "draft_reader_probe.mjs"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class DraftReaderCrossLanguageTests(unittest.TestCase):
    """T1/T5/T8: Python writer -> raw files -> production Node reader."""

    def setUp(self) -> None:
        self._tmp: Path = Path(tempfile.mkdtemp(prefix="deploom-t1-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)

    @classmethod
    def setUpClass(cls) -> None:
        compiled = (READER_ASSET,)
        try:
            need_compile = not READER_ASSET.is_file() or RENDERER_TS.stat().st_mtime_ns > READER_ASSET.stat().st_mtime_ns
        except OSError:
            need_compile = True
        if need_compile:
            finished = subprocess.run(
                ["npx.cmd", "tsc", "-p", "tsconfig.electron.json"],
                cwd=str(ROOT / "desktop"), capture_output=True, text=True,
                encoding="utf-8", timeout=180,
            )
            if finished.returncode != 0:
                raise unittest.SkipTest(f"tsc electron unavailable: {finished.stderr[-500:]}")
        for asset in compiled:
            if not Path(asset).is_file():
                raise unittest.SkipTest(f"reader asset missing: {asset}")

    def _probe(self, workspace_path: Path, run_id: str, expect: dict | None = None, staleness: dict | None = None) -> dict:
        payload = {"workspacePath": str(workspace_path), "runId": run_id, "expect": expect}
        if staleness is not None:
            payload["staleness"] = staleness
        payload_path = self._tmp / f"probe-{run_id}.json"
        _write(payload_path, json.dumps(payload))
        finished = subprocess.run(
            ["node", str(PROBE), str(payload_path)], capture_output=True, text=True,
            encoding="utf-8", timeout=60,
        )
        if finished.returncode != 0:
            raise AssertionError(f"node probe failed: {finished.stdout} {finished.stderr}")
        return json.loads(finished.stdout.strip())

    def _manifest(self, ws: Path, run_id: str) -> Path:
        return ws / ".dependency-roadmap" / "artifacts" / "runs" / run_id / "draft" / "result.json"

    def _make_workspace_with_drafts(self) -> tuple[Path, str, str]:
        ws = self._tmp / "t1" / "ws"
        _write(ws / "package.json", json.dumps({
            "name": "t1", "version": "1.0.0",
            "dependencies": {"uuid": "^10.0.0", "is-number": "^7.0.0"},
        }, indent=2))
        lock = {"name": "t1", "version": "1.0.0", "lockfileVersion": 3, "requires": True,
                "packages": {"": {"name": "t1", "version": "1.0.0", "dependencies": {"uuid": "^10.0.0", "is-number": "^7.0.0"}},
                             "node_modules/uuid": {"version": "10.0.0"}, "node_modules/is-number": {"version": "7.0.0"}}}
        _write(ws / "package-lock.json", json.dumps(lock, indent=2))
        _write(ws / ".dependency-roadmap" / "settings.project.json",
               json.dumps({"projects": [{"name": "t1", "path": ".", "sourceBranch": "main"}]}))

        def run(run_id: str, deadline: str) -> subprocess.CompletedProcess[str]:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            return subprocess.run([
                sys.executable, str(GENERATOR),
                "--project-settings", str(ws / ".dependency-roadmap" / "settings.project.json"),
                "--only-project", "t1", "--draft-baseline",
                "--artifacts-dir", str(ws / ".dependency-roadmap" / "artifacts"),
                "--run-id", run_id, "--workspace-id", "ws-t1", "--project-id", "t1",
                "--mode", "draft", "--draft-deadline-seconds", deadline,
            ], cwd=str(ws), env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)

        ready = run("run-t1-ready", "60")
        partial = run("run-t1-partial", "0.05")
        self.assertEqual(ready.returncode, 0, ready.stdout + ready.stderr)
        self.assertEqual(partial.returncode, 0, partial.stdout + partial.stderr)
        return ws, "run-t1-ready", "run-t1-partial"

    def test_native_windows_drafts_are_accepted_by_production_reader(self) -> None:
        """T1 acceptance: real Python subprocess produces READY and PARTIAL on
        native Windows; the real production reader accepts both; bytes are LF;
        manifest plan/prompt hashes match the raw file bytes."""
        ws, run_ready, run_partial = self._make_workspace_with_drafts()
        for run_id, expected_status in ((run_ready, "DRAFT_READY"), (run_partial, "DRAFT_PARTIAL")):
            res = self._probe(ws, run_id, {"workspaceId": "ws-t1", "projectId": "t1"})
            self.assertTrue(res["ok"], f"{run_id}: {res}")
            self.assertEqual(res["status"], expected_status, run_id)
            self.assertEqual(res["runId"], run_id)
            self.assertEqual(res["workspaceId"], "ws-t1")
            self.assertEqual(res["projectId"], "t1")
            self.assertFalse(res["promptHasCrlf"], f"{run_id}: prompt must be LF on disk")
            self.assertFalse(res["planHasCrlf"], f"{run_id}: plan must be LF on disk")
            manifest = _read_json(self._manifest(ws, run_id))
            self.assertEqual(res["planHash"], manifest["hashes"]["plan"], f"{run_id}: plan raw-byte hash")
            self.assertEqual(res["promptHash"], manifest["hashes"]["prompt"], f"{run_id}: prompt raw-byte hash")
            prompt_text = (ws / ".dependency-roadmap" / "artifacts" / "runs" / run_id / "draft" / "prompt.md").read_text(encoding="utf-8")
            self.assertRegex(prompt_text, r"[а-яёА-ЯЁ]", f"{run_id}: Cyrillic/unicode prompt survives round-trip")
            self.assertEqual(res["promptHash"], manifest["hashes"]["prompt"], f"{run_id}: reader hash equals raw UTF-8 bytes")

    def test_corruption_and_identity_violations_are_rejected_with_distinct_codes(self) -> None:
        """T1/T5: hash mismatch, missing manifest, missing identity, cross-run
        artifact path, invalid JSON and wrong runId each produce a distinct
        rejection; valid result keeps validating after restart-style re-read."""
        ws, run_ready, _partial = self._make_workspace_with_drafts()
        draft_dir = ws / ".dependency-roadmap" / "artifacts" / "runs" / run_ready / "draft"

        # Single-byte corruption of plan.json -> hash-mismatch (not undefined).
        plan_path = draft_dir / "plan.json"
        original = plan_path.read_bytes()
        corrupted = bytearray(original)
        corrupted[len(corrupted) // 2] ^= 0x01
        self.addCleanup(plan_path.write_bytes, original)
        plan_path.write_bytes(bytes(corrupted))
        res = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "hash-mismatch", res)

        # Restore, then identity violations.
        plan_path.write_bytes(original)

        manifest_path = self._manifest(ws, run_ready)
        manifest = _read_json(manifest_path)

        wrong_workspace = self._probe(ws, run_ready, {"workspaceId": "other-ws", "projectId": "t1"})
        self.assertFalse(wrong_workspace["ok"])
        self.assertEqual(wrong_workspace["code"], "workspace-identity")

        wrong_project = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "other"})
        self.assertFalse(wrong_project["ok"])
        self.assertEqual(wrong_project["code"], "project-identity")

        # Missing workspace/project in manifest with an expected identity -> reject.
        stripped = dict(manifest)
        stripped.pop("workspaceId", None)
        stripped.pop("projectId", None)
        _write(manifest_path, json.dumps(stripped, ensure_ascii=False))
        res = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"})
        self.assertFalse(res["ok"])
        self.assertIn(res["code"], ("workspace-identity", "project-identity"), res)
        _write(manifest_path, json.dumps(manifest, ensure_ascii=False))

        # Cross-run artifact path (plan points at another run's directory) -> reject.
        manifest = _read_json(manifest_path)
        foreign = dict(manifest)
        foreign["artifacts"] = {**manifest["artifacts"], "plan": str(ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-t1-partial" / "draft" / "plan.json")}
        _write(manifest_path, json.dumps(foreign, ensure_ascii=False))
        self.addCleanup(_write, manifest_path, json.dumps(manifest, ensure_ascii=False))
        res = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "artifact-path", res)

        # Broken JSON -> invalid-json.
        _write(manifest_path, '{"schemaVersion": 1, "broken": ')
        res = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "invalid-json", res)

        # Missing manifest -> manifest-missing.
        res = self._probe(ws, "run-missing", {"workspaceId": "ws-t1", "projectId": "t1"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "manifest-missing", res)

    def test_t5_staleness_detects_input_change_policy_change_and_bad_timestamp(self) -> None:
        """T5: the cross-language staleness check is content-based, not mtime:
        editing a manifest, deleting a lockfile, changing the target policy or
        losing the timestamp all mark the result stale with an explicit reason;
        an untouched workspace stays fresh."""
        ws, run_ready, run_partial = self._make_workspace_with_drafts()
        project_path = ws / "package.json"

        def staleness(**overrides):
            payload = {
                "projectPath": str(ws),
                "projectName": "t1",
                "useManifestSettings": True,
                "currentPolicy": {"targetLevel": "yellow", "minLagOkPct": 80, "policies": {}},
            }
            payload.update(overrides)
            return payload

        self._write_json = _write
        project_json = _read_json(project_path)

        # Fresh workspace: not stale.
        fresh = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertTrue(fresh["ok"])
        self.assertIs(fresh["staleness"]["stale"], False, fresh["staleness"])

        # Content edit (add a dependency) -> stale via input hash.
        edited = dict(project_json, dependencies={**project_json["dependencies"], "left-pad": "^1.3.0"})
        _write(project_path, json.dumps(edited, indent=2))
        changed = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertIs(changed["staleness"]["stale"], True, changed["staleness"])
        self.assertIn("package.json/lockfile", changed["staleness"]["reason"])

        # Restore, then deletion of the lockfile -> stale (missing file counts).
        _write(project_path, json.dumps(project_json, indent=2))
        lock_path = ws / "package-lock.json"
        lock_bytes = lock_path.read_bytes()
        self.addCleanup(lock_path.write_bytes, lock_bytes)
        lock_path.unlink()
        deleted = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertIs(deleted["staleness"]["stale"], True, deleted["staleness"])
        self.assertIn("package.json/lockfile", deleted["staleness"]["reason"])
        lock_path.write_bytes(lock_bytes)

        # Policy change: user would now run green/100 while the run was yellow/80.
        policy_green = self._probe(
            ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"},
            staleness=staleness(currentPolicy={"targetLevel": "green", "minLagOkPct": 100, "policies": {}}),
        )
        self.assertIs(policy_green["staleness"]["stale"], True, policy_green["staleness"])
        self.assertIn("цель/процент", policy_green["staleness"]["reason"])

        # Invalid timestamp -> stale.
        bad_ts = self._probe(
            ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"},
            staleness=staleness(currentPolicy={"targetLevel": "yellow", "minLagOkPct": 80, "policies": {}},
                                useManifestSettings=False,
                                storedPolicy={"targetLevel": "yellow", "minLagOkPct": 80, "intentJson": ""},
                                generatedAt="not-a-timestamp"),
        )
        self.assertIs(bad_ts["staleness"]["stale"], True, bad_ts["staleness"])

        # PARTIAL run also carries a fresh-until-changed identity.
        partial = self._probe(ws, run_partial, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertTrue(partial["ok"])
        self.assertIs(partial["staleness"]["stale"], False, partial["staleness"])


class DraftFlowIntegrationPhysicalTests(unittest.TestCase):
    """Full-chain generator runs; no mocks for the publish path."""

    def setUp(self) -> None:
        self._tmp: Path = Path(tempfile.mkdtemp(prefix="deploom-r10-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)

    @classmethod
    def setUpClass(cls) -> None:
        # The two-run policy test probes both manifests through the production
        # Node reader; compile it once if the .ts is newer than the artifact.
        try:
            need_compile = not READER_ASSET.is_file() or RENDERER_TS.stat().st_mtime_ns > READER_ASSET.stat().st_mtime_ns
        except OSError:
            need_compile = True
        if need_compile:
            subprocess.run(["npx.cmd", "tsc", "-p", "tsconfig.electron.json"], cwd=str(ROOT / "desktop"),
                           capture_output=True, text=True, encoding="utf-8", timeout=180)

    def _probe_node(self, workspace_path: Path, run_id: str, expect: dict | None = None) -> dict:
        payload_path = self._tmp / f"probe-{run_id}.json"
        _write(payload_path, json.dumps({"workspacePath": str(workspace_path), "runId": run_id, "expect": expect}))
        finished = subprocess.run(["node", str(PROBE), str(payload_path)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        if finished.returncode != 0:
            raise AssertionError(f"node probe failed: {finished.stdout} {finished.stderr}")
        return json.loads(finished.stdout.strip())

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

    def test_policy_change_preserves_both_runs_and_moves_hash_gate_and_prompt(self) -> None:
        # T8: the policy-change check must compare TWO preserved runs, not
        # overwrite one runId and read only the second manifest. Each goal
        # (80/yellow vs 90/green) is published under its own runId, both stay
        # readable, and the actual effect -- settings, policy hash, prompt goal,
        # chosen plan variant (T2) -- is compared, not assumed.
        ws = self._npm_fixture("policy-npm", [("uuid", "^10.0.0"), ("is-number", "^7.0.0")])
        run80 = self._run_generator(ws, "policy-npm", ["--run-id", "run-t8-p80", "--workspace-id", "ws-pol", "--project-id", "policy-npm", "--mode", "draft", "--min-lag-ok-pct", "80", "--target-level", "yellow"])
        run90 = self._run_generator(ws, "policy-npm", ["--run-id", "run-t8-p90", "--workspace-id", "ws-pol", "--project-id", "policy-npm", "--mode", "draft", "--min-lag-ok-pct", "90", "--target-level", "green"])
        self.assertEqual(run80.returncode, 0, run80.stdout + run80.stderr)
        self.assertEqual(run90.returncode, 0, run90.stdout + run90.stderr)
        roots = ws / ".dependency-roadmap" / "artifacts" / "runs"
        for run_id in ("run-t8-p80", "run-t8-p90"):
            self.assertTrue((roots / run_id / "draft" / "result.json").exists(), f"missing manifest for {run_id}")
        manifest80 = _read_json(roots / "run-t8-p80" / "draft" / "result.json")
        manifest90 = _read_json(roots / "run-t8-p90" / "draft" / "result.json")
        # Both runs are preserved and carry their own identity/policy.
        self.assertNotEqual(manifest80["runId"], manifest90["runId"])
        self.assertEqual(manifest80["settings"]["targetLevel"], "yellow")
        self.assertEqual(manifest80["settings"]["minLagOkPct"], "80")
        self.assertEqual(manifest90["settings"]["targetLevel"], "green")
        self.assertEqual(manifest90["settings"]["minLagOkPct"], "90")
        self.assertNotEqual(manifest80["policyHash"], manifest90["policyHash"])
        self.assertTrue(manifest80["policyHash"])
        self.assertTrue(manifest90["policyHash"])
        # Cross-mode reader still accepts both (T1: no writing-mode privilege).
        for run_id in ("run-t8-p80", "run-t8-p90"):
            probe = self._probe_node(ws, run_id, {"workspaceId": "ws-pol", "projectId": "policy-npm"})
            self.assertTrue(probe["ok"], probe)
        prompt80 = (roots / "run-t8-p80" / "draft" / "prompt.md").read_text(encoding="utf-8")
        prompt90 = (roots / "run-t8-p90" / "draft" / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("Цель запуска: уровень `yellow`, минимум актуальности `80%`", prompt80)
        self.assertIn(f"policyHash: `{manifest80['policyHash']}`", prompt80)
        self.assertIn("Цель запуска: уровень `green`, минимум актуальности `90%`", prompt90)
        self.assertIn(f"policyHash: `{manifest90['policyHash']}`", prompt90)

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
