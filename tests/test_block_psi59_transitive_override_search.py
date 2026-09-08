from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi59_transitive_override_search import (
    TransitiveOverridePolicy,
    parse_yarn1_subject_points,
    propose_transitive_duplicate_override_candidates,
    reserve_transitive_override_attempt,
    transitive_duplicate_subject,
)


PREDICATE = "duplicate-type-universe:rollup"


class FakeStateStore:
    def __init__(self, attempts=(), *, fail_mark=False):
        self.attempts = set(attempts)
        self.fail_mark = fail_mark
        self.events = []

    def load_session(
        self, project, mode, *, run_identity, package, predicate
    ):
        self.events.append(("load", package, predicate))
        return SimpleNamespace(attempted_versions=tuple(sorted(self.attempts)))

    def mark_attempt(
        self, project, mode, *, run_identity, package, predicate, version
    ):
        self.events.append(("mark", package, predicate, version))
        if self.fail_mark:
            raise OSError("synthetic persistence failure")
        self.attempts.add(version)


def write_state_artifact(root: Path, lockfile: bytes):
    state_key = "a" * 64
    lock_hash = hashlib.sha256(lockfile).hexdigest()
    relative = Path("resolved-state") / state_key / "lockfile.bin"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(lockfile)
    return state_key, lock_hash, relative.as_posix()


