"""Validation #7 regressions: register N1-N5 as failing tests BEFORE the fixes.

Each test drives the production code paths the validation report actually
probed (Python generator helpers/health AND the compiled Node modules for the
dialog-budget chain and the closure), so a green result means the reported gap
is closed -- not that a helper returns a label.
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

import dependency_live_roadmap_generator as generator
from dependency_live_roadmap_generator import DependencyRow, compute_project_health
from tests._electron_dist import ensure_dist_electron


def _file_uri(path: Path) -> str:
    return "file:///" + str(path).replace("\\", "/")


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
        vuln_evidence_by_version=dict(evidence or {}),
    )


def _acceptance_path(env: dict) -> None:
    mock.patch.dict(os.environ, env).start()


class NodeDriver:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        ensure_dist_electron()

    def run(self, script: str) -> dict:
        target = self.tmp / f"probe-h7-{abs(hash(script))}.mjs"
        target.write_text(script, encoding="utf-8")
        done = subprocess.run(["node", str(target)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        if done.returncode != 0:
            raise AssertionError(f"node probe failed: {done.stdout}\n{done.stderr}")
        return json.loads(done.stdout.strip().splitlines()[-1])


class N1DialogBudgetExplicitnessTests(unittest.TestCase):
    """N1: a plain Fast/Deep choice must keep the declared mode budgets
    (fast=300s/2, deep=3600s/12). The dialog only carries an explicit budget
    once the user actually chose one; saving a default must never turn it into
    a user choice, and old intents with a real budget migrate as explicit."""

    def _probe(self, script: str) -> dict:
        return NodeDriver(Path(tempfile.gettempdir())).run(script)

    def test_n1_default_dialog_omits_budget(self) -> None:
        out = self._probe(f"""
import {{ handleDialogBudget }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'baseline-intent.js')}'
const d = handleDialogBudget({{ edited: false, planExplicit: false, shownMinutes: 30, planBudgetMinutes: 30 }})
const e = handleDialogBudget({{ edited: true, planExplicit: false, shownMinutes: 45, planBudgetMinutes: 30 }})
const saved = handleDialogBudget({{ edited: false, planExplicit: true, shownMinutes: 60, planBudgetMinutes: 60 }})
console.log(JSON.stringify({{ d, e, saved }}))
""")
        self.assertEqual(out["d"], {}, "an untouched budget field must NOT be carried as an explicit override")
        self.assertEqual(out["e"], {"budgetMinutes": 45, "budgetMinutesExplicit": True}, "an edited budget must be carried explicitly")
        self.assertEqual(out["saved"], {"budgetMinutes": 60, "budgetMinutesExplicit": True}, "a saved explicit budget must be preserved")

    def test_n1_normalize_flag_and_migration(self) -> None:
        out = self._probe(f"""
import {{ normalizeBudgetField }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'baseline-intent.js')}'
const cases = {{
  empty: normalizeBudgetField({{}}),
  default30: normalizeBudgetField({{ schemaVersion: 2, budgetMinutes: 30 }}),
  real45: normalizeBudgetField({{ schemaVersion: 2, budgetMinutes: 45 }}),
  flaggedTrue: normalizeBudgetField({{ schemaVersion: 2, budgetMinutes: 30, budgetMinutesExplicit: true }}),
  flaggedFalse: normalizeBudgetField({{ schemaVersion: 2, budgetMinutes: 30, budgetMinutesExplicit: false }}),
  legacyV1: normalizeBudgetField({{ schemaVersion: 1, budgetMinutes: 30 }}),
}}
console.log(JSON.stringify(cases))
""")
        self.assertFalse(out["empty"]["budgetMinutesExplicit"], "no budget anywhere is not a user choice")
        self.assertEqual(out["empty"]["budgetMinutes"], 30)
        self.assertFalse(out["default30"]["budgetMinutesExplicit"], "an implicit default-30 must not look like a user choice (N1)")
        self.assertTrue(out["real45"]["budgetMinutesExplicit"], "a really chosen non-default budget migrates as explicit")
        self.assertTrue(out["flaggedTrue"]["budgetMinutesExplicit"])
        self.assertFalse(out["flaggedFalse"]["budgetMinutesExplicit"])
        self.assertTrue(out["legacyV1"]["budgetMinutesExplicit"], "legacy v1 intents always carried a real budget")
        self.assertEqual(out["real45"]["budgetMinutes"], 45)

    def test_n1_full_chain_keeps_mode_defaults(self) -> None:
        # The chain the report probed: untouched dialog -> no explicit budget ->
        # baselineBudgetOverride returns {} -> engine keeps FAST/DEEP mode budgets,
        # and the SAVED intent round-trips as non-explicit so later runs do not
        # start treating the default as a user choice.
        out = self._probe(f"""
