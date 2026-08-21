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

    def test_generator_uses_one_sealed_source_snapshot_epoch(self) -> None:
        # BLOCK_X_REGRESSION_CONTRACT_V1
        generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn("from source_snapshot import (", generator)
        self.assertIn("activate_source_snapshot_epoch", generator)
        self.assertIn("source_snapshot_provenance_head", generator)
        self.assertIn("source_snapshot = activate_source_snapshot_epoch(", generator)
        self.assertIn("project_source_snapshot_key = (", generator)
        self.assertIn("source_snapshot.key", generator)

        # Dirty state is part of the captured proof subject, not a rejection gate.
        self.assertNotIn("source_dirty_entries", generator)
        self.assertNotIn("PROVEN_DEPENDENCY_SOURCE_DIRTY", generator)
        self.assertIn("SOURCE_SNAPSHOT_CAPTURE_FAILED", generator)
    def test_proof_identity_uses_captured_source_snapshot_not_git_proxy(self) -> None:
        proof = (ROOT / "verification_proof.py").read_text(encoding="utf-8")
        source_snapshot = (ROOT / "source_snapshot.py").read_text(encoding="utf-8")

        self.assertIn("BLOCK_X_SOURCE_TRUTH_V1", proof)
        self.assertIn("captured_source_snapshot_fingerprint", proof)
        self.assertIn("active_source_snapshot", proof)
        self.assertIn("proof_subject_project_dir", proof)
        self.assertNotIn("git_exclude_pathspecs", proof)

        # gitignore is not the proof boundary; SourceInputPolicy is explicit.
        self.assertIn("class SourceInputPolicy", source_snapshot)
        self.assertIn("DEFAULT_EXCLUDED_DIR_NAMES", source_snapshot)
        self.assertIn('"node_modules"', source_snapshot)
        self.assertIn('".idea"', source_snapshot)
        self.assertIn("build_source_tree_manifest", source_snapshot)
        self.assertIn("SOURCE_CAPTURE_UNSTABLE", source_snapshot)
    def test_source_snapshot_identity_failures_remain_explicit(self) -> None:
        main = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")

        self.assertIn("PROVEN_DEPENDENCY_SOURCE_SNAPSHOT_INVALID", main)
        self.assertIn("PROVEN_DEPENDENCY_PROOF_IDENTITY_UNAVAILABLE", main)
        self.assertIn("SOURCE_SNAPSHOT_CAPTURE_FAILED", generator)
        self.assertIn("PROVEN_DEPENDENCY_SOURCE_IDENTITY_UNAVAILABLE", generator)
        self.assertNotIn("PROVEN_DEPENDENCY_SOURCE_DIRTY", generator)

if __name__ == "__main__":
    unittest.main()
