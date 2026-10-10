import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import snapshot_compaction as c
import storage_cleanup_inventory as inventory
from source_snapshot import capture_durable_source_snapshot, open_source_snapshot, SourceCaptureError


class SnapshotCompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deploom-cold-demo-")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.run = self.workspace / ".dependency-roadmap/iterative/demo"
        self.run.mkdir(parents=True)
        (self.run / "run.json").write_text(json.dumps({"runId":"demo", "phase":"TERMINAL", "activeCheckpointId":"C2"}))
        self.source = Path(self.temp.name) / "source"; self.source.mkdir()
        (self.source / "package.json").write_text('{"name":"cold-demo","version":"1.0.0"}')
        (self.source / "payload.bin").write_bytes(b"shared-data" * 150000)
        (self.source / "empty").mkdir()

    def capture(self, name):
        return capture_durable_source_snapshot(self.source, self.run / "sources" / name, timeout_seconds=0)

    def compact(self, snapshot):
        with contextlib.redirect_stdout(io.StringIO()): return c.compact(snapshot.container)

    def test_lossless_reopen_preserves_exact_identity_and_private_files(self):
        snapshot = self.capture("C0"); original = (snapshot.root / "payload.bin").read_bytes()
        reclaimed = self.compact(snapshot)
        self.assertGreater(reclaimed, 1024 * 1024)
        self.assertFalse(snapshot.root.exists())
        restored = open_source_snapshot(snapshot.container, expected_key=snapshot.key, timeout_seconds=0)
        self.assertEqual(restored.key, snapshot.key)
        self.assertEqual((restored.root / "payload.bin").read_bytes(), original)
        self.assertTrue((restored.root / "empty").is_dir())
        self.assertEqual((restored.root / "payload.bin").stat().st_nlink, 1)

    def test_repeated_snapshots_store_identical_bytes_once_and_new_bytes_separately(self):
        first = self.capture("C0"); second = self.capture("C1")
        self.compact(first); objects = list(c.store_root(first.container).rglob("*.gz"))
        self.compact(second)
        self.assertEqual(list(c.store_root(first.container).rglob("*.gz")), objects)
        (self.source / "package.json").write_text('{"name":"cold-demo","version":"2.0.0"}')
        third = self.capture("C2"); self.compact(third)
        self.assertEqual(len(list(c.store_root(first.container).rglob("*.gz"))), len(objects) + 1)
        self.assertEqual(open_source_snapshot(first.container, expected_key=first.key).key, first.key)

    def test_corrupt_object_blocks_recovery_without_publishing_partial_tree(self):
        snapshot = self.capture("C0"); self.compact(snapshot)
        entry = next(e for e in c.manifest(snapshot.container)[0]["entries"] if e.get("size",0)>1000000)
        object_file = c.object_path(c.store_root(snapshot.container), entry["sha256"])
        object_file.chmod(0o600); object_file.write_bytes(b"corrupt")
        with self.assertRaisesRegex(SourceCaptureError, "PACKED_INVALID"):
            open_source_snapshot(snapshot.container, expected_key=snapshot.key)
        self.assertFalse(snapshot.root.exists())

    def test_new_file_omitted_by_source_policy_is_never_discarded(self):
        snapshot = self.capture("C0")
        (snapshot.root / "node_modules").mkdir()
        (snapshot.root / "node_modules/personal.txt").write_text("unique")
        with self.assertRaisesRegex(ValueError, "unregistered"):
            self.compact(snapshot)
        self.assertTrue((snapshot.root / "node_modules/personal.txt").exists())

    def test_write_failure_preserves_original(self):
        snapshot = self.capture("C0")
        with patch("snapshot_compaction.gzip.GzipFile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.compact(snapshot)
        self.assertTrue((snapshot.root / "payload.bin").exists())
        self.assertEqual(open_source_snapshot(snapshot.container, expected_key=snapshot.key).key, snapshot.key)

    def test_inventory_compacts_old_snapshots_but_preserves_current_and_reports(self):
        old = self.capture("C0"); hot = self.capture("C2")
        report = self.run / "report.md"; report.write_text("keep")
        with contextlib.redirect_stdout(io.StringIO()):
            plan = inventory.inspect(None, [str(self.workspace)])
            with patch.object(inventory, "progress") as events:
                result = inventory.clean(None, [str(self.workspace)], plan)
        self.assertEqual(result["compacted"], 1)
        packing_events = [call for call in events.call_args_list if call.args[0] == "clean" and call.kwargs.get("processed",0) > 0]
        self.assertTrue(packing_events)
        self.assertEqual([call.kwargs["processed"] for call in packing_events], sorted(call.kwargs["processed"] for call in packing_events))
        self.assertFalse(old.root.exists()); self.assertTrue(hot.root.exists())
        self.assertEqual(report.read_text(), "keep")
        self.assertEqual(open_source_snapshot(old.container, expected_key=old.key).key, old.key)

    def test_preview_change_or_new_active_candidate_prevents_compaction(self):
        old = self.capture("C0")
        with contextlib.redirect_stdout(io.StringIO()): plan = inventory.inspect(None, [str(self.workspace)])
        state = json.loads((self.run / "run.json").read_text()); state["activeCandidateId"] = "pending"
        (self.run / "run.json").write_text(json.dumps(state))
        with contextlib.redirect_stdout(io.StringIO()): result = inventory.clean(None, [str(self.workspace)], plan)
        self.assertEqual(result["compacted"],0); self.assertTrue(old.root.exists())

    def test_foreign_key_rejected_before_rehydration(self):
        snapshot = self.capture("C0"); self.compact(snapshot)
        with self.assertRaisesRegex(SourceCaptureError, "KEY_MISMATCH"):
            open_source_snapshot(snapshot.container, expected_key="wrong")
        self.assertFalse(snapshot.root.exists())

    def test_interrupted_retirement_remains_recoverable_and_residue_is_cleaned(self):
        snapshot = self.capture('C0')
        with patch('storage_cleanup_inventory.remove_tree', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError): self.compact(snapshot)
        self.assertFalse(snapshot.root.exists())
        self.assertTrue((snapshot.container/'.packed-tree').exists())
        with contextlib.redirect_stdout(io.StringIO()):
            plan=inventory.inspect(None,[str(self.workspace)])
            result=inventory.clean(None,[str(self.workspace)],plan)
        self.assertEqual(result['removed'],1)
        self.assertFalse((snapshot.container/'.packed-tree').exists())
        self.assertEqual(open_source_snapshot(snapshot.container,expected_key=snapshot.key).key,snapshot.key)

    def test_unique_file_in_crash_residue_is_protected(self):
        snapshot=self.capture('C0')
        with patch('storage_cleanup_inventory.remove_tree', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError): self.compact(snapshot)
        (snapshot.container/'.packed-tree/personal.txt').write_text('unique work')
        with contextlib.redirect_stdout(io.StringIO()): plan=inventory.inspect(None,[str(self.workspace)])
        self.assertEqual(plan['eligible'],0)
        self.assertTrue((snapshot.container/'.packed-tree/personal.txt').exists())

    def test_low_destination_space_preserves_original_and_does_not_claim_progress(self):
        snapshot=self.capture('C0')
        with patch('storage_capacity.require_capacity',side_effect=OSError('DISK_SPACE_LOW')):
            with self.assertRaisesRegex(OSError,'DISK_SPACE_LOW'): self.compact(snapshot)
        self.assertTrue(snapshot.root.exists())
        self.assertFalse(c.has_packed_source(snapshot.container))
