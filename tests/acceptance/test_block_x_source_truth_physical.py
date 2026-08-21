from __future__ import annotations

# BLOCK_X_PHYSICAL_ACCEPTANCE_V1

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import source_snapshot
from baseline_constraint_verifier import BaselineVerifyConfig, verify_assignment


def _run(
    argv: list[str],
    cwd: Path,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n{result.stdout}"
        )
    return result


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo), *args], repo, check=check)


def _init_repo(repo: Path) -> None:
    result = _run(["git", "init", "-b", "master"], repo, check=False)
    if result.returncode != 0:
        _run(["git", "init"], repo)
        _git(repo, "checkout", "-B", "master")
    _git(repo, "config", "user.email", "block-x-physical@example.invalid")
    _git(repo, "config", "user.name", "Block X Physical")


def _command_argv(command: str, *args: str) -> list[str]:
    resolved = shutil.which(command)
    if not resolved and os.name == "nt":
        for suffix in (".cmd", ".bat", ".exe", ".com"):
            resolved = shutil.which(command + suffix)
            if resolved:
                break
    if not resolved:
        raise unittest.SkipTest(f"{command} is not available in PATH")
    if os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
        quoted = subprocess.list2cmdline([resolved, *args])
        return [comspec, "/d", "/s", "/c", quoted]
    return [resolved, *args]


def _write_npm_project(project: Path, *, name: str, check_file: str, check_source: str) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "package.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "private": True,
                "dependencies": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (project / check_file).write_text(check_source, encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "npm_config_audit": "false",
            "npm_config_fund": "false",
            "npm_config_ignore_scripts": "true",
            "npm_config_offline": "true",
        }
    )
    _run(
        _command_argv(
            "npm",
            "install",
            "--package-lock-only",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ),
        project,
        env=env,
    )


def _config(root: Path, command: str) -> BaselineVerifyConfig:
    return BaselineVerifyConfig(
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
        commands=(command,),
        registry="",
        telemetry_path=str(root / "verification-telemetry.jsonl"),
        proof_cache_dir=str(root / "proof-cache"),
    )


def _assert_pass(test: unittest.TestCase, result, *, label: str) -> None:
    test.assertTrue(
        result.ok,
        f"{label} failed: kind={result.kind}; summary={result.summary}; "
        f"command={result.command}\n{result.output}",
    )
    test.assertEqual("passed", result.kind, f"{label}: unexpected kind {result.kind}")
    test.assertFalse(result.project_failures, f"{label}: {result.project_failures!r}")


class BlockXPhysicalSourceTruthAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("git") is None:
            raise unittest.SkipTest("git unavailable")
        if shutil.which("node") is None:
            raise unittest.SkipTest("node unavailable")
        if os.name == "nt":
            if not any(shutil.which(name) for name in ("npm", "npm.cmd", "npm.exe")):
                raise unittest.SkipTest("npm unavailable")
        elif shutil.which("npm") is None:
            raise unittest.SkipTest("npm unavailable")

    def tearDown(self) -> None:
        source_snapshot.clear_source_snapshot_epochs()

    def test_real_verifier_consumes_dirty_untracked_and_ignored_sealed_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deploom-x-physical-") as raw:
            root = Path(raw)
            _init_repo(root)

            check = r'''const fs = require('fs');
const expected = new Map([
  ['tracked.txt', 'DIRTY_TRACKED'],
  ['untracked.txt', 'UNTRACKED_INPUT'],
  ['ignored.env', 'IGNORED_SEMANTIC']
]);
for (const [name, wanted] of expected) {
  const actual = fs.readFileSync(name, 'utf8').trim();
  if (actual !== wanted) {
    console.error(`${name}: expected=${wanted} actual=${actual}`);
    process.exit(41);
  }
}
console.log('BLOCK_X_DIRTY_SUBJECT_OK');
'''

            _write_npm_project(
                root,
                name="block-x-dirty-subject",
                check_file="check-source.js",
                check_source=check,
            )
            (root / ".gitignore").write_text(
                "ignored.env\nnode_modules/\nproof-cache/\nverification-telemetry.jsonl\n",
                encoding="utf-8",
            )
            (root / "tracked.txt").write_text("COMMITTED\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "baseline")

            (root / "tracked.txt").write_text("DIRTY_TRACKED\n", encoding="utf-8")
            (root / "untracked.txt").write_text("UNTRACKED_INPUT\n", encoding="utf-8")
            (root / "ignored.env").write_text("IGNORED_SEMANTIC\n", encoding="utf-8")

            snapshot = source_snapshot.activate_source_snapshot_epoch(
                root, replace=True, timeout_seconds=120
            )
            key = snapshot.key

            (root / "tracked.txt").write_text("LIVE_AFTER_SEAL\n", encoding="utf-8")
            (root / "untracked.txt").write_text("LIVE_AFTER_SEAL\n", encoding="utf-8")
            (root / "ignored.env").write_text("LIVE_AFTER_SEAL\n", encoding="utf-8")
            _git(root, "gc", "--prune=now")

            result = verify_assignment(
                root,
                {},
                config=_config(root, "node check-source.js"),
                run_project_checks=True,
            )
            _assert_pass(self, result, label="dirty/untracked/ignored physical verifier")
            self.assertEqual(key, source_snapshot.source_snapshot_fingerprint(root))

    def test_real_verifier_preserves_detached_head_even_after_live_checkout_moves(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deploom-x-detached-") as raw:
            root = Path(raw)
            _init_repo(root)

            check = r'''const fs = require('fs');
const cp = require('child_process');
const marker = fs.readFileSync('marker.txt', 'utf8').trim();
const branch = cp.execFileSync('git', ['rev-parse', '--abbrev-ref', 'HEAD'], {
  encoding: 'utf8'
}).trim();
if (marker !== 'COMMIT_A' || branch !== 'HEAD') {
  console.error(`wrong detached subject marker=${marker} branch=${branch}`);
  process.exit(42);
}
console.log('BLOCK_X_DETACHED_HEAD_OK');
'''

            _write_npm_project(
                root,
                name="block-x-detached",
                check_file="check-detached.js",
                check_source=check,
            )
            (root / ".gitignore").write_text(
                "node_modules/\nproof-cache/\nverification-telemetry.jsonl\n",
                encoding="utf-8",
            )
            (root / "marker.txt").write_text("COMMIT_A\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "commit-a")
            commit_a = _git(root, "rev-parse", "HEAD").stdout.strip()

            (root / "marker.txt").write_text("COMMIT_B\n", encoding="utf-8")
            _git(root, "add", "marker.txt")
            _git(root, "commit", "-m", "commit-b")

            _git(root, "checkout", "--detach", commit_a)
            snapshot = source_snapshot.activate_source_snapshot_epoch(
                root, replace=True, timeout_seconds=120
            )
            self.assertEqual(commit_a, snapshot.git_head)

            _git(root, "checkout", "-f", "master")
            self.assertEqual("COMMIT_B", (root / "marker.txt").read_text(encoding="utf-8").strip())

            result = verify_assignment(
                root,
                {},
                config=_config(root, "node check-detached.js"),
                run_project_checks=True,
            )
            _assert_pass(self, result, label="detached HEAD physical verifier")

    def test_nested_package_can_consume_dirty_repo_level_input_from_same_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deploom-x-nested-") as raw:
            root = Path(raw)
            _init_repo(root)
            frontend = root / "frontend"

            check = r'''const fs = require('fs');
const path = require('path');
const rootInput = fs.readFileSync(path.join(process.cwd(), '..', 'root-input.txt'), 'utf8').trim();
const ignored = fs.readFileSync(path.join(process.cwd(), '..', 'root-ignored.env'), 'utf8').trim();
if (path.basename(process.cwd()) !== 'frontend') {
  console.error(`wrong cwd ${process.cwd()}`);
  process.exit(43);
}
if (rootInput !== 'DIRTY_ROOT_INPUT' || ignored !== 'DIRTY_ROOT_IGNORED') {
  console.error(`wrong root inputs tracked=${rootInput} ignored=${ignored}`);
  process.exit(44);
}
console.log('BLOCK_X_NESTED_ROOT_INPUT_OK');
'''

            _write_npm_project(
                frontend,
                name="block-x-nested",
                check_file="check-nested.js",
                check_source=check,
            )
            (root / ".gitignore").write_text(
                "root-ignored.env\nnode_modules/\nfrontend/node_modules/\n"
                "proof-cache/\nverification-telemetry.jsonl\n",
                encoding="utf-8",
            )
            (root / "root-input.txt").write_text("COMMITTED_ROOT\n", encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "nested-baseline")

            (root / "root-input.txt").write_text("DIRTY_ROOT_INPUT\n", encoding="utf-8")
            (root / "root-ignored.env").write_text("DIRTY_ROOT_IGNORED\n", encoding="utf-8")

            snapshot = source_snapshot.activate_source_snapshot_epoch(
                frontend, replace=True, timeout_seconds=120
            )
            self.assertEqual(Path("frontend"), snapshot.project_relative)

            (root / "root-input.txt").write_text("LIVE_AFTER_SEAL\n", encoding="utf-8")
            (root / "root-ignored.env").write_text("LIVE_AFTER_SEAL\n", encoding="utf-8")

            result = verify_assignment(
                frontend,
                {},
                config=_config(root, "node check-nested.js"),
                run_project_checks=True,
            )
            _assert_pass(self, result, label="nested repo-level input physical verifier")

    def test_initialized_dirty_submodule_bytes_are_part_of_real_verification_subject(self) -> None:
        with tempfile.TemporaryDirectory(prefix="deploom-x-submodule-") as raw, \
                tempfile.TemporaryDirectory(prefix="deploom-x-submodule-src-") as subraw:
            root = Path(raw)
            sub = Path(subraw)
            _init_repo(root)
            _init_repo(sub)

            (sub / "sub.txt").write_text("SUB_COMMITTED\n", encoding="utf-8")
            _git(sub, "add", ".")
            _git(sub, "commit", "-m", "sub-baseline")

            check = r'''const fs = require('fs');
const value = fs.readFileSync('vendor/sub/sub.txt', 'utf8').trim();
if (value !== 'DIRTY_SUBMODULE') {
  console.error(`wrong submodule subject ${value}`);
  process.exit(45);
}
console.log('BLOCK_X_SUBMODULE_OK');
'''

            _write_npm_project(
                root,
                name="block-x-submodule",
                check_file="check-submodule.js",
                check_source=check,
            )
            (root / ".gitignore").write_text(
                "node_modules/\nproof-cache/\nverification-telemetry.jsonl\n",
                encoding="utf-8",
            )
            _git(root, "add", ".")
            _git(root, "commit", "-m", "root-baseline")

            added = _run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "-C",
                    str(root),
                    "submodule",
                    "add",
                    str(sub),
                    "vendor/sub",
                ],
                root,
                check=False,
            )
            if added.returncode != 0:
                self.skipTest(f"local submodule add unavailable: {added.stdout}")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "add-submodule")

            (root / "vendor" / "sub" / "sub.txt").write_text(
                "DIRTY_SUBMODULE\n", encoding="utf-8"
            )
            snapshot = source_snapshot.activate_source_snapshot_epoch(
                root, replace=True, timeout_seconds=120
            )
            self.assertEqual(
                "DIRTY_SUBMODULE",
                (snapshot.project_path / "vendor" / "sub" / "sub.txt")
                .read_text(encoding="utf-8")
                .strip(),
            )

            (root / "vendor" / "sub" / "sub.txt").write_text(
                "LIVE_AFTER_SEAL\n", encoding="utf-8"
            )

            result = verify_assignment(
                root,
                {},
                config=_config(root, "node check-submodule.js"),
                run_project_checks=True,
            )
            _assert_pass(self, result, label="dirty submodule physical verifier")


if __name__ == "__main__":
    unittest.main()
