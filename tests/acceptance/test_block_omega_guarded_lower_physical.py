from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from prepared_workspace_fastpath import (
    cleanup_guarded_clone,
    dependency_root_manifest,
    guarded_clone_is_active,
    stop_guarded_clone,
    try_materialize_guarded_clone,
)

# BLOCK_OMEGA_VERIFICATION_SUBSTRATE_V2


@unittest.skipUnless(os.name == "nt", "Windows guarded lower acceptance")
class BlockOmegaGuardedLowerPhysicalAcceptance(unittest.TestCase):
    def _prepared_tree(self, root: Path) -> Path:
        prepared = root / "prepared"
        package = prepared / "node_modules" / "demo"
        package.mkdir(parents=True)
        (prepared / "package.json").write_text(
            '{"name":"omega-fixture","private":true}\n',
            encoding="utf-8",
        )
        (prepared / "source.txt").write_text("sealed-source\n", encoding="utf-8")
        (package / "package.json").write_text(
            '{"name":"demo","version":"1.0.0"}\n',
            encoding="utf-8",
        )
        return prepared

    def test_guarded_lower_is_private_above_and_shared_only_under_watch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = self._prepared_tree(root)
            target = root / "clone"
            roots = dependency_root_manifest(prepared)

            project = try_materialize_guarded_clone(
                source_project=prepared,
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
                dependency_roots=roots,
            )
            self.assertIsNotNone(project)
            self.assertTrue(guarded_clone_is_active(target))

            assert project is not None
            (project / "source.txt").write_text("private-change\n", encoding="utf-8")
            self.assertEqual(
                "sealed-source\n",
                (prepared / "source.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "demo",
                __import__("json").loads(
                    (project / "node_modules" / "demo" / "package.json").read_text(
                        encoding="utf-8"
                    )
                )["name"],
            )

            result = stop_guarded_clone(target)
            self.assertEqual((), result.errors)
            self.assertEqual((), result.mutations)
            cleanup_guarded_clone(target)

    def test_dependency_write_is_detected_without_pre_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = self._prepared_tree(root)
            target = root / "clone"
            roots = dependency_root_manifest(prepared)

            project = try_materialize_guarded_clone(
                source_project=prepared,
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
                dependency_roots=roots,
            )
            self.assertIsNotNone(project)
            assert project is not None

            dependency_file = project / "node_modules" / "demo" / "package.json"
            dependency_file.write_text(
                '{"name":"demo","version":"9.9.9"}\n',
                encoding="utf-8",
            )
            time.sleep(0.10)

            result = stop_guarded_clone(target)
            self.assertFalse(result.errors, result.errors)
            self.assertTrue(
                result.mutations,
                "shared dependency mutation must be detected without a baseline hash",
            )
            cleanup_guarded_clone(target)


if __name__ == "__main__":
    unittest.main()
