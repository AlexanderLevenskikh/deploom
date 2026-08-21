from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import baseline_constraint_verifier as verifier
from baseline_constraint_verifier import BaselineVerifyConfig, BaselineVerifyResult
from verification_proof import emit_verification_event

# BLOCK_Y_OBSERVABILITY_CONTRACT_V1


class BlockYObservabilityContractTests(unittest.TestCase):
    def _config(self, root: Path) -> BaselineVerifyConfig:
        return BaselineVerifyConfig(
            timeout_seconds=30,
            attempt_timeout_seconds=60,
            localization_timeout_seconds=60,
            progress_interval_seconds=5,
            snapshot_copy_timeout_seconds=30,
            project_checks="off",
            commands=(),
            telemetry_path=str(root / "telemetry.jsonl"),
            proof_cache_dir="",
        )

    def _events(self, path: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_early_return_closes_attempt_and_open_stage_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            telemetry = root / "telemetry.jsonl"
            config = self._config(root)
            original = verifier._verify_assignment_uncached_impl

            def fake_impl(*args, **kwargs):
                emit_verification_event(telemetry, "verify.resolver.start", command="fake")
                return BaselineVerifyResult(False, "infrastructure", "synthetic stop")

            verifier._verify_assignment_uncached_impl = fake_impl
            try:
                result = verifier._verify_assignment_uncached(
                    root, {}, config=config, run_project_checks=False
                )
            finally:
                verifier._verify_assignment_uncached_impl = original

            self.assertFalse(result.ok)
            events = self._events(telemetry)
            starts = [e for e in events if e["event"] == "verify.attempt.start"]
            finishes = [e for e in events if e["event"] == "verify.attempt.finish"]
            self.assertEqual(1, len(starts))
            self.assertEqual(1, len(finishes))
            self.assertEqual(starts[0]["attemptId"], finishes[0]["attemptId"])
            self.assertEqual("infrastructure", finishes[0]["outcome"])
            self.assertIn("cpuMs", finishes[0])
            self.assertIn("rssBytes", finishes[0])
            self.assertIn("peakRssBytes", finishes[0])

            rstart = [e for e in events if e["event"] == "verify.resolver.start"]
            rfinish = [e for e in events if e["event"] == "verify.resolver.finish"]
            self.assertEqual(1, len(rstart))
            self.assertEqual(1, len(rfinish))
            self.assertEqual(rstart[0]["stageId"], rfinish[0]["stageId"])
            self.assertTrue(rfinish[0]["synthetic"])
            self.assertEqual("abandoned", rfinish[0]["outcome"])

    def test_public_request_is_paired_and_emits_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            telemetry = root / "telemetry.jsonl"
            config = self._config(root)
            original = verifier._verify_assignment_cache_aware_impl

            def fake_public(*args, **kwargs):
                return BaselineVerifyResult(True, "passed", "synthetic pass")

            verifier._verify_assignment_cache_aware_impl = fake_public
            try:
                result = verifier.verify_assignment(
                    root, {}, config=config, run_project_checks=False
                )
            finally:
                verifier._verify_assignment_cache_aware_impl = original

            self.assertTrue(result.ok)
            events = self._events(telemetry)
            starts = [e for e in events if e["event"] == "verify.request.start"]
            finishes = [e for e in events if e["event"] == "verify.request.finish"]
            summaries = [e for e in events if e["event"] == "verification.run.summary"]
            self.assertEqual(1, len(starts))
            self.assertEqual(1, len(finishes))
            self.assertEqual(starts[0]["requestId"], finishes[0]["requestId"])
            self.assertEqual(starts[0]["runId"], finishes[0]["runId"])
            self.assertTrue(summaries)
            summary = summaries[-1]
            self.assertGreaterEqual(int(summary["requestsStarted"]), 1)
            self.assertGreaterEqual(int(summary["requestsFinished"]), 1)
            self.assertIn("stageDurationMs", summary)
            self.assertIn("cacheHits", summary)
            self.assertIn("peakRssBytes", summary)
            sequences = [int(e["sequence"]) for e in events]
            self.assertEqual(sequences, sorted(sequences))
            self.assertEqual(len(sequences), len(set(sequences)))

    def test_cache_lookup_contract_has_operation_correlation(self) -> None:
        source = Path(verifier.__file__).read_text(encoding="utf-8")
        self.assertIn("cacheOperationId=operation_id", source)
        self.assertIn('new_observability_id("cache")', source)

    def test_observability_is_non_authoritative(self) -> None:
        root = Path(verifier.__file__).resolve().parent
        proof = (root / "verification_proof.py").read_text(encoding="utf-8")
        obs = (root / "verification_observability.py").read_text(encoding="utf-8")
        self.assertIn("never proof authority", proof)
        self.assertIn("can never participate in proof", obs)


if __name__ == "__main__":
    unittest.main()
