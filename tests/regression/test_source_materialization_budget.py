"""Conservative source preflight stops before copying any file payload."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from reparse_materialization import inventory_reparse_plan, ReparseMaterializationError
from source_snapshot import SourceCaptureError, capture_source_snapshot, source_copy_budget, clear_source_snapshot_epochs

class SourceMaterializationBudgetTests(unittest.TestCase):
    def tearDown(self):clear_source_snapshot_epochs()
    def test_capture_rejects_over_budget_before_materialization(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)/"source";root.mkdir()
            (root/"package.json").write_text('{"name":"fixture"}')
            (root/"payload.bin").write_bytes(b"x"*2048)
            with patch.dict(os.environ,{"DEPLOOM_SOURCE_COPY_MAX_BYTES":"1024"}):
                with patch("verification_workspace_backend.run_supervised") as run:
                    with self.assertRaisesRegex(SourceCaptureError,"BUDGET_EXCEEDED.*no source bytes copied"):
                        capture_source_snapshot(root)
                    run.assert_not_called()
            self.assertEqual(2048,(root/"payload.bin").stat().st_size)

    def test_insufficient_disk_is_a_typed_source_failure_before_copy(self):
        from storage_capacity import StorageCapacityError
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)/"source";root.mkdir();(root/"package.json").write_text('{"name":"fixture"}')
            with patch("storage_capacity.require_capacity",side_effect=StorageCapacityError("DISK_SPACE_LOW: fixture")):
                with self.assertRaisesRegex(SourceCaptureError,"DISK_SPACE_LOW"):
                    capture_source_snapshot(root)

    def test_existing_snapshot_obeys_copy_budget_without_copying_again(self):
        import source_snapshot as source
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)/"source";root.mkdir();(root/"package.json").write_text('{"name":"fixture"}')
            (root/"payload.bin").write_bytes(b"x"*2048)
            snapshot=capture_source_snapshot(root)
            self.addCleanup(source._force_rmtree,snapshot.container)
            with source.bind_source_snapshot(snapshot),patch.dict(os.environ,{"DEPLOOM_SOURCE_COPY_MAX_BYTES":"1024"}),patch("source_snapshot.materialize_private_tree") as copy:
                with self.assertRaisesRegex(SourceCaptureError,"BUDGET_EXCEEDED"):
                    source.materialize_source_for_verification(root,Path(raw)/"trial",timeout_seconds=60)
                copy.assert_not_called()

    def test_excluded_dependency_payload_does_not_spend_source_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);(root/"node_modules").mkdir()
            (root/"node_modules/payload.bin").write_bytes(b"x"*4096)
            (root/"source.js").write_bytes(b"x"*16)
            self.assertEqual((),inventory_reparse_plan(root,excluded_dir_names=("node_modules",),maximum_copy_bytes=16))

    def test_selected_project_in_service_exclusion_is_rejected_before_copy(self):
        import shutil, subprocess
        if not shutil.which("git"):self.skipTest("git required")
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve();app=root/".dependency-roadmap/app";app.mkdir(parents=True)
            (app/"package.json").write_text('{"name":"fixture"}')
            for args in (("init","-q"),("config","user.name","Fixture"),("config","user.email","fixture@example.invalid"),("commit","--allow-empty","-qm","fixture")):
                subprocess.run(["git","-C",str(root),*args],check=True,capture_output=True)
            with patch("source_snapshot.materialize_private_tree") as copy:
                with self.assertRaisesRegex(SourceCaptureError,"SOURCE_SELECTED_PROJECT_EXCLUDED"):
                    capture_source_snapshot(app)
                copy.assert_not_called()

    def test_invalid_budget_fails_closed_and_unlimited_is_explicit(self):
        for value in ("-1","bad"):
            with patch.dict(os.environ,{"DEPLOOM_SOURCE_COPY_MAX_BYTES":value}):
                with self.assertRaises(SourceCaptureError):source_copy_budget()
        with patch.dict(os.environ,{"DEPLOOM_SOURCE_COPY_MAX_BYTES":"0"}):self.assertIsNone(source_copy_budget())

if __name__=="__main__":unittest.main()
