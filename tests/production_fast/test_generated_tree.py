import tempfile
from pathlib import Path
import unittest
from examples.production_file_tree import generate_dependency_tree
from artifact_integrity import build_artifact_tree_integrity

class GeneratedTree(unittest.TestCase):
    def test_scaled_tree_has_deterministic_seal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("first", "second"):
                self.assertEqual(130, generate_dependency_tree(root / name, entries=128))
            first = build_artifact_tree_integrity(root / "first")
            second = build_artifact_tree_integrity(root / "second")
            self.assertEqual(first.key, second.key)
            self.assertEqual(130, first.file_count)
            self.assertGreater(first.directory_count, 48)
