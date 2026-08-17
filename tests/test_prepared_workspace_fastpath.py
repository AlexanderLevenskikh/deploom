from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from prepared_workspace_fastpath import (
    _DirectoryWatcher,
    _notification_is_authoritative_mutation,
    cleanup_guarded_clone,
    guarded_clone_is_active,
    stop_guarded_clone,
    try_materialize_guarded_clone,
)


@unittest.skipUnless(os.name == "nt", "Windows NTFS fast path")
class PreparedWorkspaceFastPathTests(unittest.TestCase):
    def test_junction_clone_detects_dependency_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            prepared = root / "prepared"
            target = root / "check"
            source.mkdir()
            (source / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            (source / "src").mkdir()
            (source / "src" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=source, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "fastpath@example.invalid"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.name", "Fast Path Test"], cwd=source, check=True)
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=source, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "clone", "--quiet", str(source), str(prepared)], check=True)
            dependency = prepared / "node_modules" / "demo"
            dependency.mkdir(parents=True)
            (dependency / "package.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")

            project = try_materialize_guarded_clone(
                source_project=source,
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
            )
            self.assertIsNotNone(project)
            self.assertTrue(guarded_clone_is_active(target))
            assert project is not None
            self.assertTrue((project / "node_modules" / "demo" / "package.json").is_file())

            (project / "node_modules" / "demo" / "mutated.txt").write_text("x\n", encoding="utf-8")
            time.sleep(0.25)
            result = stop_guarded_clone(target)
            self.assertFalse(result.errors, result.errors)
            self.assertTrue(result.mutations, "shared dependency mutation was not observed")
            cleanup_guarded_clone(target)
            self.assertFalse(guarded_clone_is_active(target))

    def test_ephemeral_local_caches_do_not_count_as_dependency_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            prepared = root / "prepared"
            target = root / "check"
            source.mkdir()
            (source / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            subprocess.run(["git", "init"], cwd=source, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "fastpath@example.invalid"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.name", "Fast Path Test"], cwd=source, check=True)
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=source, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "clone", "--quiet", str(source), str(prepared)], check=True)
            for cache_name in (".vite", ".cache"):
                cache = prepared / "node_modules" / cache_name
                cache.mkdir(parents=True)
                (cache / "seed").write_text("seed\n", encoding="utf-8")

            project = try_materialize_guarded_clone(
                source_project=source,
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
            )
            self.assertIsNotNone(project)
            assert project is not None
            for cache_name in (".vite", ".cache"):
                (project / "node_modules" / cache_name / "generated").write_text("cache\n", encoding="utf-8")
            time.sleep(0.25)
            result = stop_guarded_clone(target)
            self.assertFalse(result.errors, result.errors)
            self.assertFalse(result.mutations, result.mutations)
            cleanup_guarded_clone(target)


    def test_existing_dependency_file_modification_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            prepared = root / "prepared"
            target = root / "check"
            source.mkdir()
            (source / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
            subprocess.run(["git", "init"], cwd=source, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "fastpath@example.invalid"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.name", "Fast Path Test"], cwd=source, check=True)
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=source, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "clone", "--quiet", str(source), str(prepared)], check=True)
            dependency = prepared / "node_modules" / "demo"
            dependency.mkdir(parents=True)
            package_json = dependency / "package.json"
            package_json.write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")

            project = try_materialize_guarded_clone(
                source_project=source,
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
            )
            self.assertIsNotNone(project)
            assert project is not None
            shared_package_json = project / "node_modules" / "demo" / "package.json"
            shared_package_json.write_text('{"name":"demo","version":"1.0.1"}\n', encoding="utf-8")
            time.sleep(0.25)
            result = stop_guarded_clone(target)
            self.assertFalse(result.errors, result.errors)
            self.assertTrue(result.mutations, "existing shared dependency file modification was not observed")
            cleanup_guarded_clone(target)

    def test_directory_only_modified_notification_is_not_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "existing-dir").mkdir()
            self.assertFalse(
                _notification_is_authoritative_mutation(
                    root,
                    _DirectoryWatcher.FILE_ACTION_MODIFIED,
                    "existing-dir",
                )
            )

    def test_file_modified_notification_is_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "existing.txt").write_text("x\n", encoding="utf-8")
            self.assertTrue(
                _notification_is_authoritative_mutation(
                    root,
                    _DirectoryWatcher.FILE_ACTION_MODIFIED,
                    "existing.txt",
                )
            )

    def test_add_remove_and_rename_notifications_are_always_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dir").mkdir()
            for action in (
                _DirectoryWatcher.FILE_ACTION_ADDED,
                _DirectoryWatcher.FILE_ACTION_REMOVED,
                _DirectoryWatcher.FILE_ACTION_RENAMED_OLD_NAME,
                _DirectoryWatcher.FILE_ACTION_RENAMED_NEW_NAME,
            ):
                with self.subTest(action=action):
                    self.assertTrue(
                        _notification_is_authoritative_mutation(root, action, "dir")
                    )


if __name__ == "__main__":
    unittest.main()
