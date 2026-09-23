"""Validation #5 regressions: register the G1-G5 issues as failing tests
BEFORE the fixes, then re-run after the implementation lands.

Each test drives the production code paths (Python generator helpers and the
compiled Node acceptance/closure/staleness modules) the validation report
actually probed, so a green result means the reported gap is closed, not that a
helper returns a label.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as generator
from dependency_live_roadmap_generator import DependencyRow, ProjectSpec, DraftBudgetExceeded, compute_project_health
from tests._electron_dist import ensure_dist_electron


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _file_uri(path: Path) -> str:
    """file:// URL for an ES-module import on Windows (node rejects C:/ paths)."""
    return "file:///" + str(path).replace("\\", "/")


def _js(value: str) -> str:
    """Escape a string literal for embedding inside a JS single-quoted string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _row(name: str, *, vulns: str = "C:0;H:0;M:0;L:0", current: str = "1.0.0",
         latest: str = "1.0.0", min12: str = "1.0.0", current_source: str = "lock") -> DependencyRow:
    return DependencyRow(
        project="demo",
        package_dir=".",
        name=name,
        kind="runtime",
        requested_spec="^1.0.0",
        current_version=current,
        current_source=current_source,
        latest_version=latest,
        current_vulns=vulns,
        min_no_critical="1.0.0",
        min_no_high="1.0.0",
        min_no_vuln="1.0.0",
        min_lag_12m=min12,
        min_lag_9m=min12,
        min_lag_6m=min12,
        min_lag_3m=min12,
        group=0,
        reason="test",
        notes="",
        lag_threshold_months=12,
        scope_excluded=False,
        exclusion_reason="",
    )


class NodeDriver:
    """Runs short ES module probes against the COMPILED production modules."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        # Probe the COMPILED production modules; build once when missing/stale
        # (toolchain unavailable -> SkipTest so a Node-less env skips, not fails).
        ensure_dist_electron()

    def run(self, script: str) -> dict:
        target = self.tmp / f"probe-{abs(hash(script))}.mjs"
        target.write_text(script, encoding="utf-8")
        done = subprocess.run(["node", str(target)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        if done.returncode != 0:
            raise AssertionError(f"node probe failed: {done.stdout}\n{done.stderr}")
        return json.loads(done.stdout.strip().splitlines()[-1])


class G1PlannerEffectivePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="deploom-g1-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def test_g1_probe_lag_policy_months3_gives_3_not_12(self) -> None:
        ws = self._tmp / "demo"
        _write(ws / "package.json", json.dumps({"name": "demo", "version": "1.0.0", "dependencies": {"is-number": "^7.0.0"}}))
        project = ProjectSpec(name="demo", path=ws)
        with mock.patch.dict(os.environ, {
            "DEPLOOM_ACCEPTANCE_POLICY_JSON": json.dumps({
                "lagPolicyMonths": 3, "maxKnownHigh": 0, "targetLevel": "yellow",
                "minLagOkPct": 80, "maxKnownModerate": 0, "maxKnownLow": 0,
            }),
        }, clear=False):
            rows = generator._draft_local_inventory_rows(project, {})
        self.assertTrue(rows)
        self.assertEqual(3, rows[0].lag_threshold_months)

    def test_g1_green_health_respects_ml_zero_limits(self) -> None:
        row = _row("pkg", vulns="C:0;H:0;M:1;L:0")
        with mock.patch.dict(os.environ, {
            "DEPLOOM_ACCEPTANCE_POLICY_JSON": json.dumps({
                "targetLevel": "green", "minLagOkPct": 100, "lagPolicyMonths": 3,
                "maxKnownHigh": 0, "maxKnownModerate": 0, "maxKnownLow": 0,
            }),
        }, clear=False):
            health = compute_project_health([row], "demo")
        # A green goal with M=0/L=0 must not report green while a known
        # Moderate finding is still present on an in-scope row.
        self.assertNotEqual("green", health.status)

    def test_g1_generate_all_keeps_each_projects_policy(self) -> None:
        raw = json.dumps({
            "app-a": {"targetLevel": "yellow", "minLagOkPct": 80, "lagPolicyMonths": 3, "maxKnownHigh": 1},
            "app-b": {"targetLevel": "green", "minLagOkPct": 90, "lagPolicyMonths": 6, "maxKnownHigh": 0},
        })
        with mock.patch.dict(os.environ, {"DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT": raw}, clear=False):
            pa = generator.effective_acceptance_policy("app-a")
            pb = generator.effective_acceptance_policy("app-b")
        self.assertEqual(3, pa["lagPolicyMonths"])
        self.assertEqual("yellow", pa["targetLevel"])
        self.assertEqual(6, pb["lagPolicyMonths"])
        self.assertEqual("green", pb["targetLevel"])
        self.assertEqual(0, pb["maxKnownHigh"])


class G2ClosureAndVerdictSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="deploom-g2-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.node = NodeDriver(self._tmp)

    def test_g2_closure_never_reached_with_unknown_security(self) -> None:
        out = self.node.run(
            ROOT.read_text and f"""
import {{ targetClosureFromRoadmap }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'target-closure.js')}'
const roadmap = {{
  project_health: {{
    Demo: {{ status: 'yellow', lag_ok_pct: 80, scope_total: 10, scope_lag_ok: 8,
            scope_lag_pct: 80, scope_lag_unknown: 2, security_unknown: 2, unknown: 2,
            critical: 0, high: 0 }},
  }},
  projects: {{ Demo: [] }},
}}
const closure = targetClosureFromRoadmap(roadmap, 'Demo', 'yellow', 80, 1)
console.log(JSON.stringify({{ reached: closure.reached, securityUnknown: closure.securityUnknown }}))
"""
        )
        self.assertFalse(out["reached"])
        self.assertEqual(2, out["securityUnknown"])

    def test_g2_incomplete_policy_snapshot_is_unbound_unknown(self) -> None:
        out = self.node.run(f"""
import {{ dependencyInputIdentity, acceptanceVerdictFromManualAudit }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'acceptance-policy.js')}'
import {{ mkdirSync, writeFileSync }} from 'node:fs'
const project = '{_js(str(self._tmp / 'p'))}'
mkdirSync(project, {{ recursive: true }})
writeFileSync(project + '/package.json', '{{"dependencies":{{"a":"1.0.0"}}}}\\n')
writeFileSync(project + '/yarn.lock', 'a@1.0.0:\\n  version "1.0.0"\\n')
const identity = dependencyInputIdentity(project)
const report = {{
  auditComplete: true,
  dependencyInputHash: identity.hash,
  generatedAt: new Date(Date.now() - 60 * 1000).toISOString(),
  // G2: snapshot omits the numeric goal criteria entirely.
  policy: {{ targetLevel: 'yellow' }},
  audit: {{
    complete: true, engine: 'npm-native',
    packageTotals: {{ critical: 0, high: 0, moderate: 0, low: 0 }},
    lagOkPct: 100, lagOk: 1, lagTotal: 1, lagUnknown: 0,
  }},
}}
const policy = {{ targetLevel: 'yellow', minLagOkPct: 80, lagPolicyMonths: 3, maxKnownHigh: 1, maxKnownCritical: 0 }}
const verdict = acceptanceVerdictFromManualAudit(report, identity.hash, policy)
console.log(JSON.stringify({{ status: verdict.status, reasons: verdict.reasons }}))
"""
        )
        self.assertEqual("UNKNOWN", out["status"])


class G3StalenessRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="deploom-g3-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.node = NodeDriver(self._tmp)

    def _project(self) -> str:
        ws = self._tmp / "ws"
        _write(ws / "package.json", json.dumps({"name": "demo", "dependencies": {"a": "1.0.0"}}))
        _write(ws / "yarn.lock", 'a@1.0.0:\n  version "1.0.0"\n')
        return str(ws)

    def test_g3_keep_current_changes_make_run_stale_with_both_snapshots(self) -> None:
        project = self._project()
        out = self.node.run(f"""
import {{ draftInputHashForProject, draftResultStaleness }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'draft-artifact-reader.js')}'
const projectPath = '{project}'
const run = {{
  projectPath,
  generatedAt: new Date(Date.now() - 1000).toISOString(),
  inputHashRecorded: draftInputHashForProject(projectPath),
  storedPolicy: {{
    targetLevel: 'yellow', minLagOkPct: '80',
    intentJson: JSON.stringify({{ policies: {{ 'uuid': 'required' }} }}),
    acceptancePolicyJson: JSON.stringify({{ targetLevel: 'yellow', minLagOkPct: 80, maxKnownHigh: 1, lagPolicyMonths: 3 }}),
  }},
  currentPolicy: {{
    targetLevel: 'yellow', minLagOkPct: '80',
    policies: {{ 'uuid': 'keep-current' }},
    acceptancePolicy: {{ targetLevel: 'yellow', minLagOkPct: 80, maxKnownHigh: 1, lagPolicyMonths: 3 }},
  }},
}}
const stale = draftResultStaleness(run)
console.log(JSON.stringify({{ stale: stale.stale, reason: stale.reason }}))
"""
        )
        self.assertTrue(out["stale"], out)
        self.assertIn("keep-current/required", out["reason"])

    def test_g3_nested_layout_settings_change_is_detected_via_resolved_inputs(self) -> None:
        ws = self._tmp / "ws"
        frontend = ws / "frontend"
        _write(frontend / "package.json", json.dumps({"name": "demo", "dependencies": {"a": "1.0.0"}}))
        _write(frontend / "yarn.lock", 'a@1.0.0:\n  version "1.0.0"\n')
        settings = ws / ".dependency-roadmap" / "settings.project.json"
        _write(settings, json.dumps({"projects": [{"name": "demo"}]}))
        front = str(frontend)
        out = self.node.run(f"""
import {{ draftInputHashFromFiles, draftResultStaleness }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'draft-artifact-reader.js')}'
import {{ readFileSync, writeFileSync }} from 'node:fs'
import {{ createHash }} from 'node:crypto'
const projectPath = '{_js(front)}'
const settingsAbs = '{_js(str(settings))}'
const inputFiles = [
  {{ name: 'package.json' }},
  {{ name: 'yarn.lock' }},
  {{ name: settingsAbs }},
]
const recorded = draftInputHashFromFiles(projectPath, inputFiles)
const facts = {{
  projectPath,
  generatedAt: new Date(Date.now() - 1000).toISOString(),
  inputHashRecorded: recorded,
  inputFiles,
  storedPolicy: {{ targetLevel: 'yellow', minLagOkPct: '80', acceptancePolicyJson: '{{"targetLevel":"yellow","minLagOkPct":80,"maxKnownHigh":1,"lagPolicyMonths":3}}' }},
  currentPolicy: {{ targetLevel: 'yellow', minLagOkPct: '80', acceptancePolicy: {{ targetLevel: 'yellow', minLagOkPct: 80, maxKnownHigh: 1, lagPolicyMonths: 3 }} }},
}}
const before = draftResultStaleness(facts)
writeFileSync('{_js(str(settings))}', JSON.stringify({{ projects: [{{ name: 'demo' }}], lagPolicyMonths: 3 }}))
const after = draftResultStaleness(facts)
console.log(JSON.stringify({{ beforeStale: before.stale, afterStale: after.stale }}))
"""
        )
        self.assertFalse(out["beforeStale"], out)
        self.assertTrue(out["afterStale"], out)


class G4SupervisorIsolationTests(unittest.TestCase):
    def test_g4_timed_out_worker_mutations_never_reach_published_rows(self) -> None:
        shared = {"demo": [_row("pkg")]}
        proceed = threading.Event()
        deadline = generator.DeadlineClock(0.2)

        def late_mutator(copy_rows: dict) -> None:
            # Simulates a planner that is still working after the deadline:
            # it waits until the main thread already returned the timeout, then
            # writes into its own working copy.
            proceed.wait(timeout=5)
            copy_rows["demo"][0].current_version = "9.9.9-late-write"

        with self.assertRaises(DraftBudgetExceeded):
            generator.run_supervised_planning("planning", deadline, late_mutator, shared)
        before = shared["demo"][0].current_version
        proceed.set()
        time.sleep(0.3)
        # The late worker write must not appear in the shared rows that the
        # publish path would read.
        self.assertEqual(before, shared["demo"][0].current_version)


class G5FastStopPolicyTests(unittest.TestCase):
    def test_g5_fast_stop_never_satisfied_by_remaining_critical(self) -> None:
        rows = [_row("ok-a"), _row("ok-b"), _row("ok-c"), _row("ok-d"), _row("ok-e")]
        # Known Critical on the current version; the row is only fixed when a
        # candidate actually plans a version >= min_no_critical.
        vuln = _row("vuln", vulns="C:1;H:0;M:0;L:0", current="0.5.0")
        active = [*rows, vuln]
        satisfied = generator._candidate_satisfies_fast_policy(
            rows=active,
            candidate_targets={},
            current_targets={r.name: r.current_version for r in active},
            active_names=set(r.name for r in active),
        )
        self.assertFalse(satisfied, "a remaining Critical must not satisfy fast stop")

    def test_g5_fast_stop_satisfied_when_goal_already_complete(self) -> None:
        rows = [_row("ok-a")]
        satisfied = generator._candidate_satisfies_fast_policy(
            rows=rows,
            candidate_targets={},
            current_targets={"ok-a": "1.0.0"},
            active_names={"ok-a"},
        )
        self.assertTrue(satisfied)

    def test_g5_fast_stop_satisfied_when_candidate_covers_critical(self) -> None:
        vuln = _row("vuln", vulns="C:1;H:0;M:0;L:0", current="0.5.0")
        satisfied = generator._candidate_satisfies_fast_policy(
            rows=[vuln],
            candidate_targets={"vuln": "1.0.0"},
            current_targets={"vuln": "0.5.0"},
            active_names={"vuln"},
        )
        self.assertTrue(satisfied)


if __name__ == "__main__":
    unittest.main()
