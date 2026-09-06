from __future__ import annotations

import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier


class _FakeGuard:
    next_changes = ()
    next_errors = ()
    starts = 0
    stops = 0

    def __init__(self, root: Path):
        self.root = Path(root)

    def start(self) -> bool:
        type(self).starts += 1
        return True

    def stop(self):
        type(self).stops += 1
        return types.SimpleNamespace(
            changes=tuple(type(self).next_changes),
            errors=tuple(type(self).next_errors),
        )


class _FakeProofStore:
    root = Path("proof-root")

    def __init__(self, *args, **kwargs):
        pass

    def lookup_pass(self, proof_type: str, key: str):
        if proof_type == "preparation" and key:
            return types.SimpleNamespace(metadata={})
        return None


class Psi3PrivatePreparedTests(unittest.TestCase):
    def setUp(self) -> None:
        verifier.reset_same_run_verification_reuse()
        _FakeGuard.next_changes = ()
        _FakeGuard.next_errors = ()
        _FakeGuard.starts = 0
        _FakeGuard.stops = 0

    def tearDown(self) -> None:
        verifier.reset_same_run_verification_reuse()

    def _snapshot(self, root: Path, source: Path, key: str = "k" * 32):
        workspace = root / "stage" / "workspace"
        project = workspace / "app"
        project.mkdir(parents=True)
        (project / "package.json").write_text(
            '{"name":"fixture"}\n', encoding="utf-8"
        )
        return verifier.PreparedWorkspaceSnapshot(
            key=key,
            workspace_root=workspace,
            project_relative=Path("app"),
            source_project=source.resolve(),
            storage_mode="moved-sealed-workspace",
            observed_resolved_versions={"demo": "2.0.0"},
            observed_resolved_hash="observed",
            dependency_roots=(),
            dependency_integrity={},
        )

    def test_private_snapshot_claim_requires_clean_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            snapshot = self._snapshot(root / "private", source)
            verifier._register_same_run_private_prepared_snapshot(
                snapshot, producer="adaptive-screen"
            )
            with patch.object(verifier, "WorkspaceChangeGuard", _FakeGuard):
                self.assertTrue(
                    verifier._park_same_run_private_prepared_snapshot(
                        snapshot.key, source
                    )
                )
                claimed, producer, reason = (
                    verifier._claim_same_run_private_prepared_snapshot(
                        snapshot.key, source
                    )
                )
            self.assertIsNotNone(claimed)
            self.assertEqual("adaptive-screen", producer)
            self.assertEqual("hit", reason)
            self.assertTrue(snapshot.workspace_root.is_dir())

    def test_private_watcher_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            snapshot = self._snapshot(root / "private", source)
            verifier._register_same_run_private_prepared_snapshot(
                snapshot, producer="screen"
            )
            _FakeGuard.next_changes = ("node_modules/demo/package.json",)
            with patch.object(verifier, "WorkspaceChangeGuard", _FakeGuard):
                self.assertTrue(
                    verifier._park_same_run_private_prepared_snapshot(
                        snapshot.key, source
                    )
                )
                claimed, _, reason = (
                    verifier._claim_same_run_private_prepared_snapshot(
                        snapshot.key, source
                    )
                )
            self.assertIsNone(claimed)
            self.assertEqual("workspace-mutated", reason)
            self.assertFalse(
                verifier._same_run_private_snapshot_exists(snapshot.key, source)
            )

    def test_reset_destroys_private_root_and_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = verifier._same_run_private_prepared_root(parent)
            source = parent / "source"
            source.mkdir()
            snapshot = self._snapshot(root, source)
            verifier._register_same_run_private_prepared_snapshot(
                snapshot, producer="screen"
            )
            self.assertTrue(root.is_dir())
            verifier.reset_same_run_verification_reuse()
            self.assertFalse(root.exists())
            self.assertFalse(
                verifier._same_run_private_snapshot_exists(snapshot.key, source)
            )

    def test_promotion_uses_existing_strong_durable_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            snapshot = self._snapshot(root / "private", source)
            verifier._register_same_run_private_prepared_snapshot(
                snapshot, producer="project-preflight"
            )
            with patch.object(verifier, "WorkspaceChangeGuard", _FakeGuard):
                self.assertTrue(
                    verifier._park_same_run_private_prepared_snapshot(
                        snapshot.key, source
                    )
                )

            durable_workspace = root / "durable" / "workspace"
            durable_workspace.mkdir(parents=True)
            durable_snapshot = verifier.PreparedWorkspaceSnapshot(
                key=snapshot.key,
                workspace_root=durable_workspace,
                project_relative=snapshot.project_relative,
                source_project=source.resolve(),
                storage_mode="moved-sealed-workspace",
                observed_resolved_versions=snapshot.observed_resolved_versions,
                observed_resolved_hash=snapshot.observed_resolved_hash,
            )
            calls = []

            def fake_publish(*args, **kwargs):
                calls.append(kwargs)
                return durable_snapshot

            with patch.object(verifier, "WorkspaceChangeGuard", _FakeGuard), \
                 patch.object(verifier, "VerificationProofStore", _FakeProofStore), \
                 patch.object(verifier, "configure_prepared_artifact_store"), \
                 patch.object(
                     verifier, "_lookup_prepared_workspace_snapshot",
                     return_value=None
                 ), \
                 patch.object(
                     verifier, "_publish_prepared_workspace_snapshot",
                     side_effect=fake_publish
                 ), \
                 patch.object(
                     verifier, "load_prepared_artifact_record",
                     return_value={"key": snapshot.key}
                 ), \
                 patch.object(
                     verifier, "is_durable_prepared_path",
                     return_value=True
                 ), \
                 patch.object(verifier, "pin_prepared_artifact_record"):
                promoted = verifier.promote_same_run_prepared_artifact(
                    source,
                    snapshot.key,
                    proof_cache_dir=str(root / "proof"),
                )

            self.assertTrue(promoted)
            self.assertEqual(1, len(calls))
            self.assertTrue(calls[0]["shared_reuse_allowed"])
            self.assertIsNone(calls[0]["publication_root"])

    def test_source_contract_keeps_authority_fail_closed(self) -> None:
        source = Path(verifier.__file__).read_text(encoding="utf-8")
        self.assertIn("PSI3_SAME_RUN_PRIVATE_PREPARED_HIT", source)
        self.assertIn("_same_run_private_prepared_root(trial_parent)", source)
        self.assertIn("preparation_record is not None", source)
        self.assertIn(
            "return not any(str(command).strip() for command in commands)",
            source,
        )

    def test_incumbent_is_recorded_before_durable_promotion(self) -> None:
        generator = (
            Path(verifier.__file__).resolve().parent
            / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")
        incumbent = generator.index(
            "completion_status = record_verified_incumbent("
        )
        promotion = generator.index(
            "promote_same_run_prepared_artifact(", incumbent
        )
        final_assignment = generator.index(
            "final_assignments.setdefault(project, {})[mode] = assignment",
            promotion,
        )
        self.assertLess(incumbent, promotion)
        self.assertLess(promotion, final_assignment)
        self.assertIn('authority="PERFORMANCE_ONLY"', generator)


if __name__ == "__main__":
    unittest.main()
