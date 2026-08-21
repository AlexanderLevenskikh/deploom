from __future__ import annotations

# BLOCK_W_P0_P1_TYPES_NESTED_FIX_V1

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

import dependency_live_roadmap_generator as roadmap


def npm_tarball(package_json: dict, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        manifest = json.dumps(package_json).encode("utf-8")
        info = tarfile.TarInfo("package/package.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        for relative, payload in files.items():
            file_info = tarfile.TarInfo("package/" + relative.lstrip("/"))
            file_info.size = len(payload)
            archive.addfile(file_info, io.BytesIO(payload))
    return buffer.getvalue()


class TypeProviderTarballProofTests(unittest.TestCase):
    def test_uuid11_shape_infers_types_from_main_and_exports_entrypoints(self) -> None:
        data = npm_tarball(
            {
                "name": "uuid",
                "version": "11.1.1",
                "type": "module",
                "main": "./dist/cjs/index.js",
                "module": "./dist/esm/index.js",
                "exports": {
                    ".": {
                        "node": {
                            "import": "./dist/esm/index.js",
                            "require": "./dist/cjs/index.js",
                        },
                        "browser": {
                            "import": "./dist/esm-browser/index.js",
                            "require": "./dist/cjs-browser/index.js",
                        },
                        "default": "./dist/esm-browser/index.js",
                    }
                },
            },
            {
                "dist/cjs/index.js": b"exports.v4 = function () {};",
                "dist/cjs/index.d.ts": b"export declare function v4(): string;",
                "dist/esm/index.js": b"export function v4() {}",
                "dist/esm/index.d.ts": b"export declare function v4(): string;",
            },
        )
        self.assertTrue(roadmap.registry_tarball_provides_own_types(data))

    def test_unrelated_dts_does_not_count_as_self_types(self) -> None:
        data = npm_tarball(
            {"name": "plain-js", "version": "1.0.0", "main": "./dist/index.js"},
            {
                "dist/index.js": b"module.exports = {};",
                "dist/internal-only.d.ts": b"export {};",
            },
        )
        self.assertFalse(roadmap.registry_tarball_provides_own_types(data))

    def test_explicit_types_remain_supported(self) -> None:
        data = npm_tarball(
            {"name": "typed", "version": "1.0.0", "types": "./types/index.d.ts"},
            {"types/index.d.ts": b"export {};"},
        )
        self.assertTrue(roadmap.registry_tarball_provides_own_types(data))


class NestedPackageResolutionTests(unittest.TestCase):
    def test_repository_with_one_nested_package_is_auto_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "frontend"
            package.mkdir()
            (package / "package.json").write_text('{"private":true}', encoding="utf-8")
            self.assertEqual(package.resolve(), roadmap.resolve_project_package_path(root))

    def test_direct_package_root_wins_without_descendant_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"private":true}', encoding="utf-8")
            child = root / "examples" / "demo"
            child.mkdir(parents=True)
            (child / "package.json").write_text('{"private":true}', encoding="utf-8")
            self.assertEqual(root.resolve(), roadmap.resolve_project_package_path(root))

    def test_multiple_nested_packages_are_explicitly_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("frontend", "admin"):
                package = root / name
                package.mkdir()
                (package / "package.json").write_text('{"private":true}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PROJECT_PACKAGE_ROOT_AMBIGUOUS"):
                roadmap.resolve_project_package_path(root)


class DesktopNestedProjectContractTests(unittest.TestCase):
    def test_desktop_has_nested_package_and_git_tree_path_contract(self) -> None:
        source = (Path(__file__).resolve().parents[2] / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
        self.assertIn("resolveProjectPackageDirectory", source)
        self.assertIn("resolveProjectGitLayout", source)
        self.assertIn("projectTreePath(packageRelativePath, 'package.json')", source)
        self.assertIn("projectPathInWorktree", source)
        self.assertIn("path: projectPathInWorktree(existing, packageRelativePath)", source)
        self.assertIn("path: projectPathInWorktree(worktreePath, packageRelativePath)", source)
        self.assertIn("path: projectPathInWorktree(existingWorktree, packageRelativePath)", source)
        self.assertIn("'rev-parse', '--is-inside-work-tree'", source)

    def test_baseline_prepare_error_is_visible_instead_of_dead_click(self) -> None:
        source = (Path(__file__).resolve().parents[2] / "desktop" / "src" / "components" / "FlowWorkspace.tsx").read_text(encoding="utf-8")
        self.assertIn("window.alert(error instanceof Error ? error.message : String(error))", source)


if __name__ == "__main__":
    unittest.main()
