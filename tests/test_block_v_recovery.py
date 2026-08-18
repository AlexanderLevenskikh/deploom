from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from block_v_recovery import (
    BaselineRunRecoveryStore,
    RecoveryEpochs,
    baseline_run_identity,
    build_run_state,
    restore_liveness_budget,
    restore_run_state,
)


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.progress = self.root / "baseline-progress.json"
        self.store = BaselineRunRecoveryStore(self.progress)
        self.identity = baseline_run_identity(
            project="p", mode="yellow", source_snapshot_key="s",
            resolver_context_key="r", config={"commands": ["yarn lint:types"]},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _state(self):
        return build_run_state(
            iteration=3,
            learned_constraints=[{"a": "1.2.3"}],
            global_exact_exclusions=[{"a": "1.2.3", "b": "2.0.0"}],
            confirmed_failed_assignments=["abc"],
            liveness={"certifiedExtensions": 1, "learnedConstraints": 1},
            last_assignment="abc",
        )

    def test_exact_crash_resume(self):
        self.store.checkpoint("p", "yellow", identity=self.identity, state=self._state(), status="running")
        plan = self.store.begin("p", "yellow", identity=self.identity)
        self.assertTrue(plan.resumable)
        self.assertTrue(plan.interrupted)
        learned, exclusions, failed, iteration, live = restore_run_state(plan.state)
        self.assertEqual(iteration, 3)
        self.assertEqual(learned, [{"a": "1.2.3"}])
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(failed, {"abc"})
        self.assertEqual(live["certifiedExtensions"], 1)

    def test_orchestration_only_epoch_change_keeps_authority(self):
        old = RecoveryEpochs(orchestration="old")
        self.store.checkpoint("p", "yellow", identity=self.identity, state=self._state(), status="tool-error", epochs=old)
        plan = self.store.inspect("p", "yellow", identity=self.identity, epochs=RecoveryEpochs(orchestration="new"))
        self.assertTrue(plan.resumable)
        self.assertEqual(plan.changed_epochs, ("orchestration",))

    def test_predicate_epoch_change_requires_recheck(self):
        old = RecoveryEpochs(predicate="old")
        self.store.checkpoint("p", "yellow", identity=self.identity, state=self._state(), status="tool-error", epochs=old)
        plan = self.store.inspect("p", "yellow", identity=self.identity, epochs=RecoveryEpochs(predicate="new"))
        self.assertFalse(plan.resumable)
        self.assertEqual(plan.recheck_from, "predicate")

    def test_restart_discards_orchestration_checkpoint(self):
        self.store.checkpoint("p", "yellow", identity=self.identity, state=self._state(), status="running")
        plan = self.store.begin("p", "yellow", identity=self.identity, policy="restart")
        self.assertFalse(plan.resumable)
        self.assertFalse(self.store.inspect("p", "yellow", identity=self.identity).found)

    def test_corrupt_checkpoint_fails_open_and_is_preserved(self):
        path = self.progress.with_name("baseline-run-recovery.json")
        path.write_text("{bad", encoding="utf-8")
        plan = self.store.inspect("p", "yellow", identity=self.identity)
        self.assertFalse(plan.found)
        self.assertTrue(list(self.root.glob("baseline-run-recovery.json.corrupt-*")))

    def test_restore_liveness_is_bounded(self):
        class Budget:
            max_learning_extensions = 4
            certified_extensions = 0
            learned_extensions = 0
            exact_extension_credits = 0
            learned_constraints = 0
            exact_exclusions = 0
            exact_since_learning = 0
            generalization_attempts = 0
            diagnostics = 0
        budget = Budget()
        restore_liveness_budget(budget, {"certifiedExtensions": 99, "learnedConstraints": 3})
        self.assertEqual(budget.certified_extensions, 4)
        self.assertEqual(budget.learned_constraints, 3)

    def test_restore_liveness_never_drops_preloaded_constraints(self):
        class Budget:
            max_learning_extensions = 8
            starting_learned_constraints = 5
            certified_extensions = 0
            learned_extensions = 0
            exact_extension_credits = 0
            learned_constraints = 5
            exact_exclusions = 0
            exact_since_learning = 0
            generalization_attempts = 0
            diagnostics = 0
        budget = Budget()
        restore_liveness_budget(budget, {"learnedConstraints": 2})
        self.assertEqual(budget.learned_constraints, 5)


if __name__ == "__main__":
    unittest.main()
