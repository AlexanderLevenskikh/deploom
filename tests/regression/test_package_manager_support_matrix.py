from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from package_manager_profile import resolve_package_manager_profile


class PackageManagerSupportMatrixTests(unittest.TestCase):
    def _profile(self, *, package_manager: str, lockfile: str, lock_text: str = ""):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        package = {"name": "fixture", "version": "1.0.0"}
        if package_manager:
            package["packageManager"] = package_manager
        (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
        lock = root / lockfile
        lock.write_text(lock_text, encoding="utf-8")
        return resolve_package_manager_profile(root, package_json=package, lockfile=lock)

    def test_release_support_matrix_matches_profile_authority(self) -> None:
        npm = self._profile(package_manager="npm@10.0.0", lockfile="package-lock.json", lock_text='{"lockfileVersion":3}')
        yarn1 = self._profile(package_manager="yarn@1.22.22", lockfile="yarn.lock", lock_text="# yarn lockfile v1\n")
        berry = self._profile(package_manager="yarn@4.5.0", lockfile="yarn.lock", lock_text="__metadata:\n  version: 8\n")
        pnpm = self._profile(package_manager="pnpm@9.0.0", lockfile="pnpm-lock.yaml", lock_text="lockfileVersion: '9.0'\n")

        self.assertTrue(npm.authoritative_supported)
        self.assertTrue(yarn1.authoritative_supported)
        self.assertFalse(berry.authoritative_supported)
        self.assertEqual("PACKAGE_MANAGER_YARN_BERRY_UNSUPPORTED", berry.unsupported_code)
        self.assertFalse(pnpm.authoritative_supported)
        self.assertEqual("PACKAGE_MANAGER_PNPM_UNSUPPORTED", pnpm.unsupported_code)

    def test_readme_documents_the_same_release_boundary(self) -> None:
        text = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
        self.assertIn("Yarn Classic 1.x", text)
        self.assertIn("PACKAGE_MANAGER_YARN_BERRY_UNSUPPORTED", text)
        self.assertIn("PACKAGE_MANAGER_PNPM_UNSUPPORTED", text)
        self.assertIn("Typed unsupported", text)


if __name__ == "__main__":
    unittest.main()
