from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import block_vex_storage as storage


class VexStorageTests(unittest.TestCase):
    def test_semantic_environment_removes_only_internal_performance_controls(self):
        env = {
            "PATH": "x",
            "NODE_ENV": "test",
            "DEPLOOM_VERIFICATION_ROOT": "D:/fast",
            "DEPLOOM_BASELINE_RESUME": "restart",
            "DEPLOOM_PREDICATE_PROBE_BUDGET": "3",
            "MY_PROJECT_FLAG": "keep",
        }
        result = storage.semantic_verification_environment(env)
        self.assertEqual(result["PATH"], "x")
        self.assertEqual(result["NODE_ENV"], "test")
        self.assertEqual(result["MY_PROJECT_FLAG"], "keep")
        self.assertNotIn("DEPLOOM_VERIFICATION_ROOT", result)
        self.assertNotIn("DEPLOOM_BASELINE_RESUME", result)
        self.assertNotIn("DEPLOOM_PREDICATE_PROBE_BUDGET", result)

    def test_explicit_root_is_created(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "verify"
            resolved = storage.verification_root(
                {"DEPLOOM_VERIFICATION_ROOT": str(root)}
            )
            self.assertEqual(resolved, root.absolute())
            self.assertTrue(root.is_dir())

    def test_zero_config_root_is_created_outside_project_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            default_root = Path(temp) / "user-cache" / "verification"
            with mock.patch.object(
                storage, "_default_verification_root", return_value=default_root
            ):
                resolved = storage.verification_root({})
            self.assertEqual(resolved, default_root)
            self.assertTrue(default_root.is_dir())

    def test_missing_explicit_root_falls_back_to_zero_config_root(self):
        with tempfile.TemporaryDirectory() as temp:
            preferred = Path(temp) / "missing-volume"
            fallback = Path(temp) / "fallback"
            with mock.patch.object(
                storage, "_default_verification_root", return_value=fallback
            ), mock.patch.object(
                storage,
                "_ensure_writable_root",
                side_effect=[None, fallback],
            ):
                resolved = storage.verification_root(
                    {"DEPLOOM_VERIFICATION_ROOT": str(preferred)}
                )
            self.assertEqual(resolved, fallback)

    def test_user_yarn_cache_override_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            result = storage.package_manager_cache_environment(
                manager="yarn",
                proof_cache_dir=Path(temp) / "proofs",
                inherited_environment={"YARN_CACHE_FOLDER": "X:/user-cache"},
            )
            self.assertEqual(result, {})

    def test_yarn_cache_moves_to_selected_refs_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "refs"
            profile = storage.VerificationStorageProfile(
                root=root,
                filesystem="refs",
                volume_root="R:\\",
                free_bytes=10**9,
                explicit=True,
                refs_same_volume_capable=True,
            )
            with mock.patch.object(
                storage, "verification_storage_profile", return_value=profile
            ):
                result = storage.package_manager_cache_environment(
                    manager="yarn",
                    proof_cache_dir=Path(temp) / "proofs",
                    inherited_environment={},
                )
            self.assertEqual(
                Path(result["YARN_CACHE_FOLDER"]),
                root / "package-manager-artifacts" / "yarn",
            )

    def test_npm_zero_config_uses_isolated_verification_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            proof = Path(temp) / "baseline-proofs"
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
                    proof_cache_dir=proof,
                    inherited_environment={},
                )
            self.assertEqual(
                Path(result["npm_config_cache"]),
                verification / "package-manager-artifacts" / "npm",
            )


if __name__ == "__main__":
    unittest.main()
