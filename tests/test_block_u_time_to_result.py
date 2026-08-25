from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

import baseline_constraint_verifier as verifier


class BlockUTimeToResultTests(unittest.TestCase):
    def setUp(self) -> None:
        verifier._PREPARED_FASTPATH_COMMAND_DISABLED.clear()

    def tearDown(self) -> None:
        verifier._PREPARED_FASTPATH_COMMAND_DISABLED.clear()

    @unittest.skipUnless(os.name == "nt", "Windows/NTFS fastpath policy")
    def test_single_command_new_preparation_skips_expensive_sealing(self) -> None:
        project = Path(".")
        with mock.patch.dict(
            os.environ,
            {"DEPLOOM_NTFS_FASTPATH_MIN_COMMANDS": "2"},
            clear=False,
        ):
            self.assertFalse(
                verifier._prepared_snapshot_fastpath_worth_sealing(
                    ("yarn lint:types",), project
                )
            )
            self.assertTrue(
                verifier._prepared_snapshot_fastpath_worth_sealing(
                    ("yarn lint:types", "yarn build"), project
                )
            )

    def test_command_quarantine_survives_preparation_key_changes(self) -> None:
        project = Path(".")
        self.assertTrue(
            verifier._prepared_command_fastpath_allowed(project, "yarn build")
        )
        verifier._disable_prepared_command_fastpath(project, "yarn build")
        self.assertFalse(
            verifier._prepared_command_fastpath_allowed(project, "yarn build")
        )
        self.assertTrue(
            verifier._prepared_command_fastpath_allowed(
                project, "yarn lint:types"
            )
        )

    def test_bad_threshold_override_is_bounded_and_nonfatal(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"DEPLOOM_NTFS_FASTPATH_MIN_COMMANDS": "not-an-int"},
            clear=False,
        ):
            # Ω removed the O(files) dependency integrity seal. Invalid
            # overrides therefore fall back to the current one-command default.
            self.assertEqual(1, verifier._ntfs_fastpath_min_commands())
        with mock.patch.dict(
            os.environ,
            {"DEPLOOM_NTFS_FASTPATH_MIN_COMMANDS": "999"},
            clear=False,
        ):
            self.assertEqual(32, verifier._ntfs_fastpath_min_commands())

    def test_full_copy_retry_cannot_request_integrity_sealing(self) -> None:
        source = Path(verifier.__file__).read_text(encoding="utf-8")
        self.assertIn("preparation_fastpath_enabled = (", source)
        self.assertIn(
            "seal_dependency_integrity=preparation_fastpath_enabled", source
        )
        self.assertIn("_allow_prepared_fastpath=False", source)

    def test_private_copy_still_uses_exclusive_snapshot_lease(self) -> None:
        source = Path(verifier.__file__).read_text(encoding="utf-8")
        start = source.index("def _materialize_prepared_workspace_snapshot(")
        end = source.index("def _package_manager_cache_environment(", start)
        section = source[start:end]
        self.assertIn("acquire_snapshot_copy_lease(", section)
        self.assertIn("copy_lease.close()", section)

    def test_notification_still_retries_fresh_private_project_checks(self) -> None:
        source = Path(verifier.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "_disable_prepared_command_fastpath(project_dir, command)", source
        )
        self.assertIn("_retry_assignment_without_prepared_fastpath(", source)
        self.assertIn(
            "reuse_resolver_proof_key=proof_identity.resolver_input_key", source
        )


if __name__ == "__main__":
    unittest.main()
