from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as generator


class BaselineDecisionRecoveryClosureTests(unittest.TestCase):
    def test_decision_and_recovery_are_not_transient_retries(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("function baselineHumanDecisionRequired", main)
        self.assertIn("DEPLOOM_BASELINE_DECISION_V1 ", main)
        self.assertIn("BASELINE_RECOVERY_CONTINUE_UNAVAILABLE", main)
        self.assertIn("BASELINE_RECOVERY_CONCURRENT_RUN", main)
        self.assertIn(
            "if (baselineHumanDecisionRequired(result) || deterministicWatchdogFailure(result) || deterministicPythonProgrammingFailure(result)) return true",
            main,
        )

    def test_human_decision_is_clean_expected_exit(self) -> None:
        # Ψ.5 makes the generator callable inside a long-lived Desktop worker.
        # The reusable boundary returns an exit code; only the standalone CLI
        # converts that code into SystemExit. Test behavior rather than a
        # particular implementation string.
        class FakeDecisionError(Exception):
            pass

        decision = FakeDecisionError(
            f"{generator.BASELINE_DECISION_MARKER} "
            '{"schemaVersion":1,"event":"BASELINE_CONTINUATION_REQUIRED"}'
        )
        with (
            patch.object(generator, "BaselineConstraintVerificationError", FakeDecisionError),
            patch.object(generator, "main", side_effect=decision),
            patch.object(generator, "eprint"),
        ):
            self.assertEqual(3, generator.run_cli())

        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('raise SystemExit(run_cli())', source)

    def test_policy_change_restarts_orchestration_not_raw_continue(self) -> None:
        flow = (ROOT / "desktop" / "src" / "components" / "FlowWorkspace.tsx").read_text(encoding="utf-8")
        self.assertIn("baselinePolicyIdentity", flow)
        self.assertIn("const policyChanged =", flow)
        self.assertIn("policyChanged ? 'restart' : pending.resume", flow)
        self.assertIn("baselineResume: effectiveBaselineResume", flow)

    def test_user_policy_is_explicit_part_of_recovery_identity(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn('"baselineIntent": {', source)
        self.assertIn('"keepCurrent": baseline_keep_current', source)
        self.assertIn('"required": baseline_required', source)

    def test_baseline_snapshots_its_project_private_output(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("function baselineProjectOutputDir", main)
        self.assertIn(
            "const baselineOutput = baselineProjectOutputDir(job.workspace, job.projectName)",
            main,
        )
        self.assertIn(
            "roadmap: join(baselineOutput, 'dependency-roadmap.json')",
            main,
        )
        self.assertIn(
            "dashboard: join(baselineOutput, 'dependency-roadmap.html')",
            main,
        )

    def test_transient_baseline_retry_never_replays_explicit_restart(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("function commandSpecForRetry", main)
        self.assertIn("DEPLOOM_BASELINE_RESUME: 'auto'", main)
        self.assertIn("result = await executeCommand(job, retrySpec)", main)

    def test_completed_checkpoint_is_not_advertised_as_continue(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn(
            "const resumable = status !== 'completed' && status !== 'passed'",
            main,
        )
        self.assertIn("reason: 'already-complete'", main)

    def test_continue_does_not_clear_planner_epoch_before_recovery(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn(
            "input.action === 'baseline' && requestedBaselineProofMode !== 'DRAFT' && input.baselineResume !== 'continue'",
            main,
        )


if __name__ == "__main__":
    unittest.main()
