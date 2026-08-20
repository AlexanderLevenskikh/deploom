from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout


class ProofSourceCleanlinessRegressionTests(unittest.TestCase):
    def test_shared_noise_policy_filters_ide_noise_but_not_real_source_changes(self) -> None:
        workspace_noise = (ROOT / "workspace_noise.py").read_text(encoding="utf-8")
        namespace: dict[str, object] = {}
        exec(compile(workspace_noise, str(ROOT / "workspace_noise.py"), "exec"), namespace)
        relevant_porcelain_entries = namespace["relevant_porcelain_entries"]

        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            run_git(repo, "init")
            run_git(repo, "config", "user.email", "deploom-test@example.invalid")
            run_git(repo, "config", "user.name", "DepLoom Test")
            (repo / "package.json").write_text('{"name":"demo-project","version":"1.0.0"}\n', encoding="utf-8")
            run_git(repo, "add", "package.json")
            run_git(repo, "commit", "-m", "baseline")

            idea = repo / ".idea"
            idea.mkdir()
            (idea / "workspace.xml").write_text("<workspace />\n", encoding="utf-8")

            raw_status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
            self.assertIn(".idea", raw_status)
            filtered = relevant_porcelain_entries(raw_status)
            self.assertEqual([], filtered)

            (repo / "real-source-change.txt").write_text("real\n", encoding="utf-8")
            raw_status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
            filtered = relevant_porcelain_entries(raw_status)
            self.assertTrue(any("real-source-change.txt" in entry for entry in filtered))
            self.assertFalse(any(".idea" in entry for entry in filtered))

    def test_generator_uses_shared_noise_policy_and_reports_relevant_paths(self) -> None:
        generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn("from workspace_noise import relevant_porcelain_entries", generator)
        self.assertIn("def _proof_source_head_clean_and_entries(", generator)
        self.assertIn("def _proof_source_head_and_clean(", generator)
        self.assertIn("relevant_porcelain_entries(status_result.stdout)", generator)
        self.assertIn("source_dirty_entries", generator)
        self.assertIn("relevantStatus=", generator)

    def test_proof_identity_uses_same_git_exclusions(self) -> None:
        proof = (ROOT / "verification_proof.py").read_text(encoding="utf-8")
        self.assertIn("from workspace_noise import git_exclude_pathspecs", proof)
        self.assertIn("*git_exclude_pathspecs(),", proof)

    def test_source_safety_stop_is_non_retryable(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("PROVEN_DEPENDENCY_SOURCE_DIRTY", main)
        self.assertIn("PROVEN_DEPENDENCY_SOURCE_SNAPSHOT_INVALID", main)
        self.assertIn("PROVEN_DEPENDENCY_PROOF_IDENTITY_UNAVAILABLE", main)


if __name__ == "__main__":
    unittest.main()
