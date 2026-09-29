"""#2 bounded target discovery from the direct dependency set.

When begin gets NO roadmap targets (no dashboard-state, no targets file), it
derives targets from the registry ``dist-tags.latest`` of every direct managed
dependency — yellow lag policy WITHOUT the legacy generator/solver. A version
is never invented: only real registry metadata is accepted; a package with no
resolvable latest is skipped as evidence, never pinned. A provided targets
file ALWAYS wins (saved roadmap bootstraps the run).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iterative_migration import (
    SCHEMA_VERSION,
    build_run_config,
)


def _make_project(root: Path, dependencies: dict) -> Path:
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "package.json").write_text(
        json.dumps({"name": "discovery-probe", "private": True, "version": "1.0.0", "dependencies": dependencies}),
        encoding="utf-8",
    )
    return project


def _base_args(project_dir: str, **overrides) -> object:
    values = dict(
        run_dir=str(tempfile.mkdtemp(prefix="iter-discovery-")),
        project_dir=project_dir,
        project_name="discovery-probe",
        target_level="yellow",
        verify_config="",
        targets_file="",
        workspace_id="ws-test",
        project_id="discovery-probe",
        run_budget_minutes=None,
        max_repair_attempts=None,
        max_infra_retries=None,
        phase_timeout_seconds=None,
        dashboard_state="",
        requested_node="",
        lag_months=None,
        min_lag_ok_pct=None,
        max_known_high=None,
        tool_build_id="tool-test",
        run_id=None,
    )
    values.update(overrides)
    return type("Args", (), values)()


class IterativeTargetDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="iter-discovery-root-"))
        self.addCleanup(lambda: _rmtree(self._tmp))

    def test_empty_targets_discovers_registry_latest_for_lagged_deps(self) -> None:
        project = _make_project(
            self._tmp,
            {"is-number": "6.0.0", "is-finite": "1.0.0", "up-to-date": "2.0.0"},
        )

        def fake_latest(project_dir, name, runtime_env):
            return {"is-number": "7.0.0", "is-finite": "1.1.0", "up-to-date": "2.0.0"}[name]

        with mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest):
            config = build_run_config(self._tmp, _base_args(str(project)))

        self.assertEqual(config["targets"], {"is-number": "7.0.0", "is-finite": "1.1.0"})
        discovered = [e for e in config["targetDiscovery"] if e["status"] == "discovered"]
        self.assertEqual([e["package"] for e in discovered], ["is-finite", "is-number"])
        up_to_date = [e for e in config["targetDiscovery"] if e["status"] == "up-to-date"]
        self.assertEqual([e["package"] for e in up_to_date], ["up-to-date"])

    def test_registry_unavailable_package_is_skipped_never_pinned(self) -> None:
        project = _make_project(self._tmp, {"is-number": "6.0.0", "private-lib": "0.1.0"})

        def fake_latest(project_dir, name, runtime_env):
            return "7.0.0" if name == "is-number" else ""

        with mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest):
            config = build_run_config(self._tmp, _base_args(str(project)))

        self.assertEqual(config["targets"], {"is-number": "7.0.0"})
        unavailable = [e for e in config["targetDiscovery"] if e["status"] == "registry-unavailable"]
        self.assertEqual([e["package"] for e in unavailable], ["private-lib"])

    def test_explicit_targets_file_wins_over_discovery(self) -> None:
        project = _make_project(self._tmp, {"is-number": "6.0.0"})
        targets_file = self._tmp / "roadmap-targets.json"
        targets_file.write_text(json.dumps({"is-number": "6.9.9"}), encoding="utf-8")

        def fail_discovery(project_dir, current, runtime_env):
            self.fail("Discovery must not run when a targets file is provided")

        with mock.patch("iterative_migration._discover_targets", side_effect=fail_discovery):
            config = build_run_config(
                self._tmp,
                _base_args(str(project), targets_file=str(targets_file)),
            )

        self.assertEqual(config["targets"], {"is-number": "6.9.9"})
        self.assertEqual(config["targetDiscovery"], [])

    def test_up_to_date_project_produces_no_targets(self) -> None:
        project = _make_project(self._tmp, {"is-number": "7.0.0"})

        with mock.patch("iterative_migration._npm_latest_version", return_value="7.0.0"):
            config = build_run_config(self._tmp, _base_args(str(project)))

        self.assertEqual(config["targets"], {})
        self.assertEqual(config["targetDiscovery"], [{"package": "is-number", "declared": "7.0.0", "latest": "7.0.0", "status": "up-to-date"}])


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
