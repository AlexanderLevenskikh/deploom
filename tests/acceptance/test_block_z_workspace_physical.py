from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from baseline_constraint_verifier import BaselineVerifyConfig, verify_assignment
from project_topology import resolve_project_topology

# BLOCK_Z_PROJECT_TOPOLOGY_V1


def _command(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        for suffix in (".cmd", ".exe", ".bat"):
            found = shutil.which(name + suffix)
            if found:
                return found
    return None


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    executable = command[0]
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        argv = [
            comspec, "/d", "/s", "/c",
            subprocess.list2cmdline(command),
        ]
    else:
        argv = command
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=180,
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class BlockZWorkspacePhysicalAcceptance(unittest.TestCase):
    def test_real_npm_workspace_resolves_at_root_and_checks_target_package(self) -> None:
        npm = _command("npm")
        git = _command("git")
        node = _command("node")
        if not npm or not git or not node:
            self.skipTest("npm/git/node required for physical workspace acceptance")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "repo"
            app = root / "packages" / "app"
            app.mkdir(parents=True)
            _write_json(root / "package.json", {
                "name": "block-z-root",
                "private": True,
                "packageManager": "npm@11.0.0",
                "workspaces": ["packages/*"],
            })
            _write_json(app / "package.json", {
                "name": "block-z-app",
                "version": "1.0.0",
            })
            (root / "root-marker.txt").write_text(
                "ROOT_MANAGER_CONTEXT\n", encoding="utf-8"
            )
            check = app / "check-topology.js"
            check.write_text(
                """
const fs = require('fs');
const path = require('path');
const cwd = process.cwd().replace(/\\\\/g, '/');
if (!cwd.endsWith('/packages/app')) {
  console.error('wrong project-check cwd: ' + cwd);
  process.exit(31);
}
if (!fs.existsSync(path.resolve(process.cwd(), '../../package-lock.json'))) {
  console.error('root lockfile unavailable');
  process.exit(32);
}
if (fs.readFileSync(path.resolve(process.cwd(), '../../root-marker.txt'), 'utf8').trim() !== 'ROOT_MANAGER_CONTEXT') {
  process.exit(33);
}
const workspaceLink = path.resolve(process.cwd(), '../../node_modules/block-z-app');
if (!fs.existsSync(workspaceLink)) {
  console.error('workspace link missing: ' + workspaceLink);
  process.exit(34);
}
const linkedReal = fs.realpathSync.native(workspaceLink).replace(/\\\\/g, '/').toLowerCase();
const projectReal = fs.realpathSync.native(process.cwd()).replace(/\\\\/g, '/').toLowerCase();
if (linkedReal !== projectReal) {
  console.error('workspace link escaped private clone: ' + linkedReal + ' != ' + projectReal);
  process.exit(35);
}
console.log('BLOCK_Z_WORKSPACE_CHECK_PASS');
""".lstrip(),
                encoding="utf-8",
            )

            init = _run([git, "init", "-q"], root)
            self.assertEqual(0, init.returncode, init.stdout)
            self.assertEqual(0, _run([git, "config", "user.email", "z@example.invalid"], root).returncode)
            self.assertEqual(0, _run([git, "config", "user.name", "Block Z"], root).returncode)

            lock = _run([
                npm,
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
            ], root)
            self.assertEqual(0, lock.returncode, lock.stdout)
            self.assertTrue((root / "package-lock.json").is_file())

            self.assertEqual(0, _run([git, "add", "."], root).returncode)
            commit = _run([git, "commit", "-q", "-m", "fixture"], root)
            self.assertEqual(0, commit.returncode, commit.stdout)

            topology = resolve_project_topology(app, require_supported=True)
            self.assertEqual(root.resolve(), topology.package_manager_root)
            self.assertEqual(Path("packages/app"), topology.package_relative_to_manager)

            telemetry = root.parent / "verification-telemetry.jsonl"
            proof_cache = root.parent / "proof-cache"
            config = BaselineVerifyConfig(
                enabled=True,
                parallelism=1,
                max_iterations=2,
                max_delta_checks=2,
                timeout_seconds=120,
                attempt_timeout_seconds=300,
                localization_timeout_seconds=300,
                progress_interval_seconds=5,
                snapshot_copy_timeout_seconds=120,
                project_checks="strict",
                commands=("node check-topology.js",),
                registry="",
                telemetry_path=str(telemetry),
                proof_cache_dir=str(proof_cache),
            )
            result = verify_assignment(
                app,
                {},
                config=config,
                run_project_checks=True,
            )
            self.assertTrue(result.ok, result.summary + "\n" + result.output)
            self.assertEqual("passed", result.kind)

            events = [
                json.loads(line)
                for line in telemetry.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            topology_events = [
                event for event in events
                if event.get("event") == "project.topology"
            ]
            self.assertTrue(topology_events)
            event = topology_events[-1]
            self.assertEqual("npm", event["managerFamily"])
            self.assertEqual("packages/app", event["packageRelativeToManager"])

    @unittest.skipUnless(os.name == "nt", "Windows scoped Yarn Classic workspace acceptance")
    def test_real_yarn1_scoped_workspace_link_is_rebased_into_private_clone(self) -> None:
        yarn = _command("yarn")
        git = _command("git")
        node = _command("node")
        if not yarn or not git or not node:
            self.skipTest("Yarn Classic/git/node required")
        version = _run([yarn, "--version"], Path.cwd())
        if version.returncode != 0 or not version.stdout.strip().startswith("1."):
            self.skipTest(f"Yarn Classic 1.x required; observed={version.stdout.strip()!r}")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "repo"
            app = root / "packages" / "app"
            app.mkdir(parents=True)
            _write_json(root / "package.json", {
                "name": "block-z-yarn-root",
                "private": True,
                "packageManager": "yarn@1.22.22",
                "workspaces": ["packages/*"],
            })
            _write_json(app / "package.json", {
                "name": "@block-z/app",
                "version": "1.0.0",
            })
            topology_script = app / "check-topology.js"
            topology_script.write_text(
                """
const fs = require('fs');
const path = require('path');
const cwd = fs.realpathSync.native(process.cwd()).replace(/\\\\/g, '/').toLowerCase();
const root = path.resolve(process.cwd(), '../..');
if (!fs.existsSync(path.join(root, 'yarn.lock'))) process.exit(41);
const link = path.join(root, 'node_modules', '@block-z', 'app');
if (!fs.existsSync(link)) { console.error('scoped workspace link missing: ' + link); process.exit(42); }
const linked = fs.realpathSync.native(link).replace(/\\\\/g, '/').toLowerCase();
if (linked !== cwd) { console.error('scoped workspace escaped private clone: ' + linked + ' != ' + cwd); process.exit(43); }
console.log('BLOCK_Z_YARN_SCOPED_WORKSPACE_PASS');
""".lstrip(),
                encoding="utf-8",
            )
            syntax = _run([node, "--check", str(topology_script)], app)
            self.assertEqual(0, syntax.returncode, syntax.stdout)

            self.assertEqual(0, _run([git, "init", "-q"], root).returncode)
            self.assertEqual(0, _run([git, "config", "user.email", "z@example.invalid"], root).returncode)
            self.assertEqual(0, _run([git, "config", "user.name", "Block Z"], root).returncode)
            install = _run([yarn, "install", "--ignore-scripts", "--non-interactive"], root)
            self.assertEqual(0, install.returncode, install.stdout)
            self.assertTrue((root / "yarn.lock").is_file())
            self.assertEqual(0, _run([git, "add", "."], root).returncode)
            self.assertEqual(0, _run([git, "commit", "-q", "-m", "fixture"], root).returncode)

            topology = resolve_project_topology(app, require_supported=True)
            self.assertEqual("yarn-classic", topology.profile.family)
            self.assertEqual(Path("packages/app"), topology.package_relative_to_manager)

            config = BaselineVerifyConfig(
                enabled=True,
                parallelism=1,
                timeout_seconds=120,
                attempt_timeout_seconds=300,
                localization_timeout_seconds=300,
                progress_interval_seconds=5,
                snapshot_copy_timeout_seconds=120,
                project_checks="strict",
                commands=("node check-topology.js",),
                proof_cache_dir=str(root.parent / "proof-cache-yarn"),
            )
            result = verify_assignment(app, {}, config=config, run_project_checks=True)
            self.assertTrue(result.ok, result.summary + "\n" + result.output)
            self.assertEqual("passed", result.kind)


if __name__ == "__main__":
    unittest.main()
