from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import block_psi57_yarn1_resolver_seed as seed_module
from block_psi57_yarn1_resolver_seed import (
    AUTHORITY,
    ResolverSeedError,
    SameRunResolverSeedStore,
    prepare_yarn1_resolver_seed_for_lifecycle,
    resolver_seed_identity,
    validate_yarn1_lifecycle_completion,
    validate_yarn1_resolver_seed,
    yarn1_integrity_path,
    yarn1_resolver_seed_runtime_supported,
)
from package_manager_profile import (
    PackageManagerProfile,
    resolver_seed_continuation_capability,
)


def write_integrity(root: Path, flags: list[str]) -> Path:
    marker = yarn1_integrity_path(root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"flags": flags}) + "\n", encoding="utf-8")
    return marker


class Psi57CapabilityTests(unittest.TestCase):
    def test_yarn_classic_is_the_only_enabled_family(self) -> None:
        yarn = PackageManagerProfile(
            manager="yarn",
            family="yarn-classic",
            declared="yarn@1.22.22",
            declared_version="1.22.22",
            lockfile_name="yarn.lock",
            node_linker="node-modules",
            authoritative_supported=True,
        )
        npm = PackageManagerProfile(
            manager="npm",
            family="npm",
            declared="npm@10.0.0",
            declared_version="10.0.0",
            lockfile_name="package-lock.json",
            node_linker="node-modules",
            authoritative_supported=True,
        )
        yarn_cap = resolver_seed_continuation_capability(yarn)
        npm_cap = resolver_seed_continuation_capability(npm)
        self.assertTrue(yarn_cap.supported)
        self.assertIn("resolver-seed", yarn_cap.strategy)
        self.assertFalse(npm_cap.supported)

    def test_runtime_gate_is_exact_windows_yarn_1_22_22_only(self) -> None:
        with mock.patch.object(seed_module.os, "name", "nt"), mock.patch.object(
            seed_module, "yarn1_runtime_version", return_value="1.22.22"
        ):
            self.assertEqual((True, "1.22.22"), yarn1_resolver_seed_runtime_supported("yarn"))
        with mock.patch.object(seed_module.os, "name", "nt"), mock.patch.object(
            seed_module, "yarn1_runtime_version", return_value="1.22.21"
        ):
            self.assertEqual((False, "1.22.21"), yarn1_resolver_seed_runtime_supported("yarn"))
        with mock.patch.object(seed_module.os, "name", "posix"), mock.patch.object(
            seed_module, "yarn1_runtime_version", return_value="1.22.22"
        ):
            self.assertEqual((False, "1.22.22"), yarn1_resolver_seed_runtime_supported("yarn"))

    def test_seed_identity_binds_resolver_and_resolved_state(self) -> None:
        base = resolver_seed_identity(
            resolver_input_key="a" * 64,
            resolved_state_key="b" * 64,
            observed_resolved_hash="c" * 64,
            manager_family="yarn-classic",
        )
        changed_resolver = resolver_seed_identity(
            resolver_input_key="d" * 64,
            resolved_state_key="b" * 64,
            observed_resolved_hash="c" * 64,
            manager_family="yarn-classic",
        )
        changed_state = resolver_seed_identity(
            resolver_input_key="a" * 64,
            resolved_state_key="e" * 64,
            observed_resolved_hash="c" * 64,
            manager_family="yarn-classic",
        )
        self.assertNotEqual(base, changed_resolver)
        self.assertNotEqual(base, changed_state)


class Psi57YarnIntegrityContractTests(unittest.TestCase):
    def test_resolver_seed_requires_ignore_scripts_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_integrity(root, ["ignoreScripts"])
            self.assertEqual(("ignoreScripts",), validate_yarn1_resolver_seed(root))

            write_integrity(root, [])
            with self.assertRaises(ResolverSeedError):
                validate_yarn1_resolver_seed(root)

    def test_lifecycle_preparation_deletes_integrity_marker_only_in_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = write_integrity(root, ["ignoreScripts"])
            returned = prepare_yarn1_resolver_seed_for_lifecycle(root)
            self.assertEqual(marker, returned)
            self.assertFalse(marker.exists())

    def test_lifecycle_completion_requires_new_scripts_enabled_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_integrity(root, [])
            self.assertEqual((), validate_yarn1_lifecycle_completion(root))

            write_integrity(root, ["ignoreScripts"])
            with self.assertRaises(ResolverSeedError):
                validate_yarn1_lifecycle_completion(root)


