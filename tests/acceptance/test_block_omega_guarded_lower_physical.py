from __future__ import annotations

import hashlib
import os
import subprocess
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

    def _junction(self, link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        result = subprocess.run(
            [comspec, "/d", "/s", "/c", subprocess.list2cmdline(
                ["mklink", "/J", str(link), str(target)]
            )],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout)

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_scoped_workspace_alias_rebases_into_private_upper(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = self._prepared_tree(root)
            workspace = prepared / "packages" / "ui"
            workspace.mkdir(parents=True)
            source = workspace / "index.js"
            source.write_text("module.exports = 'sealed';\n", encoding="utf-8")
            self._junction(prepared / "node_modules" / "@acme" / "ui", workspace)

            before = self._sha(source)
            target = root / "clone"
            project = try_materialize_guarded_clone(
                source_project=prepared,
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
                dependency_roots=dependency_root_manifest(prepared),
            )
            self.assertIsNotNone(project)
            assert project is not None
            scoped_shell = project / "node_modules" / "@acme"
            alias = scoped_shell / "ui"
            self.assertFalse(scoped_shell.samefile(prepared / "node_modules" / "@acme"))
            self.assertTrue(alias.samefile(project / "packages" / "ui"))

            (alias / "index.js").write_text("module.exports = 'private';\n", encoding="utf-8")
            self.assertEqual(before, self._sha(source))
            result = stop_guarded_clone(target)
            self.assertFalse(result.errors, result.errors)
            self.assertFalse(result.mutations, result.mutations)
            cleanup_guarded_clone(target)

    def test_nested_and_sibling_workspace_links_rebase_privately(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = self._prepared_tree(root)
            for name in ("a", "b"):
                package = prepared / "packages" / name
                package.mkdir(parents=True)
                (package / "index.js").write_text(name, encoding="utf-8")
            self._junction(prepared / "node_modules" / "a", prepared / "packages" / "a")
            self._junction(
                prepared / "packages" / "a" / "node_modules" / "@scope" / "b",
                prepared / "packages" / "b",
            )
            target = root / "clone"
            project = try_materialize_guarded_clone(
                source_project=prepared,
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
                dependency_roots=dependency_root_manifest(prepared),
            )
            self.assertIsNotNone(project)
            assert project is not None
            self.assertTrue((project / "node_modules" / "a").samefile(project / "packages" / "a"))
            self.assertTrue(
                (project / "packages" / "a" / "node_modules" / "@scope" / "b").samefile(
                    project / "packages" / "b"
                )
            )
            cleanup_guarded_clone(target)

    def test_external_and_cycle_junctions_reject_guarded_optimization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root / "outside"
            outside.mkdir()
            prepared = self._prepared_tree(root)
            self._junction(prepared / "node_modules" / "external", outside)
            target = root / "external-clone"
            self.assertIsNone(try_materialize_guarded_clone(
                source_project=prepared,
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
                dependency_roots=dependency_root_manifest(prepared),
            ))
            self.assertFalse(target.exists())

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = self._prepared_tree(root)
            self._junction(prepared / "cycle", prepared)
            target = root / "cycle-clone"
            self.assertIsNone(try_materialize_guarded_clone(
                source_project=prepared,
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
                dependency_roots=dependency_root_manifest(prepared),
            ))
            self.assertFalse(target.exists())

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
