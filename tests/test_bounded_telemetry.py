import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from bounded_telemetry import append_bounded_telemetry


class BoundedTelemetryTests(unittest.TestCase):
    def test_rotation_preserves_json_and_bounds_total_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            for index in range(100):
                append_bounded_telemetry(path, json.dumps({"n": index}) + "\n", max_bytes=100)
            logs = [path, path.with_name(path.name + ".1"), path.with_name(path.name + ".2")]
            self.assertLessEqual(sum(p.stat().st_size for p in logs), 300)
            for p in logs:
                for line in p.read_text().splitlines():
                    self.assertIsInstance(json.loads(line)["n"], int)
            self.assertEqual(99, json.loads(path.read_text().splitlines()[-1])["n"])

    def test_expired_logs_removed_without_touching_proofs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            archive = path.with_name(path.name + ".1")
            proof = Path(tmp) / "proof.json"
            for p in (path, archive, proof):
                p.write_text("old")
                os.utime(p, (time.time() - 3600, time.time() - 3600))
            append_bounded_telemetry(path, '{"fresh":true}\n', max_age_seconds=60)
            self.assertFalse(archive.exists())
            self.assertEqual("old", proof.read_text())
            self.assertTrue(json.loads(path.read_text())["fresh"])

    def test_oversized_legacy_log_is_not_retained_as_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_bytes(b"x" * 1000)
            append_bounded_telemetry(path, '{}\n', max_bytes=100)
            self.assertEqual('{}\n', path.read_text())
            self.assertFalse(path.with_name(path.name + ".1").exists())
