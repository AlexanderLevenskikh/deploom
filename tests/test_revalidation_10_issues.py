"""Revalidation #10 (C2): the Draft deadline must fit a healthy registry read.

Root cause found in production diagnostics (run-5fadef80 / a real 76-dependency project):
Nexus responded in ~1s and WAS reachable, but the Draft run died with
"deadline (15.0s) exceeded at network-read" after fetching ZERO metadata
documents out of 76. A hard-coded 15s default is not schedulable when every
dependency needs a multi-MB registry JSON read, so the whole dependency set
was reported "registry unavailable" even though the registry was fine.

The fix: when the orchestrator does NOT pin an explicit Draft deadline, the
default scales with the dependency count (base + per-package). An explicit
--draft-deadline-seconds / DEPLOOM_DRAFT_DEADLINE_SECONDS is honored verbatim.
Registry-unavailable stays the honest empty-prompt path (unchanged).
"""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
GENERATOR = ROOT / "dependency_live_roadmap_generator.py"

import dependency_live_roadmap_generator as roadmap


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _mini_tarball(name: str, version: str) -> bytes:
    """Minimal valid gzip tarball with package/package.json (no type/main
    declarations, so no type/runtime blocker is raised)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        payload = json.dumps({"name": name, "version": version}).encode("utf-8")
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _MockRegistry:
    """Tiny local npm registry for a fixed set of packages.

    Serves packuments (dist-tags + versions with a local tarball URL that the
    generator's registry guard accepts) and valid mini tarballs, so a Draft
    run can complete metadata + structural candidate enrichment end to end.
    """

    def __init__(self, deps, versions):
        # deps: list[(name, current, latest)]; versions: dict name -> [v1, v2, ..]
        self.deps = deps
        self.versions = versions
        self.requests: list[str] = []

    def __enter__(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append(self.path)
                if self.path.endswith(".tgz"):
                    for name, current, latest in outer.deps:
                        if self.path == f"/{name}-{latest}.tgz":
                            body = _mini_tarball(name, latest)
                            break
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                name = self.path.lstrip("/").split("%2F")[0]
                entry = next((d for d in outer.deps if d[0] == name), None)
                if entry is None:
                    self.send_error(404)
                    return
                _, current, latest = entry
                packument = {
                    "name": name,
                    "dist-tags": {"latest": latest},
                    "versions": {},
                }
                for version in outer.versions.get(name, [current, latest]):
                    packument["versions"][version] = {
                        "name": name,
                        "version": version,
                        "dist": {"tarball": f"http://127.0.0.1:{outer.port}/{name}-{version}.tgz"},
                    }
                body = json.dumps(packument).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: N802
                pass

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


class DraftDeadlineUnitTests(unittest.TestCase):
    """C2: pure scaling contract for the auto-scaled default deadline."""

    def test_scale_draft_deadline_grows_with_package_count(self):
        # The production repro workload (76 deps) must not stay at
        # the 15s floor that expired during the first metadata body reads.
        self.assertEqual(roadmap.scale_draft_deadline_seconds(76), 15.0 + 1.5 * 76)
        self.assertEqual(roadmap.scale_draft_deadline_seconds(3), 15.0 + 1.5 * 3)
        self.assertEqual(roadmap.scale_draft_deadline_seconds(2), 15.0 + 1.5 * 2)
        self.assertEqual(roadmap.scale_draft_deadline_seconds(0), 15.0)

    def test_scale_draft_deadline_is_floor_and_cap_bounded(self):
        self.assertEqual(roadmap.scale_draft_deadline_seconds(-7), 15.0)
        huge = roadmap.scale_draft_deadline_seconds(100_000)
        self.assertLessEqual(huge, roadmap.DRAFT_DEADLINE_MAX_SECONDS)
        self.assertGreaterEqual(huge, 15.0)

    def test_scale_draft_deadline_respects_explicit_curve(self):
        self.assertEqual(roadmap.scale_draft_deadline_seconds(10, base=5.0, per_package=0.5, max_seconds=20.0), 10.0)
        self.assertEqual(roadmap.scale_draft_deadline_seconds(100, base=5.0, per_package=0.5, max_seconds=20.0), 20.0)

    def test_deadline_clock_reports_extended_deadline_after_scale(self):
        # In-place extension (inventory happened before scaling) must make both
        # the reported deadlineSeconds and remaining reflect the scaled budget.
        clock = roadmap.DeadlineClock(15.0)
        clock.started = time.monotonic() - 2.0
        clock.deadline_seconds = roadmap.scale_draft_deadline_seconds(3)
        self.assertEqual(clock.as_dict()["deadlineSeconds"], 15.0 + 1.5 * 3)
        self.assertGreater(clock.remaining, (15.0 + 1.5 * 3) - 2.0 - 0.5)


class DraftDeadlineIntegrationTests(unittest.TestCase):
    """C2: the running generator (real subprocess) must pin the scaled default
    in the manifest and still honor an explicit deadline verbatim."""

    DEPS = [("uuid", "9.0.0", "10.0.0"), ("is-number", "6.0.0", "7.0.0"), ("clsx", "1.0.0", "2.0.0")]

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="deploom-r10-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def _fixture(self, name: str, deps, registry: str) -> Path:
        ws = self._tmp / name
        _write(ws / ".dependency-roadmap" / "settings.project.json",
               json.dumps({"registry": registry,
                           "projects": [{"name": name, "path": ".", "sourceBranch": "main"}]}))
        package_deps = {pkg: f"^{current}" for pkg, current, _latest in deps}
        lock_packages = {
            "": {"name": name, "version": "1.0.0", "dependencies": package_deps},
        }
        for pkg, current, _latest in deps:
            lock_packages[f"node_modules/{pkg}"] = {"version": current}
        lock = {"name": name, "version": "1.0.0", "lockfileVersion": 3, "requires": True, "packages": lock_packages}
        _write(ws / "package.json", json.dumps({"name": name, "version": "1.0.0", "dependencies": package_deps}, indent=2))
        _write(ws / "package-lock.json", json.dumps(lock, indent=2))
        return ws

    def _run(self, ws: Path, project: str, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        run_env = os.environ.copy()
        run_env.pop("DEPLOOM_DRAFT_DEADLINE_SECONDS", None)
        run_env["PYTHONIOENCODING"] = "utf-8"
        cmd = [
            sys.executable, str(GENERATOR),
            "--project-settings", str(ws / ".dependency-roadmap" / "settings.project.json"),
            "--only-project", project, "--draft-baseline",
            "--artifacts-dir", str(ws / ".dependency-roadmap" / "artifacts"),
            *args,
        ]
        return subprocess.run(cmd, cwd=str(ws), env=run_env, capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout)

    def _manifest(self, ws: Path, run_id: str):
        return _read_json(ws / ".dependency-roadmap" / "artifacts" / "runs" / run_id / "draft" / "result.json")

    def test_default_deadline_scales_with_package_count_even_when_registry_down(self):
        # Registry on a guaranteed-closed port: the run must still publish
        # (honest unknown rows) and REPORT the scaled default deadline.
        ws = self._fixture("scale-down", self.DEPS, registry="http://127.0.0.1:1")
        result = self._run(ws, "scale-down",
                           ["--run-id", "run-r10-down", "--workspace-id", "ws-down", "--project-id", "scale-down", "--mode", "draft"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = self._manifest(ws, "run-r10-down")
        self.assertAlmostEqual(manifest["deadline"]["deadlineSeconds"],
                               roadmap.scale_draft_deadline_seconds(len(self.DEPS)), places=6,
                               msg=result.stderr)
        self.assertIn("auto-scaled", result.stderr, result.stderr)

    def test_explicit_deadline_is_never_rescaled(self):
        ws = self._fixture("scale-explicit", self.DEPS, registry="http://127.0.0.1:1")
        result = self._run(ws, "scale-explicit",
                           ["--run-id", "run-r10-explicit", "--workspace-id", "ws-exp", "--project-id", "scale-explicit",
                            "--mode", "draft", "--draft-deadline-seconds", "7.5"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = self._manifest(ws, "run-r10-explicit")
        self.assertAlmostEqual(manifest["deadline"]["deadlineSeconds"], 7.5, places=6, msg=result.stderr)
        self.assertNotIn("auto-scaled", result.stderr, result.stderr)

    def test_draft_with_reachable_registry_fetches_metadata_with_scaled_deadline(self):
        # The user-facing requirement: with Nexus reachable the Draft must
        # actually read the metadata (not die inside the first body read and
        # report the whole set as "registry unavailable").
        versions = {name: [current, latest] for name, current, latest in self.DEPS}
        with _MockRegistry(self.DEPS, versions) as reg:
            ws = self._fixture("scale-online", self.DEPS, registry=reg.url)
            result = self._run(ws, "scale-online",
                               ["--run-id", "run-r10-online", "--workspace-id", "ws-on", "--project-id", "scale-online", "--mode", "draft"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = self._manifest(ws, "run-r10-online")
        self.assertAlmostEqual(manifest["deadline"]["deadlineSeconds"],
                               roadmap.scale_draft_deadline_seconds(len(self.DEPS)), places=6,
                               msg=result.stderr)
        self.assertIn(manifest["status"], ("DRAFT_READY", "DRAFT_PARTIAL"), result.stderr)
        self.assertEqual(manifest["metadata"]["metadataTotal"], len(self.DEPS))
        self.assertEqual(manifest["metadata"]["metadataKnown"], len(self.DEPS),
                         "reachable registry must populate metadata; got unknown=%s" % manifest["metadata"].get("unknown"))
        self.assertIn("auto-scaled", result.stderr, result.stderr)


if __name__ == "__main__":
    unittest.main()
