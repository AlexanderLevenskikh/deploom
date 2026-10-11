"""C4 acceptance: dependency topology and full-copy materialization.

The reviewer asked for first-party root AND monorepo fixtures (the existing Ω
tests only built one tiny root-style tree). These tests build a real root
project and a real monorepo (hoisted root node_modules + per-package
node_modules + nested node_modules and a scoped package), then verify
dependency-root discovery, the sealed integrity manifest, and a full
proof-safe private copy of the published snapshot on both topologies.

All tests are cross-platform: they exercise the topology and the robust
full-copy path. The Windows-only guarded-clone fast path is measured in
test_prepared_workspace_cold_warm.py.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier
from prepared_workspace_fastpath import (
    _dependency_roots,
    build_dependency_integrity_manifest,
    dependency_root_manifest,
)

KEY = "e" * 32
NESTED_BINARY = b"\x7fELF\x02\x01\x01\x00sealed-esbuild-win32-x64"


def _write(tree: Path, relative: str, text: bytes | str) -> Path:
    """Write one fixture file under {tree}/{relative}."""
    path = tree.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, str):
        path.write_text(text, encoding="utf-8")
    else:
        path.write_bytes(text)
    return path


def _norm(relative: str) -> str:
    """Mirror prepared_workspace_fastpath._integrity_key normalization."""
    return os.path.normcase(relative.replace("/", os.sep)).replace("\\", "/")


def _root_fixture(prepared: Path) -> None:
    """Ordinary single-application project: one root node_modules."""
    _write(prepared, "package.json", '{"name":"root-app","private":true}\n')
    _write(prepared, "source.txt", "sealed-source\n")
    _write(prepared, "node_modules/demo/package.json", '{"name":"demo","version":"1.0.0"}\n')
    _write(prepared, "node_modules/demo/index.js", "module.exports = 1;\n")
    _write(
        prepared,
        "node_modules/vite/node_modules/@esbuild/win32-x64/esbuild.exe",
        NESTED_BINARY,
    )
    _write(prepared, "node_modules/.bin/vite", "#! /usr/bin/env node\n")


def _monorepo_fixture(prepared: Path) -> None:
    """Yarn-workspaces monorepo: hoisted root + per-package node_modules."""
    _write(
        prepared,
        "package.json",
        '{"name":"mono","private":true,"workspaces":["packages/*"]}\n',
    )
    _write(prepared, "yarn.lock", "# yarn lockfile v1\nresolved-root:\n  version 1.0.0\n")
    _write(prepared, "node_modules/hoisted/package.json", '{"name":"hoisted","version":"1.0.0"}\n')
    _write(prepared, "packages/alpha/package.json", '{"name":"alpha","version":"0.1.0"}\n')
    _write(prepared, "packages/alpha/index.js", "module.exports = 'alpha';\n")
    _write(
        prepared,
        "packages/alpha/node_modules/alpha-local/package.json",
        '{"name":"alpha-local","version":"1.0.0"}\n',
    )
    _write(prepared, "packages/beta/package.json", '{"name":"beta","version":"0.1.0"}\n')
    _write(
        prepared,
        "packages/beta/node_modules/beta-local/package.json",
        '{"name":"beta-local","version":"1.0.0"}\n',
    )
    _write(
        prepared,
        "packages/beta/node_modules/esbuild/node_modules/@esbuild/win32-x64/esbuild.exe",
        NESTED_BINARY,
    )


def _count_files(path: Path) -> int:
    total = 0
    for _current, _directories, names in os.walk(path):
        total += len(names)
    return total


class PreparedWorkspaceRootFixtureTests(unittest.TestCase):
    def _snapshot(self, prepared: Path, *, dependency_roots: tuple[str, ...]):
        return verifier.PreparedWorkspaceSnapshot(
            key=KEY,
            workspace_root=prepared,
            project_relative=Path("."),
            source_project=prepared,
            storage_mode="test",
            observed_resolved_versions={},
            observed_resolved_hash="b" * 64,
            dependency_roots=dependency_roots,
        )

    def test_root_fixture_dependency_topology_and_sealed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prepared = Path(tmp) / "prepared"
            _root_fixture(prepared)

            self.assertEqual(("node_modules",), dependency_root_manifest(prepared))
            self.assertEqual(
                [(prepared / "node_modules").resolve()],
                _dependency_roots(prepared),
            )

            manifest = build_dependency_integrity_manifest(prepared)
            expected = {
                _norm("node_modules/demo/package.json"),
                _norm("node_modules/demo/index.js"),
                _norm("node_modules/vite/node_modules/@esbuild/win32-x64/esbuild.exe"),
                _norm("node_modules/.bin/vite"),
            }
            self.assertEqual(expected, set(manifest))
            # Source/config files are not dependency payload and are never sealed.
            for escaped in ("package.json", "source.txt"):
                self.assertNotIn(_norm(escaped), set(manifest))

    def test_root_fixture_mutation_changes_only_the_mutated_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prepared = Path(tmp) / "prepared"
            _root_fixture(prepared)
            before = build_dependency_integrity_manifest(prepared)
            _write(
                prepared,
                "node_modules/demo/index.js",
                b"module.exports = 42;\n",
            )
            after = build_dependency_integrity_manifest(prepared)
            changed = {
                key for key in before if before[key] != after.get(key)
            }
            self.assertEqual(
                {_norm("node_modules/demo/index.js")}, changed,
            )

    def test_root_fixture_full_copy_materializes_every_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = root / "prepared"
            _root_fixture(prepared)
            fixture_files = _count_files(prepared)

            snapshot = self._snapshot(prepared, dependency_roots=("node_modules",))
            target = verifier._materialize_prepared_workspace_snapshot(
                snapshot, root / "cold", allow_fastpath=False,
            )
            self.assertTrue(target.is_dir())
            self.assertEqual(
                "sealed-source\n",
                (target / "source.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "module.exports = 1;\n",
                (target / "node_modules" / "demo" / "index.js").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                NESTED_BINARY,
                (
                    target
                    / "node_modules"
                    / "vite"
                    / "node_modules"
                    / "@esbuild"
                    / "win32-x64"
                    / "esbuild.exe"
                ).read_bytes(),
            )
            self.assertEqual(
                fixture_files,
                _count_files(target),
                "full copy must materialize every source and dependency file",
            )


class PreparedWorkspaceMonorepoFixtureTests(unittest.TestCase):
    def test_monorepo_dependency_topology_discovers_every_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prepared = Path(tmp) / "prepared"
            _monorepo_fixture(prepared)
            self.assertEqual(
                (
                    "node_modules",
                    "packages/alpha/node_modules",
                    "packages/beta/node_modules",
                ),
                dependency_root_manifest(prepared),
            )
            roots = {str(path.resolve()) for path in _dependency_roots(prepared)}
            self.assertEqual(
                {
                    str((prepared / "node_modules").resolve()),
                    str((prepared / "packages" / "alpha" / "node_modules").resolve()),
                    str((prepared / "packages" / "beta" / "node_modules").resolve()),
                },
                roots,
            )

    def test_monorepo_sealed_manifest_covers_only_payload_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prepared = Path(tmp) / "prepared"
            _monorepo_fixture(prepared)
            manifest = build_dependency_integrity_manifest(prepared)
            expected = {
                _norm("node_modules/hoisted/package.json"),
                _norm("packages/alpha/node_modules/alpha-local/package.json"),
                _norm("packages/beta/node_modules/beta-local/package.json"),
                _norm(
                    "packages/beta/node_modules/esbuild/"
                    "node_modules/@esbuild/win32-x64/esbuild.exe"
                ),
            }
            self.assertEqual(expected, set(manifest))
            # Source/config and workspace-package sources are never sealed.
            for escaped in (
                "package.json",
                "yarn.lock",
                "packages/alpha/package.json",
                "packages/alpha/index.js",
            ):
                self.assertNotIn(_norm(escaped), set(manifest))

    def test_monorepo_full_copy_preserves_nested_and_per_package_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = root / "prepared"
            _monorepo_fixture(prepared)
            fixture_files = _count_files(prepared)

            snapshot = verifier.PreparedWorkspaceSnapshot(
                key=KEY,
                workspace_root=prepared,
                project_relative=Path("packages/alpha"),
                source_project=prepared,
                storage_mode="test",
                observed_resolved_versions={},
                observed_resolved_hash="b" * 64,
                dependency_roots=(
                    "node_modules",
                    "packages/alpha/node_modules",
                    "packages/beta/node_modules",
                ),
            )
            project = verifier._materialize_prepared_workspace_snapshot(
                snapshot, root / "cold", allow_fastpath=False,
            )
            self.assertTrue(project.is_dir())
            self.assertEqual(project, root / "cold" / "packages" / "alpha")
            self.assertEqual("module.exports = 'alpha';\n",
                             (project / "index.js").read_text(encoding="utf-8"))
            self.assertEqual(
                '{"name":"alpha-local","version":"1.0.0"}\n',
                (
                    project / "node_modules" / "alpha-local" / "package.json"
                ).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                '{"name":"hoisted","version":"1.0.0"}\n',
                (
                    project.parent.parent / "node_modules" / "hoisted" / "package.json"
                ).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                NESTED_BINARY,
                (
                    root
                    / "cold"
                    / "packages"
                    / "beta"
                    / "node_modules"
                    / "esbuild"
                    / "node_modules"
                    / "@esbuild"
                    / "win32-x64"
                    / "esbuild.exe"
                ).read_bytes(),
            )
            self.assertEqual(
                fixture_files,
                _count_files(root / "cold"),
                "full copy must materialize every source and dependency file",
            )


if __name__ == "__main__":
    unittest.main()
