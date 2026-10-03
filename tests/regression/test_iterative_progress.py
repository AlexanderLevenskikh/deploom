import threading
import unittest
from iterative_progress import MigrationProgress


class MigrationProgressTests(unittest.TestCase):
    def test_quiet_operation_heartbeats_and_stops_without_fake_completion(self):
        events = []
        heartbeat = threading.Event()
        def emit(event):
            events.append(event)
            if event["heartbeat"]: heartbeat.set()
        with MigrationProgress(emit, operation="verify-exact", message="Hashing source", run_id="run", candidate_id="cand", interval=0.01) as progress:
            self.assertTrue(heartbeat.wait(2), "Quiet work must remain observable")
            progress("project check 1/4 started: yarn typecheck")
        count = len(events)
        progress("Late callback must be ignored")
        self.assertEqual(len(events), count)
        self.assertEqual(events[-1]["message"], "project check 1/4 started: yarn typecheck")
        self.assertEqual([e["sequence"] for e in events], list(range(1, count + 1)))
        self.assertTrue(all(e["runId"] == "run" and e["candidateId"] == "cand" for e in events))
        self.assertFalse(any("ok" in e or "percent" in e for e in events))
        self.assertFalse(progress.thread.is_alive())

    def test_live_counts_include_accepted_updates_and_unresolved_goals(self):
        from iterative_task import build_progress_summary
        config = {"targets": {"a": "2", "b": "2"}, "targetDiscovery": [{"package": "c", "status": "discovery-budget-skipped"}]}
        base = {"checkpointId": "C0", "fullAssignment": {"a": "1", "b": "1"}, "acceptedDelta": {}}
        active = {"checkpointId": "C1", "fullAssignment": {"a": "2", "b": "1"}, "acceptedDelta": {"changed": {"a": "2"}}}
        summary = build_progress_summary(config, [base, active], active, {"deferrals": [{"package": "b", "reason": "blocked"}]})
        self.assertEqual(summary, {"checkpointId": "C1", "remaining": 2, "denominator": 3, "accepted": 1, "deferred": 1, "targetCount": 2, "unresolvedGoals": 1})
        # Candidate work never enters counts until it becomes a checkpoint.
        before = build_progress_summary(config, [base], base, {})
        self.assertEqual(before["accepted"], 0)
        self.assertEqual(before["remaining"], 3)
        future = {"checkpointId": "C2", "parentCheckpointId": "C1", "acceptedDelta": {"changed": {"b": "2"}}}
        consistent = build_progress_summary(config, [base, active, future], active, {})
        self.assertEqual(consistent["accepted"], 1, "A newly written descendant must not race the active checkpoint")

    def test_exception_stops_reporter_without_turning_failure_green(self):
        events = []
        reporter = MigrationProgress(events.append, operation="verify", message="Working", interval=0.01)
        with self.assertRaisesRegex(RuntimeError, "failed"):
            with reporter: raise RuntimeError("failed")
        self.assertFalse(reporter.thread.is_alive())
        self.assertEqual(len(events), 1)
        self.assertNotIn("ok", events[0])
