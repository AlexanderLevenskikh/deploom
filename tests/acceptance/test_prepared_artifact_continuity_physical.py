"""P0-B: physical adversarial tests for PreparedArtifact reuse.

A PreparedArtifact HIT is a proof-bearing statement: the same sealed dependency
bytes, the same directory object, and proven notification delivery. It must
never mean merely "the watcher has not spoken yet".

These use real filesystem operations and the real watcher.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import block_v_prepared_artifact as artifact_store
from artifact_integrity import build_artifact_tree_integrity

KEY = "a" * 64


def _populate(workspace: Path) -> Path:
    project = workspace / "project"
    package = workspace / "node_modules" / "demo"
    project.mkdir(parents=True)
    package.mkdir(parents=True)
    (project / "package.json").write_text(
        json.dumps({"name": "demo-project", "dependencies": {"demo": "1.0.0"}}),
        encoding="utf-8",
    )
    (package / "package.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0"}), encoding="utf-8"
    )
    return workspace


class PreparedArtifactContinuityPhysical(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old = os.environ.get("DEPLOOM_VERIFICATION_ROOT")
        os.environ["DEPLOOM_VERIFICATION_ROOT"] = str(self.root / "substrate")
        artifact_store.configure_prepared_artifact_store(self.root / "proofs")
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "package.json").write_text('{"name":"src"}', encoding="utf-8")
        trees = artifact_store.prepared_snapshot_storage_root()
        assert trees is not None
        self.workspace = _populate(
            Path(tempfile.mkdtemp(prefix="artifact-", dir=trees)) / "workspace"
        )

    def tearDown(self) -> None:
        artifact_store._retire_validation_watchers()
        if self._old is None:
            os.environ.pop("DEPLOOM_VERIFICATION_ROOT", None)
        else:
            os.environ["DEPLOOM_VERIFICATION_ROOT"] = self._old
        self._tmp.cleanup()

    def _publish(self) -> str:
        published = artifact_store.publish_prepared_artifact_record(
            key=KEY,
            workspace_root=self.workspace,
            project_relative=Path("project"),
            source_project=self.source,
            storage_mode="test-durable",
            dependency_roots=("node_modules",),
            observed_resolved_versions={"demo": "1.0.0"},
            observed_resolved_hash="b" * 64,
        )
        self.assertTrue(published, "fixture failed to publish")
        return KEY

    def _load(self):
        return artifact_store.load_prepared_artifact_record(KEY, self.source)

    @unittest.skipUnless(os.name == "nt", "the watcher memo fast path is Windows-only")
    def test_cold_then_hot_hit_without_rehashing(self) -> None:
        self._publish()
        self.assertIsNotNone(self._load())
        with patch.object(
            artifact_store,
            "build_artifact_tree_integrity",
            wraps=build_artifact_tree_integrity,
        ) as integrity:
            self.assertIsNotNone(self._load())
            integrity.assert_not_called()

    def test_mutated_artifact_never_returns_a_hit(self) -> None:
        self._publish()
        self.assertIsNotNone(self._load())
        victim = self.workspace / "node_modules" / "demo" / "package.json"
        os.chmod(victim, 0o666)
        victim.write_text(
            json.dumps({"name": "demo", "version": "9.9.9"}), encoding="utf-8"
        )
        # Consumed immediately, without waiting for the watcher notification.
        self.assertIsNone(self._load(), "mutated artifact returned a HIT")

    @unittest.skipUnless(os.name == "nt", "Windows directory-swap acceptance")
    def test_directory_swap_at_same_path_never_returns_a_hit(self) -> None:
        self._publish()
        self.assertIsNotNone(self._load())

        evil = self.root / "evil"
        (evil / "project").mkdir(parents=True)
        (evil / "project" / "package.json").write_text("{}", encoding="utf-8")
        retired = self.workspace.parent / "retired"
        try:
            os.rename(self.workspace, retired)
            os.rename(evil, self.workspace)
        except OSError:
            self.skipTest("directory swap not permitted here")
        self.assertTrue(self.workspace.is_dir(), "precondition: path still valid")
        self.assertIsNone(
            self._load(), "swapped directory object returned a HIT"
        )

    def test_watcher_failure_falls_back_instead_of_destroying_the_record(self) -> None:
        """Availability: an unusable watcher must not delete a valid record."""
        self._publish()
        self.assertIsNotNone(self._load())
        entries = list(artifact_store._VALIDATION_WATCHERS.values())
        if not entries:
            self.skipTest("no active validation watcher on this platform")
        entries[0][0].errors.append("SIMULATED_WATCHER_OVERFLOW")
        # Content is untouched, so authoritative fallback must still HIT.
        self.assertIsNotNone(
            self._load(), "watcher failure destroyed a valid durable record"
        )


if __name__ == "__main__":
    unittest.main()
