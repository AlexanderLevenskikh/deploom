from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import block_v_prepared_artifact as artifact_store


KEY = "c" * 64


@unittest.skipUnless(os.name == "nt", "Windows PreparedArtifact watcher acceptance")
class PreparedArtifactExternalHardlinkPhysical(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old = os.environ.get("DEPLOOM_VERIFICATION_ROOT")
        os.environ["DEPLOOM_VERIFICATION_ROOT"] = str(self.root / "substrate")
        artifact_store.configure_prepared_artifact_store(self.root / "proofs")
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "package.json").write_text(
            '{"name":"src"}', encoding="utf-8"
        )
        trees = artifact_store.prepared_snapshot_storage_root()
        assert trees is not None
        self.workspace = (
            Path(tempfile.mkdtemp(prefix="artifact-", dir=trees)) / "workspace"
        )
        project = self.workspace / "project"
        package = self.workspace / "node_modules" / "demo"
        project.mkdir(parents=True)
        package.mkdir(parents=True)
        (project / "package.json").write_text(
            json.dumps({"name": "project", "dependencies": {"demo": "1.0.0"}}),
            encoding="utf-8",
        )
        self.victim = package / "package.json"
        self.victim.write_text(
            json.dumps({"name": "demo", "version": "1.0.0"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        artifact_store._retire_validation_watchers()
        if self._old is None:
            os.environ.pop("DEPLOOM_VERIFICATION_ROOT", None)
        else:
            os.environ["DEPLOOM_VERIFICATION_ROOT"] = self._old
        self._tmp.cleanup()

    def test_external_hardlink_write_cannot_survive_hot_watcher_memo(self) -> None:
        self.assertTrue(
            artifact_store.publish_prepared_artifact_record(
                key=KEY,
                workspace_root=self.workspace,
                project_relative=Path("project"),
                source_project=self.source,
                storage_mode="physical-hardlink-test",
                dependency_roots=("node_modules",),
                observed_resolved_versions={"demo": "1.0.0"},
                observed_resolved_hash="d" * 64,
            )
        )
        # Cold load performs authoritative validation and arms the hot watcher.
        self.assertIsNotNone(
            artifact_store.load_prepared_artifact_record(KEY, self.source)
        )

        alias_root = self.root / "outside-watched-tree"
        alias_root.mkdir()
        alias = alias_root / "demo-package.json"
        try:
            os.link(self.victim, alias)
        except OSError as exc:
            self.skipTest(f"hardlink unavailable on this filesystem: {exc}")

        before = self.victim.stat()
        payload = self.victim.read_bytes()
        replacement = (b"X" * len(payload)) if payload else b"X"
        with alias.open("r+b") as handle:
            handle.seek(0)
            handle.write(replacement)
            handle.truncate(len(replacement))
            handle.flush()
            os.fsync(handle.fileno())
        # Restore size/mtime signals. A HIT must rely on content/link
        # continuity or proven notification delivery, not metadata heuristics.
        os.utime(alias, ns=(before.st_atime_ns, before.st_mtime_ns))

        self.assertIsNone(
            artifact_store.load_prepared_artifact_record(KEY, self.source),
            "PreparedArtifact hot HIT survived a write through an external hardlink",
        )


if __name__ == "__main__":
    unittest.main()
