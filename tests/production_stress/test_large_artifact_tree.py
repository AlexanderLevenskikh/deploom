"""Explicit manual stress gate. Set DEPLOOM_STRESS_FILES=100000..200000."""
import os
from pathlib import Path
import tempfile
import unittest

from artifact_integrity import build_artifact_tree_integrity, ArtifactIntegrityError
from examples.production_file_tree import generate_dependency_tree


class LargeArtifactTree(unittest.TestCase):
    def test_large_tree_seal_and_mutation(self):
        count = int(os.environ.get("DEPLOOM_STRESS_FILES", "100000"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tree"
            files = generate_dependency_tree(root, entries=count)
            before = build_artifact_tree_integrity(root, progress=print)
            self.assertEqual(files, before.file_count)
            victim = root / "node_modules/@fixture/island-01/file-000001.js"
            victim.write_text("module.exports = 'changed';\n", encoding="utf-8")
            after = build_artifact_tree_integrity(root, progress=print)
            self.assertNotEqual(before.key, after.key)