import {{ baselineBudgetOverride }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'baseline-budget.js')}'
import {{ handleDialogBudget, normalizeBudgetField }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'baseline-intent.js')}'
const dialog = handleDialogBudget({{ edited: false, planExplicit: false, shownMinutes: 30, planBudgetMinutes: 30 }})
const persisted = normalizeBudgetField({{ ...dialog, schemaVersion: 2 }})
const override = baselineBudgetOverride({{ explicitBudgetMinutes: dialog.budgetMinutes, persistedHasBudgetMinutes: persisted.budgetMinutesExplicit, clampedBudgetMinutes: 30 }})
const persistedAfterSave = normalizeBudgetField({{ schemaVersion: 2, budgetMinutes: 30, budgetMinutesExplicit: false }})
console.log(JSON.stringify({{ dialog, persisted, override, persistedAfterSave }}))
""")
        self.assertEqual(out["dialog"], {}, "default run must pass no budget")
        self.assertFalse(out["persisted"]["budgetMinutesExplicit"], "saving a default must not create a user choice")
        self.assertEqual(out["override"], {}, "no explicit budget -> mode defaults stay in effect")
        self.assertFalse(out["persistedAfterSave"]["budgetMinutesExplicit"], "a later run must still see no saved legacy budget")


class N2ExactVersionSecurityTests(unittest.TestCase):
    """N2: Fast must judge the EXACT version of each package in the verified
    candidate from its own OSV evidence -- a new version can be vulnerable again
    (1.0.0 H:1 -> 2.0.0 clean -> 3.0.0 H:1); missing evidence is unknown, never
    a version-comparison shortcut."""

    def _gate(self, rows, candidate, *, project="app") -> bool:
        current = {r.name: r.current_version for r in rows}
        return generator._candidate_satisfies_fast_policy(
            rows=rows,
            candidate_targets=candidate,
            current_targets=current,
            active_names=set(r.name for r in rows),
            project_name=project,
        )

    def _gate_zero_high(self, rows, candidate) -> bool:
        with mock.patch.dict(os.environ, {"DEPLOOM_ACCEPTANCE_POLICY_JSON": json.dumps({
            "targetLevel": "yellow", "minLagOkPct": 80, "maxKnownHigh": 0,
        })}):
            return self._gate(rows, candidate)

    def test_n2_revulnerable_newer_version_is_not_satisfied(self) -> None:
        # OSV per version: 1.0.0 -> H:1, 2.0.0 -> 0, 3.0.0 -> H:1.
        rows = [_row("a", vulns="C:0;H:1;M:0;L:0", current="1.0.0", min_high="2.0.0",
                     evidence={"1.0.0": "C:0;H:1", "2.0.0": "0", "3.0.0": "C:0;H:1"})]
        self.assertFalse(self._gate_zero_high(rows, {"a": "3.0.0"}),
                         "the exact candidate version 3.0.0 carries H:1 -> must not satisfy")

    def test_n2_exact_safe_version_is_satisfied(self) -> None:
        rows = [_row("a", vulns="C:0;H:1;M:0;L:0", current="1.0.0", min_high="2.0.0",
                     evidence={"1.0.0": "C:0;H:1", "2.0.0": "0", "3.0.0": "C:0;H:1"})]
        self.assertTrue(self._gate_zero_high(rows, {"a": "2.0.0"}),
                        "the exact candidate version 2.0.0 is clean")

    def test_n2_missing_evidence_for_exact_version_is_unknown(self) -> None:
        rows = [_row("a", vulns="C:0;H:1;M:0;L:0", current="1.0.0", min_high="2.0.0")]
        self.assertFalse(self._gate_zero_high(rows, {"a": "2.0.0"}),
                         "no OSV evidence for the exact candidate version must not claim safety")

    def test_n2_verified_cycle_never_emits_fast_stop(self) -> None:
        import block_psi_anytime
        rows = [_row("a", vulns="C:0;H:1;M:0;L:0", current="1.0.0", min_high="2.0.0",
                     evidence={"1.0.0": "C:0;H:1", "2.0.0": "0", "3.0.0": "C:0;H:1"})]
        satisfied = self._gate_zero_high(rows, {"a": "3.0.0"})
        self.assertFalse(satisfied, "the first physically-passing, security-unacceptable candidate fails the gate")
        fast = block_psi_anytime.BaselineAnytimeState(policy=block_psi_anytime.AutomaticBudgetPolicy(strategy="fast"))
        fast.incumbent = block_psi_anytime.BestVerifiedIncumbent(
            assignment={"a": "3.0.0"}, assignment_identity="id", changed_dependency_count=1,
            policy_score=1.0, yellow_coverage=1.0, distance_from_desired=0,
            deferred_targets=(), verification_evidence="ok", verified_at="now",
            run_identity="run", objective_rank=(0, 0, 0),
        )
        self.assertIsNone(fast.fast_policy_satisfied(satisfied),
                          "a security-unacceptable verified candidate must never stop Fast")


class N3PartialFixRemainingCountTests(unittest.TestCase):
    """N3: after exact-version evaluation the gate sums the REMAINING findings;
    a partial fix that leaves exactly the allowed High must satisfy, instead of
    demanding every overstated package be fully cleaned."""

    def _gate(self, rows, candidate, *, max_high: int) -> bool:
        current = {r.name: r.current_version for r in rows}
        with mock.patch.dict(os.environ, {"DEPLOOM_ACCEPTANCE_POLICY_JSON": json.dumps({
            "targetLevel": "yellow", "minLagOkPct": 80, "maxKnownHigh": max_high,
        })}):
            return generator._candidate_satisfies_fast_policy(
                rows=rows,
                candidate_targets=candidate,
                current_targets=current,
                active_names=set(r.name for r in rows),
                project_name="app",
            )

    def _two_high_rows(self) -> list:
        return [
            _row("a", vulns="C:0;H:1;M:0;L:0", current="1.0.0", min_high="2.0.0",
                 evidence={"1.0.0": "C:0;H:1", "2.0.0": "0"}),
            _row("b", vulns="C:0;H:1;M:0;L:0", current="1.0.0", min_high="2.0.0",
                 evidence={"1.0.0": "C:0;H:1", "2.0.0": "0"}),
        ]

    def test_n3_partial_fix_leaving_allowed_high_satisfies(self) -> None:
        rows = self._two_high_rows()
        # Policy allows High<=1 and the candidate fixes only `a`: one H remains.
        self.assertTrue(self._gate(rows, {"a": "2.0.0"}, max_high=1),
                        "one remaining High within the allowed limit must satisfy")
        # Without any fix both H remain -> aggregate 2 > 1 -> not satisfied.
        self.assertFalse(self._gate(rows, {}, max_high=1))

    def test_n3_boundaries_across_packages(self) -> None:
        rows = self._two_high_rows()
        # limit=1: fixing both -> 0 remaining -> satisfied; fixing none -> not.
        self.assertTrue(self._gate(rows, {"a": "2.0.0", "b": "2.0.0"}, max_high=1))
        # limit=0: fixing only one leaves 1 > 0 -> not satisfied; both -> yes.
        self.assertFalse(self._gate(rows, {"a": "2.0.0"}, max_high=0))
        self.assertTrue(self._gate(rows, {"a": "2.0.0", "b": "2.0.0"}, max_high=0))
        # limit=1 with three overstated packages: fixing two leaves 1 <= 1 -> ok.
        three = self._two_high_rows() + [
            _row("c", vulns="C:0;H:1;M:0;L:0", current="1.0.0", min_high="2.0.0",
                 evidence={"1.0.0": "C:0;H:1", "2.0.0": "0"}),
        ]
        self.assertTrue(self._gate(three, {"a": "2.0.0", "b": "2.0.0"}, max_high=1))
        self.assertFalse(self._gate(three, {"a": "2.0.0"}, max_high=1))


class N4ScopeHealthConsistencyTests(unittest.TestCase):
    """N4: the yellow status, lag_needed_for_yellow and projections must be
    judged on the FULL active scope (unknown rows are not compliant and never
    drop out of the goal denominator), matching the Fast gate and the closure."""

    def test_n4_one_of_ten_is_red_and_needs_seven(self) -> None:
        rows = [_row("ok", min12="1.0.0")] + [_row(f"u-{i}", min12="") for i in range(9)]
        h = compute_project_health(rows, "app")
        self.assertEqual(h.scope_total, 10)
        self.assertEqual(h.scope_lag_ok, 1)
        self.assertEqual("red", h.status, "10% of the scope must be red at the 80% gate, not a reached yellow")
        self.assertEqual(7, h.lag_needed_for_yellow, "an 80% goal over 10 packages needs 8 compliant; only 1 is known compliant")

    def test_n4_eight_of_ten_with_two_unknown_is_not_green(self) -> None:
        rows = [_row(f"ok-{i}", min12="1.0.0") for i in range(8)] + [_row(f"u-{i}", min12="") for i in range(2)]
        h = compute_project_health(rows, "app")
        self.assertEqual(8, h.scope_lag_ok)
        self.assertEqual("yellow", h.status, "80% of the scope is at the threshold, but the 2 unknown rows keep it yellow")
        self.assertEqual(0, h.lag_needed_for_yellow)
        self.assertEqual(2, h.scope_lag_unknown)

    def test_n4_nine_of_ten_unknown_reports_zero_scope_compliance(self) -> None:
        rows = [_row(f"u-{i}", min12="") for i in range(9)]
        h = compute_project_health(rows, "app")
        self.assertEqual(0, h.scope_lag_ok)
        self.assertEqual(0.0, h.scope_lag_pct)
        # Zero researched rows cannot be claimed as yellow; the honest answer
        # follows the established T3 contract: insufficient data (yellow label
        # with the "недостаточно данных" reason, never a measured red).
        self.assertEqual("yellow", h.status, "zero known rows is insufficient-data yellow, not a measured red")
        self.assertTrue(h.insufficient_data)
        self.assertEqual(8, h.lag_needed_for_yellow)  # ceil(9 * 0.8) == 8

    def test_n4_zero_known_is_insufficient_data_with_honest_need(self) -> None:
        rows = [_row(f"u-{i}", min12="") for i in range(10)]
        h = compute_project_health(rows, "app")
        self.assertTrue(h.insufficient_data)
        self.assertEqual(8, h.lag_needed_for_yellow, "even with no known rows, the whole-scope goal still needs 8 of 10")

    def test_n4_projections_use_scope_denominator(self) -> None:
        rows = [_row("ok", min12="1.0.0")] + [_row(f"u-{i}", min12="") for i in range(9)]
        h = compute_project_health(rows, "app")
        self.assertEqual(1, h.yellow_projected_lag_ok, "unknown rows contribute 0 to the projection")
        self.assertAlmostEqual(10.0, h.yellow_projected_lag_pct, places=1)
        # planning reserve 80 + 5 = 85% -> ceil(10*0.85) = 9 needed; projected 1 -> 8 short.
        self.assertEqual(8, h.yellow_plan_shortfall)

    def test_n4_fast_gate_matches_scope_health(self) -> None:
        rows = [_row("ok", min12="1.0.0")] + [_row(f"u-{i}", min12="") for i in range(9)]
        h = compute_project_health(rows, "app")
        current = {r.name: r.current_version for r in rows}
        fast_ok = generator._candidate_satisfies_fast_policy(
            rows=rows, candidate_targets={}, current_targets=current,
            active_names=set(r.name for r in rows), project_name="app",
        )
        self.assertFalse(fast_ok, "Fast and health must use the same whole-scope goal")
        self.assertNotEqual("green", h.status)


class N5ClosureSecurityEvidenceTests(unittest.TestCase):
    """N5: a colour is not proof of no security findings either -- closing a
    goal requires numeric Critical/High evidence and confirmed security
    coverage by scope; missing mandatory fields are insufficientData with a
    clear list of what is missing."""

    def _closure(self, raw: dict, target: str = "green") -> dict:
        node = NodeDriver(Path(tempfile.gettempdir()))
        payload = json.dumps(raw).replace("'", "\\'")
        return node.run(f"""
