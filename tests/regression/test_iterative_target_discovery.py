"""#2 bounded target discovery from the direct dependency set.

When begin gets NO roadmap targets (no dashboard-state, no targets file), it
derives targets from the registry ``dist-tags.latest`` of every direct managed
dependency — yellow lag policy WITHOUT the legacy generator/solver. A version
is never invented: only real registry metadata is accepted; a package with no
resolvable latest is skipped as evidence, never pinned. A provided targets
file ALWAYS wins (saved roadmap bootstraps the run).

Postfix round (postfix review POSTFIX_2026-09-30): the engine-aware bounded
search must NEVER downgrade — a candidate is proposed only when npm-semver
proves it strictly newer than the declared floor (exact/equal/older and
prerelease cases covered); when no strictly-newer verified-compatible version
exists the CURRENT version is kept and the unfulfilled intent is recorded
(status ``no-newer-compatible``), and plan-next then has no actionable target
for it instead of a false completion. Discovery probes additionally run under
the SELECTED runtime env (node dir prepended to PATH), not the ambient PATH.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iterative_migration import (
    SCHEMA_VERSION,
    _plan_next_locked,
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

    # P1.2: with a pinned Node, a registry-known-incompatible latest must NOT
    # silently become a deferred target — a bounded search picks the highest
    # VERIFIED-compatible published version instead.
    def _args_with_node(self, project_dir: str, node_version: str) -> object:
        return _base_args(str(project_dir), requested_node=node_version)

    def _patch_node_resolution(self, effective_version: str):
        from project_runtime import NodeRuntimeResolution

        def fake_resolve(requested: str) -> NodeRuntimeResolution:
            return NodeRuntimeResolution(
                requested=requested,
                effective_version=effective_version,
                node_path="C:/fake/node.exe",
                npm_path="C:/fake/npm.exe",
                source="requested",
            )

        return mock.patch("project_runtime.resolve_requested_node", side_effect=fake_resolve)

    def test_pinned_node_prefers_highest_compatible_over_incompatible_latest(self) -> None:
        project = _make_project(self._tmp, {"is-number": "6.0.0"})

        def fake_latest(project_dir, name, runtime_env):
            return "9.0.0"

        def fake_versions(project_dir, name, runtime_env, top=12):
            return ["9.0.0", "8.0.0", "7.0.0", "6.0.0"]

        def fake_engines(project_dir, name, version, runtime_env):
            return {"9.0.0": ">=22", "8.0.0": ">=22", "7.0.0": ">=18", "6.0.0": ">=12"}[version]

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest), \
             mock.patch("iterative_migration._npm_versions", side_effect=fake_versions), \
             mock.patch("iterative_migration._npm_engines_node", side_effect=fake_engines):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        # 9.0.0/8.0.0 require Node 22+; 7.0.0 is the highest compatible -> target.
        self.assertEqual(config["targets"], {"is-number": "7.0.0"})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "discovered-compatible")
        self.assertEqual(entry["target"], "7.0.0")
        self.assertEqual(entry["latest"], "9.0.0")

    def test_pinned_node_keeps_current_version_when_no_newer_compatible(self) -> None:
        project = _make_project(self._tmp, {"is-number": "6.0.0"})

        def fake_latest(project_dir, name, runtime_env):
            return "9.0.0"

        def fake_versions(project_dir, name, runtime_env, top=12):
            return ["9.0.0", "8.0.0", "7.0.0", "6.0.0"]

        def fake_engines(project_dir, name, version, runtime_env):
            return {"9.0.0": ">=22", "8.0.0": ">=22", "7.0.0": ">=22", "6.0.0": ">=22"}[version]

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest), \
             mock.patch("iterative_migration._npm_versions", side_effect=fake_versions), \
             mock.patch("iterative_migration._npm_engines_node", side_effect=fake_engines):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        # Postfix P1: no strictly-newer verified-compatible version exists, so
        # the CURRENT version is KEPT — the package is not a target at all, not
        # latest and not a lower version. The unfulfilled intent is recorded.
        self.assertEqual(config["targets"], {})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "no-newer-compatible")
        self.assertEqual(entry["declared"], "6.0.0")
        self.assertEqual(entry["latest"], "9.0.0")
        self.assertIn("keeping the current version", entry["reason"])
        self.assertTrue(any(rejected["version"] == "8.0.0" for rejected in entry["rejected"]))

    def test_pinned_node_never_downgrades_below_declared_exact_version(self) -> None:
        # Postfix-review repro: latest 9.0.0 requires Node 22+, current is an
        # EXACT 8.0.0, 8.0.0 has unknown engines and 7.0.0 would be compatible.
        # The old code picked 7.0.0 (a DOWNGRADE); the fix must keep 8.0.0.
        project = _make_project(self._tmp, {"sample": "8.0.0"})

        def fake_latest(project_dir, name, runtime_env):
            return "9.0.0"

        def fake_versions(project_dir, name, runtime_env, top=12):
            return ["9.0.0", "8.0.0", "7.0.0", "6.0.0"]

        def fake_engines(project_dir, name, version, runtime_env):
            return {
                "9.0.0": ">=22", "8.0.0": "", "7.0.0": ">=18", "6.0.0": ">=18",
            }[version]

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest), \
             mock.patch("iterative_migration._npm_versions", side_effect=fake_versions), \
             mock.patch("iterative_migration._npm_engines_node", side_effect=fake_engines):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        # 7.0.0/8.0.0 are NOT newer than the declared exact 8.0.0 -> refused
        # (6.0.0 too); nothing compatible-but-older may ever be chosen.
        self.assertEqual(config["targets"], {})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "no-newer-compatible")
        not_newer = {
            row["version"] for row in entry["rejected"]
            if row.get("status") == "not-newer-than-current"
        }
        self.assertTrue(
            not_newer.issuperset({"6.0.0", "7.0.0", "8.0.0"}),
            f"all non-newer candidates must be refused: {sorted(not_newer)}",
        )
        self.assertFalse(any(row["version"] == "7.0.0" for row in entry["rejected"] if row.get("status") != "not-newer-than-current"))

    def test_pinned_node_chooses_newer_within_declared_range(self) -> None:
        # A declared RANGE keeps a floor (^8.0.0 -> 8.0.0): 8.0.2 is an
        # upgrade within the range and is chosen; nothing below the floor is.
        project = _make_project(self._tmp, {"sample": "^8.0.0"})

        def fake_latest(project_dir, name, runtime_env):
            return "9.0.0"

        def fake_versions(project_dir, name, runtime_env, top=12):
            return ["9.0.0", "8.0.2", "8.0.0", "7.0.0"]

        def fake_engines(project_dir, name, version, runtime_env):
            return {
                "9.0.0": ">=22", "8.0.2": ">=18", "8.0.0": ">=18", "7.0.0": ">=18",
            }[version]

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest), \
             mock.patch("iterative_migration._npm_versions", side_effect=fake_versions), \
             mock.patch("iterative_migration._npm_engines_node", side_effect=fake_engines):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        self.assertEqual(config["targets"], {"sample": "8.0.2"})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "discovered-compatible")
        self.assertEqual(entry["target"], "8.0.2")

    def test_pinned_node_never_downgrades_below_declared_range_floor(self) -> None:
        # Everything available that is compatible is at or BELOW the declared
        # floor (^8.0.0): 8.0.0 equals it, 7.0.0 is below it — both refused as
        # not-newer, so the current version is kept with an honest reason.
        project = _make_project(self._tmp, {"sample": "^8.0.0"})

        def fake_latest(project_dir, name, runtime_env):
            return "9.0.0"

        def fake_versions(project_dir, name, runtime_env, top=12):
            return ["9.0.0", "8.0.0", "7.0.0"]

        def fake_engines(project_dir, name, version, runtime_env):
            return ">=18" if version in ("8.0.0", "7.0.0") else ">=22"

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest), \
             mock.patch("iterative_migration._npm_versions", side_effect=fake_versions), \
             mock.patch("iterative_migration._npm_engines_node", side_effect=fake_engines):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        self.assertEqual(config["targets"], {})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "no-newer-compatible")
        not_newer = {
            row["version"] for row in entry["rejected"]
            if row.get("status") == "not-newer-than-current"
        }
        self.assertEqual(not_newer, {"7.0.0", "8.0.0"})

    def test_prerelease_candidates_are_ordered_by_npm_semver_not_sort_key(self) -> None:
        # A prerelease of a HIGHER version (9.0.0-beta.1) is strictly newer than
        # a stable 8.0.0 per npm semver and may be chosen; lower/equal
        # candidates are refused, never proposed.
        project = _make_project(self._tmp, {"sample": "8.0.0"})

        def fake_latest(project_dir, name, runtime_env):
            return "9.0.0"

        def fake_versions(project_dir, name, runtime_env, top=12):
            return ["9.0.0", "9.0.0-beta.1", "8.0.0", "8.0.0-beta.1", "7.0.0"]

        def fake_engines(project_dir, name, version, runtime_env):
            return {
                "9.0.0": ">=22", "9.0.0-beta.1": ">=18", "8.0.0": ">=18",
                "8.0.0-beta.1": ">=18", "7.0.0": ">=18",
            }[version]

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest), \
             mock.patch("iterative_migration._npm_versions", side_effect=fake_versions), \
             mock.patch("iterative_migration._npm_engines_node", side_effect=fake_engines):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        self.assertEqual(config["targets"], {"sample": "9.0.0-beta.1"})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "discovered-compatible")
        self.assertEqual(entry["target"], "9.0.0-beta.1")

    def test_prerelease_of_current_version_is_never_an_upgrade(self) -> None:
        # 8.0.0-beta.1 is OLDER than the stable 8.0.0 (npm semver: prerelease of
        # the same version sorts before it) and must be refused as not-newer —
        # no higher compatible alternative exists, so the current is kept.
        project = _make_project(self._tmp, {"sample": "8.0.0"})

        def fake_latest(project_dir, name, runtime_env):
            return "9.0.0"

        def fake_versions(project_dir, name, runtime_env, top=12):
            return ["9.0.0", "8.0.0", "8.0.0-beta.1", "7.0.0"]

        def fake_engines(project_dir, name, version, runtime_env):
            return {
                "9.0.0": ">=22", "8.0.0": ">=18", "8.0.0-beta.1": ">=18", "7.0.0": ">=18",
            }[version]

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest), \
             mock.patch("iterative_migration._npm_versions", side_effect=fake_versions), \
             mock.patch("iterative_migration._npm_engines_node", side_effect=fake_engines):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        self.assertEqual(config["targets"], {})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "no-newer-compatible")
        not_newer = {
            row["version"] for row in entry["rejected"]
            if row.get("status") == "not-newer-than-current"
        }
        self.assertIn("8.0.0-beta.1", not_newer)
        self.assertIn("8.0.0", not_newer)

    def test_pinned_node_passes_selected_runtime_env_to_discovery_probes(self) -> None:
        # Postfix D3.1: metadata probes must run under the SAME runtime the
        # install/verify will execute — the resolved node dir prepended to
        # PATH — never the ambient PATH.
        project = _make_project(self._tmp, {"is-number": "6.0.0"})
        captured: list = []

        def fake_latest(project_dir, name, runtime_env):
            captured.append(runtime_env)
            return "9.0.0"

        def fake_engines(project_dir, name, version, runtime_env):
            captured.append(runtime_env)
            return {version: ""}.get(version, ">=22" if version == "9.0.0" else "")

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest), \
             mock.patch("iterative_migration._npm_engines_node", side_effect=fake_engines), \
             mock.patch("iterative_migration._npm_versions", return_value=["9.0.0", "8.0.0"]):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        self.assertTrue(captured, "discovery probes must receive a runtime env")
        head = os.pathsep.join(
            (captured[0] or {}).get("PATH", "").split(os.pathsep)[:1]
        )
        self.assertEqual(os.path.normcase(head), os.path.normcase("C:/fake"))
        self.assertTrue(all(env and env.get("PATH", "").split(os.pathsep)[0] != "" for env in captured))

    def test_no_newer_compatible_reaches_plan_next_as_no_actionable(self) -> None:
        # begin -> plan-next integration for the never-downgrade contract: the
        # discovery result (CURRENT kept, no target) must make plan-next see NO
        # actionable work for the package instead of planning the downgrade.
        project = _make_project(self._tmp, {"sample": "8.0.0"})

        def fake_latest(project_dir, name, runtime_env):
            return "9.0.0"

        def fake_versions(project_dir, name, runtime_env, top=12):
            return ["9.0.0", "8.0.0", "7.0.0"]

        def fake_engines(project_dir, name, version, runtime_env):
            return {
                "9.0.0": ">=22", "8.0.0": "", "7.0.0": ">=18",
            }[version]

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", side_effect=fake_latest), \
             mock.patch("iterative_migration._npm_versions", side_effect=fake_versions), \
             mock.patch("iterative_migration._npm_engines_node", side_effect=fake_engines):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        self.assertEqual(config["targets"], {})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "no-newer-compatible")

        run_dir = self._tmp / "run-plan"
        run_dir.mkdir(exist_ok=True)
        checkpoints = run_dir / "checkpoints"
        checkpoints.mkdir(exist_ok=True)
        (run_dir / "run-config.json").write_text(json.dumps(config), encoding="utf-8")
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "runId": "iter-plan-1",
                    "activeCheckpointId": "C0",
                    "phase": "READY",
                    "createdAt": "2026-09-30T00:00:00Z",
                    "updatedAt": "2026-09-30T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        (checkpoints / "C0.json").write_text(
            json.dumps(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "checkpointId": "C0",
                    "parentCheckpointId": "",
                    "status": "VERIFIED",
                    "fullAssignment": {"sample": "8.0.0"},
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "ledger.json").write_text(json.dumps({"schemaVersion": 1, "blocks": [], "deferrals": []}), encoding="utf-8")

        from iterative_migration import load_config, load_run

        # plan-next re-asserts the durable runtime identity (RUNTIME_CHANGED
        # guard); keep the same fake resolution alive so the run continues.
        with self._patch_node_resolution("20.11.0"):
            exit_code = _plan_next_locked(run_dir, load_run(run_dir), load_config(run_dir))
        self.assertEqual(exit_code, 0)
        from iterative_migration import load_run as reload_run

        run_after = reload_run(run_dir)
        self.assertEqual(run_after.get("terminal"), "NO_ACTIONABLE")
        self.assertEqual(run_after.get("phase"), "TERMINAL")
        self.assertFalse((run_dir / "trial" / "candidate.json").exists())

    def test_pinned_node_unknown_latest_engines_is_never_false_incompatibility(self) -> None:
        project = _make_project(self._tmp, {"is-number": "6.0.0"})

        with self._patch_node_resolution("20.11.0"), \
             mock.patch("iterative_migration._manager_runtime_identity", return_value=("npm", "10.0.0")), \
             mock.patch("iterative_migration._npm_latest_version", return_value="9.0.0"), \
             mock.patch("iterative_migration._npm_engines_node", return_value=""):
            config = build_run_config(self._tmp, self._args_with_node(project, "20.11.0"))

        # Unknown engines = abstention (compatible per the #6 contract), target
        # stays latest with the evidence flag; never a false incompatibility.
        self.assertEqual(config["targets"], {"is-number": "9.0.0"})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "discovered")
        self.assertTrue(entry.get("enginesUnknown"))

    def test_unpinned_node_marks_discovery_engine_unchecked(self) -> None:
        project = _make_project(self._tmp, {"is-number": "6.0.0"})

        with mock.patch("iterative_migration._npm_latest_version", return_value="9.0.0"):
            config = build_run_config(self._tmp, _base_args(str(project)))

        self.assertEqual(config["targets"], {"is-number": "9.0.0"})
        entry = config["targetDiscovery"][0]
        self.assertEqual(entry["status"], "discovered")
        self.assertTrue(entry.get("engineUnchecked"))


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
