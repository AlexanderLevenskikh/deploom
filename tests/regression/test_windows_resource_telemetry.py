"""Native HANDLE width must not silently produce zero memory on Windows x64."""
import os
import unittest
from verification_observability import process_resource_snapshot

class WindowsResourceTelemetryTests(unittest.TestCase):
    @unittest.skipUnless(os.name=="nt","Windows native memory counters")
    def test_real_working_set_and_peak_are_available(self):
        before=process_resource_snapshot()
        self.assertGreater(before.rss_bytes,0)
        self.assertGreaterEqual(before.peak_rss_bytes,before.rss_bytes)
        allocation=bytearray(8*1024*1024)
        allocation[0]=1;allocation[-1]=2
        after=process_resource_snapshot()
        self.assertGreater(after.rss_bytes,0)
        self.assertGreaterEqual(after.peak_rss_bytes,before.peak_rss_bytes)
        self.assertEqual(3,allocation[0]+allocation[-1])

if __name__=="__main__":unittest.main()
