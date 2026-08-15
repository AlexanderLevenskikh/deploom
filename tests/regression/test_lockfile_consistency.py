from __future__ import annotations

import json
import subprocess
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lockfile_consistency as lc
import dependency_live_roadmap_generator as generator


class LockfileConsistencyRegressionTests(unittest.TestCase):
    def write_package(self, project: Path, data: dict) -> None:
        (project / "package.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_yarn_project_rejects_incidental_root_package_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_package(project, {
                "packageManager": "yarn@1.22.22",
                "dependencies": {"react": "18.2.0"},
            })
            (project / "yarn.lock").write_text('react@18.2.0:\n  version "18.2.0"\n', encoding="utf-8")
            (project / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3}), encoding="utf-8")

            with self.assertRaises(lc.LockfileConsistencyError) as raised:
                lc.ensure_lockfile_consistency(project, "https://nexus.example/repository/npm", mode="validate")
            self.assertEqual("LOCKFILE_CONFLICT", raised.exception.code)
            self.assertIn("package-lock.json", raised.exception.detail)

    def test_changed_package_json_does_not_reuse_single_old_yarn_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_package(project, {
                "packageManager": "yarn@1.22.22",
                "dependencies": {"react": "19.2.7"},
            })
            lock = project / "yarn.lock"
            lock.write_text('react@18.2.0:\n  version "18.2.0"\n', encoding="utf-8")

            state = lc.ensure_lockfile_consistency(project, "https://nexus.example/repository/npm", mode="validate")
            self.assertFalse(state.valid)
            self.assertEqual("YARN_SELECTOR_MISSING", state.issues[0].code)
            self.assertIsNone(generator.yarn_lock_version(lock, "react", "19.2.7"))

    def test_yarn_update_requires_and_runs_deduplication_then_frozen_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_package(project, {
                "packageManager": "yarn@1.22.22",
                "scripts": {"deduplicate": "yarn-deduplicate yarn.lock"},
                "dependencies": {"react": "19.2.7"},
            })
            lock = project / "yarn.lock"
            lock.write_text('react@18.2.0:\n  version "18.2.0"\n', encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command, **kwargs):
                cmd = [str(x) for x in command]
                calls.append(cmd)
                if "install" in cmd and "--frozen-lockfile" not in cmd:
                    lock.write_text('react@19.2.7:\n  version "19.2.7"\n', encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with mock.patch.object(lc, "resolve_executable", return_value="yarn.cmd"):
                state = lc.ensure_lockfile_consistency(
                    project,
                    "https://nexus.example/repository/npm",
                    {"mode": "update", "yarnDeduplicate": True},
                    mode="update",
                    runner=fake_run,
                )

            self.assertTrue(state.valid)
            self.assertTrue(state.updated)
            self.assertTrue(state.deduplicated)
            self.assertEqual(3, len(calls))
            self.assertEqual(["yarn.cmd", "run", "deduplicate"], calls[1])
            self.assertIn("--frozen-lockfile", calls[2])

    def test_yarn_update_continues_when_optional_deduplication_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_package(project, {
                "packageManager": "yarn@1.22.22",
                "dependencies": {"react": "19.2.7"},
            })
            lock = project / "yarn.lock"
            lock.write_text('react@18.2.0:\n  version "18.2.0"\n', encoding="utf-8")

            def fake_run(command, **kwargs):
                if "--frozen-lockfile" not in command:
                    lock.write_text('react@19.2.7:\n  version "19.2.7"\n', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(lc, "resolve_executable", return_value="yarn.cmd"):
                state = lc.ensure_lockfile_consistency(
                    project, "https://nexus.example/repository/npm",
                    {"yarnDeduplicate": "auto"}, mode="update", runner=fake_run
                )
            self.assertTrue(state.valid)
            self.assertEqual("not-available", state.deduplication_status)
            self.assertFalse(state.deduplicated)
            self.assertTrue(state.warnings)

    def test_yarn_update_can_require_deduplication_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_package(project, {
                "packageManager": "yarn@1.22.22",
                "dependencies": {"react": "19.2.7"},
            })
            lock = project / "yarn.lock"
            lock.write_text('react@18.2.0:\n  version "18.2.0"\n', encoding="utf-8")

            def fake_run(command, **kwargs):
                lock.write_text('react@19.2.7:\n  version "19.2.7"\n', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(lc, "resolve_executable", return_value="yarn.cmd"):
                with self.assertRaises(lc.LockfileConsistencyError) as raised:
                    lc.ensure_lockfile_consistency(
                        project, "https://nexus.example/repository/npm",
                        {"yarnDeduplicate": "required"}, mode="update", runner=fake_run
                    )
            self.assertEqual("YARN_DEDUPLICATION_COMMAND_MISSING", raised.exception.code)

    def test_source_checkout_cannot_be_silently_mutated_by_auto_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.write_package(project, {
                "packageManager": "yarn@1.22.22",
                "scripts": {"deduplicate": "yarn-deduplicate yarn.lock"},
                "dependencies": {"react": "19.2.7"},
            })
            (project / "yarn.lock").write_text('react@18.2.0:\n  version "18.2.0"\n', encoding="utf-8")
            with self.assertRaises(lc.LockfileConsistencyError) as raised:
                lc.ensure_lockfile_consistency(
                    project,
                    "https://nexus.example/repository/npm",
                    mode="update",
                    allow_update=False,
                )
            self.assertEqual("LOCKFILE_UPDATE_NOT_ALLOWED_ON_SOURCE_CHECKOUT", raised.exception.code)


    def test_current_checkout_cli_refreshes_yarn_lock_before_dashboard_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "Demo"
            project.mkdir()
            (project / "vendor-v2").mkdir()
            self.write_package(project, {
                "name": "demo",
                "packageManager": "yarn@1.22.22",
                "scripts": {"deduplicate": "yarn-deduplicate yarn.lock"},
                "dependencies": {"local-demo": "file:vendor-v2"},
            })
            (project / "vendor-v2" / "package.json").write_text(
                json.dumps({"name": "local-demo", "version": "2.0.0"}), encoding="utf-8"
            )
            (project / "yarn.lock").write_text(
                'local-demo@file:vendor-v1:\n  version "1.0.0"\n', encoding="utf-8"
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "fake_yarn.py"
            updated_lock = 'local-demo@file:vendor-v2:\n  version "2.0.0"\n'
            fake.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "lock = Path.cwd() / 'yarn.lock'\n"
                "args = sys.argv[1:]\n"
                "if args and args[0] == 'install' and '--frozen-lockfile' not in args:\n"
                f"    lock.write_text({updated_lock!r}, encoding='utf-8')\n"
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            if os.name == "nt":
                (bin_dir / "yarn.cmd").write_text(
                    f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n', encoding="ascii"
                )
            else:
                launcher = bin_dir / "yarn"
                launcher.write_text(
                    f'#!/usr/bin/env sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8"
                )
                launcher.chmod(0o755)
            settings = root / "settings.project.json"
            settings.write_text(json.dumps({
                "root": str(root),
                "projects": [{"name": "Demo", "path": "Demo"}],
                "out": "artifacts/report.md",
                "jsonOut": "artifacts/report.json",
                "htmlOut": "artifacts/report.html",
                "historyDir": "history",
                "dashboardState": "state.json",
                "releaseIntelEnabled": False,
                "lockfileSync": {"currentMode": "update", "yarnDeduplicate": "auto"},
            }), encoding="utf-8")
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            result = subprocess.run([
                sys.executable, str(Path(generator.__file__)),
                "--project-settings", str(settings),
                "--only-project", "Demo",
                "--skip-release-intel",
                "--no-history-snapshot",
            ], cwd=root, env=env, text=True, capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            report = json.loads((root / "artifacts" / "report.json").read_text(encoding="utf-8"))
            row = report["projects"]["Demo"][0]
            self.assertEqual("2.0.0", row["current_version"])
            self.assertEqual("yarn.lock", row["current_source"])
            self.assertTrue(report["project_lockfiles"]["Demo"]["updated"])
            self.assertTrue(report["project_lockfiles"]["Demo"]["deduplicated"])
            self.assertFalse((project / "package-lock.json").exists())
            self.assertFalse(any((root / "artifacts").glob("manual-audit-*-workspace")))
            self.assertEqual("manual-only", report["project_manual_audit"]["Demo"]["mode"])
            self.assertEqual("yarn.lock", report["project_manual_audit"]["Demo"]["dashboardLockfile"])

    def test_generator_defaults_to_current_checkout_and_exposes_lockfile_and_audit_metadata(self) -> None:
        source = Path(generator.__file__).read_text(encoding="utf-8")
        self.assertIn('"--post-update"', source)  # backward-compatible alias
        self.assertIn('"--lockfile-mode"', source)
        self.assertIn('"project_lockfiles"', source)
        self.assertIn('"project_manual_audit"', source)
        self.assertIn("analysis mode: current checkout compared with the saved baseline", source)
        self.assertIn("the selected project-manager lockfile", source)
        self.assertIn("manual-only", source)

    def test_yarn_dashboard_version_is_read_from_yarn_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "yarn.lock"
            lock.write_text('react@^19.2.7:\n  version "19.2.9"\n', encoding="utf-8")
            current, source = generator.resolved_current_version(
                Path(tmp), "react", "^19.2.7", "runtime", lock
            )
        self.assertEqual("19.2.9", current)
        self.assertEqual("yarn.lock", source)

    def test_npm_dashboard_version_is_read_from_package_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "package-lock.json"
            lock.write_text(json.dumps({
                "lockfileVersion": 3,
                "packages": {"node_modules/react": {"version": "19.2.9"}},
            }), encoding="utf-8")
            current, source = generator.resolved_current_version(
                Path(tmp), "react", "^19.2.7", "runtime", lock
            )
        self.assertEqual("19.2.9", current)
        self.assertEqual("package-lock.json", source)

    def test_peer_only_dependency_is_explicitly_not_locally_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "yarn.lock"
            lock.write_text('# yarn lockfile v1\n', encoding="utf-8")
            current, source = generator.resolved_current_version(
                Path(tmp), "react", "^18 || ^19", "peer", lock
            )
        self.assertEqual("^18 || ^19", current)
        self.assertIn("peer declaration", source)


if __name__ == "__main__":
    unittest.main()
