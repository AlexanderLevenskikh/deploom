"""Validation #8 regressions: B1 fast-gate must evaluate the EXACT chosen
version for every active package and severity (not only rows whose CURRENT
version already exceeds a limit), B2 the dev harness must load the same base
theme as the production shell so the visual CTA check is meaningful.

Each test drives the production code path the report actually probed; a green
result means the gap is closed, not that a label matches.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as generator
from dependency_live_roadmap_generator import DependencyRow


def _row(name: str, *, vulns: str = "C:0;H:0;M:0;L:0", current: str = "1.0.0",
         min12: str = "1.0.0", min_high: str = "1.0.0",
         evidence: dict | None = None) -> DependencyRow:
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
            rows=rows,
            candidate_targets=candidate,
            current_targets=current,
            active_names=set(r.name for r in rows),
            project_name="app",
        )


class B1FastGateExactVersionAlwaysTests(unittest.TestCase):
    """B1: the exact chosen version's OSV evidence must be checked for EVERY
    active package and severity, including packages with zero findings on the
    CURRENT version. The old gate skipped evaluation whenever the current
    aggregate was already inside the policy limit, so a candidate could
    introduce a finding (or an unknown) without the gate noticing."""

    def test_b1_clean_current_to_vulnerable_candidate_blocks_at_zero_limit(self) -> None:
        # Current 1.0.0 is clean; the lag-update to 2.0.0 carries H:1 and the
        # policy allows High=0. The chosen version violates the limit.
        rows = [_row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0",
                     evidence={"2.0.0": "C:0;H:1;M:0;L:0"})]
        self.assertFalse(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownHigh": 0}),
                         "the chosen 2.0.0 has H:1 under a High=0 policy -> must not satisfy")

    def test_b1_clean_current_to_unknown_candidate_blocks(self) -> None:
        # No OSV evidence for the chosen 2.0.0 at all: Fast must not claim the
        # goal on a version it cannot measure.
        rows = [_row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0")]
        self.assertFalse(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownHigh": 1}),
                         "no evidence for the exact chosen version is unknown, not safe")

    def test_b1_growth_inside_current_allowance_is_checked(self) -> None:
        # A11: the Fast gate counts PACKAGES per severity, matching the
        # acceptance reader and the renderer. Two distinct packages with a High
        # finding exceed a limit of 1 package even when every current version is
        # H:0 - the candidate INTRODUCES the second High package.
        rows = [
            _row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0", min_high="2.0.0",
                 evidence={"1.0.0": "0", "2.0.0": "C:0;H:1"}),
            _row("b", vulns="C:0;H:0;M:0;L:0", current="1.0.0", min_high="2.0.0",
                 evidence={"1.0.0": "0", "2.0.0": "C:0;H:1"}),
        ]
        # Only `a` moves to a High version: now exactly 1 High package -> OK.
        self.assertTrue(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownHigh": 1}),
                         "exactly one High package within the package-name limit -> must satisfy (A11 unit)")
        # Both move: 2 High packages exceed the 1-package limit.
        self.assertFalse(_gate(rows, {"a": "2.0.0", "b": "2.0.0"}, policy={"maxKnownHigh": 1}),
                         "second High package introduced by the candidate must not satisfy")

    def test_b1_multiple_findings_in_one_package_count_as_one_package(self) -> None:
        # A11: one package with High:2 still counts as ONE High package, so it
        # stays within a limit of 1 - the acceptance reader (package names) and
        # the renderer count the same unit, and Fast must not be stricter than
        # the final acceptance verdict it feeds.
        rows = [_row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0", min_high="2.0.0",
                     evidence={"1.0.0": "0", "2.0.0": "C:0;H:2"})]
        self.assertTrue(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownHigh": 1}),
                         "a single package with two findings equals one High package within the limit")
        self.assertFalse(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownHigh": 0}),
                         "one High package still exceeds a zero-package tolerance")

    def test_b1_clean_to_clean_candidate_still_satisfies(self) -> None:
        # Positive control: 0 -> 0 under a zero-tolerance policy stays green.
        rows = [_row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0",
                     evidence={"2.0.0": "0"})]
        self.assertTrue(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownHigh": 0}),
                        "0 -> 0 keeps the goal satisfiable")

    def test_b1_partial_fix_remaining_allowed_high_still_satisfies(self) -> None:
        # N3 preservation: 2 x H:1 at limit 1; the candidate fixes only `a`,
        # one H remains == the allowed limit -> must still satisfy.
        rows = [
            _row("a", vulns="C:0;H:1;M:0;L:0", current="1.0.0", min_high="2.0.0",
                 evidence={"1.0.0": "C:0;H:1", "2.0.0": "0"}),
            _row("b", vulns="C:0;H:1;M:0;L:0", current="1.0.0", min_high="2.0.0",
                 evidence={"1.0.0": "C:0;H:1", "2.0.0": "0"}),
        ]
        self.assertTrue(_gate(rows, {"a": "2.0.0"}, policy={"maxKnownHigh": 1}),
                        "one remaining High within the allowed limit must satisfy (N3)")
        # And leaving both H (aggregate 2) still fails.
        self.assertFalse(_gate(rows, {}, policy={"maxKnownHigh": 1}))

    def test_b1_verified_assignment_with_introduced_finding_never_emits_fast_stop(self) -> None:
        # The report: verify the gate on a REAL verified assignment before the
        # progressive-fast-policy-satisfied event, not only the boolean
        # wrapper. The incumbent carries the exact same assignment the gate
        # evaluated False for -> Fast must not stop, DEEP never stops.
        import block_psi_anytime
        rows = [_row("a", vulns="C:0;H:0;M:0;L:0", current="1.0.0",
                     evidence={"2.0.0": "C:0;H:1;M:0;L:0"})]
        chosen = {"a": "2.0.0"}
        satisfied = _gate(rows, chosen, policy={"maxKnownHigh": 0})
        self.assertFalse(satisfied, "the verified assignment introduces H:1 -> policy not satisfied")
        for strategy in ("fast", "deep"):
            state = block_psi_anytime.BaselineAnytimeState(
                policy=block_psi_anytime.AutomaticBudgetPolicy(strategy=strategy))
            state.incumbent = block_psi_anytime.BestVerifiedIncumbent(
                assignment=chosen, assignment_identity="id", changed_dependency_count=1,
                policy_score=1.0, yellow_coverage=1.0, distance_from_desired=0,
                deferred_targets=(), verification_evidence="ok", verified_at="now",
                run_identity="run", objective_rank=(0, 0, 0),
            )
            self.assertIsNone(state.fast_policy_satisfied(satisfied),
                              f"{strategy} must not emit a fast-stop event for a policy-failing verified assignment")

    def test_b1_real_cycle_wires_gate_before_fast_stop_event(self) -> None:
        # Source contract: the live verified cycle calls the gate on the actual
        # incumbent assignment and feeds its boolean into fast_policy_satisfied
        # before the progressive-fast-policy-satisfied finalize.
        source = Path(ROOT / "dependency_live_roadmap_generator.py").read_text("utf-8")
        self.assertIn("def _verified_assignment_satisfies_policy(", source)
        self.assertIn("_candidate_satisfies_fast_policy(", source)
        segment = source[source.index("def _continue_after_verified_candidate("):]
        self.assertIn("anytime.fast_policy_satisfied(", segment)
        self.assertIn("_verified_assignment_satisfies_policy(", segment)
        self.assertIn("_finalize_verified_incumbent(\n                        \"progressive-fast-policy-satisfied\"", segment)


class B2HarnessThemeContractTests(unittest.TestCase):
    """B2: the dev dialog harness must load the SAME base theme as the
    production shell (index.css defines --blue/--blue-hover used by
    .button.primary), otherwise the visual CTA check is meaningless (white
    text on a transparent button -- exactly what the report measured)."""

    def test_b2_harness_loads_index_css_before_app_css(self) -> None:
        harness = Path(ROOT / "desktop" / "src" / "e2e" / "dialog-harness.tsx").read_text("utf-8")
        self.assertIn("import '../index.css'", harness, "harness must import the base theme (index.css)")
        self.assertIn("import '../App.css'", harness, "harness must import the component styles")
        self.assertLess(harness.index("import '../index.css'"), harness.index("import '../App.css'"),
                        "index.css must be imported first so its variables exist when App.css is evaluated")

    def test_b2_harness_exposes_style_probe_for_primary(self) -> None:
        harness = Path(ROOT / "desktop" / "src" / "e2e" / "dialog-harness.tsx").read_text("utf-8")
        self.assertIn("__harnessStyles", harness,
                      "harness must expose a runtime probe reporting the primary button's computed styles")

    def test_b2_production_shell_imports_theme(self) -> None:
        main = Path(ROOT / "desktop" / "src" / "main.tsx").read_text("utf-8")
        self.assertIn("import './index.css'", main, "production shell loads index.css")

    def test_b2_primary_button_uses_contrast_safe_fill_in_both_themes(self) -> None:
        # The bright dark-theme --blue (#6ea8ff) behind white text measured
        # 2.41:1 (fails AA). The primary button must use the dedicated solid
        # fill in EVERY theme block, not the bright accent.
        index_css = Path(ROOT / "desktop" / "src" / "index.css").read_text("utf-8")
        self.assertGreaterEqual(index_css.count("--blue-fill: #1769e0;"), 3,
                                "light, html[data-theme=dark] and prefers-color-scheme:dark must all pin the solid fill")
        app_css = Path(ROOT / "desktop" / "src" / "App.css").read_text("utf-8")
        self.assertIn("background: var(--blue-fill, var(--blue))", app_css,
                      "primary must resolve to the contrast-safe fill, not the bright accent")


if __name__ == "__main__":
    unittest.main()
