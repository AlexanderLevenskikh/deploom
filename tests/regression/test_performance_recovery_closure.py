from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PerformanceRecoveryClosureTests(unittest.TestCase):
    def test_resume_probes_current_exact_resolver_proof_without_process_local_hint(self) -> None:
        verifier = (ROOT / "baseline_constraint_verifier.py").read_text(encoding="utf-8")
        self.assertIn(
            'os.environ.get("DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE")',
            verifier,
        )
        self.assertIn(
            "PreparationProof HIT but its durable PreparedArtifact is unavailable",
            verifier,
        )
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn(
            "DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE: input.baselineResume === 'continue' ? '1' : '0'",
            main,
        )
        self.assertIn(
            "DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE: '1'",
            main,
        )

    def test_snapshot_publish_reuses_canonical_reparse_plan(self) -> None:
        verifier = (ROOT / "baseline_constraint_verifier.py").read_text(encoding="utf-8")
        store = (ROOT / "block_v_prepared_artifact.py").read_text(encoding="utf-8")
        artifact = (ROOT / "artifact_integrity.py").read_text(encoding="utf-8")
        self.assertIn("reparse_plan=canonical_reparse_plan", verifier)
        self.assertIn("reparse_plan=reparse_plan", store)
        self.assertIn(
            "reparse_plan: Optional[Sequence[ReparseLink]] = None",
            artifact,
        )
        self.assertIn("metadata = item.stat(follow_symlinks=False)", artifact)
        self.assertNotIn(
            'getattr(item.stat(follow_symlinks=False), "st_nlink"',
            artifact,
        )

    def test_missing_logical_base_is_bootstrapped_from_reviewed_proof(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("async function ensureMigrationBaseBranch(", main)
        self.assertIn(
            "await ensureMigrationBaseBranch(job, project, initialPlan, savedPromptMarkdown)",
            main,
        )
        self.assertIn("validateScopeProofEnvelope(savedPromptMarkdown, project.name)", main)
        self.assertIn("'branch', baseBranch, sourceHead", main)
        self.assertIn("MIGRATION_BASE_SOURCE_COMMIT_MISSING", main)


if __name__ == "__main__":
    unittest.main()
