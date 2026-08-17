from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import prepared_workspace_fastpath as fastpath


class ParallelIntegrityManifestTests(unittest.TestCase):
    def _fixture(self, root: Path, *, count: int = 48) -> None:
        node_modules = root / "node_modules"
        for index in range(count):
            package = node_modules / f"pkg-{index:03d}"
            package.mkdir(parents=True, exist_ok=True)
            (package / "index.js").write_bytes(
                (f"module.exports={index};\\n".encode("utf-8")) * (1 + index % 5)
            )
        nested = node_modules / "vite" / "node_modules" / "@esbuild" / "win32-x64"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "esbuild.exe").write_bytes(b"sealed-native-binary")

    def test_parallel_and_single_worker_manifests_are_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            with mock.patch.dict(os.environ, {"DEPLOOM_INTEGRITY_HASH_WORKERS": "1"}):
                serial = fastpath.build_dependency_integrity_manifest(root)
            with mock.patch.dict(os.environ, {"DEPLOOM_INTEGRITY_HASH_WORKERS": "8"}):
                parallel = fastpath.build_dependency_integrity_manifest(root)
        self.assertEqual(serial, parallel)
        self.assertEqual(list(parallel), sorted(parallel))
        self.assertTrue(parallel)

    def test_manifest_still_hashes_exact_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "node_modules" / "demo"
            package.mkdir(parents=True)
            payload = b"proof-strength-must-not-change"
            (package / "index.js").write_bytes(payload)
            with mock.patch.dict(os.environ, {"DEPLOOM_INTEGRITY_HASH_WORKERS": "4"}):
                manifest = fastpath.build_dependency_integrity_manifest(root)
        self.assertEqual(
            "file:" + hashlib.sha256(payload).hexdigest(),
            manifest["node_modules/demo/index.js"],
        )

    def test_hash_failure_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root, count=1)
            with mock.patch.object(
                fastpath,
                "_fingerprint_path_with_size",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "PREPARED_DEPENDENCY_INTEGRITY_CAPTURE_FAILED",
                ):
                    fastpath.build_dependency_integrity_manifest(root)

    def test_worker_override_is_bounded(self) -> None:
        with mock.patch.dict(os.environ, {"DEPLOOM_INTEGRITY_HASH_WORKERS": "999"}):
            self.assertEqual(32, fastpath._integrity_hash_workers())
        with mock.patch.dict(os.environ, {"DEPLOOM_INTEGRITY_HASH_WORKERS": "0"}):
            self.assertEqual(1, fastpath._integrity_hash_workers())


if __name__ == "__main__":
    unittest.main()
