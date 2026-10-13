import json
import unittest
from package_manager_telemetry import PackageManagerTelemetry

class PhaseTelemetryTests(unittest.TestCase):
    def observer(self):
        self.now=0;self.events=[]
        return PackageManagerTelemetry("resolver",clock=lambda:self.now,emit=lambda name,**data:self.events.append((name,data)))

    def test_berry_chunk_boundaries_reported_duration_and_unknown_cache(self):
        obs=self.observer();obs.consume("➤ YN0000: ┌ Resolu");obs.consume("tion step\n")
        self.now=1.25;obs.consume("➤ YN0000: └ Completed in 1s 200ms\n")
        result=obs.finish(0)
        self.assertEqual([{"phase":"resolve","observedMs":1250,"reportedMs":1200}],result["phases"])
        self.assertIsNone(result["reportedCacheHitMessages"])
        self.assertEqual("PERFORMANCE_ONLY",result["authority"])

    def test_classic_timings_and_network_secret_redaction(self):
        obs=self.observer()
        for index,text in enumerate(("Resolving packages","Fetching packages","Linking dependencies","Building fresh packages")):
            self.now=index;obs.consume(f"[{index+1}/4] {text}...\n")
        obs.consume("error ESOCKETTIMEDOUT " + "https://" + "secret:password@registry.example.invalid/pkg?token=private\n")
        self.now=5;result=obs.finish(1)
        self.assertEqual(["resolve","fetch","link","build"],[row["phase"] for row in result["phases"]])
        self.assertEqual(["registry.example.invalid"],result["networkHosts"])
        self.assertEqual({"ESOCKETTIMEDOUT":1},result["networkErrors"])
        for secret in ("password","private","secret:"):self.assertNotIn(secret,json.dumps(self.events))

    def test_cache_evidence_and_ansi_split_no_false_hit(self):
        obs=self.observer();obs.consume("\x1b[32m➤ YN0013: pkg can't be found in the cache and will be fetched from the remote registry\x1b[0m\n")
        result=obs.finish(0)
        self.assertEqual(1,result["reportedCacheMissMessages"])
        self.assertEqual(0,result["reportedCacheHitMessages"])

    def test_bounded_unterminated_output_and_no_phase_claim(self):
        obs=self.observer();obs.consume("x"*100000)
        self.assertLessEqual(len(obs.buffer),16384)
        self.assertEqual([],obs.finish(None)["phases"])

if __name__=="__main__":unittest.main()
