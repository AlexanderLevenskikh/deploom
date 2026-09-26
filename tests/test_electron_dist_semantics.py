"""A12: _electron_dist.py must not turn a real compile error into a skip.

The shared helper builds desktop/dist-electron for cross-language tests. It
may SkipTest ONLY when the Node/TypeScript toolchain is genuinely unavailable:

  - no node/npx on PATH           -> skip (honest environment limitation)
  - tsc ran but emitted "error TS" -> FAILURE (broken build, never skip)
  - tsc reported success but the   -> FAILURE (broken build, never skip)
    output assets are missing

Release CI builds the Electron modules explicitly (npm ci + npx tsc) before
running the Python suites, so a compile error fails the release instead of a
hidden skip.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

import tests._electron_dist as electron_dist

ROOT = Path(__file__).resolve().parents[1]


def _fake_tsc(stderr: str, returncode: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["npx", "tsc"],
        returncode=returncode,
        stdout="",
        stderr=stderr,
    )


class ElectronDistSkipSemanticsTests(unittest.TestCase):
    def tearDown(self) -> None:
        mock.patch.stopall()

    def test_fresh_build_is_a_noop(self) -> None:
        with mock.patch.object(electron_dist, "_needs_build", return_value=False):
            electron_dist.ensure_dist_electron()  # must not raise or skip

    def test_no_node_on_path_skips(self) -> None:
        with (
            mock.patch.object(electron_dist, "_needs_build", return_value=True),
            mock.patch.object(electron_dist, "_toolchain_present", return_value=False),
        ):
            with self.assertRaises(unittest.SkipTest):
                electron_dist.ensure_dist_electron()

    def test_npx_missing_is_a_skip(self) -> None:
        with (
            mock.patch.object(electron_dist, "_needs_build", return_value=True),
            mock.patch.object(electron_dist, "_toolchain_present", return_value=True),
            mock.patch.object(
                electron_dist, "_run_tsc", side_effect=FileNotFoundError()
            ),
        ):
            with self.assertRaises(unittest.SkipTest):
                electron_dist.ensure_dist_electron()

    def test_real_compile_error_is_a_failure(self) -> None:
        with (
            mock.patch.object(electron_dist, "_needs_build", return_value=True),
            mock.patch.object(electron_dist, "_toolchain_present", return_value=True),
            mock.patch.object(
                electron_dist,
                "_run_tsc",
                return_value=_fake_tsc("src/a.ts(1,5): error TS2322: type mismatch"),
            ),
        ):
            with self.assertRaises(AssertionError) as ctx:
                electron_dist.ensure_dist_electron()
            self.assertIn("error TS2322", str(ctx.exception))

    def test_success_without_output_asset_is_a_failure(self) -> None:
        with (
            mock.patch.object(electron_dist, "_needs_build", return_value=True),
            mock.patch.object(electron_dist, "_toolchain_present", return_value=True),
            mock.patch.object(electron_dist, "_run_tsc", return_value=_fake_tsc("", 0)),
            mock.patch.object(
                electron_dist,
                "DIST_DIR",
                ROOT / "tests" / "__nonexistent_dist_electron__",
            ),
        ):
            with self.assertRaises(AssertionError) as ctx:
                electron_dist.ensure_dist_electron()
            self.assertIn("produced no output asset", str(ctx.exception))

    def test_tsc_command_never_installs_typescript(self) -> None:
        """--no-install keeps the build deterministic and offline-safe."""
        cmd = electron_dist.tsc_command()
        self.assertIn("--no-install", cmd)
        self.assertIn("tsc", cmd)


if __name__ == "__main__":
    unittest.main()
