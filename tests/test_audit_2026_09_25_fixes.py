"""Regressions for the static audit of v0.2.133 (docs/audits/2026-09-25/).

A03: the report HTML must NOT recompute project health from the DOM with a
legacy hardcoded policy. The authoritative ProjectHealth the producer computed
with the versioned acceptance policy is embedded in REPORT_CONTEXT and wins;
the DOM fallback is conservative (whole active scope denominator, U never
yields green, non-zero security yields non-green).

A04: a projection is one metric in ONE universe. Removed-from-baseline
dependencies participate in the baseline-union closure view (union members)
but never inflate the current-scope projection's numerator over its own
denominator (the old code added removed_closed to the numerator only, which
could report >100% / 200%).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as generator
import block_psi_anytime as anytime
from dependency_live_roadmap_generator import DependencyRow, compute_project_health

TEMPLATE = Path(ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")


def _row(name: str, *, vulns: str = "C:0;H:0;M:0;L:0", current: str = "1.0.0",
         min12: str = "1.0.0", target_yellow: str | None = None,
         target_green: str | None = None, latest: str | None = None) -> DependencyRow:
    return DependencyRow(
        project="app",
        package_dir=".",
        name=name,
        kind="runtime",
        requested_spec="^1.0.0",
        current_version=current,
        current_source="lock",
        latest_version=latest or current,
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
        vuln_evidence_by_version={},
        target_yellow=target_yellow or "-",
        target_green=target_green or "-",
    )


# --- A04: projection universe -------------------------------------------------

class A04ProjectionUniverseTests(unittest.TestCase):
    """A04: removed-from-baseline deps never inflate the scope-only numerator;
    the union view adds them to BOTH members; percentages stay in [0, 100]."""

    REMOVED_BASELINE = {"directDependencies": {
        "dependencies:removed-a": "1.0.0",
        "dependencies:removed-b": "1.0.0",
    }}

    def _health(self, rows, baseline=None):
        return compute_project_health(rows, "app", baseline=baseline)

    def test_scope_projection_never_exceeds_scope_total_with_removed_closed(self) -> None:
        # 10/10 scope-compliant plus 2 removed-from-baseline deps. The scope
        # projection must stay 10/10 = 100%, NOT 12/10 = 120% (the old bug).
        rows = [_row(f"ok-{i}", min12="1.0.0") for i in range(10)]
        health = self._health(rows, baseline=self.REMOVED_BASELINE)
        self.assertEqual(2, health.removed, "removed_closed must be visible")
        self.assertEqual(10, health.scope_total)
        self.assertEqual(10, health.yellow_projected_lag_ok,
                         "A04: removed deps are not part of the current scope")
        self.assertEqual(100.0, health.yellow_projected_lag_pct)
        self.assertEqual(10, health.green_projected_lag_ok)
        self.assertEqual(100.0, health.green_projected_lag_pct)
        # The baseline-union closure view adds removal to BOTH members: the
        # closure is 12 of 12, still exactly 100 - never 200%.
        self.assertEqual(12, health.yellow_union_projected_lag_ok)
        self.assertEqual(12, health.scope_total + health.removed)
        self.assertEqual(100.0, health.yellow_union_projected_lag_pct)
        self.assertEqual(100.0, health.green_union_projected_lag_pct)

    def test_partial_scope_projection_keeps_removed_out_of_numerator(self) -> None:
        rows = [_row(f"ok-{i}", min12="1.0.0") for i in range(5)]
        rows += [_row(f"bad-{i}", min12="9.0.0") for i in range(5)]
        health = self._health(rows, baseline=self.REMOVED_BASELINE)
        self.assertEqual(10, health.scope_total)
        self.assertEqual(5, health.yellow_projected_lag_ok)
        self.assertEqual(50.0, health.yellow_projected_lag_pct)
        # Union view: 5 scope-ok + 2 removed over 12 -> 58.33%, not 70%.
        self.assertEqual(7, health.yellow_union_projected_lag_ok)
        self.assertEqual(12, health.yellow_union_projected_lag_ok + (health.scope_total - health.yellow_projected_lag_ok))
        self.assertAlmostEqual(7 / 12 * 100.0, health.yellow_union_projected_lag_pct, places=4)
        self.assertEqual(5, health.green_projected_lag_ok)
        self.assertEqual(7, health.green_union_projected_lag_ok)

    def test_planned_targets_count_in_projection(self) -> None:
        # 8 already-compliant + 2 rows where the YELLOW plan already moves the
        # package to its target: the yellow projection counts them, the green
        # projection (no green plan for those rows) does not.
        rows = [_row(f"ok-{i}", min12="1.0.0") for i in range(8)]
        rows += [_row(f"py-{i}", min12="2.0.0", current="1.0.0", target_yellow="2.0.0") for i in range(2)]
        health = self._health(rows)
        self.assertEqual(10, health.scope_total)
        self.assertEqual(10, health.yellow_projected_lag_ok, "yellow planned work must project as lag-ok")
        self.assertEqual(8, health.green_projected_lag_ok, "green sees only current compliance")
        self.assertEqual(100.0, health.yellow_projected_lag_pct)
        self.assertEqual(80.0, health.green_projected_lag_pct)
        self.assertEqual(10, health.yellow_union_projected_lag_ok)
        self.assertEqual(8, health.green_union_projected_lag_ok)

    def test_projection_invariants_hold_in_every_case(self) -> None:
        cases = []
        for ok in (0, 1, 5, 10):
            for bad in (0, 3):
                rows = [_row(f"ok-{i}", min12="1.0.0") for i in range(ok)]
                rows += [_row(f"bad-{i}", min12="9.0.0") for i in range(bad)]
                cases.append(rows)
        for rows in cases:
            health = self._health(rows, baseline=self.REMOVED_BASELINE)
            scope = health.scope_total
            for ok_field, pct_field, union_ok_field, union_pct_field in (
                ("yellow_projected_lag_ok", "yellow_projected_lag_pct",
                 "yellow_union_projected_lag_ok", "yellow_union_projected_lag_pct"),
                ("green_projected_lag_ok", "green_projected_lag_pct",
                 "green_union_projected_lag_ok", "green_union_projected_lag_pct"),
            ):
                okv = getattr(health, ok_field)
                pctv = getattr(health, pct_field)
                uok = getattr(health, union_ok_field)
                upct = getattr(health, union_pct_field)
                self.assertGreaterEqual(okv, 0)
                self.assertLessEqual(okv, scope, f"A04: {ok_field} must never exceed the scope total")
                self.assertLessEqual(pctv, 100.0, f"A04: {pct_field} must never exceed 100%")
                self.assertGreaterEqual(uok, okv)
                self.assertLessEqual(uok, scope + health.removed)
                self.assertGreaterEqual(upct, 0.0)
                self.assertLessEqual(upct, 100.0, f"A04: {union_pct_field} must never exceed 100%")
                self.assertAlmostEqual(upct, (uok / (scope + health.removed) * 100.0) if (scope + health.removed) else 100.0, places=4)


# --- A03: DOM health authority -------------------------------------------------

class A03DomHealthAuthorityTests(unittest.TestCase):
    """A03: the report HTML must render the authoritative ProjectHealth from
    REPORT_CONTEXT instead of recomputing it from the DOM with a legacy
    hardcoded 80% / M+L<=20 / U-ignored policy."""

    def _evaluate(self, rows_js: str, project_health_js: str) -> dict:
        probe = r"""
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
function extract(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function ${name} not found`);
  const end = source.indexOf('\nfunction ', start + 1);
  return source.slice(start, end).replaceAll('\\\\', '\\');
}
const evaluate = new Function('section', 'REPORT_CONTEXT', 'document', 'targetForRow', 'isActionTargetValue',
  ['semverParts', 'compareSemverText', 'lagComplianceTargetForDomRow', 'vulnerabilityCountsForDomRow', 'calculateProjectHealthFromDom']
    .map(extract).join('\n') + '\nreturn calculateProjectHealthFromDom(section);');
function row(current, lag, vulns = 'C:0, H:0, M:0, L:0, U:0', excluded = '0') {
  return { dataset: { current, vulns, lagThresholdMonths: '12', scopeExcluded: excluded }, getAttribute: () => lag };
}
const rows = __ROWS__;
const ctx = { projectHealth: __PROJECT_HEALTH__ };
const document = { getElementById: () => ({ value: 'default' }) };
const targetForRow = () => ({ value: '' });
const isActionTargetValue = () => false;
const result = evaluate({ dataset: { projectSection: 'fixture' }, querySelectorAll: () => rows }, ctx, document, targetForRow, isActionTargetValue, row);
console.log(JSON.stringify(result));
"""
        body = probe.replace("__ROWS__", rows_js).replace("__PROJECT_HEALTH__", project_health_js)
        script = ROOT / "probe-a03-2026-09-25.cjs"
        script.write_text(body, encoding="utf-8")
        try:
            done = subprocess.run(
                ["node", str(script), str(ROOT / "dependency_live_roadmap_generator.py")],
                capture_output=True, text=True, encoding="utf-8", timeout=120,
            )
            if done.returncode != 0:
                raise AssertionError(f"node probe failed: {done.stdout}\n{done.stderr}")
            return json.loads(done.stdout.strip().splitlines()[-1])
        finally:
            try:
                script.unlink()
            except OSError:
                pass

    def test_authoritative_project_health_wins_over_dom(self) -> None:
        # The DOM looks "100% healthy" (one known-ok row, rest with lag target),
        # but the producer measured 3/10 lag-ok over the whole active scope and
        # said RED. The report must render the authoritative numbers, never
        # recompute a legacy-policy status from the DOM.
        rows = "[" + ",".join([
            "{ dataset: { current: '1.0.0', vulns: 'C:0, H:0, M:0, L:0, U:0', lagThresholdMonths: '12', scopeExcluded: '0' }, getAttribute: () => '1.0.0' }",
            *["{ dataset: { current: '1.0.0', vulns: 'C:0, H:0, M:0, L:0, U:0', lagThresholdMonths: '12', scopeExcluded: '0' }, getAttribute: () => '9.0.0' }" for _ in range(9)],
        ]) + "]"
        ctx = json.dumps({
            "fixture": {
                "status": "red", "scope_total": 10, "scope_lag_ok": 3, "scope_lag_pct": 30.0,
                "lag_unknown": 0, "critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0,
                "reason": "только 30.0% активного scope соблюдают lag-policy",
                "yellow_plan_required": 8, "yellow_projected_lag_ok": 4,
                "yellow_projected_lag_pct": 40.0, "yellow_plan_shortfall": 4,
            },
        })
        result = self._evaluate(rows, ctx)
        self.assertEqual("red", result["status"])
        self.assertEqual(10, result["scope_total"])
        self.assertEqual(3, result["lag_ok_12m"])
        self.assertAlmostEqual(30.0, result["lag_ok_pct"], places=4)
        self.assertEqual(7, result["lag_bad_12m"])
        # A03: the DOM recompute must NOT re-derive a legacy-policy status; the
        # authoritative plan projections surface as rendered.
        self.assertEqual(8, result["yellow_plan_required"])
        self.assertEqual(4, result["yellow_projected_lag_ok"])
        self.assertEqual(40.0, result["yellow_projected_lag_pct"])
        self.assertEqual(4, result["yellow_plan_shortfall"])

    def test_authoritative_green_survives_misleading_dom(self) -> None:
        # Authoritative green with full scope compliance; the DOM (all rows
        # lagging) would have recomputed a red legacy status before the fix.
        rows = "[" + ",".join([
            "{ dataset: { current: '1.0.0', vulns: 'C:0, H:0, M:0, L:0, U:0', lagThresholdMonths: '12', scopeExcluded: '0' }, getAttribute: () => '1.0.0' }" for _ in range(10)
        ]) + "]"
        ctx = json.dumps({
            "fixture": {
                "status": "green", "scope_total": 10, "scope_lag_ok": 10, "scope_lag_pct": 100.0,
                "lag_unknown": 0, "critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0, "reason": "",
            },
        })
        result = self._evaluate(rows, ctx)
        self.assertEqual("green", result["status"])
        self.assertEqual(100.0, result["lag_ok_pct"])

    def test_fallback_uses_whole_active_scope_denominator(self) -> None:
        # No projectHealth entry: the conservative fallback must use the WHOLE
        # active scope as denominator (1 of 10 ok -> 10%, RED at the 80% gate),
        # not the researched share (1 of 1 -> 100%, the old misleading yellow).
        rows = "[" + ",".join([
            "{ dataset: { current: '1.0.0', vulns: 'C:0, H:0, M:0, L:0, U:0', lagThresholdMonths: '12', scopeExcluded: '0' }, getAttribute: () => '1.0.0' }",
            *["{ dataset: { current: '1.0.0', vulns: 'C:0, H:0, M:0, L:0, U:0', lagThresholdMonths: '12', scopeExcluded: '0' }, getAttribute: () => 'unknown' }" for _ in range(9)],
        ]) + "]"
        result = self._evaluate(rows, "{}")
        self.assertEqual("red", result["status"], "1/10 scope compliance must be red, not a legacy 100% yellow")
        self.assertAlmostEqual(10.0, result["lag_ok_pct"], places=4)
        self.assertEqual(9, result["lag_unknown"])

    def test_fallback_unrated_vulns_never_green(self) -> None:
        # U:1 (a detected finding without a severity rating) must never render
        # green in the fallback; the legacy formula ignored U entirely.
        result = self._evaluate(
            "[{ dataset: { current: '1.0.0', vulns: 'C:0, H:0, M:0, L:0, U:1', lagThresholdMonths: '12', scopeExcluded: '0' }, getAttribute: () => '1.0.0' }]",
            "{}",
        )
        self.assertNotEqual("green", result["status"], "U>0 must prevent green even at 100% lag compliance")
        self.assertEqual(1, result["unknown"])

    def test_fallback_green_control(self) -> None:
        result = self._evaluate(
            "[" + ",".join([
                "{ dataset: { current: '1.0.0', vulns: 'C:0, H:0, M:0, L:0, U:0', lagThresholdMonths: '12', scopeExcluded: '0' }, getAttribute: () => '1.0.0' }" for _ in range(10)
            ]) + "]",
            "{}",
        )
        self.assertEqual("green", result["status"])
        self.assertEqual(100.0, result["lag_ok_pct"])


class A09RestoreAuthorizationTests(unittest.TestCase):
    """A09: restore() rebuilds evidence/counters, never the user's current
    authorization. A brand-new explicit EXHAUSTIVE choice must survive a
    restore of an old bounded checkpoint; an old exhaustive checkpoint may
    only ADD BACK consent to a bounded resume."""

    def _state(self, mode: str):
        return anytime.BaselineAnytimeState(search_mode=anytime.BaselineSearchMode(mode))

    def _bounded_checkpoint(self, *, authorized: bool = False) -> dict:
        return {
            "searchMode": "BOUNDED_IMPROVEMENT", "searchStrategy": "exact-neighbor-refinement",
            "elapsedSeconds": 5.0, "expensiveAttempts": 3, "rejectedAssignments": 2,
            "consecutiveWithoutImprovement": 1, "learnedConstraintsAtLastRejection": 0,
            "repeatedPredicate": "", "repeatedPredicateCount": 0,
            "observedCandidateDurationSeconds": 1.5, "exhaustiveAuthorized": authorized,
            "continuationReason": "", "budgetSource": "user",
        }

    def test_fresh_exhaustive_not_regressed_by_old_bounded_checkpoint(self) -> None:
        # The audit repro: EXHAUSTIVE -> restore(old bounded) ->
        # exhaustive_authorized=False. The fresh grant must survive.
        state = self._state("EXHAUSTIVE")
        self.assertTrue(state.exhaustive_authorized, "a fresh EXHAUSTIVE state starts authorized")
        state.restore(self._bounded_checkpoint())
        self.assertTrue(state.exhaustive_authorized,
                        "A09: a new explicit EXHAUSTIVE choice must win over the stale checkpoint flag")
        # Evidence and counters still restore.
        self.assertEqual(3, state.expensive_attempts, "A09: evidence restore is untouched")
        self.assertAlmostEqual(5.0, state.elapsed_before_resume_seconds)

    def test_bounded_resume_over_exhaustive_checkpoint_stays_authorized(self) -> None:
        state = self._state("BOUNDED_IMPROVEMENT")
        self.assertFalse(state.exhaustive_authorized)
        state.restore(self._bounded_checkpoint(authorized=True))
        self.assertTrue(state.exhaustive_authorized,
                        "A09: a resumed run keeps the exhaustive consent its checkpoint carried")

    def test_bounded_resume_over_bounded_checkpoint_stays_unauthorized(self) -> None:
        state = self._state("BOUNDED_IMPROVEMENT")
        state.restore(self._bounded_checkpoint(authorized=False))
        self.assertFalse(state.exhaustive_authorized)

    def test_checkpoint_without_flag_keeps_current_grant(self) -> None:
        state = self._state("EXHAUSTIVE")
        state.restore({"searchMode": "BOUNDED_IMPROVEMENT", "expensiveAttempts": 1, "elapsedSeconds": 2.0})
        self.assertTrue(state.exhaustive_authorized,
                        "A09: a checkpoint without the flag must not revoke a fresh grant")


if __name__ == "__main__":
    unittest.main()