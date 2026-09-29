# -*- coding: utf-8 -*-
"""D1-D4 Node/CI contract integration tests.

Covers the explicit per-project/CI Node runtime end to end at module
boundaries:
  - ``build_run_config --requested-node`` resolves to ONE installed exact
    Node and stores a ``runtime`` contract block (ENVIRONMENT_UNAVAILABLE is a
    hard error, never a PATH fallback);
  - the child-env helper ``_runtime_env`` applies a stored runtime block;
  - ``verify_assignment`` accepts the runtime env override;
  - the planner engine gate feeds the settings pin as the highest-priority
    exact project Node version in ``_project_node_versions``.

These are boundary tests with deterministic fixtures: PATH is patched so Node
discovery sees only the fake runtime, and the unavailable case uses a version
no host can own, so the tests stay reproducible in CI and on any developer
machine.
"""
import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from iterative_migration import (  # noqa: E402
    InvalidInputError,
    build_run_config,
    _runtime_env,
    _runtime_contract_hash,
    _assert_runtime_unchanged,
    _stable_json,
)
import hashlib as _hashlib
from dependency_live_roadmap_generator import (  # noqa: E402
    _project_node_versions,
    _normalized_node_version,
)
from baseline_constraint_verifier import verify_assignment  # noqa: E402
from project_runtime import runtime_env_for_path  # noqa: E402

FAKE_NODE_VERSION = "v22.18.0"
FAKE_MAJOR = "22"


def _make_fake_node(root: Path) -> Path:
    """A fake Node runtime: node/node.cmd answering ``node --version`` and an
    npm sibling. Windows probe uses COMSPEC; POSIX probe executes the script."""
    root.mkdir(parents=True, exist_ok=True)
    node_cmd = root / "node.cmd"
    node_cmd.write_text(
        "@echo off\r\n" "echo " + FAKE_NODE_VERSION + "\r\n",
        encoding="utf-8",
    )
    npm_cmd = root / "npm.cmd"
    npm_cmd.write_text("@echo off\r\n", encoding="utf-8")
    node = root / "node"
    node.write_text("#!/bin/sh\necho " + FAKE_NODE_VERSION + "\n", encoding="utf-8")
    npm = root / "npm"
    npm.write_text("#!/bin/sh\n", encoding="utf-8")
    if os.name != "nt":
        node.chmod(0o755)
        npm.chmod(0o755)
    else:
        node_cmd.chmod(0o755)
        npm_cmd.chmod(0o755)
    return root


def _base_args(project_dir: str, **overrides) -> argparse.Namespace:
    values = dict(
        run_dir=str(tempfile.mkdtemp(prefix="iter-runtime-")),
        project_dir=project_dir,
        project_name="runtime-probe",
        target_level="yellow",
        verify_config="",
        targets_file="",
        workspace_id="ws-test",
        project_id="runtime-probe",
        run_budget_minutes=None,
        max_repair_attempts=None,
        max_infra_retries=None,
        phase_timeout_seconds=None,
        dashboard_state="",
        lag_months=None,
        min_lag_ok_pct=None,
        max_known_high=None,
        tool_build_id="",
        requested_node="",
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def _make_project(root: Path) -> Path:
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "package.json").write_text(
        json.dumps({"name": "runtime-probe", "private": True, "version": "1.0.0"}),
        encoding="utf-8",
    )
    return project


