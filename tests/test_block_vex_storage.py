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
            "DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE": "1",
            "DEPLOOM_BASELINE_SEARCH_MODE": "BOUNDED_IMPROVEMENT",
            "DEPLOOM_BASELINE_EXECUTION_MODE": "FAST",
            "DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS": "900",
            "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS": "2",
            "DEPLOOM_PREDICATE_PROBE_BUDGET": "3",
            "DEPLOOM_IO_COPY_SLOTS": "2",
            "DEPLOOM_IO_HASH_SLOTS": "2",
            "DEPLOOM_IO_PM_SLOTS": "1",
            "DEPLOOM_SOURCE_HASH_WORKERS": "4",
            "DEPLOOM_ARTIFACT_HASH_WORKERS": "4",
            "DEPLOOM_INTEGRITY_HASH_WORKERS": "4",
            "DEPLOOM_BASELINE_INTENT_JSON": '{"schemaVersion":1}',
            "DEPLOOM_BASELINE_EXTRA_ITERATIONS": "2",
            "DEPLOOM_BASELINE_DECISION_GRANT_ITERATIONS": "1",
            "DEPLOOM_BASELINE_INTERACTIVE": "1",
            "DEPLOOM_RUN_ID": "run-1",
            "MY_PROJECT_FLAG": "keep",
        }
        result = storage.semantic_verification_environment(env)
        self.assertEqual(result["PATH"], "x")
        self.assertEqual(result["NODE_ENV"], "test")
        self.assertEqual(result["MY_PROJECT_FLAG"], "keep")
        self.assertNotIn("DEPLOOM_VERIFICATION_ROOT", result)
        self.assertNotIn("DEPLOOM_BASELINE_RESUME", result)
        self.assertNotIn("DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE", result)
        self.assertNotIn("DEPLOOM_BASELINE_SEARCH_MODE", result)
        self.assertNotIn("DEPLOOM_BASELINE_EXECUTION_MODE", result)
        self.assertNotIn("DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS", result)
        self.assertNotIn("DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS", result)
        self.assertNotIn("DEPLOOM_PREDICATE_PROBE_BUDGET", result)
        self.assertNotIn("DEPLOOM_IO_COPY_SLOTS", result)
        self.assertNotIn("DEPLOOM_IO_HASH_SLOTS", result)
        self.assertNotIn("DEPLOOM_IO_PM_SLOTS", result)
        self.assertNotIn("DEPLOOM_SOURCE_HASH_WORKERS", result)
        self.assertNotIn("DEPLOOM_ARTIFACT_HASH_WORKERS", result)
        self.assertNotIn("DEPLOOM_INTEGRITY_HASH_WORKERS", result)
        self.assertNotIn("DEPLOOM_BASELINE_INTENT_JSON", result)
        self.assertNotIn("DEPLOOM_BASELINE_EXTRA_ITERATIONS", result)
        self.assertNotIn("DEPLOOM_BASELINE_DECISION_GRANT_ITERATIONS", result)
        self.assertNotIn("DEPLOOM_BASELINE_INTERACTIVE", result)
        self.assertNotIn("DEPLOOM_RUN_ID", result)

    def test_baseline_continuation_controls_do_not_change_semantic_environment(self) -> None:
        base = {
            "PATH": "x",
            "NODE_ENV": "test",
            "NODE_OPTIONS": "--max-old-space-size=4096",
            "DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE": "0",
            "DEPLOOM_BASELINE_SEARCH_MODE": "AUTO",
            "DEPLOOM_BASELINE_EXECUTION_MODE": "FAST",
            "DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS": "900",
            "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS": "2",
        }
        continued = {
            **base,
            "DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE": "1",
            "DEPLOOM_BASELINE_SEARCH_MODE": "BOUNDED_IMPROVEMENT",
            "DEPLOOM_BASELINE_EXECUTION_MODE": "BACKGROUND",
            "DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS": "3600",
            "DEPLOOM_BASELINE_MAX_EXPENSIVE_ATTEMPTS": "8",
        }
        self.assertEqual(
            storage.semantic_verification_environment(base),
            storage.semantic_verification_environment(continued),
        )
        changed_semantics = {**continued, "NODE_OPTIONS": "--max-old-space-size=8192"}
        self.assertNotEqual(
            storage.semantic_verification_environment(base),
            storage.semantic_verification_environment(changed_semantics),
        )

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
