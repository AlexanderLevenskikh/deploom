from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import baseline_constraint_verifier as verifier
from verification_process_supervisor import run_supervised


class SigmaProcessSupervisionTests(unittest.TestCase):
    def test_success_path_reports_tree_quiescence(self) -> None:
        result = run_supervised(
            [sys.executable, "-c", "print('ok')"],
            Path.cwd(),
            timeout_seconds=10,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual(0, result.supervision.descendants_remaining)
        if os.name == "nt":
            self.assertEqual("guaranteed-tree", result.supervision.quality)
        elif sys.platform.startswith("linux"):
            self.assertEqual("best-effort", result.supervision.quality)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper physical check")
    def test_detached_descendant_cannot_write_after_parent_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "late-write.txt"
            child = (
                "import time; "
                "time.sleep(0.6); "
                f"open({str(marker)!r}, 'w', encoding='utf-8').write('late')"
            )
            parent = (
                "import subprocess,sys; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)"
            )
            result = run_supervised(
                [sys.executable, "-c", parent],
                Path.cwd(),
                timeout_seconds=10,
            )
            self.assertEqual("best-effort", result.supervision.quality)
            self.assertGreaterEqual(result.supervision.descendants_terminated, 1)
            time.sleep(0.8)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux token scrub capability check")
    def test_scrubbed_supervision_token_never_reports_guaranteed_tree(self) -> None:
        child = "import time; time.sleep(0.2)"
        parent = (
            "import os,subprocess,sys; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}], "
            "start_new_session=True, env={'PATH': os.environ.get('PATH', '')})"
        )
        result = run_supervised(
            [sys.executable, "-c", parent],
            Path.cwd(),
            timeout_seconds=10,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("best-effort", result.supervision.quality)

    def test_output_is_bounded_but_full_stream_infra_match_survives(self) -> None:
        script = (
            "import sys; "
            "print('ENOSPC synthetic infrastructure marker'); "
            "sys.stdout.write('x' * 1500000)"
        )
        result = verifier._run(
            [sys.executable, "-c", script],
            Path.cwd(),
            timeout_seconds=10,
        )
        self.assertTrue(result.output_truncated)
        self.assertGreater(result.dropped_bytes, 0)
        self.assertTrue(getattr(result, "deploom_infra_detected", False))
        self.assertLess(len((result.stdout or "").encode("utf-8")), 1200000)

    def test_deferred_cleanup_telemetry_does_not_shadow_telemetry_path(self) -> None:
        from verification_proof import emit_verification_event

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / "stale-trial"
            stale.mkdir()
            telemetry = root / "events.jsonl"

            def event(name: str, **fields: object) -> None:
                emit_verification_event(telemetry, name, **fields)

            with mock.patch.object(
                verifier.shutil,
                "rmtree",
                side_effect=OSError("synthetic deferred Windows cleanup"),
            ):
                cleaned = verifier._cleanup_trial_root(
                    stale,
                    event=event,
                    command="yarn test:unit",
                    attempts=1,
                )

            self.assertFalse(cleaned)
            payload = telemetry.read_text(encoding="utf-8")
            self.assertIn('"event":"verify.workspace.cleanup-deferred"', payload)
            self.assertIn('"cleanupPath":', payload)
            self.assertNotIn('"path":"' + str(stale).replace('\\', '\\\\') + '"', payload)

    def test_stale_project_check_path_never_forces_target_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / "project-check-005"
            stale.mkdir()
            with mock.patch.object(verifier, "_cleanup_trial_root", return_value=False):
                fresh = verifier._fresh_project_check_root(root, 5)
            self.assertNotEqual(stale, fresh)
            self.assertEqual("project-check-005-001", fresh.name)

    def test_installed_lookup_never_escapes_manager_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            manager = outer / "trial"
            project = manager / "packages" / "app"
            project.mkdir(parents=True)
            external = outer / "node_modules" / "demo"
            external.mkdir(parents=True)
            (external / "package.json").write_text('{"version":"9.9.9"}', encoding="utf-8")
            self.assertIsNone(
                verifier._installed_package_json_path(
                    project,
                    "demo",
                    package_manager_root=manager,
                )
            )


if __name__ == "__main__":
    unittest.main()
