from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import lockfile_consistency
from lockfile_consistency import LockfileConsistencyError
from project_topology import (
    ProjectTopologyError,
    resolve_project_topology,
)
from source_snapshot import SourceCaptureError, capture_source_snapshot

# BLOCK_Z_PROJECT_TOPOLOGY_V1


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    git = shutil.which("git")
    if not git:
        raise unittest.SkipTest("git unavailable")
    subprocess.run(
        [git, "-C", str(root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _init_git(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "block-z@example.invalid")
    _git(root, "config", "user.name", "Block Z")


def _npm_workspace_lock(
    root: Path,
    *,
    app_dependencies: dict[str, str] | None = None,
) -> None:
    app_dependencies = app_dependencies or {}
    packages: dict[str, object] = {
        "": {
            "name": "root",
            "private": True,
            "workspaces": ["packages/*"],
        },
        "packages/app": {
            "name": "app",
            "version": "1.0.0",
            **({"dependencies": app_dependencies} if app_dependencies else {}),
        },
        "packages/lib": {
            "name": "lib",
            "version": "1.0.0",
        },
    }
    if "demo" in app_dependencies:
        packages["node_modules/demo"] = {"version": "1.2.3"}
    _write_json(root / "package-lock.json", {
        "name": "root",
        "lockfileVersion": 3,
        "packages": packages,
    })


class BlockZProjectTopologyTests(unittest.TestCase):
    def test_root_npm_is_authoritatively_supported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _write_json(root / "package.json", {
                "name": "root",
                "packageManager": "npm@11.0.0",
            })
            _write_json(root / "package-lock.json", {
                "name": "root",
                "lockfileVersion": 3,
                "packages": {"": {"name": "root"}},
            })
            topology = resolve_project_topology(root, require_supported=True)
            self.assertEqual(root.resolve(), topology.package_root)
            self.assertEqual(root.resolve(), topology.package_manager_root)
            self.assertEqual("npm", topology.profile.family)

    def test_single_nested_package_is_discovered_without_guessing_multiple(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            app = repo / "frontend"
            _write_json(app / "package.json", {"name": "app"})
            _write_json(app / "package-lock.json", {
                "name": "app",
                "lockfileVersion": 3,
                "packages": {"": {"name": "app"}},
            })
            topology = resolve_project_topology(repo, require_supported=True)
            self.assertEqual(app.resolve(), topology.package_root)
            self.assertEqual(repo.resolve(), topology.source_root)

    def test_multiple_nested_packages_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            for name in ("a", "b"):
                _write_json(repo / name / "package.json", {"name": name})
                _write_json(repo / name / "package-lock.json", {
                    "lockfileVersion": 3,
                    "packages": {"": {"name": name}},
                })
            with self.assertRaisesRegex(
                ProjectTopologyError, "PROJECT_PACKAGE_ROOT_AMBIGUOUS"
            ):
                resolve_project_topology(repo)

    def test_npm_workspace_separates_target_and_package_manager_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "packages" / "app"
            lib = root / "packages" / "lib"
            _write_json(root / "package.json", {
                "name": "root",
                "private": True,
                "packageManager": "npm@11.0.0",
                "workspaces": ["packages/*"],
            })
            _write_json(app / "package.json", {"name": "app", "version": "1.0.0"})
            _write_json(lib / "package.json", {"name": "lib", "version": "1.0.0"})
            _npm_workspace_lock(root)
            _init_git(root)

            topology = resolve_project_topology(app, require_supported=True)
            self.assertEqual(app.resolve(), topology.package_root)
            self.assertEqual(root.resolve(), topology.package_manager_root)
            self.assertEqual(Path("packages/app"), topology.package_relative_to_manager)
            self.assertTrue(topology.is_workspace_package)

    def test_yarn_classic_workspace_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "packages" / "app"
            _write_json(root / "package.json", {
                "name": "root",
                "private": True,
                "packageManager": "yarn@1.22.22",
                "workspaces": ["packages/*"],
            })
            _write_json(app / "package.json", {
                "name": "app",
                "dependencies": {"demo": "^1.0.0"},
            })
            (root / "yarn.lock").write_text(
                '# yarn lockfile v1\n\n'
                'demo@^1.0.0:\n'
                '  version "1.2.3"\n'
                '  resolved "https://registry.example/demo/-/demo-1.2.3.tgz"\n',
                encoding="utf-8",
            )
            _init_git(root)
            topology = resolve_project_topology(app, require_supported=True)
            self.assertEqual("yarn-classic", topology.profile.family)
            self.assertEqual(root.resolve(), topology.package_manager_root)

    def test_yarn_berry_fails_closed_even_with_node_modules_linker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _write_json(root / "package.json", {
                "name": "root",
                "packageManager": "yarn@4.3.1",
            })
            (root / "yarn.lock").write_text(
                "__metadata:\n  version: 8\n", encoding="utf-8"
            )
            (root / ".yarnrc.yml").write_text(
                "nodeLinker: node-modules\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ProjectTopologyError, "PACKAGE_MANAGER_YARN_BERRY_UNSUPPORTED"
            ):
                resolve_project_topology(root, require_supported=True)

    def test_pnpm_fails_closed_until_importer_proof_is_implemented(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _write_json(root / "package.json", {
                "name": "root",
                "packageManager": "pnpm@10.0.0",
            })
            (root / "pnpm-lock.yaml").write_text(
                "lockfileVersion: '9.0'\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ProjectTopologyError, "PACKAGE_MANAGER_PNPM_UNSUPPORTED"
            ):
                resolve_project_topology(root, require_supported=True)

    def test_mixed_package_manager_lockfiles_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _write_json(root / "package.json", {"name": "root"})
            _write_json(root / "package-lock.json", {
                "lockfileVersion": 3,
                "packages": {"": {"name": "root"}},
            })
            (root / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ProjectTopologyError, "PROJECT_LOCKFILE_AMBIGUOUS"
            ):
                resolve_project_topology(root)

    def test_sibling_workspace_manifest_changes_topology_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "packages" / "app"
            lib = root / "packages" / "lib"
            _write_json(root / "package.json", {
                "name": "root",
                "private": True,
                "workspaces": ["packages/*"],
            })
            _write_json(app / "package.json", {"name": "app"})
            _write_json(lib / "package.json", {"name": "lib", "version": "1.0.0"})
            _npm_workspace_lock(root)
            _init_git(root)
            first = resolve_project_topology(app).key
            _write_json(lib / "package.json", {
                "name": "lib",
                "version": "1.0.1",
            })
            second = resolve_project_topology(app).key
            self.assertNotEqual(first, second)

    def test_nested_npm_lockfile_validation_uses_workspace_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "packages" / "app"
            lib = root / "packages" / "lib"
            _write_json(root / "package.json", {
                "name": "root",
                "private": True,
                "packageManager": "npm@11.0.0",
                "workspaces": ["packages/*"],
            })
            _write_json(app / "package.json", {
                "name": "app",
                "dependencies": {"demo": "^1.0.0"},
            })
            _write_json(lib / "package.json", {"name": "lib"})
            _npm_workspace_lock(root, app_dependencies={"demo": "^1.0.0"})
            _init_git(root)

            state = lockfile_consistency.ensure_lockfile_consistency(
                app,
                "",
                mode="validate",
            )
            self.assertTrue(state.valid)
            self.assertTrue(
                state.lockfile.samefile(root / "package-lock.json"),
                f"canonical lockfile alias mismatch: {state.lockfile!s} vs "
                f"{root / 'package-lock.json'!s}",
            )
            self.assertEqual(
                "1.2.3",
                lockfile_consistency.exact_package_lock_version(
                    state.lockfile,
                    "demo",
                    package_relative="packages/app",
                ),
            )

    def test_nested_yarn_uses_root_yarn_lock(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            app = root / "packages" / "app"
            _write_json(root / "package.json", {
                "name": "root",
                "private": True,
                "packageManager": "yarn@1.22.22",
                "workspaces": ["packages/*"],
            })
            _write_json(app / "package.json", {
                "name": "app",
                "dependencies": {"demo": "^1.0.0"},
            })
            (root / "yarn.lock").write_text(
                '# yarn lockfile v1\n\n'
                'demo@^1.0.0:\n  version "1.2.3"\n',
                encoding="utf-8",
            )
            _init_git(root)
            state = lockfile_consistency.ensure_lockfile_consistency(
                app, "", mode="validate"
            )
            self.assertTrue(state.valid)
            self.assertEqual((root / "yarn.lock").resolve(), state.lockfile.resolve())

    def test_lockfile_consistency_rejects_unsupported_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _write_json(root / "package.json", {
                "name": "root",
                "packageManager": "pnpm@10.0.0",
            })
            (root / "pnpm-lock.yaml").write_text(
                "lockfileVersion: '9.0'\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                LockfileConsistencyError, "PACKAGE_MANAGER_PNPM_UNSUPPORTED"
            ):
                lockfile_consistency.ensure_lockfile_consistency(
                    root, "", mode="validate"
                )

    def test_sibling_external_local_dependency_fails_source_capture(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root.parent / (root.name + "-outside")
            outside.mkdir(exist_ok=True)
            self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
            app = root / "packages" / "app"
            lib = root / "packages" / "lib"
            _write_json(root / "package.json", {
                "name": "root",
                "private": True,
                "workspaces": ["packages/*"],
            })
            _write_json(app / "package.json", {"name": "app"})
            relative = Path("..") / ".." / ".." / outside.name
            _write_json(lib / "package.json", {
                "name": "lib",
                "dependencies": {"outside": f"file:{relative.as_posix()}"},
            })
            _npm_workspace_lock(root)
            _init_git(root)
            _git(root, "add", ".")
            _git(root, "commit", "-q", "-m", "block-z source fixture")
            with self.assertRaisesRegex(
                SourceCaptureError,
                "SOURCE_EXTERNAL_LOCAL_DEPENDENCY_UNSUPPORTED",
            ):
                capture_source_snapshot(app)

    def test_invalid_assignment_semantics_precede_topology_even_with_cache(self) -> None:
        import baseline_constraint_verifier as verifier

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _write_json(root / "package.json", {
                "name": "root",
                "dependencies": {"demo": "1.0.0"},
            })
            result = verifier.verify_assignment(
                root,
                {"added": "2.0.0"},
                config=verifier.BaselineVerifyConfig(
                    project_checks="off",
                    proof_cache_dir=str(root / ".proof-cache"),
                ),
            )
            self.assertFalse(result.ok)
            self.assertEqual("unknown", result.kind)
            self.assertIn("ASSIGNMENT_PACKAGE_NOT_DECLARED: added", result.summary)

    def test_observed_resolution_sentinels_survive_assignment_refactors(self) -> None:
        import baseline_constraint_verifier as verifier

        self.assertEqual("<removed>", verifier.OBSERVED_REMOVED)
        self.assertEqual("<peer-only>", verifier.OBSERVED_PEER_ONLY)
        self.assertEqual(
            "<optional-not-installed>",
            verifier.OBSERVED_OPTIONAL_NOT_INSTALLED,
        )
        self.assertEqual(
            (
                "dependencies",
                "devDependencies",
                "optionalDependencies",
                "peerDependencies",
            ),
            verifier.DIRECT_DEPENDENCY_SECTIONS,
        )

    def test_lockfileless_topology_identity_is_unbound_not_authority(self) -> None:
        import project_topology

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _write_json(root / "package.json", {
                "name": "identity-only",
                "packageManager": "npm@11.0.0",
                "dependencies": {"demo": "^1.0.0"},
            })

            payload = project_topology.topology_identity_payload(root)
            self.assertEqual(
                "unbound-no-canonical-lockfile",
                payload["authorityState"],
            )
            self.assertFalse(
                payload["profile"]["authoritativeSupported"]
            )

            with self.assertRaisesRegex(
                ProjectTopologyError,
                "PROJECT_CANONICAL_LOCKFILE_MISSING",
            ):
                resolve_project_topology(
                    root,
                    require_supported=True,
                )

    def test_generator_missing_lockfile_keeps_package_json_fallback(self) -> None:
        import dependency_live_roadmap_generator as roadmap

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _write_json(root / "package.json", {
                "name": "dashboard-only",
                "dependencies": {"demo": "^1.0.0"},
            })
            _current, source = roadmap.resolved_current_version(
                root,
                "demo",
                "^1.0.0",
                "runtime",
                None,
            )
            self.assertEqual(
                "package.json; canonical lockfile unavailable",
                source,
            )

    def test_live_data_client_survives_resolved_version_refactors(self) -> None:
        import dependency_live_roadmap_generator as roadmap

        self.assertTrue(hasattr(roadmap, "LiveDataClient"))
        client = roadmap.LiveDataClient(
            "https://registry.example.invalid/npm",
            timeout=1,
            batch_size=10,
            sleep_sec=0,
        )
        self.assertTrue(callable(client.fetch_npm_metadata))
        self.assertTrue(callable(client.query_osv_versions))
        self.assertTrue(callable(client.fetch_release_intelligence))
        client.session.close()

    def test_explicit_manager_plus_foreign_lockfile_is_conflict_not_ambiguity(self) -> None:
        import lockfile_consistency as lc

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _write_json(root / "package.json", {
                "name": "root",
                "packageManager": "yarn@1.22.22",
                "dependencies": {"demo": "1.0.0"},
            })
            (root / "yarn.lock").write_text(
                'demo@1.0.0:\n  version "1.0.0"\n',
                encoding="utf-8",
            )
            _write_json(root / "package-lock.json", {
                "lockfileVersion": 3,
            })

            with self.assertRaises(lc.LockfileConsistencyError) as raised:
                lc.ensure_lockfile_consistency(
                    root,
                    "",
                    mode="validate",
                )

            self.assertEqual(
                "LOCKFILE_CONFLICT",
                raised.exception.code,
            )

    def test_generic_lockfileless_prepared_snapshot_needs_no_topology_without_junctions(self) -> None:
        import baseline_constraint_verifier as verifier

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = root / "prepared"
            project = prepared / "packages" / "app"
            _write_json(project / "package.json", {
                "name": "app",
                "version": "1.0.0",
            })

            plan = verifier._workspace_junction_rebase_plan(
                prepared,
                project,
            )
            self.assertEqual({}, plan)


if __name__ == "__main__":
    unittest.main()