class Psi59EvidenceTests(unittest.TestCase):
    def test_transitive_subject_does_not_steal_direct_psi55_case(self):
        self.assertEqual(
            "",
            transitive_duplicate_subject(
                signatures=(PREDICATE,),
                managed_packages=("rollup", "vite"),
                manager="yarn",
            ),
        )
        self.assertEqual(
            "rollup",
            transitive_duplicate_subject(
                signatures=(PREDICATE,),
                managed_packages=("vite", "vitest"),
                manager="yarn",
            ),
        )

    def test_yarn_parser_extracts_multiple_subject_versions(self):
        payload = b'''# yarn lockfile v1\n\nrollup@^3.20.0:\n  version "3.29.5"\n\n"rollup@>=3.25.0 <4":\n  version "3.28.1"\n\nvite@^5.0.0:\n  version "5.4.0"\n'''
        points = parse_yarn1_subject_points(payload, package="rollup")
        self.assertEqual(2, len(points))
        self.assertEqual(
            {"3.29.5", "3.28.1"},
            {item.resolved_version for item in points},
        )

    def test_overlapping_ranges_produce_observed_exact_unification(self):
        payload = b'''# yarn lockfile v1\n\nrollup@^3.20.0:\n  version "3.29.5"\n\n"rollup@>=3.25.0 <4":\n  version "3.28.1"\n'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_key, lock_hash, artifact = write_state_artifact(root, payload)
            candidates = propose_transitive_duplicate_override_candidates(
                signatures=(PREDICATE,),
                managed_packages=("vite", "vitest"),
                manager="yarn",
                proof_cache_dir=root,
                resolved_state_key=state_key,
                resolved_state_artifact=artifact,
                resolved_lockfile_path="yarn.lock",
                resolved_lockfile_hash=lock_hash,
                published_versions=("3.27.0", "3.29.5", "3.30.0"),
                limit=2,
            )
        self.assertGreaterEqual(len(candidates), 1)
        self.assertEqual("3.29.5", candidates[0].version)
        self.assertEqual("observed-resolved", candidates[0].candidate_source)
        self.assertEqual(("3.29.5", "3.28.1"), candidates[0].evidence.observed_versions)

    def test_empty_range_intersection_produces_no_override(self):
        payload = b'''# yarn lockfile v1\n\nrollup@^3.20.0:\n  version "3.29.5"\n\nrollup@^4.0.0:\n  version "4.22.4"\n'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_key, lock_hash, artifact = write_state_artifact(root, payload)
            candidates = propose_transitive_duplicate_override_candidates(
                signatures=(PREDICATE,),
                managed_packages=("vite", "vitest"),
                manager="yarn",
                proof_cache_dir=root,
                resolved_state_key=state_key,
                resolved_state_artifact=artifact,
                resolved_lockfile_path="yarn.lock",
                resolved_lockfile_hash=lock_hash,
                published_versions=("3.29.5", "4.22.4"),
            )
        self.assertEqual((), candidates)

    def test_non_semver_subject_selector_fails_closed(self):
        payload = b'''# yarn lockfile v1\n\nrollup@^3.20.0:\n  version "3.29.5"\n\nrollup@git+https://example.invalid/repo.git:\n  version "3.28.1"\n'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_key, lock_hash, artifact = write_state_artifact(root, payload)
            candidates = propose_transitive_duplicate_override_candidates(
                signatures=(PREDICATE,),
                managed_packages=("vite",),
                manager="yarn",
                proof_cache_dir=root,
                resolved_state_key=state_key,
                resolved_state_artifact=artifact,
                resolved_lockfile_path="yarn.lock",
                resolved_lockfile_hash=lock_hash,
                published_versions=("3.29.5",),
            )
        self.assertEqual((), candidates)

    def test_tampered_lockfile_artifact_produces_no_candidate(self):
        payload = b'''# yarn lockfile v1\n\nrollup@^3.20.0:\n  version "3.29.5"\n\nrollup@>=3.25.0 <4:\n  version "3.28.1"\n'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_key, lock_hash, artifact = write_state_artifact(root, payload)
            (root / artifact).write_bytes(payload + b"# tampered\n")
            candidates = propose_transitive_duplicate_override_candidates(
                signatures=(PREDICATE,),
                managed_packages=("vite",),
                manager="yarn",
                proof_cache_dir=root,
                resolved_state_key=state_key,
                resolved_state_artifact=artifact,
                resolved_lockfile_path="yarn.lock",
                resolved_lockfile_hash=lock_hash,
                published_versions=("3.29.5",),
            )
        self.assertEqual((), candidates)

    def test_registry_intersection_candidate_used_when_observed_versions_do_not_fit_all(self):
        # Both observed points are duplicated, but neither is in the complete
        # intersection. A registry exact point inside every range is allowed.
        payload = b'''# yarn lockfile v1\n\nrollup@>=3.20.0 <3.29.0:\n  version "3.28.1"\n\nrollup@>=3.28.5 <4:\n  version "3.29.5"\n'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_key, lock_hash, artifact = write_state_artifact(root, payload)
            candidates = propose_transitive_duplicate_override_candidates(
                signatures=(PREDICATE,),
                managed_packages=("vite",),
                manager="yarn",
                proof_cache_dir=root,
                resolved_state_key=state_key,
                resolved_state_artifact=artifact,
                resolved_lockfile_path="yarn.lock",
                resolved_lockfile_hash=lock_hash,
                published_versions=("3.28.6", "3.28.7"),
            )
        self.assertEqual(1, len(candidates))
        self.assertEqual("3.28.7", candidates[0].version)
        self.assertEqual("registry-range-intersection", candidates[0].candidate_source)


class Psi59BudgetTests(unittest.TestCase):
    def test_attempt_is_persisted_before_physical_work(self):
        store = FakeStateStore()
        permit = reserve_transitive_override_attempt(
            predicate_state_store=store,
            policy=TransitiveOverridePolicy(family_physical_budget=2),
            project="fixture-project",
            mode="yellow",
            run_identity="run",
            predicate=PREDICATE,
            subject_package="rollup",
            direct_assignment_fingerprint="assignment-a",
            override_version="3.29.5",
        )
        self.assertTrue(permit.granted)
        self.assertTrue(any(event[0] == "mark" for event in store.events))

    def test_same_version_can_be_retried_after_direct_context_changes(self):
        store = FakeStateStore()
        policy = TransitiveOverridePolicy(family_physical_budget=2)
        first = reserve_transitive_override_attempt(
            predicate_state_store=store,
            policy=policy,
            project="fixture-project",
            mode="yellow",
            run_identity="run",
            predicate=PREDICATE,
            subject_package="rollup",
            direct_assignment_fingerprint="assignment-a",
            override_version="3.29.5",
        )
        second = reserve_transitive_override_attempt(
            predicate_state_store=store,
            policy=policy,
            project="fixture-project",
            mode="yellow",
            run_identity="run",
            predicate=PREDICATE,
            subject_package="rollup",
            direct_assignment_fingerprint="assignment-b",
            override_version="3.29.5",
        )
        self.assertTrue(first.granted)
        self.assertTrue(second.granted)
        self.assertEqual(2, len(store.attempts))

    def test_family_budget_survives_store_reconstruction(self):
        policy = TransitiveOverridePolicy(family_physical_budget=2)
        store = FakeStateStore()
        for context in ("assignment-a", "assignment-b"):
            self.assertTrue(
                reserve_transitive_override_attempt(
                    predicate_state_store=store,
                    policy=policy,
                    project="fixture-project",
                    mode="yellow",
                    run_identity="run",
                    predicate=PREDICATE,
                    subject_package="rollup",
                    direct_assignment_fingerprint=context,
                    override_version="3.29.5",
                ).granted
            )
        restored = FakeStateStore(store.attempts)
        denied = reserve_transitive_override_attempt(
            predicate_state_store=restored,
            policy=policy,
            project="fixture-project",
            mode="yellow",
            run_identity="run",
            predicate=PREDICATE,
            subject_package="rollup",
            direct_assignment_fingerprint="assignment-c",
            override_version="3.29.5",
        )
        self.assertFalse(denied.granted)
        self.assertEqual("transitive-override-family-budget-exhausted", denied.reason)

    def test_persistence_failure_denies_physical_attempt(self):
        store = FakeStateStore(fail_mark=True)
        permit = reserve_transitive_override_attempt(
            predicate_state_store=store,
            policy=TransitiveOverridePolicy(),
            project="fixture-project",
            mode="yellow",
            run_identity="run",
            predicate=PREDICATE,
            subject_package="rollup",
            direct_assignment_fingerprint="assignment-a",
            override_version="3.29.5",
        )
        self.assertFalse(permit.granted)
        self.assertTrue(permit.reason.startswith("reservation-persist-failed:"))


class Psi59StaticIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = (
            ROOT / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")

    def test_transitive_search_is_composed_after_direct_psi55_proposal(self):
        marker = self.generator.index(
            "# BLOCK_PSI59_TRANSITIVE_OVERRIDE_SEARCH_V1"
        )
        direct = self.generator.rfind(
            "propose_duplicate_type_override(", 0, marker
        )
        transitive = self.generator.find(
            "propose_transitive_duplicate_override_candidates(", marker
        )
        self.assertGreaterEqual(direct, 0)
        self.assertGreater(transitive, marker)

    def test_physical_budget_is_reserved_before_override_verifier(self):
        marker = self.generator.index(
            "# BLOCK_PSI59_TRANSITIVE_OVERRIDE_SEARCH_V1"
        )
        reserve = self.generator.index(
            "reserve_transitive_override_attempt(", marker
        )
        verify = self.generator.index(
            "override_resolver = verify_assignment(", reserve
        )
        self.assertLess(reserve, verify)

    def test_same_transitive_version_is_context_keyed(self):
        self.assertIn(
            'f"{override_proposal.version}@{fingerprint}"',
            self.generator,
        )

    def test_search_remains_one_candidate_per_direct_assignment(self):
        marker = self.generator.index(
            "# BLOCK_PSI59_TRANSITIVE_OVERRIDE_SEARCH_V1"
        )
        tail = self.generator[marker : marker + 12000]
        self.assertIn("limit=1", tail)
        self.assertIn("transitive_candidates[0]", tail)



if __name__ == "__main__":
    unittest.main()
