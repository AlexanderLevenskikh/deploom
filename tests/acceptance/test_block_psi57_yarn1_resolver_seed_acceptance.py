from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi57_yarn1_resolver_seed import (
    prepare_yarn1_resolver_seed_for_lifecycle,
    validate_yarn1_lifecycle_completion,
    validate_yarn1_resolver_seed,
)


def _yarn_executable() -> str:
    return shutil.which("yarn") or ""


def _yarn_version(executable: str) -> str:
    if not executable:
        return ""
    argv = _yarn_argv(executable, ["--version"])
    try:
        result = subprocess.run(
            argv,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").strip().splitlines()[0] if result.returncode == 0 and (result.stdout or "").strip() else ""


def _yarn_argv(executable: str, args: list[str]) -> list[str]:
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        quoted = subprocess.list2cmdline([executable, *args])
        return [comspec, "/d", "/s", "/c", quoted]
    return [executable, *args]


def _run_yarn(executable: str, cwd: Path, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _yarn_argv(executable, args),
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
        check=False,
    )


@unittest.skipUnless(_yarn_executable(), "Yarn is required for physical Ψ.5.7 acceptance")
class Psi57YarnClassicPhysicalAcceptance(unittest.TestCase):
    def test_ignore_scripts_seed_can_be_forced_through_real_frozen_lifecycle(self) -> None:
        yarn = _yarn_executable()
        version = _yarn_version(yarn)
        if version != "1.22.22":
            self.skipTest(
                f"certified Yarn Classic 1.22.22 required, observed {version or '<unknown>'}"
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "app"
            dependency = root / "seed-dep"
            cache = root / "yarn-cache"
            project.mkdir()
            dependency.mkdir()
            cache.mkdir()

            # The lifecycle marker is deliberately written *inside the installed
            # package*.  It cannot exist after --ignore-scripts and proves that
            # the later ordinary Yarn lifecycle actually executed dependency
            # postinstall, not merely that our test process ran some script.
            (dependency / "package.json").write_text(
                json.dumps(
                    {
                        "name": "seed-dep",
                        "version": "1.0.0",
                        "scripts": {"postinstall": "node postinstall.js"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (dependency / "postinstall.js").write_text(
                "require('fs').writeFileSync('postinstall-ran.txt', 'yes\\n');\n",
                encoding="utf-8",
            )
            (project / "package.json").write_text(
                json.dumps(
                    {
                        "name": "psi57-fixture",
                        "private": True,
                        "version": "1.0.0",
                        "dependencies": {"seed-dep": "file:../seed-dep"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            env = dict(os.environ)
            env.update(
                {
                    "CI": "1",
                    "YARN_CACHE_FOLDER": str(cache),
                    "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
                    "YARN_ENABLE_SCRIPTS": "false",
                    "npm_config_ignore_scripts": "true",
                }
            )
            resolver = _run_yarn(yarn, project, ["install", "--ignore-scripts"], env)
            self.assertEqual(0, resolver.returncode, resolver.stdout)
            installed_marker = project / "node_modules" / "seed-dep" / "postinstall-ran.txt"
            self.assertFalse(installed_marker.exists(), resolver.stdout)
            self.assertIn("ignoreScripts", validate_yarn1_resolver_seed(project))

            lock_before = (project / "yarn.lock").read_bytes()
            prepare_yarn1_resolver_seed_for_lifecycle(project)
            self.assertFalse((project / "node_modules" / ".yarn-integrity").exists())

            env.update(
                {
                    "YARN_ENABLE_IMMUTABLE_INSTALLS": "true",
                    "YARN_ENABLE_SCRIPTS": "true",
                    "npm_config_ignore_scripts": "false",
                }
            )
            lifecycle = _run_yarn(yarn, project, ["install", "--frozen-lockfile"], env)
            self.assertEqual(0, lifecycle.returncode, lifecycle.stdout)
            self.assertTrue(
                installed_marker.is_file(),
                "real Yarn Classic frozen lifecycle did not execute dependency postinstall\n"
                + lifecycle.stdout,
            )
            self.assertEqual("yes", installed_marker.read_text(encoding="utf-8").strip())
            self.assertNotIn("ignoreScripts", validate_yarn1_lifecycle_completion(project))
            self.assertEqual(lock_before, (project / "yarn.lock").read_bytes())


if __name__ == "__main__":
    unittest.main()