import {{ targetClosureFromRoadmap, targetClosureMessage }} from '{_file_uri(ROOT / 'desktop' / 'dist-electron' / 'target-closure.js')}'
const roadmap = JSON.parse('{payload}')
const closure = targetClosureFromRoadmap(roadmap, 'demo', '{target}')
console.log(JSON.stringify({{ reached: closure.reached, insufficientData: closure.insufficientData ?? false, message: targetClosureMessage(closure) }}))
""")

    def test_n5_colour_without_security_evidence_is_insufficient(self) -> None:
        out = self._closure({"project_health": {"demo": {"status": "green", "lag_ok_pct": 100}}, "projects": {"demo": []}})
        self.assertFalse(out["reached"], "a green colour without Critical/High/coverage numbers must not close the goal")
        self.assertTrue(out["insufficientData"])
        self.assertIn("недостаточно данных", out["message"])

    def test_n5_each_missing_mandatory_evidence_blocks_with_reason(self) -> None:
        base = {"status": "green", "lag_ok_pct": 100, "scope_lag_pct": 100,
                "critical": 0, "high": 0, "security_unknown": 0}
        for missing in ("critical", "high", "security_unknown"):
            raw = {"project_health": {"demo": {**base}}, "projects": {"demo": []}}
            del raw["project_health"]["demo"][missing]
            out = self._closure(raw)
            self.assertFalse(out["reached"], f"missing {missing} must not close the goal")
            self.assertTrue(out["insufficientData"], f"missing {missing} must be reported as insufficient data")
            self.assertIn("недостаточно данных", out["message"])

    def test_n5_full_correct_evidence_reaches(self) -> None:
        green = self._closure({
            "project_health": {"demo": {"status": "green", "lag_ok_pct": 100, "scope_lag_pct": 100,
                                        "scope_total": 1, "scope_lag_ok": 1, "scope_lag_unknown": 0,
                                        "critical": 0, "high": 0, "security_unknown": 0}},
            "projects": {"demo": []},
        })
        self.assertTrue(green["reached"], "a fully measured green road map must close")
        yellow = self._closure({
            "project_health": {"demo": {"status": "yellow", "lag_ok_pct": 80, "scope_lag_pct": 80,
                                        "scope_total": 5, "scope_lag_ok": 4, "scope_lag_unknown": 1,
                                        "critical": 0, "high": 0, "security_unknown": 0}},
            "projects": {"demo": []},
        }, target="yellow")
        self.assertTrue(yellow["reached"], "a fully measured yellow with security coverage must close")

    def test_n5_green_high_gate_still_honest_with_evidence(self) -> None:
        out = self._closure({
            "project_health": {"demo": {"status": "green", "lag_ok_pct": 100, "scope_lag_pct": 100,
                                        "critical": 0, "high": 2, "security_unknown": 0}},
            "projects": {"demo": []},
        })
        self.assertFalse(out["reached"], "an explicitly non-zero High is not a green closure even with evidence present")


if __name__ == "__main__":
    unittest.main()
