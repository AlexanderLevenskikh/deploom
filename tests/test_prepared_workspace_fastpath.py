from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from prepared_workspace_fastpath import (
    _DirectoryWatcher,
    _classify_integrity_notification,
    build_dependency_integrity_manifest,
)


class PreparedWorkspaceFastPathTests(unittest.TestCase):
    def test_nested_native_binary_notification_is_verified_against_sealed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node_modules = root / "node_modules"
            binary = (
                node_modules
                / "vite"
                / "node_modules"
                / "@esbuild"
                / "win32-x64"
                / "esbuild.exe"
            )
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"sealed-esbuild")

            manifest = build_dependency_integrity_manifest(root)
            relative = "vite/node_modules/@esbuild/win32-x64/esbuild.exe"

            self.assertEqual(
                "notification-only",
                _classify_integrity_notification(
                    root,
                    node_modules,
                    _DirectoryWatcher.FILE_ACTION_MODIFIED,
                    relative,
                    manifest,
                ),
            )

            binary.write_bytes(b"mutated-esbuild")
            self.assertEqual(
                "mutation",
                _classify_integrity_notification(
                    root,
                    node_modules,
                    _DirectoryWatcher.FILE_ACTION_MODIFIED,
                    relative,
                    manifest,
                ),
            )

    def test_inventory_changes_are_always_authoritative_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node_modules = root / "node_modules"
            package = node_modules / "demo"
            package.mkdir(parents=True)
            (package / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
            manifest = build_dependency_integrity_manifest(root)

            self.assertEqual(
                "mutation",
                _classify_integrity_notification(
                    root,
                    node_modules,
                    _DirectoryWatcher.FILE_ACTION_ADDED,
                    "demo/generated.js",
                    manifest,
                ),
            )


if __name__ == "__main__":
    unittest.main()