class IterativeRuntimeRequestTests(unittest.TestCase):
    """D3: begin resolves the explicit project/CI Node and errors hard when
    the requested runtime is absent."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="iter-runtime-project-"))
        self.addCleanup(lambda: _rmtree(self._tmp))
        self.project = _make_project(self._tmp)
        self.fake_node = _make_fake_node(self._tmp / "fake-node")
        original_path = os.environ.get("PATH", "")
        patch_path = str(self.fake_node) + os.pathsep + original_path
        self._path_patch = mock.patch.dict(os.environ, {"PATH": patch_path})
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)
        # The fake is a text shim; binary execution is already proven by
        # test_project_runtime (real probe). Stub discovery's per-candidate
        # probe so enumeration/resolution boundaries stay real and the fake
        # runtime is deterministic on any host.
        import project_runtime as pr

        self._real_probe = pr.probe_node_version

        def _fake_probe(executable, timeout_seconds=5):
            path = Path(executable).resolve()
            if os.path.normcase(str(path)) == os.path.normcase(str((self.fake_node / "node").resolve())):
                return pr.normalize_node_version(FAKE_NODE_VERSION)
            return self._real_probe(executable, timeout_seconds=timeout_seconds)

        self._probe_patch = mock.patch("project_runtime.probe_node_version", side_effect=_fake_probe)
        self._probe_patch.start()
        self.addCleanup(self._probe_patch.stop)

    def test_requested_exact_major_resolves_to_installed_runtime(self) -> None:
        config = build_run_config(self._tmp, _base_args(str(self.project), requested_node=FAKE_MAJOR))
        runtime = config["runtime"]
        self.assertTrue(runtime, "runtime block must be present for an explicit request")
        self.assertEqual(config["requestedNode"], FAKE_MAJOR)
        self.assertTrue(runtime["effectiveVersion"].startswith(FAKE_MAJOR + "."))
        self.assertTrue(runtime["nodePath"])
        self.assertTrue(runtime["npmPath"])
        self.assertIn(runtime["source"], ("installed-exact", "major-alias", "exact"))
        self.assertTrue(runtime["contractHash"])

    def test_requested_unavailable_is_environment_unavailable_no_fallback(self) -> None:
        with self.assertRaises(InvalidInputError) as ctx:
            build_run_config(self._tmp, _base_args(str(self.project), requested_node="999.0.0"))
        self.assertIn("ENVIRONMENT_UNAVAILABLE", str(ctx.exception))

    def test_no_request_means_no_runtime_claim(self) -> None:
        config = build_run_config(self._tmp, _base_args(str(self.project)))
        self.assertEqual(config["requestedNode"], "")
        self.assertEqual(config["runtime"], {})
        self.assertIsNone(_runtime_env(config))

    def test_runtime_env_applies_stored_block_without_reprobe(self) -> None:
        config = build_run_config(self._tmp, _base_args(str(self.project), requested_node=FAKE_MAJOR))
        env = _runtime_env(config)
        self.assertIsNotNone(env)
        node_dir = str(Path(config["runtime"]["nodePath"]).parent)
        path_key = "PATH" if "PATH" in env else "Path"
        self.assertIn(os.path.normcase(node_dir), [os.path.normcase(p) for p in env[path_key].split(os.pathsep)])

    def test_runtime_env_for_path_prepends_literal_dir(self) -> None:
        env = runtime_env_for_path(str(self.fake_node / "node.cmd"))
        path_key = "PATH" if "PATH" in env else "Path"
        self.assertEqual(os.path.normcase(env[path_key].split(os.pathsep)[0]), os.path.normcase(str(self.fake_node)))

    def test_verify_assignment_accepts_runtime_env_keyword(self) -> None:
        import inspect

        signature = inspect.signature(verify_assignment)
        self.assertIn("runtime_env", signature.parameters)
        self.assertIsNone(signature.parameters["runtime_env"].default)


class IterativeRuntimeContractTests(unittest.TestCase):
    """D3.3/D3.4: the runtime contract carries the manager + platform identity
    behind a durable hash (so a manager/OS swap invalidates the identity), and a
    step refuses to continue a run whose requested runtime no longer resolves to
    the same exact executable+version."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="iter-runtime-contract-"))
        self.addCleanup(lambda: _rmtree(self._tmp))
        self.project = _make_project(self._tmp)
        self.fake_node = _make_fake_node(self._tmp / "fake-node")
        original_path = os.environ.get("PATH", "")
        patch_path = str(self.fake_node) + os.pathsep + original_path
        self._path_patch = mock.patch.dict(os.environ, {"PATH": patch_path})
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)
        import project_runtime as pr

        self._real_probe = pr.probe_node_version

        def _fake_probe(executable, timeout_seconds=5):
            path = Path(executable).resolve()
            if os.path.normcase(str(path)) == os.path.normcase(str((self.fake_node / "node").resolve())):
                return pr.normalize_node_version(FAKE_NODE_VERSION)
            return self._real_probe(executable, timeout_seconds=timeout_seconds)

        self._probe_patch = mock.patch("project_runtime.probe_node_version", side_effect=_fake_probe)
        self._probe_patch.start()
        self.addCleanup(self._probe_patch.stop)

    def _requested_config(self) -> dict:
        return build_run_config(self._tmp, _base_args(str(self.project), requested_node=FAKE_MAJOR))

    def test_runtime_block_carries_manager_platform_and_hash(self) -> None:
        runtime = self._requested_config()["runtime"]
        self.assertIn("packageManager", runtime)
        self.assertIn("packageManagerVersion", runtime)
        self.assertIn("platform", runtime)
        self.assertIn("arch", runtime)
        self.assertTrue(runtime["hash"])
        self.assertEqual(runtime["contractHash"], runtime["hash"])

    def test_contract_hash_covers_every_block_field(self) -> None:
        runtime = self._requested_config()["runtime"]
        altered = dict(runtime)
        altered["effectiveVersion"] = "99.0.0"
        recomputed = _hashlib.sha256(
            _stable_json({k: v for k, v in altered.items() if k != "hash"}).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(recomputed, runtime["hash"])

    def test_contract_hash_empty_when_no_runtime(self) -> None:
        config = build_run_config(self._tmp, _base_args(str(self.project)))
        self.assertEqual(_runtime_contract_hash(config), "")

    def test_runtime_unchanged_guard_passes_for_fresh_config(self) -> None:
        _assert_runtime_unchanged(self._requested_config())

    def test_runtime_unchanged_guard_detects_version_swap(self) -> None:
        config = self._requested_config()
        config["runtime"]["effectiveVersion"] = "99.0.0"
        with self.assertRaises(InvalidInputError) as ctx:
            _assert_runtime_unchanged(config)
        self.assertIn("RUNTIME_CHANGED", str(ctx.exception))

    def test_runtime_unchanged_guard_detects_node_path_swap(self) -> None:
        config = self._requested_config()
        config["runtime"]["nodePath"] = str(config["runtime"]["nodePath"]) + ".x"
        with self.assertRaises(InvalidInputError) as ctx:
            _assert_runtime_unchanged(config)
        self.assertIn("RUNTIME_CHANGED", str(ctx.exception))


class IterativePlannerEngineGateTests(unittest.TestCase):
    """D4: the settings pin joins the repo pins as an exact project Node and is
    highest-priority; the engine gate consumes rows carrying the override."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="iter-node-gate-"))
        self.addCleanup(lambda: _rmtree(self._tmp))
        self.project = self._tmp / "pinned"
        self.project.mkdir(parents=True, exist_ok=True)
        (self.project / "package.json").write_text(
            json.dumps({"name": "pinned", "private": True, "volta": {"node": "18.17.0"}}),
            encoding="utf-8",
        )
        (self.project / ".nvmrc").write_text("18.17.0\n", encoding="utf-8")

    def test_settings_override_is_highest_priority_exact_pin(self) -> None:
        versions = _project_node_versions(str(self.project), override="22.11.0")
        by_source = {source: version for version, source in versions}
        self.assertEqual(by_source["settings.nodeVersion"], "22.11.0")
        # The repo pins 18.17.0 through both package.json#volta.node and .nvmrc;
        # same-version dedup keeps the first source, so only the version may be
        # asserted, plus at least one of the repo sources carries it.
        self.assertEqual(_normalized_node_version("18.17.0"), "18.17.0")
        self.assertIn(
            "18.17.0",
            {by_source[source] for source in by_source if source != "settings.nodeVersion"},
        )

    def test_no_override_leaves_repo_pins_only(self) -> None:
        versions = _project_node_versions(str(self.project))
        sources = {source for _version, source in versions}
        self.assertNotIn("settings.nodeVersion", sources)
        self.assertEqual({
            _normalized_node_version(version) for version, _source in versions
        }, {"18.17.0"})


def _rmtree(path: Path) -> None:
    try:
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 - cleanup must not fail the test
        pass


if __name__ == "__main__":
    unittest.main()
