from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


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
        generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn("except BaselineConstraintVerificationError as exc:", generator)
        self.assertIn("if message.startswith(BASELINE_DECISION_MARKER):", generator)
        self.assertIn("raise SystemExit(3)", generator)

    def test_policy_change_restarts_orchestration_not_raw_continue(self) -> None:
        flow = (ROOT / "desktop" / "src" / "components" / "FlowWorkspace.tsx").read_text(encoding="utf-8")
        self.assertIn("baselinePolicyIdentity", flow)
        self.assertIn("const policyChanged =", flow)
        self.assertIn("policyChanged ? 'restart' : pending.resume", flow)
        self.assertIn("baselineResume: effectiveBaselineResume", flow)

    def test_user_policy_is_explicit_part_of_recovery_identity(self) -> None:
        generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn('"baselineIntent": {', generator)
        self.assertIn('"keepCurrent": baseline_keep_current', generator)
        self.assertIn('"required": baseline_required', generator)


if __name__ == "__main__":
    unittest.main()
