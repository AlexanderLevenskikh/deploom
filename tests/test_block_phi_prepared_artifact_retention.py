from __future__ import annotations
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import block_v_prepared_artifact as artifact

class BlockPhiPreparedArtifactRetentionTests(unittest.TestCase):
    def setUp(self):
        self.previous = artifact._CONFIGURED_ROOT
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "index").mkdir(parents=True)
        (self.root / "trees").mkdir(parents=True)
        artifact._CONFIGURED_ROOT = self.root
    def tearDown(self):
        artifact.clear_prepared_artifact_pins()
        artifact._CONFIGURED_ROOT = self.previous
        self.temp.cleanup()
    def test_pin_and_unpin(self):
        key = "a" * 64
        self.assertTrue(artifact.pin_prepared_artifact_record(key))
        self.assertTrue(artifact._artifact_key_pinned(key))
        artifact.unpin_prepared_artifact_record(key)
        self.assertFalse(artifact._artifact_key_pinned(key))
    def test_stale_pin_expires(self):
        key = "b" * 64
        artifact.pin_prepared_artifact_record(key)
        pin = next((self.root / "pins").glob(f"{key}.*.pin"))
        old = time.time() - artifact._PIN_MAX_AGE_SECONDS - 60
        os.utime(pin, (old, old))
        self.assertFalse(artifact._artifact_key_pinned(key))
        self.assertFalse(pin.exists())
    def test_gc_skips_pinned_old_record(self):
        keys = ["1"*64, "2"*64, "3"*64]
        for index, key in enumerate(keys):
            path = self.root / "index" / f"{key}.json"
            path.write_text("{}", encoding="utf-8")
            stamp = time.time() - index * 10
            os.utime(path, (stamp, stamp))
        artifact.pin_prepared_artifact_record(keys[-1])
        with patch.object(artifact, "invalidate_prepared_artifact_record", return_value=True) as invalidate:
            artifact.prune_prepared_artifact_store(max_count=1)
        self.assertNotIn(keys[-1], [call.args[0] for call in invalidate.call_args_list])

if __name__ == "__main__": unittest.main()
