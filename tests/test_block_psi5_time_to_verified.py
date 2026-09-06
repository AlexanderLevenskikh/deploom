from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import artifact_integrity
import baseline_constraint_verifier as verifier
from constraint_verify import VerificationUnit, parallel_ddmin
from verification_experiment_registry import (
    ExperimentWaiterCancelled,
    PhysicalExperimentKey,
    navigation_negative_candidates,
    remember_navigation_negative,
    reset_physical_experiment_registry,
    run_physical_experiment,
)


class ExperimentRegistryTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_physical_experiment_registry()

    def test_two_concurrent_requests_use_one_physical_execution(self) -> None:
        gate = threading.Event()
        calls = 0
        values = []
        key = PhysicalExperimentKey(
            "same exact context",
            "screen",
            "diagnostic-probe",
        )

        def producer():
            nonlocal calls
            calls += 1
            gate.wait(2)
            return {"ok": True}

        def consume():
            values.append(run_physical_experiment(key, producer))

        first = threading.Thread(target=consume)
        second = threading.Thread(target=consume)
        first.start()
        time.sleep(0.03)
        second.start()
        time.sleep(0.03)
        gate.set()
        first.join(2)
        second.join(2)

        self.assertEqual(1, calls)
        self.assertEqual(2, len(values))
        self.assertIn("coalesced", {state for _, state in values})

    def test_independent_proof_slots_are_independent(self) -> None:
        calls = 0

        def producer():
            nonlocal calls
            calls += 1
            return calls

        first, _ = run_physical_experiment(
            PhysicalExperimentKey("identity", "screen", "diagnostic-probe"),
            producer,
        )
        second, _ = run_physical_experiment(
            PhysicalExperimentKey(
                "identity",
                "confirmation-1",
                "exact-failure-confirmation",
            ),
            producer,
        )
        self.assertEqual((1, 2), (first, second))
        self.assertEqual(2, calls)

    def test_cancelled_waiter_does_not_cancel_producer(self) -> None:
        gate = threading.Event()
        waiter_cancel = threading.Event()
        calls = 0
        key = PhysicalExperimentKey(
            "cancel-context",
            "screen",
            "diagnostic-probe",
        )
        producer_result = []

        def producer():
            nonlocal calls
            calls += 1
            gate.wait(2)
            return 11

        lead = threading.Thread(
            target=lambda: producer_result.append(
                run_physical_experiment(key, producer)
            )
        )
        lead.start()
        time.sleep(0.03)
        waiter_cancel.set()
        with self.assertRaises(ExperimentWaiterCancelled):
            run_physical_experiment(
                key,
                producer,
                waiter_cancelled=waiter_cancel.is_set,
            )
        gate.set()
        lead.join(2)
        self.assertEqual(1, calls)
        self.assertEqual(11, producer_result[0][0])

    def test_non_reusable_result_is_not_retained(self) -> None:
        calls = 0
        key = PhysicalExperimentKey(
            "infra-context",
            "screen",
            "diagnostic-probe",
        )

        def producer():
            nonlocal calls
            calls += 1
            return {"kind": "infrastructure", "n": calls}

        first, _ = run_physical_experiment(
            key,
            producer,
            is_reusable=lambda result: (
                result["kind"] not in {"infrastructure", "unknown"}
            ),
        )
        second, _ = run_physical_experiment(
            key,
            producer,
            is_reusable=lambda result: (
                result["kind"] not in {"infrastructure", "unknown"}
            ),
        )
        self.assertEqual(2, calls)
        self.assertNotEqual(first["n"], second["n"])

    def test_navigation_negative_is_context_bound(self) -> None:
        remember_navigation_negative("epoch-a", "nav", "candidate")
        self.assertIn(
            ("nav", "candidate"),
            navigation_negative_candidates("epoch-a"),
        )
        self.assertNotIn(
            ("nav", "candidate"),
            navigation_negative_candidates("epoch-b"),
        )


class LocalizationNavigationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_physical_experiment_registry()

    def test_process_navigation_reuse_never_replaces_serial_confirmation(self) -> None:
        units = tuple(VerificationUnit(ch, (ch,)) for ch in "abcd")
        screen = 0
        confirmation = 0

        def fails(candidate):
            nonlocal screen
            screen += 1
            return any(item.id == "a" for item in candidate)

        def confirm(candidate):
            nonlocal confirmation
            confirmation += 1
            return any(item.id == "a" for item in candidate)

        parallel_ddmin(
            units,
            fails,
            confirm_failure=confirm,
            navigation_context_key="same-proof-context",
            max_checks=16,
            parallelism=2,
        )
        first_confirmation = confirmation
        first_screen = screen
        self.assertGreater(first_confirmation, 0)

        parallel_ddmin(
            units,
            fails,
            confirm_failure=confirm,
            navigation_context_key="same-proof-context",
            max_checks=16,
            parallelism=2,
        )
        self.assertGreater(confirmation, first_confirmation)
        self.assertLess(screen - first_screen, first_screen)

    def test_changed_navigation_context_executes_screening_again(self) -> None:
        units = tuple(VerificationUnit(ch, (ch,)) for ch in "abcd")
        screen = 0

        def fails(candidate):
            nonlocal screen
            screen += 1
            return any(item.id == "a" for item in candidate)

        parallel_ddmin(
            units,
            fails,
            confirm_failure=fails,
            navigation_context_key="epoch-a",
            max_checks=8,
        )
        first = screen
        parallel_ddmin(
            units,
            fails,
            confirm_failure=fails,
            navigation_context_key="epoch-b",
            max_checks=8,
        )
        self.assertGreater(screen, first)


class IntegrityCancellationTests(unittest.TestCase):
    def test_cancelled_seal_never_returns_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(32):
                (root / f"{index}.bin").write_bytes(b"x" * 4096)
            with self.assertRaises(
                artifact_integrity.ArtifactIntegrityError
            ) as caught:
                artifact_integrity.build_artifact_tree_integrity(
                    root,
                    max_workers=2,
                    cancelled=lambda: True,
                )
            self.assertIn(
                "PREPARED_ARTIFACT_INTEGRITY_CANCELLED",
                str(caught.exception),
            )


class AuthorityRegressionTests(unittest.TestCase):
    def test_arbitrary_shell_project_proof_policy_unchanged(self) -> None:
        self.assertFalse(
            verifier.project_proof_cache_reusable(("yarn lint:types",))
        )


class WorkerSmokeTests(unittest.TestCase):
    def test_worker_protocol_can_run_generator_help(self) -> None:
        worker = ROOT / "dependency_live_roadmap_worker.py"
        process = subprocess.Popen(
            [sys.executable, str(worker)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            assert process.stdout is not None
            assert process.stdin is not None
            ready = process.stdout.readline()
            self.assertIn('"type":"ready"', ready)
            process.stdin.write(
                json.dumps({
                    "id": "help",
                    "cwd": str(ROOT),
                    "argv": ["--help"],
                    "env": {},
                })
                + "\n"
            )
            process.stdin.flush()
            deadline = time.time() + 10
            seen_complete = False
            while time.time() < deadline:
                line = process.stdout.readline()
                if not line:
                    break
                if '"type":"complete"' in line and '"id":"help"' in line:
                    self.assertIn('"code":0', line)
                    seen_complete = True
                    break
            self.assertTrue(seen_complete)
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


class SourceContractTests(unittest.TestCase):
    def test_background_is_autonomous_but_not_implicitly_exhaustive(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("PSI5_BACKGROUND_AUTONOMOUS", source)
        self.assertNotIn("PSI42_BACKGROUND_AUTONOMOUS_EXHAUSTIVE", source)
        self.assertIn(
            "time-to-first-verified-usable-result",
            source,
        )

    def test_hot_continuation_has_source_epoch_guard(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("DEPLOOM_BASELINE_HOT_CONTINUATION", source)
        self.assertIn("hot_epoch_reusable", source)
        self.assertIn("replace=not hot_epoch_reusable", source)


if __name__ == "__main__":
    unittest.main()
