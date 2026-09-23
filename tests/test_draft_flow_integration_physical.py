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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
GENERATOR = ROOT / "dependency_live_roadmap_generator.py"
RENDERER_TS = ROOT / "desktop" / "electron" / "draft-artifact-reader.ts"
READER_ASSET = ROOT / "desktop" / "dist-electron" / "draft-artifact-reader.js"
PROBE = ROOT / "tests" / "draft_reader_probe.mjs"
VENDOR_LIB = ROOT / "tests" / "fixtures" / "real-lib" / "kontur-verified-fixture-lib"
REAL_LIB_TGZ = "kontur-verified-fixture-lib-1.0.0.tgz"

import manual_dependency_audit as audit

NODE_VERDICT_SCRIPT = """\
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
const [distPath, reportPath, projectPath, policyArg] = process.argv.slice(2)
const { acceptanceVerdictFromManualAudit, dependencyInputIdentity } = await import(pathToFileURL(distPath).href)
const report = JSON.parse(readFileSync(reportPath, 'utf8'))
const policy = policyArg ? JSON.parse(policyArg) : undefined
const identity = dependencyInputIdentity(projectPath)
const verdict = acceptanceVerdictFromManualAudit(report, identity.hash, policy)
console.log(JSON.stringify({ status: verdict.status, accepted: verdict.accepted, reasons: verdict.reasons }))
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _tsc_command() -> list[str]:
    """Cross-platform tsc invocation: npx.cmd on Windows, npx elsewhere."""
    return [shutil.which("npx.cmd") or "npx", "tsc", "-p", "tsconfig.electron.json"]


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
                _tsc_command(),
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

    def test_f4_staleness_covers_settings_dashboard_and_missing_identity(self) -> None:
        """F4: the staleness decision is bound to the FULL planner input set,
        not only the lockfiles:
        - project settings edits and dashboard-state additions/deletions flip
          the fingerprint even when package.json/lockfile are untouched;
        - a run without a recorded input identity is never fresh;
        - changing the acceptance policy limits (High / lag months) flips it;
        - the historical result stays readable with an explicit reason."""
        ws, run_ready, _ = self._make_workspace_with_drafts()
        settings_rel = Path(".dependency-roadmap") / "settings.project.json"
        settings_path = ws / settings_rel
        dashboard_rel = Path(".dependency-roadmap") / "state" / "dashboard-state.json"
        dashboard_path = ws / dashboard_rel
        manifest_path = self._manifest(ws, run_ready)

        def staleness(**overrides):
            payload = {
                "projectPath": str(ws),
                "projectName": "t1",
                "useManifestSettings": True,
                "currentPolicy": {"targetLevel": "yellow", "minLagOkPct": 80, "policies": {}},
            }
            payload.update(overrides)
            return payload

        # Baseline: untouched -> fresh (the fingerprint covers the settings that
        # existed at run time too).
        fresh = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertTrue(fresh["ok"])
        self.assertIs(fresh["staleness"]["stale"], False, fresh["staleness"])

        # Project settings edit -> stale (lockfiles untouched).
        original_settings_bytes = settings_path.read_text(encoding="utf-8")
        original_settings = _read_json(settings_path)
        _write(settings_path, json.dumps({**original_settings, "hintField": 1}, indent=2))
        self.addCleanup(lambda: settings_path.write_text(original_settings_bytes, encoding="utf-8", newline="\n"))
        changed_settings = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertIs(changed_settings["staleness"]["stale"], True, changed_settings["staleness"])
        self.assertIn("package.json/lockfile", changed_settings["staleness"]["reason"])

        # Restore, then ADD a settings.local.json that did not exist at run
        # time -> stale (ADDITIONS count exactly like deletions do).
        settings_path.write_text(original_settings_bytes, encoding="utf-8", newline="\n")
        local_rel = Path(".dependency-roadmap") / "settings.local.json"
        local_path = ws / local_rel
        _write(local_path, json.dumps({"localOnly": True}, indent=2))
        self.addCleanup(lambda: local_path.unlink(missing_ok=True))
        added_settings = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertIs(added_settings["staleness"]["stale"], True, added_settings["staleness"])
        self.assertIn("package.json/lockfile", added_settings["staleness"]["reason"])

        # Remove it again: the fingerprint is byte-exact, so the run is fresh
        # once more (identity covers the CURRENT input set).
        local_path.unlink()
        restored = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertIs(restored["staleness"]["stale"], False, restored["staleness"])

        # The planner seeds an EMPTY dashboard-state as a convenience when none
        # exists, and the fingerprint binds the run to it: real planning
        # decisions written there (exclusions / per-package lag) make the run
        # stale, restoring the run-time bytes makes it fresh again.
        original_dashboard_bytes = dashboard_path.read_bytes()
        self.addCleanup(lambda: dashboard_path.write_bytes(original_dashboard_bytes))
        _write(dashboard_path, json.dumps({"schemaVersion": 2, "overrides": []}, indent=2))
        edited_dashboard = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertIs(edited_dashboard["staleness"]["stale"], True, edited_dashboard["staleness"])
        dashboard_path.write_bytes(original_dashboard_bytes)
        dashboard_restored = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertIs(dashboard_restored["staleness"]["stale"], False, dashboard_restored["staleness"])

        # Acceptance policy limit change (High 1 -> 2) -> stale via the FULL
        # policy key, even though targetLevel/minLagOkPct are unchanged.
        high_changed = self._probe(
            ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"},
            staleness=staleness(
                useManifestSettings=False,
                storedPolicy={
                    "targetLevel": "yellow", "minLagOkPct": "80",
                    "acceptancePolicyJson": json.dumps({
                        "targetLevel": "yellow", "minLagOkPct": 80, "maxKnownHigh": 1, "lagPolicyMonths": 3,
                    }),
                },
                currentPolicy={"targetLevel": "yellow", "minLagOkPct": 80, "maxKnownHigh": 2, "lagPolicyMonths": 3},
            ),
        )
        self.assertIs(high_changed["staleness"]["stale"], True, high_changed["staleness"])
        self.assertIn("лимиты acceptance-политики", high_changed["staleness"]["reason"])

        # Lag months change (3 -> 6) -> stale, same full-policy key.
        lag_changed = self._probe(
            ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"},
            staleness=staleness(
                useManifestSettings=False,
                storedPolicy={
                    "targetLevel": "yellow", "minLagOkPct": "80",
                    "acceptancePolicyJson": json.dumps({
                        "targetLevel": "yellow", "minLagOkPct": 80, "maxKnownHigh": 1, "lagPolicyMonths": 3,
                    }),
                },
                currentPolicy={"targetLevel": "yellow", "minLagOkPct": 80, "maxKnownHigh": 1, "lagPolicyMonths": 6},
            ),
        )
        self.assertIs(lag_changed["staleness"]["stale"], True, lag_changed["staleness"])
        self.assertIn("лимиты acceptance-политики", lag_changed["staleness"]["reason"])

        # Identical full policy -> not stale.
        same_policy = self._probe(
            ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"},
            staleness=staleness(
                useManifestSettings=False,
                storedPolicy={
                    "targetLevel": "yellow", "minLagOkPct": "80", "intentJson": "",
                    "acceptancePolicyJson": json.dumps({
                        "targetLevel": "yellow", "minLagOkPct": 80, "maxKnownHigh": 1, "lagPolicyMonths": 3,
                    }),
                },
                currentPolicy={"targetLevel": "yellow", "minLagOkPct": 80, "maxKnownHigh": 1, "lagPolicyMonths": 3},
            ),
        )
        self.assertIs(same_policy["staleness"]["stale"], False, same_policy["staleness"])

        # DELETE the recorded input identity from the manifest: the result is
        # readable but never fresh.
        manifest = _read_json(manifest_path)
        manifest.pop("inputHashes", None)
        _write(manifest_path, json.dumps(manifest, indent=2))
        self.addCleanup(lambda: _write(manifest_path, json.dumps(manifest, indent=2)))
        no_identity = self._probe(ws, run_ready, {"workspaceId": "ws-t1", "projectId": "t1"}, staleness=staleness())
        self.assertTrue(no_identity["ok"], no_identity)
        self.assertIs(no_identity["staleness"]["stale"], True, no_identity["staleness"])
        self.assertIn("input-идентичности", no_identity["staleness"]["reason"])


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
            subprocess.run(_tsc_command(), cwd=str(ROOT / "desktop"),
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

    def _pack_real_lib(self, ws: Path) -> None:
        """Vendor the real F6 library into ws/vendor via offline `npm pack`."""
        vendor = ws / "vendor"
        vendor.mkdir(parents=True, exist_ok=True)
        packed = subprocess.run(
            ["npm.cmd", "pack", str(VENDOR_LIB), "--offline"],
            cwd=str(vendor), capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        if packed.returncode != 0 or not (vendor / REAL_LIB_TGZ).is_file():
            raise AssertionError(f"npm pack of real lib failed: {packed.stdout} {packed.stderr}")

    def _npm_fixture_real_lib(self, name: str, lib_manifest_spec: str) -> Path:
        """F6 real-resource fixture: a REAL vendored npm package (local tarball)
        with a REAL build.js that requires it. The installed candidate is always
        1.0.0; lib_manifest_spec controls compatibility (file:1.0.0 == compatible,
        '^2.0.0' == incompatible) so the compatible/incompatible candidates are
        real and controllable, exactly as F6 requires."""
        ws = self._tmp / name / "ws"
        self._pack_real_lib(ws)
        _write(ws / "package.json", json.dumps({
            "name": name,
            "version": "1.0.0",
            "scripts": {
                "test": "node build.js test",
                "build": "node build.js build",
            },
            "dependencies": {
                "kontur-verified-fixture-lib": lib_manifest_spec,
            },
        }, indent=2))
        _write(ws / "build.js", (
            "const assert = require('node:assert');\n"
            "const { hasSatisfyingPatch, isEven } = require('kontur-verified-fixture-lib');\n"
            "const mode = process.argv[2] || 'test';\n"
            "assert.strictEqual(require('node:path').basename(process.cwd()), 'ws');\n"
            "assert.strictEqual(hasSatisfyingPatch('1.0.5', '^1.0.0'), true);\n"
            "assert.strictEqual(hasSatisfyingPatch('2.0.0', '^1.0.0'), false);\n"
            "assert.strictEqual(isEven(4), true);\n"
            "console.log('REAL_LIB_' + mode + '_OK:' + JSON.stringify({ even: isEven(4) }));\n"
        ))
        lock_packages = {
            "": {"name": name, "version": "1.0.0", "dependencies": {"kontur-verified-fixture-lib": lib_manifest_spec}},
            "node_modules/kontur-verified-fixture-lib": {"version": "1.0.0", "resolved": "file:vendor/" + REAL_LIB_TGZ},
        }
        lock = {"name": name, "version": "1.0.0", "lockfileVersion": 3, "requires": True, "packages": lock_packages}
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

    def test_f6_real_library_compatible_candidate_real_install_build_test_and_draft(self) -> None:
        # F6 real-resource fixture: the build.js genuinely REQUIRES and USES a
        # real installed npm package (vendored local tarball), and the plan the
        # generator drafts from that real manifest/lockfile matches it.
        ws = self._npm_fixture_real_lib("real-compat", "file:vendor/" + REAL_LIB_TGZ)
        installed = subprocess.run(
            ["npm.cmd", "install", "--no-audit", "--no-fund", "--offline"],
            cwd=str(ws), capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        tested = subprocess.run(
            ["npm.cmd", "test"], cwd=str(ws), capture_output=True, text=True,
            encoding="utf-8", timeout=120)
        self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
        self.assertIn("REAL_LIB_test_OK", tested.stdout)

        result = self._run_generator(
            ws, "real-compat",
            ["--run-id", "run-f6-compat", "--workspace-id", "ws-f6", "--project-id", "real-compat",
             "--mode", "draft", "--draft-deadline-seconds", "0.05"],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        draft = ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-f6-compat" / "draft"
        manifest = _read_json(draft / "result.json")
        self.assertIn(manifest["status"], ("DRAFT_PARTIAL", "DRAFT_READY"))
        prompt = (draft / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("kontur-verified-fixture-lib", prompt)

    def test_f6_real_incompatible_candidate_real_pm_refuses_and_draft_plans_update(self) -> None:
        # Incompatible candidate: manifest wants ^2.0.0, the REAL installed lib
        # is 1.0.0. The REAL package manager refuses to install it, and the
        # (planning-only, no install) Draft must still plan the update to the
        # real target spec for the real-named package.
        ws = self._npm_fixture_real_lib("real-incompat", "^2.0.0")
        refused = subprocess.run(
            ["npm.cmd", "install", "--no-audit", "--no-fund", "--offline"],
            cwd=str(ws), capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)

        result = self._run_generator(
            ws, "real-incompat",
            ["--run-id", "run-f6-incompat", "--workspace-id", "ws-f6i", "--project-id", "real-incompat",
             "--mode", "draft", "--draft-deadline-seconds", "0.05"],
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        draft = ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-f6-incompat" / "draft"
        manifest = _read_json(draft / "result.json")
        self.assertIn(manifest["status"], ("DRAFT_PARTIAL", "DRAFT_READY"))
        plan_text = (draft / "plan.json").read_text(encoding="utf-8")
        self.assertIn("kontur-verified-fixture-lib", plan_text)
        self.assertIn("^2.0.0", plan_text)

    def test_f6_two_run_policy_chain_verdicts_really_differ_for_real_candidate(self) -> None:
        # F6: the two-run policy chain must compare the INHERENT verdict for the
        # candidate, not just settings/hash/prompt text. Same real candidate,
        # real producer report, production Node verdict per run's real published
        # policy: yellow80 accepts (80% gate met), green90 remediates (80 < 90).
        ws = self._npm_fixture_real_lib("real-verdict", "file:vendor/" + REAL_LIB_TGZ)
        run80 = self._run_generator(
            ws, "real-verdict",
            ["--run-id", "run-f6-v80", "--workspace-id", "ws-f6v", "--project-id", "real-verdict",
             "--mode", "draft", "--min-lag-ok-pct", "80", "--target-level", "yellow"],
        )
        run90 = self._run_generator(
            ws, "real-verdict",
            ["--run-id", "run-f6-v90", "--workspace-id", "ws-f6v", "--project-id", "real-verdict",
             "--mode", "draft", "--min-lag-ok-pct", "90", "--target-level", "green"],
        )
        self.assertEqual(run80.returncode, 0, run80.stdout + run80.stderr)
        self.assertEqual(run90.returncode, 0, run90.stdout + run90.stderr)
        roots = ws / ".dependency-roadmap" / "artifacts" / "runs"
        manifest80 = _read_json(roots / "run-f6-v80" / "draft" / "result.json")
        manifest90 = _read_json(roots / "run-f6-v90" / "draft" / "result.json")
        self.assertEqual(manifest80["settings"]["targetLevel"], "yellow")
        self.assertEqual(manifest80["settings"]["minLagOkPct"], "80")
        self.assertEqual(manifest90["settings"]["targetLevel"], "green")
        self.assertEqual(manifest90["settings"]["minLagOkPct"], "90")

        # Real acceptance evidence: 8 of 10 lag rows known-fresh (80% -- inside
        # the yellow80 gate, below the green90 gate); the security totals are
        # clean. ENERGY: real producer + real file + real Node reader. Each
        # report is produced under ITS run's own real goal policy, because the
        # reader fails closed when intent != report policy (F1).
        deploy_dir = Path(tempfile.mkdtemp(prefix="deploom-f6v-"))
        self.addCleanup(shutil.rmtree, deploy_dir, True)

        def produce_report(target_level, min_lag_ok_pct, max_known_high) -> dict:
            with (
                mock.patch.object(audit, "run_audit", return_value={
                    "engine": "npm-native", "command": ["npm", "audit", "--json"], "exitCode": 0,
                    "complete": True, "packages": {},
                    "totals": {"critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0},
                    "packageTotals": {"critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0},
                    "notes": [],
                }),
                mock.patch.object(audit, "check_lag", return_value=[
                    {"section": "dependencies", "name": f"real-{i}", "spec": "^1.0.0", "current": "1.0.0",
                     "source": "package.json", "policyMonths": 3, "status": "ok",
                     "latest": "1.0.0", "currentPublishedAt": "2023-01-01T00:00:00+00:00",
                     "latestPublishedAt": "2024-01-01T00:00:00+00:00", "error": ""}
                    for i in range(8)
                ] + [
                    {"section": "dependencies", "name": f"ghost-{i}", "spec": "^1.0.0", "current": "",
                     "source": "package.json", "policyMonths": 3, "status": "error",
                     "latest": "", "currentPublishedAt": "", "latestPublishedAt": "", "error": "no-registry"}
                    for i in range(2)
                ]),
            ):
                return audit.build_report(
                    ws, "real-verdict", registry="https://registry.npmjs.org/", lag_months=3,
                    target_level=target_level, min_lag_ok_pct=min_lag_ok_pct, max_known_high=max_known_high,
                )

        script = deploy_dir / "verdict.mjs"
        script.write_text(NODE_VERDICT_SCRIPT, encoding="utf-8")
        dist = str(ROOT / "desktop" / "dist-electron" / "acceptance-policy.js").replace("\\", "/")

        def node_verdict(report: dict, policy: dict) -> dict:
            report_path = deploy_dir / f"f6-audit-{policy['targetLevel']}-{policy['minLagOkPct']}.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            done = subprocess.run(
                ["node", str(script), dist, str(report_path), str(ws), json.dumps(policy)],
                capture_output=True, text=True, encoding="utf-8", timeout=60)
            if done.returncode != 0:
                raise AssertionError(f"node verdict failed: {done.stdout}\n{done.stderr}")
            return json.loads(done.stdout.strip().splitlines()[-1])

        report80 = produce_report("yellow", 80, 1)
        verdict80 = node_verdict(report80, {"targetLevel": "yellow", "minLagOkPct": 80, "maxKnownHigh": 1, "lagPolicyMonths": 3})
        report90 = produce_report("green", 90, 0)
        verdict90 = node_verdict(report90, {"targetLevel": "green", "minLagOkPct": 90, "maxKnownHigh": 0, "lagPolicyMonths": 3})
        self.assertEqual("ACCEPTED", verdict80["status"], verdict80["reasons"])
        self.assertEqual("REMEDIATION_REQUIRED", verdict90["status"], verdict90["reasons"])
        self.assertNotEqual(verdict80["status"], verdict90["status"])

    def test_f6_fast_deep_are_real_strategies_not_transport_flags(self) -> None:
        # The manifest reflects the REAL mode, and the helper the ANYTIME budget
        # is created from (same env main() pins) yields genuinely different
        # Fast/Deep budgets through the real module functions.
        ws = self._npm_fixture_real_lib("real-mode", "file:vendor/" + REAL_LIB_TGZ)
        for mode, run_id in (("fast", "run-f6-fast"), ("deep", "run-f6-deep")):
            result = self._run_generator(
                ws, "real-mode",
                ["--run-id", run_id, "--workspace-id", "ws-f6m", "--project-id", "real-mode",
                 "--mode", mode, "--draft-deadline-seconds", "0.05"],
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = _read_json(ws / ".dependency-roadmap" / "artifacts" / "runs" / run_id / "draft" / "result.json")
            self.assertEqual(manifest["mode"], mode)
        driver = (
            "import os, sys\n"
            "sys.path.insert(0, r'" + str(ROOT) + "')\n"
            "import dependency_live_roadmap_generator as g\n"
            "for mode in ('fast', 'deep', 'verify'):\n"
            "    os.environ['DEPLOOM_MODE'] = mode\n"
            "    print(mode, g._baseline_max_expensive_attempts(8), g._baseline_automatic_budget_seconds())\n"
        )
        driver_path = self._tmp / "mode-budget-driver.py"
        driver_path.write_text(driver, encoding="utf-8")
        done = subprocess.run(
            [sys.executable, str(driver_path)], capture_output=True, text=True,
            encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        rows: dict[str, tuple[str, str]] = {}
        for line in done.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] in ("fast", "deep", "verify"):
                rows[parts[0]] = (parts[1], parts[2])
        # fast/deep own their budgets (F6); verify keeps the legacy
        # executionMode mapping (unset => FAST => 2), already locked by
        # test_block_phi_execution_modes.py.
        self.assertEqual({"fast": ("2", "300"), "deep": ("12", "3600"), "verify": ("2", "900")}, rows)


if __name__ == "__main__":
    unittest.main()
