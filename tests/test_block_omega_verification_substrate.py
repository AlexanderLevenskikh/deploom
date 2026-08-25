from __future__ import annotations

import ast
import inspect
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import baseline_constraint_verifier as verifier
import block_v_prepared_artifact as artifact
import block_vex_storage
import prepared_workspace_fastpath as fastpath

# BLOCK_OMEGA_VERIFICATION_SUBSTRATE_V2


class BlockOmegaVerificationSubstrateTests(unittest.TestCase):
    def setUp(self) -> None:
        verifier._cleanup_prepared_snapshot_root()

    def tearDown(self) -> None:
        verifier._cleanup_prepared_snapshot_root()

    def test_dependency_root_manifest_never_hashes_payload_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            package = root / "node_modules" / "demo"
            package.mkdir(parents=True)
            (package / "large.bin").write_bytes(b"x" * 1024)
            nested = root / "packages" / "app" / "node_modules" / "nested"
            nested.mkdir(parents=True)
            (nested / "index.js").write_text("ok\n", encoding="utf-8")

            with mock.patch.object(
                fastpath,
                "_fingerprint_path_with_size",
                side_effect=AssertionError("Ω must not hash payload bytes"),
            ):
                roots = fastpath.dependency_root_manifest(root)

            self.assertEqual(
                ("node_modules", "packages/app/node_modules"),
                roots,
            )

    def test_guarded_lower_no_longer_requires_integrity_manifest(self) -> None:
        source = inspect.getsource(fastpath.try_materialize_guarded_clone)
        self.assertIn("dependency_roots", source)
        self.assertIn("del dependency_integrity", source)
        self.assertNotIn("not dependency_integrity", source)

    def test_guarded_lower_uses_sealed_upper_not_git_shared_alternates(self) -> None:
        source = inspect.getsource(
            fastpath.try_materialize_guarded_clone
        )
        tree = ast.parse(textwrap.dedent(source))
        executable_strings = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
        }
        self.assertNotIn("--shared", executable_strings)
        command_lists = {
            tuple(
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.List)
        }
        self.assertFalse(
            any(
                len(items) >= 2
                and items[0] == "git"
                and items[1] == "clone"
                for items in command_lists
            )
        )
        self.assertIn("_run_overlay_robocopy", source)

    def test_single_command_is_worth_guarding_after_hash_removal(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEPLOOM_NTFS_FASTPATH_MIN_COMMANDS", None)
            self.assertEqual(1, verifier._ntfs_fastpath_min_commands())

    @unittest.skipUnless(os.name == "nt", "Windows guarded-lower snapshot metadata")
    def test_snapshot_carries_root_manifest_not_payload_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = root / "prepared"
            project = prepared / "app"
            package = prepared / "node_modules" / "demo"
            project.mkdir(parents=True)
            package.mkdir(parents=True)
            (project / "package.json").write_text('{"name":"app"}\n', encoding="utf-8")
            (package / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")

            with mock.patch.object(
                verifier,
                "build_dependency_integrity_manifest",
                side_effect=AssertionError("legacy O(files) seal must be dead"),
            ):
                snapshot = verifier._publish_prepared_workspace_snapshot(
                    prepared,
                    project,
                    key="e" * 32,
                    observed_versions={},
                    observed_hash="f" * 64,
                    source_project=project,
                    seal_dependency_integrity=True,
                )

            self.assertIn("node_modules", snapshot.dependency_roots)
            self.assertEqual({}, dict(snapshot.dependency_integrity))

    def test_artifact_dependency_roots_are_strict_relative_paths(self) -> None:
        self.assertEqual(
            ["node_modules", "packages/app/node_modules"],
            artifact._normalize_dependency_roots(
                ["packages/app/node_modules", "node_modules", "node_modules"]
            ),
        )
        with self.assertRaises(ValueError):
            artifact._normalize_dependency_roots(["../node_modules"])

    def test_omega_environment_switches_are_proof_neutral(self) -> None:
        env = {
            "DEPLOOM_DISABLE_GUARDED_LOWER": "1",
            "DEPLOOM_OMEGA_OVERLAY_WORKERS": "8",
            "SEMANTIC": "yes",
        }
        semantic = block_vex_storage.semantic_verification_environment(env)
        self.assertNotIn("DEPLOOM_DISABLE_GUARDED_LOWER", semantic)
        self.assertNotIn("DEPLOOM_OMEGA_OVERLAY_WORKERS", semantic)
        self.assertEqual("yes", semantic["SEMANTIC"])

    def test_disable_switch_is_performance_only(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"DEPLOOM_DISABLE_GUARDED_LOWER": "1"},
            clear=False,
        ):
            self.assertFalse(fastpath.guarded_lower_enabled())


if __name__ == "__main__":
    unittest.main()
