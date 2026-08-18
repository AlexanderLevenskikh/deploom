from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from block_v_predicate_search import PredicateObservation
from block_v_predicate_state import PredicateSearchStateStore


class PredicateSearchStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.progress = self.root / "state" / "baseline-verification-progress.json"
        self.store = PredicateSearchStateStore(self.progress)

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_is_bound_to_run_identity(self):
        observations = [
            PredicateObservation("pkg", "5.1.1", "P", True, "a"),
            PredicateObservation("pkg", "2.0.0", "P", False, "b"),
        ]
        self.store.save_observations(
            "app", "yellow", run_identity="run-a", package="pkg",
            predicate="P", observations=observations,
        )
        self.store.mark_attempt(
            "app", "yellow", run_identity="run-a", package="pkg",
            predicate="P", version="2.0.0",
        )
        self.store.set_preferred_version(
            "app", "yellow", run_identity="run-a", package="pkg",
            predicate="P", version="3.0.0",
        )
        loaded = self.store.load_session(
            "app", "yellow", run_identity="run-a", package="pkg", predicate="P"
        )
        self.assertEqual(loaded.observations, tuple(observations))
        self.assertEqual(loaded.attempted_versions, ("2.0.0",))
        self.assertEqual(loaded.preferred_version, "3.0.0")

        mismatch = self.store.load_session(
            "app", "yellow", run_identity="run-b", package="pkg", predicate="P"
        )
        self.assertEqual(mismatch.observations, ())
        self.assertEqual(mismatch.preferred_version, "")

    def test_preferred_versions_are_returned_only_when_unambiguous(self):
        self.store.set_preferred_version(
            "app", "yellow", run_identity="run-a", package="pkg",
            predicate="P1", version="3.0.0",
        )
        self.store.set_preferred_version(
            "app", "yellow", run_identity="run-a", package="pkg",
            predicate="P2", version="3.0.0",
        )
        self.assertEqual(
            self.store.preferred_versions("app", "yellow", run_identity="run-a"),
            {"pkg": "3.0.0"},
        )
        self.store.set_preferred_version(
            "app", "yellow", run_identity="run-a", package="pkg",
            predicate="P2", version="4.0.0",
        )
        self.assertEqual(
            self.store.preferred_versions("app", "yellow", run_identity="run-a"),
            {},
        )

    def test_restart_clear_removes_project_mode_diagnostics_only(self):
        for mode in ("yellow", "green"):
            self.store.set_preferred_version(
                "app", mode, run_identity="run-a", package="pkg",
                predicate="P", version="3.0.0",
            )
        self.store.clear_run("app", "yellow")
        self.assertEqual(
            self.store.preferred_versions("app", "yellow", run_identity="run-a"), {}
        )
        self.assertEqual(
            self.store.preferred_versions("app", "green", run_identity="run-a"),
            {"pkg": "3.0.0"},
        )

    def test_corrupt_state_fails_open(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{broken", encoding="utf-8")
        loaded = self.store.load_session(
            "app", "yellow", run_identity="run-a", package="pkg", predicate="P"
        )
        self.assertEqual(loaded.observations, ())


if __name__ == "__main__":
    unittest.main()
