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

    def test_ephemeral_vite_cache_does_not_count_as_dependency_mutation(self) -> None:
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
            cache = prepared / "node_modules" / ".vite"
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
            (project / "node_modules" / ".vite" / "generated").write_text("cache\n", encoding="utf-8")
            time.sleep(0.25)
            result = stop_guarded_clone(target)
            self.assertFalse(result.errors, result.errors)
            self.assertFalse(result.mutations, result.mutations)
            cleanup_guarded_clone(target)


if __name__ == "__main__":
    unittest.main()
