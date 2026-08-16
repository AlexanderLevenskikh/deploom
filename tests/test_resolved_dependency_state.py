from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resolved_dependency_state import (
    ResolvedDependencyStateError,
    assert_resolved_dependency_state,
    capture_resolved_dependency_state,
    load_resolved_dependency_state,
    resolved_state_metadata,
    restore_resolved_dependency_state,
)


class ResolvedDependencyStateTests(unittest.TestCase):
    def test_capture_persist_restore_and_detect_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            cache = root / "cache"
            project.mkdir()
            (project / "package.json").write_text(
                json.dumps({"packageManager": "yarn@1.22.22", "dependencies": {"a": "1.0.0"}}),
                encoding="utf-8",
            )
            original = b'a@1.0.0:\n  version "1.0.0"\n'
            (project / "yarn.lock").write_bytes(original)
            observed_hash = hashlib.sha256(b"observed").hexdigest()
            state = capture_resolved_dependency_state(
                project,
                manager="yarn",
                resolver_input_key="a" * 32,
                observed_resolved_hash=observed_hash,
                proof_cache_dir=cache,
            )
            self.assertEqual(hashlib.sha256(original).hexdigest(), state.lockfile_hash)
            loaded = load_resolved_dependency_state(
                resolved_state_metadata(state),
                proof_cache_dir=cache,
            )
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(state.key, loaded.key)

            (project / "yarn.lock").write_text("drift\n", encoding="utf-8")
            with self.assertRaisesRegex(ResolvedDependencyStateError, "DRIFT"):
                assert_resolved_dependency_state(project, loaded)
            restore_resolved_dependency_state(project, loaded)
            self.assertEqual(original, (project / "yarn.lock").read_bytes())
            assert_resolved_dependency_state(project, loaded)

    def test_tampered_content_addressed_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            cache = root / "cache"
            project.mkdir()
            (project / "package.json").write_text('{"packageManager":"npm@11.0.0"}\n', encoding="utf-8")
            (project / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
            state = capture_resolved_dependency_state(
                project,
                manager="npm",
                resolver_input_key="b" * 32,
                observed_resolved_hash="c" * 64,
                proof_cache_dir=cache,
            )
            artifact = cache / state.artifact_relative_path
            artifact.write_text("tampered\n", encoding="utf-8")
            self.assertIsNone(load_resolved_dependency_state(
                resolved_state_metadata(state),
                proof_cache_dir=cache,
            ))


if __name__ == "__main__":
    unittest.main()
