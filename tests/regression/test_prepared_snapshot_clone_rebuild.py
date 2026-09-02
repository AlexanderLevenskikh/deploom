from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PreparedSnapshotCloneRebuildRegression(unittest.TestCase):
    def test_clone_failure_is_rebuilt_inside_verifier_once(self):
        text = (ROOT / "baseline_constraint_verifier.py").read_text(encoding="utf-8")
        marker = 'clone_failure = f"PREPARED_SNAPSHOT_CLONE_FAILED: {command}: {exc}"'
        start = text.index(marker)
        block = text[start:start + 3200]
        self.assertIn("_evict_prepared_workspace_snapshot", block)
        self.assertIn("if _allow_prepared_fastpath:", block)
        self.assertIn("_retry_assignment_without_prepared_fastpath", block)
        self.assertIn("return BaselineVerifyResult(", block)

    def test_full_copy_rechecks_tree_after_reader_lease(self):
        text = (ROOT / "baseline_constraint_verifier.py").read_text(encoding="utf-8")
        start = text.index("def _materialize_prepared_workspace_snapshot(")
        end = text.index("def _package_manager_cache_environment(", start)
        block = text[start:end]
        lease = block.index("acquire_snapshot_copy_lease(")
        retired = block.index("PREPARED_SNAPSHOT_RETIRED_DURING_CLONE")
        copy = block.index("_copy_tree_snapshot(")
        self.assertLess(lease, retired)
        self.assertLess(retired, copy)

    def test_retry_remains_fail_closed(self):
        text = (ROOT / "baseline_constraint_verifier.py").read_text(encoding="utf-8")
        marker = 'clone_failure = f"PREPARED_SNAPSHOT_CLONE_FAILED: {command}: {exc}"'
        start = text.index(marker)
        block = text[start:start + 3200]
        self.assertIn('"infrastructure"', block)
        self.assertNotIn('"incompatible"', block)
        self.assertNotIn('"compatible"', block)


if __name__ == "__main__":
    unittest.main()
