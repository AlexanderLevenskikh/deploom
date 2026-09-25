#!/usr/bin/env python3
"""Hermetic contract for the Verified Baseline FAST budget fix (Block Psi).

Covers (P1-1) propagation of the FAST wall-clock budget into verifier phases,
(P1-2) the honest budget-exhausted-after-one-candidate outcome, and (P2) the
structured failure envelope / recovery text / bounded scrubbed check log.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PLATEAU_SOURCE = "soft-budget-ended-without-fresh-authoritative-progress"
_HONEST_REASON = "budget-exhausted-after-one-candidate"


class VerifiedBaselineFastBudgetTests(unittest.TestCase):
    maxDiff = None

    # ------------------------------------------------------------------ P1-1

    def test_verifier_config_carries_budget_phase_deadline(self) -> None:
        from baseline_constraint_verifier import BaselineVerifyConfig

        parsed = BaselineVerifyConfig.from_mapping(
            {"budgetPhaseDeadline": 1234.5, "commands": ["yarn lint:types"]}
        )
        self.assertEqual(1234.5, parsed.budget_phase_deadline)
        parsed_underscore = BaselineVerifyConfig.from_mapping(
            {"budget_phase_deadline": 99.0}
        )
        self.assertEqual(99.0, parsed_underscore.budget_phase_deadline)
        self.assertIsNone(BaselineVerifyConfig().budget_phase_deadline)

    def test_budget_deadline_hit_helper(self) -> None:
        from baseline_constraint_verifier import (
            BaselineVerifyConfig,
            _budget_deadline_hit,
        )

        now = time.monotonic()
        self.assertFalse(
            _budget_deadline_hit(BaselineVerifyConfig())
        )
        self.assertFalse(
            _budget_deadline_hit(
                BaselineVerifyConfig(budget_phase_deadline=now + 60)
            )
        )
        self.assertTrue(
            _budget_deadline_hit(
                BaselineVerifyConfig(budget_phase_deadline=now - 1)
            )
        )

    def test_budget_cutoff_exception_shape(self) -> None:
        from baseline_constraint_verifier import BaselineCandidateBudgetExceeded

        exc = BaselineCandidateBudgetExceeded("resolver", "label", -7)
        self.assertEqual(0, exc.seconds)
        self.assertEqual("resolver", exc.phase)
        self.assertIn("BASELINE_BUDGET_EXHAUSTED", str(exc))
        self.assertNotIn("infrastructure", str(exc).lower())

    def test_clamped_phase_seconds_binds_phase_to_budget_deadline(self) -> None:
        from baseline_constraint_verifier import (
            BaselineCandidateBudgetExceeded,
            _clamped_phase_seconds,
        )

        def clamp(*, budget_remaining_seconds=None, attempt_remaining_seconds=1800):
            return _clamped_phase_seconds(
                attempt_remaining_seconds=attempt_remaining_seconds,
                budget_remaining_seconds=budget_remaining_seconds,
                timeout_seconds=600,
                attempt_timeout_seconds=3600,
                progress_label="phase",
            )
        # No shared budget: legacy per-phase timeout rules.
        self.assertEqual(600, clamp())
        # A small remaining budget binds the phase (a hung check is killed at
        # deadline+cleanup, not at its own 600s timeout).
        self.assertEqual(17, clamp(budget_remaining_seconds=17.9))
        # The per-attempt deadline still binds when smaller than the timeout.
        self.assertEqual(120, clamp(attempt_remaining_seconds=120, budget_remaining_seconds=5000))
        # An already-expired budget deadline aborts the candidate honestly.
        with self.assertRaises(BaselineCandidateBudgetExceeded) as ctx:
            clamp(budget_remaining_seconds=-3)
        self.assertEqual("phase", ctx.exception.phase)
        self.assertIn("BASELINE_BUDGET_EXHAUSTED", str(ctx.exception))
        # An exhausted per-attempt deadline stays an infrastructure timeout.
        with self.assertRaises(subprocess.TimeoutExpired):
            clamp(attempt_remaining_seconds=-1)


    def test_phase_timeout_clamps_to_budget_deadline_in_source(self) -> None:
        source = (
            ROOT / "baseline_constraint_verifier.py"
        ).read_text(encoding="utf-8")
        # The clamp must be present in the ACTIVE (legacy-bound) verification
        # implementation, and budget-deadline timeouts must resolve to a
        # "budget" kind instead of infrastructure.
        self.assertIn("config.budget_phase_deadline - now", source)
        self.assertIn("_clamped_phase_seconds(", source)
        self.assertGreaterEqual(
            source.count("raise BaselineCandidateBudgetExceeded("), 1
        )
        resolver_site = source.index("package-manager verification timed out")
        self.assertIn(
            "except BaselineCandidateBudgetExceeded as exc:",
            source[source.rindex("phase_timeout()", 0, resolver_site):resolver_site],
        )
        self.assertIn('"budget"', source)
        self.assertIn("if _budget_deadline_hit(config):", source)

    def test_candidate_budget_deadline_helper(self) -> None:
        import dependency_live_roadmap_generator as roadmap
        from block_psi_anytime import (
            AutomaticBudgetPolicy,
            BaselineAnytimeState,
            BaselineSearchMode,
        )

        fast = BaselineAnytimeState(
            policy=AutomaticBudgetPolicy(
                wall_clock_seconds=30, max_expensive_attempts=2, strategy="fast"
            ),
            search_mode=BaselineSearchMode.AUTO,
            clock=lambda: 100.0,
        )
        deadline = roadmap._candidate_budget_deadline(fast)
        now = time.monotonic()
        self.assertIsNotNone(deadline)
        assert deadline is not None
        self.assertGreater(deadline, now)
        self.assertLessEqual(deadline, now + 31)

        deep = BaselineAnytimeState(
            policy=AutomaticBudgetPolicy(wall_clock_seconds=3600, strategy="deep"),
            search_mode=BaselineSearchMode.AUTO,
            clock=lambda: 100.0,
        )
        self.assertIsNone(roadmap._candidate_budget_deadline(deep))

    def test_with_candidate_deadline_preserves_config_identity(self) -> None:
        from baseline_constraint_verifier import BaselineVerifyConfig

        import dependency_live_roadmap_generator as roadmap

        original = BaselineVerifyConfig(commands=("yarn lint:types",))
        wrapped = roadmap._with_candidate_deadline(original, 12.3)
        self.assertEqual(
            original,
            dataclasses.replace(wrapped, budget_phase_deadline=None),
        )
        self.assertEqual(
            12.3, wrapped.budget_phase_deadline
        )
        self.assertIs(original, roadmap._with_candidate_deadline(original, None))

    def test_anytime_state_records_budget_metadata(self) -> None:
        from block_psi_anytime import (
            AutomaticBudgetPolicy,
            BaselineAnytimeState,
            BaselineSearchMode,
        )

        state = BaselineAnytimeState(
            policy=AutomaticBudgetPolicy(
                wall_clock_seconds=30, max_expensive_attempts=2, strategy="fast"
            ),
            search_mode=BaselineSearchMode.AUTO,
            clock=lambda: 100.0,
        )
        state.budget_source = "product-default:fast"
        state.candidate_phase_deadline_seconds = 29.5
        state.last_failed_check_phase = "exact-confirmation"
        state.last_failed_check_command = "yarn lint:types"
        state.last_failed_check_exit_code = 2
        snap = state.snapshot()
        self.assertEqual("product-default:fast", snap["budgetSource"])
        self.assertAlmostEqual(29.5, snap["candidatePhaseDeadlineSeconds"])
        self.assertEqual("exact-confirmation", snap["lastFailedCheckPhase"])
        self.assertEqual("yarn lint:types", snap["lastFailedCheckCommand"])
        self.assertEqual(2, snap["lastFailedCheckExitCode"])
        # restore() must not break on the new fields (absent in legacy snapshots)
        state.restore(snap)
        self.assertEqual("product-default:fast", state.budget_source)

    def test_budget_source_helper(self) -> None:
        import dependency_live_roadmap_generator as roadmap

        old_budget = os.environ.get("DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS")
        old_attempts = os.environ.get("DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS")
        try:
            os.environ.pop("DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS", None)
            os.environ.pop("DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS", None)
            self.assertEqual("product-default:fast", roadmap._baseline_budget_source("fast"))
            self.assertEqual("product-default:deep", roadmap._baseline_budget_source("deep"))
        finally:
            if old_budget is None:
                os.environ.pop("DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS", None)
            else:
                os.environ["DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS"] = old_budget
            if old_attempts is None:
                os.environ.pop("DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS", None)
            else:
                os.environ["DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS"] = old_attempts

    # ------------------------------------------------------------------ P1-2

    def test_cutoff_error_is_honest_budget_exhausted_plateau(self) -> None:
        import dependency_live_roadmap_generator as roadmap

        err = roadmap.BaselineBudgetExceededCutoff(
            project="app",
            mode="yellow",
            iteration="1",
            assignment="fp123",
            phase="adaptive-screen",
            command="yarn lint:types",
            predicate="ts-module-resolution:@vitejs/plugin-react",
            elapsed_seconds="18.5",
            output_tail="TS2307 Cannot find module",
            budget_source="env:DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS",
            wall_clock_seconds=300,
            exhaustive_authorized=True,
        )
        message = str(err)
        # Non-retry gating and honest formulation coexist.
        self.assertIn("BASELINE_BUDGET_EXHAUSTED", message)
        self.assertIn("BASELINE_VERIFICATION_PLATEAU", message)
        self.assertIn("budget-exhausted-after-one-candidate", message)
        self.assertIn("проверка типов кандидата не прошла", message)
        self.assertIn("yarn lint:types", message)
        self.assertIn("не отсутствие решения", message)
        self.assertNotIn("UNSAT", message)
        self.assertNotIn("unsolvable", message)
        self.assertEqual(
            roadmap.BaselineTerminalStatus.PLATEAU.value, err.terminal_status
        )
        self.assertEqual("BASELINE_VERIFICATION_PLATEAU", err.stop_code)
        self.assertEqual("solve-and-verify-budget-cutoff", err.terminal_source)
        self.assertEqual("app", err.failure_context.get("project"))
        self.assertEqual("yellow", err.failure_context.get("mode"))
        self.assertEqual("1", err.failure_context.get("iteration"))
        self.assertEqual("fp123", err.failure_context.get("assignment"))
        self.assertEqual("adaptive-screen", err.failure_context.get("phase"))
        self.assertEqual("AUTOMATIC_BUDGET_EXHAUSTED", err.failure_context.get("continuationReason"))
        self.assertEqual("true", err.failure_context.get("exhaustiveAuthorized"))
        self.assertEqual("yarn lint:types", err.check_evidence.get("command"))
        self.assertEqual(
            "ts-module-resolution:@vitejs/plugin-react",
            err.check_evidence.get("predicate"),
        )

    def test_terminal_error_carries_structured_context(self) -> None:
        import dependency_live_roadmap_generator as roadmap

        err = roadmap._baseline_terminal_error(
            roadmap.BaselineTerminalStatus.PLATEAU,
            "BASELINE_VERIFICATION_PLATEAU",
            "some message",
            source="solve-and-verify",
            project="app",
            mode="yellow",
            iteration="2",
            assignment="fp",
            phase="exact-confirmation",
            command="yarn lint:types",
            predicate="p1",
            elapsed_seconds="904.1",
            exhaustive_authorized=False,
            continuation_reason="AUTOMATIC_BUDGET_EXHAUSTED",
        )
        self.assertEqual("app", err.failure_context.get("project"))
        self.assertEqual("false", err.failure_context.get("exhaustiveAuthorized"))
        self.assertEqual("AUTOMATIC_BUDGET_EXHAUSTED", err.failure_context.get("continuationReason"))
        self.assertEqual("yarn lint:types", err.check_evidence.get("command"))
        self.assertEqual("904.1", err.check_evidence.get("elapsedSeconds"))

    def test_observed_fast_scenario_produces_honest_envelope(self) -> None:
        """Reproduce the reported Fast run (1 expensive attempt, ~904s, 300s
        budget, exhaustiveAuthorized, `yarn lint:types` exit 2) and assert the
        terminal envelope says exactly what happened, never UNSAT."""
        import dependency_live_roadmap_generator as roadmap
        from deploom_failure import build_failure

        err = roadmap._baseline_terminal_error(
            roadmap.BaselineTerminalStatus.PLATEAU,
            "BASELINE_VERIFICATION_PLATEAU",
            "budget-exhausted-after-one-candidate: "
            "проверка типов кандидата не прошла: "
            "command=yarn lint:types; exitCode=2; "
            "predicate=ts-module-resolution:@vitejs/plugin-react; "
            "elapsedSeconds=903.933; "
            "no resolver-green assignment was verified within the declared budget "
            "(wallClockSeconds=300; maxExpensiveAttempts=2; budgetSource=product-default:fast); "
            "это доказывает только отказ проверенных попыток, а не "
            "отсутствие решения; прежняя зависимая база (baseline) сохранена; "
            "для продолжения нужен новый/больший бюджет",
            source="solve-and-verify",
            project="app",
            mode="yellow",
            iteration="1",
            assignment="fp",
            phase="exact-confirmation",
            command="yarn lint:types",
            predicate="ts-module-resolution:@vitejs/plugin-react",
            exit_code="2",
            elapsed_seconds="903.933",
            exhaustive_authorized=True,
            continuation_reason="AUTOMATIC_BUDGET_EXHAUSTED",
        )
        failure = build_failure(
            err,
            context=dict(err.failure_context or {}),
            check_evidence=dict(err.check_evidence or {}),
        )
        environment = failure.to_envelope()
        self.assertEqual("BUDGET_EXHAUSTED", environment["category"])
        self.assertEqual("user-action-required", environment["retryability"])
        self.assertEqual("app", environment["project"])
        self.assertEqual("yellow", environment["mode"])
        self.assertEqual("1", environment["iteration"])
        self.assertEqual("exact-confirmation", environment["phase"])
        self.assertEqual("yarn lint:types", environment["command"])
        self.assertEqual("yarn lint:types", environment["checkCommand"])
        self.assertEqual("2", environment["checkExitCode"])
        self.assertEqual(
            "ts-module-resolution:@vitejs/plugin-react", environment["checkPredicate"]
        )
        self.assertEqual("903.933", environment["checkElapsedSeconds"])
        self.assertIn("проверка типов кандидата не прошла", environment["summary"])
        self.assertNotIn("UNSAT", environment["summary"])
        self.assertNotIn("desiredAssignment", environment)
        self.assertIn("already granted", failure.recovery_action)
        self.assertIn("does not extend the wall-clock budget", failure.recovery_action)


    def test_plateau_source_distinguishes_budget_from_base_limit(self) -> None:
        source = (
            ROOT / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")
        # The honest reason must be produced exactly when the anytime budget
        # (not the base iteration limit) ended the loop.
        self.assertIn(_HONEST_REASON, source)
        self.assertIn(_PLATEAU_SOURCE, source)
        self.assertIn(
            "if budget_exhausted and not hard_exhausted:", source
        )
        self.assertIn(
            "continuation_reason\n                    == ContinuationReason.AUTOMATIC_BUDGET_EXHAUSTED",
            source,
        )
        # Default diagnostics / human-summary must mention the last failed check.
        self.assertIn("uniqueConfirmedFailedAssignments={len(", source)

    def test_kind_budget_is_routed_to_honest_cutoff_in_generator(self) -> None:
        source = (
            ROOT / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")
        # Resolver / adaptive screen / project preflight / exact confirmation
        # must all route a "budget"-kind verification result to the honest
        # cutoff error instead of infrastructure/unknown terminal statuses.
        for site in ("resolver", "adaptive-screen", "project-preflight", "exact-confirmation"):
            marker = f'if {"screen_result" if site == "adaptive-screen" else "confirmation" if site == "exact-confirmation" else "project_result" if site == "project-preflight" else "result"}.kind == "budget":'
            self.assertIn(marker, source, site)
            self.assertIn(
                "raise BaselineBudgetExceededCutoff(",
                source[source.index(marker):source.index(marker) + 4000],
            )

    def test_exact_solver_failure_kind_reused_in_confirmation_key_cache_guard(self) -> None:
        # The budget-kind result must NOT be cached as a valid trial/project
        # observation, so a later phase cannot reuse a cut-off attempt as proof.
        source = (
            ROOT / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'confirmation.kind not in {"infrastructure", "unknown"}', source
        )

    # ------------------------------------------------------------------ P2

    def test_failure_classification_budget_exhausted(self) -> None:
        from deploom_failure import build_failure

        exc = ValueError(
            "BASELINE_BUDGET_EXHAUSTED: budget-exhausted-after-one-candidate: "
            "проверка типов кандидата не прошла: command=yarn lint:types"
        )
        failure = build_failure(exc)
        self.assertEqual("BUDGET_EXHAUSTED", failure.category)
        self.assertEqual("user-action-required", failure.retryability)
        self.assertIn("not that the project is unresolvable", failure.recovery_action)
        envelope = failure.to_envelope()
        self.assertEqual("BASELINE_BUDGET_EXHAUSTED", envelope["code"])

    def test_recovery_text_distinguishes_exhaustive_authorization(self) -> None:
        from deploom_failure import build_failure

        ctx = {
            "project": "app",
            "mode": "yellow",
            "continuationReason": "AUTOMATIC_BUDGET_EXHAUSTED",
        }
        exc = ValueError("BASELINE_BUDGET_EXHAUSTED: budget-exhausted-after-one-candidate")
        interactive = build_failure(exc, context=ctx)
        self.assertIn(
            "Choosing EXHAUSTIVE changes the localization depth, not the time budget",
            interactive.recovery_action,
        )
        autonomous = build_failure(
            exc,
            context={**ctx, "exhaustiveAuthorized": "true", "searchMode": "AUTO"},
        )
        self.assertIn(
            "already granted", autonomous.recovery_action
        )
        self.assertIn(
            "does not extend the wall-clock budget", autonomous.recovery_action
        )
        self.assertNotIn(
            "Choosing EXHAUSTIVE changes", autonomous.recovery_action
        )

    def test_envelope_and_artifact_carry_bounded_scrubbed_check_log(self) -> None:
        from deploom_failure import build_failure, write_diagnostic_artifact

        exc = ValueError(
            "BASELINE_BUDGET_EXHAUSTED: budget-exhausted-after-one-candidate: "
            "проверка типов кандидата не прошла: command=yarn lint:types"
        )
        secret = "super-secret-token-abc123"
        failure = build_failure(
            exc,
            context={
                "project": "app",
                "mode": "yellow",
                "iteration": "1",
                "phase": "adaptive-screen",
                "command": "yarn lint:types",
            },
            check_evidence={
                "phase": "adaptive-screen",
                "command": "yarn lint:types",
                "exitCode": "2",
                "predicate": "ts-module-resolution:@vitejs/plugin-react",
                "elapsedSeconds": "904.1",
                "outputTail": f"error TS2307... token={secret}\nlast line\n",
            },
        )
        envelope = failure.to_envelope()
        self.assertEqual("adaptive-screen", envelope["checkPhase"])
        self.assertEqual("yarn lint:types", envelope["checkCommand"])
        self.assertEqual("2", envelope["checkExitCode"])
        self.assertEqual("904.1", envelope["checkElapsedSeconds"])
        self.assertEqual(
            "ts-module-resolution:@vitejs/plugin-react", envelope["checkPredicate"]
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_diagnostic_artifact(exc, failure, directory=Path(tmp))
            self.assertTrue(Path(path).exists())
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertIn("checkLog", payload)
            self.assertIn("yarn lint:types", payload["checkLog"])
            self.assertIn("exitCode: 2", payload["checkLog"])
            self.assertNotIn(secret, payload["checkLog"])
            self.assertNotIn(secret, payload["rootCause"])

    def test_human_summary_mentions_last_failed_check(self) -> None:
        from deploom_failure import build_failure

        exc = ValueError("BASELINE_BUDGET_EXHAUSTED: budget-exhausted-after-one-candidate")
        failure = build_failure(
            exc,
            check_evidence={
                "phase": "exact-confirmation",
                "command": "yarn lint:types",
                "exitCode": "2",
                "predicate": "ts-module-resolution:x",
                "elapsedSeconds": "900.5",
            },
        )
        summary = failure.human_summary()
        self.assertIn("Candidate check failed:", summary)
        self.assertIn("yarn lint:types", summary)
        self.assertIn("900.5", summary)

    # ------------------------------------------------------------------ E2E

    def test_cutoff_envelope_in_subprocess(self) -> None:
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, %r)\n"
            "import dependency_live_roadmap_generator as r\n"
            "err = r.BaselineBudgetExceededCutoff(\n"
            "    project='app', mode='yellow', iteration='1', assignment='fp',\n"
            "    phase='adaptive-screen', command='yarn lint:types',\n"
            "    predicate='ts-module-resolution:@vitejs/plugin-react',\n"
            "    elapsed_seconds='18.5',\n"
            ")\n"
            "from deploom_failure import build_failure\n"
            "f = build_failure(\n"
            "    err,\n"
            "    context=dict(getattr(err, 'failure_context') or {}),\n"
            "    check_evidence=dict(getattr(err, 'check_evidence') or {}),\n"
            ")\n"
            "print(json.dumps(f.to_envelope(), ensure_ascii=False))\n"
        ) % str(ROOT)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        envelope = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("BUDGET_EXHAUSTED", envelope["category"])
        self.assertEqual("BASELINE_BUDGET_EXHAUSTED", envelope["code"])
        self.assertEqual("app", envelope["project"])
        self.assertEqual("yellow", envelope["mode"])
        self.assertEqual("1", envelope["iteration"])
        self.assertEqual("adaptive-screen", envelope["phase"])
        self.assertEqual("yarn lint:types", envelope["command"])
        self.assertEqual("yarn lint:types", envelope["checkCommand"])
        self.assertEqual(
            "ts-module-resolution:@vitejs/plugin-react", envelope["checkPredicate"]
        )
        self.assertIn(
            "budget-exhausted-after-one-candidate", envelope["summary"]
        )
        self.assertIn(
            "проверка типов кандидата не прошла", envelope["summary"]
        )
        self.assertNotIn("desiredAssignment", envelope)


if __name__ == "__main__":
    unittest.main()
