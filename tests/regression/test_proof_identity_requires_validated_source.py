"""P1-A: an authority-bearing proof identity may not stamp a bare active.key.

`build_verification_proof_identity` read `active.key` and `active.project_path`
straight out of the snapshot registry. Registry membership only proves a
snapshot was once captured -- not that the tree still holds those bytes. The
instrumented count of real validation calls was 0.

These tests count actual calls rather than asserting a branch exists.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import source_snapshot
import verification_proof


def _identity(project: Path):
    return verification_proof.build_verification_proof_identity(
        project,
        assignment={"demo": "1.0.0"},
        remove_packages=(),
        manager="npm",
        manager_executable="npm",
        registry="https://registry.npmjs.org/",
        project_checks="none",
        commands=(),
        environment={},
    )


class ProofIdentityRequiresValidatedSource(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "project"
        self.project.mkdir(parents=True)
        (self.project / "package.json").write_text(
            '{"name":"fixture","dependencies":{"demo":"1.0.0"}}\n', encoding="utf-8"
        )
        (self.project / "index.js").write_text("export const x = 1;\n", encoding="utf-8")

    def tearDown(self) -> None:
        source_snapshot.clear_source_snapshot_epochs()
        self._tmp.cleanup()

    def test_identity_build_actually_validates_the_active_snapshot(self) -> None:
        source_snapshot.activate_source_snapshot_epoch(self.project, replace=True)
        real = verification_proof.validate_source_snapshot
        calls: list[str] = []

        def counting(snapshot, **kwargs):
            calls.append(snapshot.key)
            return real(snapshot, **kwargs)

        with patch.object(verification_proof, "validate_source_snapshot", counting):
            _identity(self.project)

        self.assertEqual(
            len(calls), 1, "authority-bearing identity did not validate the snapshot"
        )

    def test_mutation_before_identity_build_is_detected(self) -> None:
        snapshot = source_snapshot.activate_source_snapshot_epoch(
            self.project, replace=True
        )
        # Mutate the sealed tree behind the registry entry.
        victim = next(snapshot.root.rglob("index.js"))
        # Model an adversary who first clears the sealed tree's write
        # protection. Detection must not depend on that protection holding.
        os.chmod(victim, os.stat(victim).st_mode | stat.S_IWRITE)
        victim.write_text("export const x = 666;\n", encoding="utf-8")
        with self.assertRaises(source_snapshot.SourceCaptureError):
            _identity(self.project)

    def test_explicit_source_key_is_still_honoured(self) -> None:
        """An already-validated key supplied by the caller stays authoritative."""
        source_snapshot.activate_source_snapshot_epoch(self.project, replace=True)
        identity = verification_proof.build_verification_proof_identity(
            self.project,
            assignment={"demo": "1.0.0"},
            remove_packages=(),
            manager="npm",
            manager_executable="npm",
            registry="https://registry.npmjs.org/",
            project_checks="none",
            commands=(),
            environment={},
            source_snapshot_key="explicit-validated-key",
        )
        self.assertEqual(identity.source_snapshot_key, "explicit-validated-key")


if __name__ == "__main__":
    unittest.main()
