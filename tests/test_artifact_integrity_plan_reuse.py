from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import artifact_integrity as integrity


class ArtifactIntegrityPlanReuseTests(unittest.TestCase):
    def test_prevalidated_empty_plan_skips_reparse_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "project").mkdir()
            (root / "project" / "package.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(
                integrity,
                "inventory_reparse_plan",
                side_effect=AssertionError("redundant whole-tree reparse inventory"),
            ):
                result = integrity.build_artifact_tree_integrity(
                    root,
                    max_workers=1,
                    reparse_plan=(),
                )
        self.assertEqual(result.file_count, 1)
        self.assertEqual(result.reparse_count, 0)

    def test_hardlinks_remain_fail_closed_with_prevalidated_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subject = root / "subject.txt"
            alias = root / "alias.txt"
            subject.write_text("same bytes", encoding="utf-8")
            try:
                os.link(subject, alias)
            except OSError:
                self.skipTest("hardlinks unavailable on this filesystem")
            with self.assertRaisesRegex(
                integrity.ArtifactIntegrityError,
                "PREPARED_ARTIFACT_HARDLINK_UNSUPPORTED",
            ):
                integrity.build_artifact_tree_integrity(
                    root,
                    max_workers=1,
                    reparse_plan=(),
                )


if __name__ == "__main__":
    unittest.main()
