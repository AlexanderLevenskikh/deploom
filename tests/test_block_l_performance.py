from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier
import dependency_live_roadmap_generator as roadmap


class BlockLPerformanceTests(unittest.TestCase):
    def test_prepared_snapshot_consumers_are_fresh_and_sealed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared_root = root / "prepared"
            project = prepared_root / "packages" / "app"
            project.mkdir(parents=True)
            (project / "package.json").write_text('{"name":"app"}\n', encoding="utf-8")
            (project / "state.txt").write_text("sealed\n", encoding="utf-8")
            snapshot = verifier._publish_prepared_workspace_snapshot(
                prepared_root, project, key="a" * 32,
                observed_versions={"demo": "1.0.0"}, observed_hash="b" * 64,
                source_project=project,
            )
            first = verifier._materialize_prepared_workspace_snapshot(snapshot, root / "first")
            (first / "state.txt").write_text("mutated\n", encoding="utf-8")
            second = verifier._materialize_prepared_workspace_snapshot(snapshot, root / "second")
            self.assertEqual("sealed\n", (second / "state.txt").read_text(encoding="utf-8"))
            self.assertEqual("sealed\n", (snapshot.workspace_root / snapshot.project_relative / "state.txt").read_text(encoding="utf-8"))

    def test_snapshot_cache_is_scoped_by_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source = root / "source-a"
            second_source = root / "source-b"
            for source in (first_source, second_source):
                source.mkdir()
                (source / "package.json").write_text('{"name":"same"}\n', encoding="utf-8")
            snapshot = verifier._publish_prepared_workspace_snapshot(
                first_source, first_source, key="c" * 32,
                observed_versions={}, observed_hash="d" * 64,
                source_project=first_source,
            )
            self.assertIsNotNone(
                verifier._lookup_prepared_workspace_snapshot("c" * 32, first_source)
            )
            self.assertIsNone(
                verifier._lookup_prepared_workspace_snapshot("c" * 32, second_source)
            )

    def test_package_manager_cache_is_isolated_without_offline_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = verifier.BaselineVerifyConfig(proof_cache_dir=str(Path(tmp) / "baseline-proofs"))
            yarn = verifier._package_manager_cache_environment(config, "yarn")
            npm = verifier._package_manager_cache_environment(config, "npm")
            # Yarn Classic intentionally keeps its native warm user cache.
            # Cache location is not proof authority; the fresh isolated install is.
            self.assertEqual({}, yarn)
            self.assertIn("npm_config_cache", npm)
            rendered = " ".join([*npm.keys(), *npm.values()]).lower()
            self.assertNotIn("offline", rendered)
            self.assertNotIn("prefer-offline", rendered)

    def test_registry_prefetch_reduces_in_sorted_order(self) -> None:
        main = SimpleNamespace(
            registry="https://registry.example.test/npm", timeout=1,
            batch_size=10, sleep_sec=0, use_system_proxy=False, npm_cache={},
        )
        class Worker:
            def __init__(self, *_args, **_kwargs):
                pass
            def fetch_npm_metadata(self, name: str):
                if name == "a":
                    time.sleep(0.03)
                return {"name": name, "versions": {}}
        dependencies = [
            ("b", "dev", "^1.0.0"),
            ("local", "dev", "workspace:*"),
            ("a", "runtime", "^1.0.0"),
        ]
        with mock.patch.object(roadmap, "LiveDataClient", Worker):
            roadmap._prefetch_registry_metadata(main, dependencies, max_workers=2)
        self.assertEqual(["a", "b"], list(main.npm_cache))
        self.assertNotIn("local", main.npm_cache)

    def test_production_path_has_snapshot_reuse_and_fail_closed_transactional_isolation(self) -> None:
        source = Path(verifier.__file__).read_text(encoding="utf-8")

        # Snapshot reuse remains mandatory.
        self.assertIn("_lookup_prepared_workspace_snapshot(", source)
        self.assertIn("_publish_prepared_workspace_snapshot(", source)
        self.assertIn("verify.preparation.snapshot-hit", source)

        # Project checks reuse one fully-private trial only when a whole-workspace
        # watcher proves that the previous command produced no filesystem events.
        # Any event or watcher uncertainty discards the trial before the next check.
        self.assertIn('else temp_root / "project-check-transaction"', source)
        self.assertIn("for command_index, command in enumerate(config.commands, start=1):", source)
        self.assertIn('clone_isolation = "reused-private-trial"', source)
        self.assertIn("WorkspaceChangeGuard(command_root)", source)
        self.assertIn("verify.project-check.trial-reuse-ready", source)
        self.assertIn("verify.project-check.trial-reuse-rejected", source)
        self.assertIn("shutil.rmtree(command_root, ignore_errors=True)", source)

        # The production path still has proof-preserving isolation backends:
        # guarded NTFS zero-copy fast path plus safe full-copy fallback.
        self.assertIn('"ntfs-junction-guarded"', source)
        self.assertIn('"fresh-prepared-snapshot-clone"', source)
        self.assertIn("guarded_clone_is_active(command_root)", source)
        self.assertIn("try_materialize_guarded_clone(", source)
        self.assertIn("_copy_tree_snapshot(", source)

        # Fast path is not an unguarded shared writable tree.
        self.assertIn("stop_guarded_clone(command_root)", source)
        self.assertIn("verify.project-check.fastpath-rejected", source)
        self.assertIn("_disable_prepared_snapshot_fastpath(", source)
        self.assertIn("_allow_prepared_fastpath=False", source)
        self.assertIn("_evict_prepared_workspace_snapshot(", source)

        # The check always runs against the materialized command workspace,
        # never directly against the sealed prepared snapshot.
        self.assertIn("command_project = _materialize_prepared_workspace_snapshot(", source)
        self.assertIn("command_project,", source)

    def test_discovery_prefetch_precedes_sequential_reduction(self) -> None:
        source = Path(roadmap.__file__).read_text(encoding="utf-8")
        prefetch = source.index("_prefetch_registry_metadata(\n        client,")
        loop = source.index("for dependency_index, (name, kind, spec) in enumerate(dependencies, start=1):", prefetch)
        self.assertLess(prefetch, loop)
        self.assertIn("for name in names:\n        client.npm_cache[name] = results[name]", source)


if __name__ == "__main__":
    unittest.main()
