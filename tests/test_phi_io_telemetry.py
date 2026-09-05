import unittest
from unittest.mock import patch
import io_governor as governor

class HashTelemetryTests(unittest.TestCase):
    def test_file_operations_are_aggregated_and_slots_released_on_failure(self):
        governor.reset_io_governor_for_tests()
        with patch.object(governor, "emit_observability_event") as emit:
            for _ in range(1024):
                with governor.io_slot("hash"):
                    pass
            self.assertEqual(4, emit.call_count)
            self.assertEqual(1024, emit.call_args.kwargs["completedOperations"])
            with self.assertRaises(RuntimeError):
                with governor.io_slot("hash"):
                    raise RuntimeError("synthetic I/O failure")
            self.assertEqual(0, governor._pool("hash").active)
            self.assertEqual(1025, governor._pool("hash").completed)
