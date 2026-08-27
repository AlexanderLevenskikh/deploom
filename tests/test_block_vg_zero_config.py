from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import block_vex_storage as storage
import verification_workspace_backend as backend


class BlockVGZeroConfigTests(unittest.TestCase):
    def test_copy_worker_override_allows_robocopy_maximum(self):
        with mock.patch.dict(os.environ, {"DEPLOOM_WORKSPACE_COPY_WORKERS": "128"}, clear=False):
            self.assertEqual(backend._copy_workers(refs_same_volume=False), 128)

    def test_copy_worker_default_is_conservative_under_global_io_governor(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            backend.os, "cpu_count", return_value=16
        ):
            self.assertEqual(backend._copy_workers(refs_same_volume=False), 8)

    def test_zero_config_storage_is_proof_neutral(self):
        env = {
            "PATH": "x",
            "DEPLOOM_VERIFICATION_ROOT": "D:/optional",
            "MY_PROJECT_FLAG": "keep",
        }
        semantic = storage.semantic_verification_environment(env)
        self.assertNotIn("DEPLOOM_VERIFICATION_ROOT", semantic)
        self.assertEqual(semantic["MY_PROJECT_FLAG"], "keep")

    def test_missing_optional_root_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            fallback = Path(temp) / "fallback"
            with mock.patch.object(
                storage, "_default_verification_root", return_value=fallback
            ), mock.patch.object(
                storage, "_ensure_writable_root", side_effect=[None, fallback]
            ):
                result = storage.verification_root(
                    {"DEPLOOM_VERIFICATION_ROOT": str(Path(temp) / "missing")}
                )
            self.assertEqual(result, fallback)

    def test_zero_config_keeps_npm_cache_isolated_in_verification_root(self):
        with tempfile.TemporaryDirectory() as temp:
            verification = Path(temp) / "verification"
            with mock.patch.object(
                storage,
                "verification_storage_profile",
                return_value=storage.VerificationStorageProfile(
                    root=verification,
                    filesystem="ntfs",
                    default=True,
                ),
            ):
                result = storage.package_manager_cache_environment(
                    manager="npm",
                    proof_cache_dir=Path(temp) / "proofs",
                    inherited_environment={},
                )
            self.assertEqual(
                Path(result["npm_config_cache"]),
                verification / "package-manager-artifacts" / "npm",
            )

    def test_verifier_contains_fail_closed_private_trial_reuse(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "baseline_constraint_verifier.py"
        ).read_text(encoding="utf-8")
        self.assertIn("reused-private-trial", source)
        self.assertIn("WorkspaceChangeGuard", source)
        self.assertIn("trial-reuse-rejected", source)
        self.assertIn("watcher-error", source)


if __name__ == "__main__":
    unittest.main()
