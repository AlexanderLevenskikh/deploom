from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from block_v_recovery import (
    BaselineRunRecoveryStore,
    RecoveryEpochs,
    baseline_run_identity,
    build_run_state,
    derive_recovery_epochs,
    semantic_component_fingerprint,
)


class RecoveryProductionClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.progress = self.root / "state" / "baseline-verification-progress.json"
        self.identity = baseline_run_identity(
            project="app", mode="yellow", source_snapshot_key="source",
            resolver_context_key="resolver", config={"commands": ["yarn lint:types"]},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _state():
        return build_run_state(
            iteration=4,
            learned_constraints=[{"pkg": "5.1.1"}],
            global_exact_exclusions=[{"pkg": "5.1.1", "peer": "2.0.0"}],
            confirmed_failed_assignments=["deadbeef"],
            liveness={"certifiedExtensions": 2, "learnedConstraints": 1},
            last_assignment="deadbeef",
            last_predicate="P",
        )

    def test_semantic_fingerprint_ignores_comments_and_formatting(self):
        path = self.root / "sample.py"
        path.write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
        one = semantic_component_fingerprint(self.root, "x", {"sample.py": ("f",)})
        path.write_text("# comment\ndef f( x ):\n    # inner\n    return x + 1\n", encoding="utf-8")
        two = semantic_component_fingerprint(self.root, "x", {"sample.py": ("f",)})
        self.assertEqual(one, two)

    def test_semantic_fingerprint_changes_on_executable_change(self):
        path = self.root / "sample.py"
        path.write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
        one = semantic_component_fingerprint(self.root, "x", {"sample.py": ("f",)})
        path.write_text("def f(x):\n    return x + 2\n", encoding="utf-8")
        two = semantic_component_fingerprint(self.root, "x", {"sample.py": ("f",)})
        self.assertNotEqual(one, two)

    def test_missing_symbol_changes_fingerprint_fail_safe(self):
        path = self.root / "sample.py"
        path.write_text("def f():\n    return 1\n", encoding="utf-8")
        existing = semantic_component_fingerprint(self.root, "x", {"sample.py": ("f",)})
        missing = semantic_component_fingerprint(self.root, "x", {"sample.py": ("renamed",)})
        self.assertNotEqual(existing, missing)

    def test_solver_only_change_preserves_authority_but_resets_cursor(self):
        store = BaselineRunRecoveryStore(self.progress)
        old = RecoveryEpochs(solver="old")
        store.checkpoint("app", "yellow", identity=self.identity, state=self._state(), status="tool-error", epochs=old)
        plan = store.inspect("app", "yellow", identity=self.identity, epochs=RecoveryEpochs(solver="new"))
        self.assertTrue(plan.resumable)
        self.assertTrue(plan.preserved_authority)
        self.assertEqual(plan.recheck_from, "solver")
        self.assertEqual(plan.state["iteration"], 0)
        self.assertEqual(plan.state["learnedConstraints"], [{"pkg": "5.1.1"}])

    def test_orchestration_change_preserves_authority_but_resets_cursor(self):
        store = BaselineRunRecoveryStore(self.progress)
        old = RecoveryEpochs(orchestration="old")
        store.checkpoint("app", "yellow", identity=self.identity, state=self._state(), status="tool-error", epochs=old)
        plan = store.inspect("app", "yellow", identity=self.identity, epochs=RecoveryEpochs(orchestration="new"))
        self.assertTrue(plan.resumable)
        self.assertEqual(plan.state["iteration"], 0)
        self.assertEqual(plan.changed_epochs, ("orchestration",))

    def test_predicate_change_drops_checkpoint_authority(self):
        store = BaselineRunRecoveryStore(self.progress)
        old = RecoveryEpochs(predicate="old")
        store.checkpoint("app", "yellow", identity=self.identity, state=self._state(), status="tool-error", epochs=old)
        plan = store.inspect("app", "yellow", identity=self.identity, epochs=RecoveryEpochs(predicate="new"))
        self.assertFalse(plan.resumable)
        self.assertFalse(plan.preserved_authority)
        self.assertEqual(plan.recheck_from, "predicate")
        self.assertEqual(plan.state["learnedConstraints"], [])
        self.assertIn("constraint", plan.invalidated_components)
        self.assertIn("solver", plan.invalidated_components)

    def test_second_live_owner_is_rejected(self):
        first = BaselineRunRecoveryStore(self.progress)
        first.checkpoint("app", "yellow", identity=self.identity, state=self._state(), status="running")
        second = BaselineRunRecoveryStore(self.progress)
        plan = second.begin("app", "yellow", identity=self.identity)
        self.assertEqual(plan.reason, "active-run")
        self.assertFalse(plan.resumable)
        self.assertEqual(plan.active_owner_pid, os.getpid())

    def test_stale_owner_is_reclaimed_and_checkpoint_resumes(self):
        first = BaselineRunRecoveryStore(self.progress)
        first.checkpoint("app", "yellow", identity=self.identity, state=self._state(), status="running")
        active_path = self.progress.with_name("baseline-run-active.json")
        payload = json.loads(active_path.read_text(encoding="utf-8"))
        slot = next(iter(payload["entries"]))
        payload["entries"][slot]["pid"] = 2147483647
        payload["entries"][slot]["birth"] = "dead"
        active_path.write_text(json.dumps(payload), encoding="utf-8")
        second = BaselineRunRecoveryStore(self.progress)
        plan = second.begin("app", "yellow", identity=self.identity)
        self.assertTrue(plan.resumable)
        self.assertTrue(plan.interrupted)
        self.assertEqual(plan.state["iteration"], 4)

    def test_restart_clears_checkpoint_but_claims_active_slot(self):
        store = BaselineRunRecoveryStore(self.progress)
        store.checkpoint("app", "yellow", identity=self.identity, state=self._state(), status="running")
        plan = store.begin("app", "yellow", identity=self.identity, policy="restart")
        self.assertEqual(plan.reason, "restart-requested")
        self.assertFalse(store.inspect("app", "yellow", identity=self.identity).found)
        active = json.loads(self.progress.with_name("baseline-run-active.json").read_text(encoding="utf-8"))
        self.assertTrue(active["entries"])

    def test_terminal_releases_active_owner(self):
        store = BaselineRunRecoveryStore(self.progress)
        store.checkpoint("app", "yellow", identity=self.identity, state=self._state(), status="running")
        store.mark_terminal("app", "yellow", identity=self.identity, status="completed", state=self._state())
        second = BaselineRunRecoveryStore(self.progress)
        plan = second.begin("app", "yellow", identity=self.identity)
        self.assertNotEqual(plan.reason, "active-run")

    def test_checkpoint_generation_is_monotonic(self):
        store = BaselineRunRecoveryStore(self.progress)
        store.checkpoint("app", "yellow", identity=self.identity, state=self._state(), status="running")
        store.checkpoint("app", "yellow", identity=self.identity, state=self._state(), status="running")
        payload = json.loads(self.progress.with_name("baseline-run-recovery.json").read_text(encoding="utf-8"))
        entry = next(iter(payload["entries"].values()))
        self.assertEqual(entry["generation"], 2)

    def test_derived_epoch_manifest_is_deterministic(self):
        one = derive_recovery_epochs(self.root)
        two = derive_recovery_epochs(self.root)
        self.assertEqual(one, two)
        self.assertTrue(one.solver.startswith("semantic-v2-"))
        self.assertTrue(one.orchestration.startswith("semantic-v2-"))


if __name__ == "__main__":
    unittest.main()
