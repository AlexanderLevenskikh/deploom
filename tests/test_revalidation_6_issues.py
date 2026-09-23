"""Validation #6 regressions: register the H1-H5 issues as failing tests
BEFORE the fixes, then re-run after the implementation lands.

Per the validation report each negative scenario is first fixed as a failing
test, so a green result means the reported gap is closed on the production code
path (Python generator helpers/engine and the compiled Node check modules).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import block_psi_anytime
import dependency_live_roadmap_generator as generator
from dependency_live_roadmap_generator import DependencyRow, ProjectSpec, compute_project_health, required_ratio_count
from tests._electron_dist import ensure_dist_electron


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _file_uri(path: Path) -> str:
    return "file:///" + str(path).replace("\\", "/")


def _js(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _row(name: str, *, vulns: str = "C:0;H:0;M:0;L:0", current: str = "1.0.0",
         min12: str = "1.0.0", min_critical: str = "1.0.0", min_high: str = "1.0.0",
         min_vuln: str = "1.0.0", evidence: dict | None = None) -> DependencyRow:
    return DependencyRow(
        project="app",
        package_dir=".",
        name=name,
        kind="runtime",
        requested_spec="^1.0.0",
        current_version=current,
        current_source="lock",
        latest_version=current,
        current_vulns=vulns,
        min_no_critical=min_critical,
        min_no_high=min_high,
        min_no_vuln=min_vuln,
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
        vuln_evidence_by_version=evidence if evidence is not None else {},
    )


class NodeDriver:
    """Runs short ES module probes against the COMPILED production modules."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        ensure_dist_electron()

    def run(self, script: str) -> dict:
        target = self.tmp / f"probe-h6-{abs(hash(script))}.mjs"
        target.write_text(script, encoding="utf-8")
        done = subprocess.run(["node", str(target)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        if done.returncode != 0:
            raise AssertionError(f"node probe failed: {done.stdout}\n{done.stderr}")
        return json.loads(done.stdout.strip().splitlines()[-1])


class H1FastPolicyScopeTests(unittest.TestCase):
    """H1: fast stop must evaluate the goal over the WHOLE active scope.

    Security findings are aggregated in the same units as the acceptance
    policy (C/H/M/L over the project), the lag denominator is the whole active
    scope (unknown-lag rows are not compliant and never drop out of it), and a
    candidate clears a finding only with real per-row evidence.
    """

    def _gate(self, rows, candidate, current=None, *, project="app") -> bool:
        current = current or {r.name: r.current_version for r in rows}
        return generator._candidate_satisfies_fast_policy(
            rows=rows,
            candidate_targets=candidate,
            current_targets=current,
            active_names=set(r.name for r in rows),
            project_name=project,
        )

    def test_h1_two_high_rows_are_not_cleared_by_one_policy_limit(self) -> None:
        # Two packages with H:1 each and maxKnownHigh=1. The aggregate is 2,
        # so the goal is satisfied only when BOTH rows are cleared by the
        # candidate. Neither has a safe high target -> must NOT satisfy.
        rows = [
            _row("a", vulns="C:0;H:1;M:0;L:0", min_high=""),
            _row("b", vulns="C:0;H:1;M:0;L:0", min_high=""),
        ]
        self.assertFalse(self._gate(rows, {}), "H aggregated over the scope must exceed the limit")

    def test_h1_moderate_and_low_respect_policy_limits(self) -> None:
        # Green policy with maxKnownModerate=0: an installed package with M:1
        # (no safe no-vuln target) must NOT satisfy the gate.
        rows_m = [_row("m", vulns="C:0;H:0;M:1;L:0", min_vuln="")]
        rows_l = [_row("l", vulns="C:0;H:0;M:0;L:1", min_vuln="")]
        green_policy = {"targetLevel": "green", "maxKnownHigh": 0, "maxKnownModerate": 0, "maxKnownLow": 0}
        with mock.patch.dict(os.environ, {"DEPLOOM_ACCEPTANCE_POLICY_JSON": json.dumps(green_policy)}):
            self.assertFalse(self._gate(rows_m, {}), "Moderate over the policy limit must not satisfy")
            self.assertFalse(self._gate(rows_l, {}), "Low over the policy limit must not satisfy")

    def test_h1_lag_denominator_is_whole_active_scope(self) -> None:
        # 10 active packages, lag confirmed and compliant for only 1, unknown
        # for the other 9. At an 80% goal the confirmed share is 10%, not
        # 100% of the researched share -> must NOT satisfy.
        rows = [_row("ok", min12="1.0.0")]
        rows += [_row(f"unknown-{i}", min12="") for i in range(9)]
        self.assertFalse(self._gate(rows, {}), "1/10 confirmed scope must not satisfy an 80% yellow goal")

    def test_h1_positive_when_every_finding_is_cleared(self) -> None:
        # Two H:1 rows BOTH cleared by the candidate (per-version OSV evidence
        # for the exact planned version shows H:0) -> aggregate drops to 0.
        rows = [
            _row("a", vulns="C:0;H:1;M:0;L:0", current="0.5.0", min_high="1.0.0",
                 evidence={"1.0.0": "C:0;H:0;M:0;L:0"}),
            _row("b", vulns="C:0;H:1;M:0;L:0", current="0.5.0", min_high="1.0.0",
                 evidence={"1.0.0": "C:0;H:0;M:0;L:0"}),
        ]
        self.assertTrue(self._gate(rows, {"a": "1.0.0", "b": "1.0.0"}))

    def test_h1_low_within_limit_is_fine(self) -> None:
        # L:1 with maxKnownLow=1 stays within the policy: the gate must not
        # turn an allowed Low into a blocker (boundary keeps positives real).
        rows = [_row("l", vulns="C:0;H:0;M:0;L:1", current="1.0.0", min_vuln="1.0.0")]
        self.assertTrue(self._gate(rows, {}))

    def test_h1_fast_stop_event_requires_satisfied_policy(self) -> None:
        # The verified-orchestrator contract: the FAST stop event is emitted
        # only for a policy-satisfying verified candidate; a first
        # physically-passing but policy-failing candidate must NOT emit it,
        # and DEEP never stops here (it preserves the incumbent instead).
        incumbent = block_psi_anytime.BestVerifiedIncumbent(
            assignment={"a": "1.0.0"}, assignment_identity="id", changed_dependency_count=1,
            policy_score=1.0, yellow_coverage=1.0, distance_from_desired=0,
            deferred_targets=(), verification_evidence="ok", verified_at="now",
            run_identity="run", objective_rank=(0, 0, 0),
        )
        fast = block_psi_anytime.BaselineAnytimeState(policy=block_psi_anytime.AutomaticBudgetPolicy(strategy="fast"))
        fast.incumbent = incumbent
        # policy-failing candidate: no stop
        self.assertIsNone(fast.fast_policy_satisfied(False), "a policy-failing candidate must not stop Fast")
        # next policy-satisfying candidate: may stop Fast with the dedicated event
        self.assertEqual(
            fast.fast_policy_satisfied(True),
            block_psi_anytime.BaselineCompletionStatus.VERIFIED_POLICY_SATISFIED_FAST,
        )
        deep = block_psi_anytime.BaselineAnytimeState(policy=block_psi_anytime.AutomaticBudgetPolicy(strategy="deep"))
        deep.incumbent = incumbent
        self.assertIsNone(deep.fast_policy_satisfied(True), "Deep keeps improving and preserves the incumbent")


class H2PerProjectHealthRatioTests(unittest.TestCase):
    """H2: health gates and the planning reserve must use each project's OWN
    minLagOkPct (generate-all per-project map), not the global 80."""

    def _project_rows(self, lag_ok: int, lag_bad: int) -> list:
        rows = [_row(f"ok-{i}", min12="1.0.0") for i in range(lag_ok)]
        rows += [_row(f"bad-{i}", min12="9.0.0") for i in range(lag_bad)]
        return rows

    def test_h2_health_yellow_gate_uses_project_threshold(self) -> None:
        # Both projects have 8/10 compliant. app-a (80%) is at its threshold;
        # app-b (90%) needs one more compliant package and its health is RED
        # at 90% (80 < 90), not a misleading threshold pass.
        env = {"DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT": json.dumps({
            "app-a": {"targetLevel": "yellow", "minLagOkPct": 80},
            "app-b": {"targetLevel": "yellow", "minLagOkPct": 90},
        })}
        with mock.patch.dict(os.environ, env):
            a = compute_project_health(self._project_rows(8, 2), "app-a")
            b = compute_project_health(self._project_rows(8, 2), "app-b")
        self.assertEqual(a.lag_needed_for_yellow, 0, "app-a is at its own 80% threshold")
        self.assertEqual(b.lag_needed_for_yellow, 1, "app-b must still need one more compliant package")
        self.assertEqual(b.status, "red", "8/10 is red against the per-project 90% gate")
        self.assertIn("90%", b.reason)

    def test_h2_planning_reserve_uses_project_threshold(self) -> None:
        env = {"DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT": json.dumps({
            "app-b": {"targetLevel": "yellow", "minLagOkPct": 90},
        })}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(required_ratio_count(10, generator.health_yellow_ratio("app-b")), 9)
            self.assertEqual(required_ratio_count(10, generator.health_planning_ratio("app-b")), 10)


class H3GroupsConfigFingerprintTests(unittest.TestCase):
    """H3: the custom groups file is part of the Draft input fingerprint, so
    editing custom-groups.json makes a stale Draft stale (and the Node reader
    re-hashes the same names byte-lockstep)."""

    def test_h3_groups_config_is_in_input_files_and_makes_hash_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = ProjectSpec(name="app", path=root / "app")
            project.path.mkdir(parents=True, exist_ok=True)
            _write(project.path / "package.json", "{}")
            groups = root / "custom-groups.json"
            _write(groups, '{"groups": {"alpha": ["lodash"]}}')
            files = generator.draft_input_files_for(project, (), None, groups)
            self.assertIn(str(groups), files, "groups_config_path must be part of the Draft input fingerprint")
            before = generator.draft_input_hash(project, files)
            _write(groups, '{"groups": {"alpha": ["lodash", "react"]}}')
            after = generator.draft_input_hash(project, files)
            self.assertNotEqual(before, after, "editing custom-groups.json must make the Draft stale")

    def test_h3_normal_layout_stays_fresh_without_groups_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = ProjectSpec(name="app", path=root / "app")
            project.path.mkdir(parents=True, exist_ok=True)
            _write(project.path / "package.json", "{}")
            groups = root / "custom-groups.json"
            _write(groups, '{"groups": {"alpha": ["lodash"]}}')
            files = generator.draft_input_files_for(project, (), None, groups)
            first = generator.draft_input_hash(project, files)
            second = generator.draft_input_hash(project, files)
            self.assertEqual(first, second, "an unchanged layout must stay fresh")

    def test_h3_node_reader_hashes_group_files_lockstep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = ProjectSpec(name="app", path=root / "app")
            project.path.mkdir(parents=True, exist_ok=True)
            _write(project.path / "package.json", "{}")
            groups = root / "custom-groups.json"
            _write(groups, '{"groups": {"alpha": ["lodash"]}}')
            files = generator.draft_input_files_for(project, (), None, groups)
            python_hash = generator.draft_input_hash(project, files)
            node = NodeDriver(root)
            names = json.dumps([{"name": name} for name in files])
            out = node.run(f"""
import {{ draftInputHashFromFiles }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'draft-artifact-reader.js')}'
const inputFiles = {names}
const recorded = draftInputHashFromFiles('{_js(str(project.path))}', inputFiles)
console.log(JSON.stringify({{ recorded }}))
""")
            self.assertEqual(out["recorded"], python_hash, "Node reader must hash the groups file in lockstep")


class H4ClosureInsufficientEvidenceTests(unittest.TestCase):
    """H4: a status colour is not a measurement. Closure requires a measured
    lag share; without numeric evidence reached=false (honest UNKNOWN), and
    green must not ignore an explicitly non-zero High."""

    def _closure(self, raw: dict, target: str = "green") -> dict:
        node = NodeDriver(Path(tempfile.gettempdir()))
        payload = json.dumps(raw).replace("'", "\\'")
        return node.run(f"""
import {{ targetClosureFromRoadmap, targetClosureMessage }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'target-closure.js')}'
const roadmap = JSON.parse('{payload}')
const closure = targetClosureFromRoadmap(roadmap, 'demo', '{target}')
const message = targetClosureMessage(closure)
console.log(JSON.stringify({{ reached: closure.reached, insufficientData: closure.insufficientData ?? false, message }}))
""")

    def test_h4_colour_without_numeric_evidence_is_not_reached(self) -> None:
        out = self._closure({"project_health": {"demo": {"status": "green"}}, "projects": {"demo": []}})
        self.assertFalse(out["reached"], "a green colour without numeric evidence must not close the goal")
        self.assertTrue(out["insufficientData"], "the closure must say the data is insufficient")
        self.assertIn("недостаточно данных", out["message"])

    def test_h4_measured_roadmap_still_reaches_when_legit(self) -> None:
        out = self._closure({"project_health": {"demo": {"status": "yellow", "lag_ok_pct": 80, "critical": 0, "high": 0, "security_unknown": 0}}, "projects": {"demo": []}}, target="yellow")
        self.assertTrue(out["reached"], "a measured 80% yellow must still close")

    def test_h4_green_does_not_ignore_explicit_nonzero_high(self) -> None:
        out = self._closure({
            "project_health": {"demo": {"status": "green", "lag_ok_pct": 100, "high": 2, "critical": 0, "security_unknown": 0}},
            "projects": {"demo": []},
        })
        self.assertFalse(out["reached"], "green with an explicitly non-zero High must not be reached")


class H5BudgetPriorityTests(unittest.TestCase):
    """H5: the Desktop must not silently replace the declared FAST/DEEP mode
    budgets with the flat automatic override. Only an explicitly chosen user
    budget or a saved legacy budget supplies the override; otherwise the
    engine's mode defaults (fast=300s/2 attempts, deep=3600s/12) apply."""

    def test_h5_desktop_budget_override_priority(self) -> None:
        script = ROOT / "desktop" / "scripts" / "check-h5-budget-priority.mjs"
        ensure_dist_electron()
        done = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(done.returncode, 0, f"check-h5-budget-priority.mjs failed:\n{done.stdout}\n{done.stderr}")
        self.assertIn("OK", done.stdout)

    def test_h5_engine_mode_defaults_apply_without_override(self) -> None:
        with mock.patch.dict(os.environ, {
            "DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS": "",
            "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS": "",
            "DEPLOOM_MODE": "",
        }):
            self.assertEqual(generator._baseline_automatic_budget_seconds("fast"), 5 * 60)
            self.assertEqual(generator._baseline_automatic_budget_seconds("deep"), 60 * 60)
            self.assertEqual(generator._baseline_max_expensive_attempts(8, product_mode="fast"), 2)
            self.assertEqual(generator._baseline_max_expensive_attempts(8, product_mode="deep"), 12)

    def test_h5_engine_honours_explicit_override_when_present(self) -> None:
        with mock.patch.dict(os.environ, {
            "DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS": "1800",
            "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS": "3",
        }):
            self.assertEqual(generator._baseline_automatic_budget_seconds("deep"), 1800)
            self.assertEqual(generator._baseline_max_expensive_attempts(8, product_mode="deep"), 3)


if __name__ == "__main__":
    unittest.main()
