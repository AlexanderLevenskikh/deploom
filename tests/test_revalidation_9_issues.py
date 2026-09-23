"""Validation #9 regressions: C1 -- an OSV vulnerability without a severity
rating (U) must block Fast completion and Yellow/Green closure, and be reported
distinctly from a wholly unassessed OSV state, while proven zeros and allowed
remainders of known severities keep passing.

Each test drives the production code path the report actually probed: the real
`vuln_summary` on a bare OSV entry, the exact-version Fast gate, the generated
project health, the Draft plan counters and the compiled Node closure module.
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
from dependency_live_roadmap_generator import DependencyRow
from tests._electron_dist import ensure_dist_electron


def _file_uri(path: Path) -> str:
    return "file:///" + str(path).replace("\\", "/")


def _row(name: str, *, vulns: str = "C:0;H:0;M:0;L:0", current: str = "1.0.0",
         min12: str = "1.0.0", min_high: str = "2.0.0", evidence: dict | None = None) -> DependencyRow:
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
        min_no_critical="1.0.0",
        min_no_high=min_high,
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
        vuln_evidence_by_version=dict(evidence or {}),
    )


def _gate(rows, candidate, *, policy: dict) -> bool:
    current = {r.name: r.current_version for r in rows}
    with mock.patch.dict(os.environ, {"DEPLOOM_ACCEPTANCE_POLICY_JSON": json.dumps(
        {"targetLevel": "yellow", "minLagOkPct": 80, **policy}
    )}):
        return generator._candidate_satisfies_fast_policy(
            rows=rows, candidate_targets=candidate, current_targets=current,
            active_names=set(r.name for r in rows), project_name="app",
        )


class NodeDriver:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        ensure_dist_electron()

    def run(self, script: str) -> dict:
        target = self.tmp / f"probe-h9-{abs(hash(script))}.mjs"
        target.write_text(script, encoding="utf-8")
        done = subprocess.run(["node", str(target)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        if done.returncode != 0:
            raise AssertionError(f"node probe failed: {done.stdout}\n{done.stderr}")
        return json.loads(done.stdout.strip().splitlines()[-1])


class C1UnratedSeverityTests(unittest.TestCase):
    """C1: `U` (a detected finding without a severity rating) is not a proven
    zero. It must block policy completion on the EXACT chosen version, keep
    yellow/green closure at reached=false and be reported with an explicit
    reason -- while `0` and allowed remainders of rated severities still pass.
    """

    def test_c1_bare_osv_entry_is_unrated_and_distinct_from_zero(self) -> None:
        self.assertEqual(generator.vuln_summary([{"id": "OSV-UNRATED"}]), "U:1",
                         "a bare OSV entry with no database_specific.severity/CVSS is detected but unrated")
        self.assertEqual(generator.vuln_summary([]), "0",
                         "an empty OSV list is a PROVEN zero, a different state from U")

    def test_c1_fast_blocks_current_unrated_finding(self) -> None:
        rows = [_row("a", vulns="U:1", current="1.0.0")]
        self.assertFalse(_gate(rows, {}, policy={"maxKnownCritical": 0, "maxKnownHigh": 0}),
                         "a detected finding with no severity rating cannot confirm the Fast goal")

    def test_c1_fast_blocks_candidate_zero_to_unrated(self) -> None:
        rows = [_row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0",
                     evidence={"2.0.0": "U:1"})]
        self.assertFalse(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownCritical": 0, "maxKnownHigh": 0}),
                         "0 -> U:1 on the exact chosen version must not satisfy")
        # And the SAME unrated finding at the current version with no update.
        rows2 = [_row("a", vulns="U:1", current="1.0.0")]
        self.assertFalse(_gate(rows2, {}, policy={"maxKnownCritical": 0, "maxKnownHigh": 0}))

    def test_c1_fast_blocks_mixed_high_and_unrated(self) -> None:
        # High=1 is allowed by policy, but U>0 is not: the mixture must block.
        rows = [_row("a", vulns="C:0;H:1;M:0;L:0", current="1.0.0",
                     evidence={"2.0.0": "C:0;H:1;U:1"})]
        self.assertFalse(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownCritical": 0, "maxKnownHigh": 1}),
                         "allowed High remains but an unrated finding appears -> must not satisfy")

    def test_c1_fast_keeps_allowing_proven_zero_and_allowed_high(self) -> None:
        rows = [_row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0", evidence={"2.0.0": "0"})]
        self.assertTrue(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownCritical": 0, "maxKnownHigh": 0}),
                        "proven zero must keep satisfying (C1 must not over-block)")
        rows2 = [_row("a", vulns="C:0;H:1;M:0;L:0", current="1.0.0", evidence={"2.0.0": "C:0;H:1"})]
        self.assertTrue(_gate(rows2, {"a": "2.0.0"}, policy={"maxKnownCritical": 0, "maxKnownHigh": 1}),
                        "allowed remaining High must keep satisfying (N3 untouched)")

    def test_c1_fast_still_blocks_missing_evidence_for_chosen_version(self) -> None:
        # N2 semantics must survive: no evidence for the exact version is
        # unknown, and unknown never confirms the goal -- even when U=0.
        rows = [_row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0")]
        self.assertFalse(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownCritical": 0, "maxKnownHigh": 0}),
                         "plain absence of OSV evidence for the chosen version still blocks (N2)")

    def test_c1_unrated_never_emits_fast_stop(self) -> None:
        # Verified-assignment chain: U on the exact chosen version -> the gate
        # is False -> fast_policy_satisfied(False) is None for both strategies.
        import block_psi_anytime
        rows = [_row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0",
                     evidence={"2.0.0": "U:1"})]
        satisfied = _gate(rows, {"a": "2.0.0"}, policy={"maxKnownCritical": 0, "maxKnownHigh": 0})
        self.assertFalse(satisfied)
        for strategy in ("fast", "deep"):
            state = block_psi_anytime.BaselineAnytimeState(
                policy=block_psi_anytime.AutomaticBudgetPolicy(strategy=strategy))
            state.incumbent = block_psi_anytime.BestVerifiedIncumbent(
                assignment={"a": "2.0.0"}, assignment_identity="id", changed_dependency_count=1,
                policy_score=1.0, yellow_coverage=1.0, distance_from_desired=0,
                deferred_targets=(), verification_evidence="ok", verified_at="now",
                run_identity="run", objective_rank=(0, 0, 0),
            )
            self.assertIsNone(state.fast_policy_satisfied(satisfied),
                              f"{strategy} must not emit a fast-stop event for an unrated verified assignment")


class C1HealthAndClosureTests(unittest.TestCase):
    """C1: the health/Draft/closure trio must agree that U>0 means security is
    not fully accounted -- represented distinctly from 'OSV unavailable' and
    without double counting `unknown`."""

    def _health(self, rows):
        return generator.compute_project_health(rows, "app")

    def test_c1_health_marks_unrated_security_not_covered(self) -> None:
        h = self._health([_row("a", vulns="U:1", current="1.0.0")])
        self.assertEqual(h.unknown, 1, "U aggregate flows into the unknown total (unchanged semantics)")
        self.assertEqual(h.security_unknown, 1, "a U:1 row must NOT be full security coverage")
        self.assertEqual(h.security_unrated, 1, "the unrated-vs-unassessed split must be explicit")
        self.assertNotEqual(h.status, "green")
        self.assertIn("без оценки серьёзности", h.reason, "the user needs an explicit reason for the U")

    def test_c1_health_does_not_double_count_unrated(self) -> None:
        # One row with two unrated findings (U:2): one unrated ROW, two findings.
        h = self._health([_row("a", vulns="U:2", current="1.0.0")])
        self.assertEqual(h.unknown, 2)
        self.assertEqual(h.security_unrated, 1)
        self.assertEqual(h.security_unknown, 1)

    def test_c1_unassessed_osv_still_its_own_state(self) -> None:
        h = self._health([_row("a", vulns="unknown", current="1.0.0")])
        self.assertEqual(h.security_unknown, 1)
        self.assertEqual(h.security_unrated, 0, "an unassessed OSV state is not an unrated finding")
        self.assertEqual(h.unknown, 1)
        self.assertIn("security неизвестна", h.reason)
        self.assertNotIn("без оценки серьёзности", h.reason, "unassessed must keep its own reason")

    def test_c1_health_generator_to_closure_yellow_not_reached(self) -> None:
        # The FULL chain the report probed: generator health -> roadmap JSON ->
        # compiled targetClosureFromRoadmap. With a U:1 row and an empty plan,
        # yellow must stay reached=false even though lag=100%, Critical=0, High=0.
        h = self._health([_row("a", vulns="U:1", current="1.0.0")])
        health = {k: v for k, v in vars(h).items() if not k.startswith("_")}
        health.pop("lag_blockers", None)
        node = NodeDriver(Path(tempfile.gettempdir()))
        payload = json.dumps({"project_health": {"app": health}, "projects": {"app": []}}, ensure_ascii=False).replace("'", "\\'")
        out = node.run(
            "import { targetClosureFromRoadmap } from '" + _file_uri(ROOT / "desktop" / "dist-electron" / "target-closure.js") + "'\n"
            "const roadmap = JSON.parse('" + payload + "')\n"
            "const yellow = targetClosureFromRoadmap(roadmap, 'app', 'yellow')\n"
            "const green = targetClosureFromRoadmap(roadmap, 'app', 'green')\n"
            "console.log(JSON.stringify({ yellowReached: yellow.reached, greenReached: green.reached, securityUnknown: yellow.securityUnknown }))\n"
        )
        self.assertFalse(out["yellowReached"], "an unrated finding must keep yellow reached=false")
        self.assertFalse(out["greenReached"], "an unrated finding must keep green reached=false")
        self.assertGreater(out["securityUnknown"], 0, "closure must see the security uncertainty")

    def test_c1_closure_does_not_trust_security_unknown_zero_over_unknown(self) -> None:
        # The priority bug: `security_unknown: 0` with `unknown: 1` previously
        # short-circuited closure into full coverage. Legacy/inconsistent
        # roadmaps must not close on that reading either.
        node = NodeDriver(Path(tempfile.gettempdir()))
        raw = {"project_health": {"demo": {
            "status": "yellow", "lag_ok_pct": 80, "scope_lag_pct": 80,
            "scope_total": 5, "scope_lag_ok": 4, "scope_lag_unknown": 1,
            "critical": 0, "high": 0, "security_unknown": 0, "unknown": 1,
        }}, "projects": {"demo": []}}
        payload = json.dumps(raw).replace("'", "\\'")
        out = node.run(
            "import { targetClosureFromRoadmap } from '" + _file_uri(ROOT / "desktop" / "dist-electron" / "target-closure.js") + "'\n"
            "const roadmap = JSON.parse('" + payload + "')\n"
            "const closure = targetClosureFromRoadmap(roadmap, 'demo', 'yellow')\n"
            "console.log(JSON.stringify({ reached: closure.reached, securityUnknown: closure.securityUnknown }))\n"
        )
        self.assertFalse(out["reached"], "unknown>0 must keep yellow reached=false even when security_unknown=0")
        self.assertGreater(out["securityUnknown"], 0)

    def test_c1_draft_plan_counts_and_reasons_unrated(self) -> None:
        row = _row("a", vulns="U:1", current="1.0.0")
        health = generator.compute_project_health([row], "app")
        plan = generator.build_draft_plan({"app": [row]}, {}, {"app": health})
        self.assertEqual(plan["counts"]["unknown-security"], 1,
                         "an unrated finding must surface in the Draft security-unknown counter")
        entry = next(u for u in plan["unknowns"] if u["package"] == "a")
        self.assertEqual(entry["clarity"], "security")
        self.assertIn("U", entry["reason"], "the Draft reason must call out the missing severity rating")
        self.assertNotIn("OSV/security state unknown", entry["reason"],
                         "an unrated finding is not the same as an unassessed OSV state")

    def test_c1_draft_plan_still_reasons_unassessed_osv_separately(self) -> None:
        un = _row("a", vulns="unknown", current="1.0.0")
        ok = _row("b", vulns="0", current="1.0.0")
        health = generator.compute_project_health([un, ok], "app")
        plan = generator.build_draft_plan({"app": [un, ok]}, {}, {"app": health})
        entry = next(u for u in plan["unknowns"] if u["package"] == "a")
        self.assertIn("OSV/security state unknown", entry["reason"])
        self.assertEqual(plan["counts"]["unknown-security"], 1, "'0' row must not count")


if __name__ == "__main__":
    unittest.main()