class Psi57StoreTests(unittest.TestCase):
    def test_store_is_performance_only_and_exact_identity_scoped(self) -> None:
        store = SameRunResolverSeedStore(max_count=1)
        self.assertEqual(0, store.size())
        self.assertEqual("PERFORMANCE_ONLY", AUTHORITY)
        store.clear()

    def test_seed_store_rejects_wrong_resolved_state_without_materializing(self) -> None:
        store = SameRunResolverSeedStore(max_count=1)
        # White-box minimal record setup avoids filesystem watcher requirements;
        # the important contract is that identity mismatch is a cache MISS, not proof.
        fake_guard = mock.Mock()
        fake_record = mock.Mock()
        fake_record.key = "f" * 64
        fake_record.source_project_identity = "wrong-source"
        fake_record.resolved_state_key = "a" * 64
        fake_record.observed_resolved_hash = "b" * 64
        fake_record.workspace_root = Path("missing")
        fake_record.guard = fake_guard
        fake_record.last_used = 0.0
        store._records[fake_record.key] = fake_record  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as tmp:
            result = store.materialize(
                key=fake_record.key,
                target_workspace_root=Path(tmp) / "target",
                source_project=Path(tmp),
                resolved_state_key="different",
                observed_resolved_hash="b" * 64,
            )
        self.assertIsNone(result)
        self.assertEqual(0, store.size())
        store.clear()


class Psi57StaticVerifierContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verifier = (ROOT / "baseline_constraint_verifier.py").read_text(
            encoding="utf-8"
        )
        cls.profile = (ROOT / "package_manager_profile.py").read_text(
            encoding="utf-8"
        )
        cls.seed_module = (ROOT / "block_psi57_yarn1_resolver_seed.py").read_text(
            encoding="utf-8"
        )

    def test_resolver_seed_is_same_run_performance_only(self) -> None:
        self.assertIn("# BLOCK_PSI57_YARN1_RESOLVER_SEED_V1", self.verifier)
        self.assertIn("same_run_resolver_seed_store()", self.verifier)
        self.assertIn("resolver-seed.published", self.verifier)
        self.assertIn("resolver-seed.hit", self.verifier)
        self.assertIn("resolver-seed.miss", self.verifier)
        self.assertIn("authority=RESOLVER_SEED_AUTHORITY", self.verifier)

    def test_seed_never_publishes_proof(self) -> None:
        self.assertIn("# BLOCK_PSI57_YARN1_RESOLVER_SEED_V1", self.verifier)
        self.assertIn("# BLOCK_PSI57_YARN1_RESOLVER_SEED_END", self.verifier)
        self.assertNotIn("VerificationProofStore", self.seed_module)
        self.assertNotIn("publish_pass", self.seed_module)
        self.assertNotIn("EVIDENCE_CONFIRMED_CONSTRAINT", self.seed_module)
        self.assertNotIn("peer_solver", self.seed_module)

    def test_seed_clone_still_runs_normal_frozen_lifecycle(self) -> None:
        self.assertIn("prepare_yarn1_resolver_seed_for_lifecycle(", self.verifier)
        self.assertIn("validate_yarn1_lifecycle_completion(", self.verifier)
        self.assertIn("ignore_scripts=False", self.verifier)
        self.assertIn("frozen=True", self.verifier)
        self.assertIn('progress_label="package-manager lifecycle install"', self.verifier)

    def test_unknown_seed_failure_falls_back_without_solver_authority(self) -> None:
        self.assertIn("resolver-seed.fallback", self.verifier)
        self.assertIn("ordinary frozen lifecycle remains authoritative", self.verifier)


if __name__ == "__main__":
    unittest.main()
