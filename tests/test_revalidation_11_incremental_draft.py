"""Revalidation #11 (R11): incremental atomic Draft publication + honest partiality.

Production repro (v0.2.128, 77-dependency project): a deadline-aborted Draft
published metadataKnown=0/76 and marked EVERY package "registry unavailable"
even though 11 packages had already been fully enriched (registry metadata +
OSV) before the run expired on package #12. analyze_project returned its rows
list only after the whole project was processed, so at publication time only
the pre-network inventory (all rows "unknown") existed.

The fixes under test:
1. Rows are committed to the project Draft snapshot as each dependency
   completes (whole-row replacement keyed by package/kind), so a mid-scan
   abort publishes completed rows + inventory for the rest.
2. Unknowns are reasoned per row: not-attempted / interrupted / registry
   request failed / OSV unavailable / unrated — "registry unavailable" is
   never claimed for a package the scan did not reach.
3. Draft targets are theoretical registry-metadata candidates (PLANNING_ONLY)
   and reach plan.json even when the greedy planner never ran (abort path).
4. The multi-MB registry JSON body is read in fast chunks under the absolute
   supervisor cutoff (no per-byte loop) while trickle aborts still fire.
"""

from __future__ import annotations

import hashlib
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

import dependency_live_roadmap_generator as roadmap  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _mini_tarball(name: str, version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        payload = json.dumps({"name": name, "version": version}).encode("utf-8")
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _MockRegistry:
    """Hermetic npm-registry + OSV mock.

    ``hang`` is a package whose packument response dribbles a body forever
    (headers arrive instantly) so the deadline fires inside the body read.
    OSV endpoints (/osv/querybatch POST, /osv/vulns/{id} GET) always answer
    "no known vulnerabilities" so the tests do not touch api.osv.dev.
    """

    def __init__(self, deps, versions, times=None, hang=None, big_file: int = 0):
        self.deps = deps  # list[(name, current, latest)]
        self.versions = versions  # name -> [v1, v2, ...]
        self.times = times or {}  # name -> {version: iso}
        self.hang = hang
        self.big_file = big_file  # approximate packument size in bytes
        self.requests: list[str] = []

    def __enter__(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append(self.path)
                if self.path.startswith("/osv/"):
                    self.send_error(404)
                    return
                name = self.path.lstrip("/").split("%2F")[0].split("?")[0]
                if name == outer.hang:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "64000000")
                    self.end_headers()
                    try:
                        while True:
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.1)
                    except OSError:
                        return
                if self.path.endswith(".tgz"):
                    for pkg, _current, latest in outer.deps:
                        if self.path == f"/{pkg}-{latest}.tgz":
                            body = _mini_tarball(pkg, latest)
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
                entry = next((d for d in outer.deps if d[0] == name), None)
                if entry is None:
                    self.send_error(404)
                    return
                _pkg, current, latest = entry
                packument = {
                    "name": name,
                    "dist-tags": {"latest": latest},
                    "versions": {},
                    "time": dict(outer.times.get(name) or {}),
                }
                for version in outer.versions.get(name, [current, latest]):
                    packument["versions"][version] = {
                        "name": name,
                        "version": version,
                        "dist": {"tarball": f"http://127.0.0.1:{outer.port}/{name}-{version}.tgz"},
                    }
                body = json.dumps(packument).encode("utf-8")
                if outer.big_file and len(body) < outer.big_file:
                    filler = {"_bloat": "x" * (outer.big_file - len(body))}
                    body = json.dumps({**packument, **filler}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                outer.requests.append(self.path)
                if not self.path.startswith("/osv/querybatch"):
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                queries = (payload.get("queries") or [])
                body = json.dumps({"results": [{"vulns": []} for _ in queries]}).encode("utf-8")
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


class DraftIncrementalPublishTests(unittest.TestCase):
    DEPS = [
        ("fast-a", "9.0.0", "10.0.0"),
        ("fast-b", "6.0.0", "7.0.0"),
        ("fast-c", "1.0.0", "2.0.0"),
        ("slow-hanging", "1.0.0", "2.0.0"),
        ("zz-never-started", "1.0.0", "2.0.0"),
    ]
    TIMES = {name: {current: "2025-01-01T00:00:00Z", latest: "2026-09-01T00:00:00Z"} for name, current, latest in DEPS}

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="deploom-r11-"))
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

    def _run(self, ws: Path, project: str, args: list[str], osv_base: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        run_env = os.environ.copy()
        run_env.pop("DEPLOOM_DRAFT_DEADLINE_SECONDS", None)
        run_env["PYTHONIOENCODING"] = "utf-8"
        run_env["DEPLOOM_OSV_QUERY_BATCH"] = f"{osv_base}/osv/querybatch"
        run_env["DEPLOOM_OSV_VULN"] = f"{osv_base}/osv/vulns/{{id}}"
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

    def test_deadline_abort_publishes_completed_rows_with_distinct_reasons(self):
        """Before R11 this scenario published metadataKnown=0 for all packages.

        Three fast packages are fully enriched, the fourth hangs mid-body so
        the explicit 6s deadline fires during its network read, and the fifth
        is never reached. The published plan must keep the three completed
        rows (with their theoretical PLANNING_ONLY targets), mark the hanging
        package as interrupted and the untouched one as not-attempted, and
        never claim the reachable registry was unavailable for work that was
        simply not done.
        """
        versions = {name: [current, latest] for name, current, latest in self.DEPS}
        with _MockRegistry(self.DEPS, versions, times=self.TIMES, hang="slow-hanging") as reg:
            ws = self._fixture("inc-abort", self.DEPS, registry=reg.url)
            result = self._run(ws, "inc-abort",
                               ["--run-id", "run-r11-inc", "--workspace-id", "ws-r11", "--project-id", "inc-abort",
                                "--mode", "draft", "--draft-deadline-seconds", "6"],
                               osv_base=reg.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = self._manifest(ws, "run-r11-inc")
        self.assertEqual(manifest["status"], "DRAFT_PARTIAL", result.stderr)
        meta = manifest["metadata"]
        self.assertEqual(meta["metadataTotal"], len(self.DEPS))
        self.assertEqual(meta["metadataKnown"], 3,
                         "completed packages must survive a deadline abort; stderr:\n" + result.stderr)
        self.assertEqual(meta["processed"], 3, result.stderr)
        self.assertEqual(meta["processedTotal"], len(self.DEPS), result.stderr)
        self.assertEqual(meta["pending"], 1, result.stderr)
        self.assertEqual(meta["interrupted"], 1, result.stderr)

        plan = _read_json(ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-r11-inc" / "draft" / "plan.json")
        rows = {r["package"]: r for r in plan["proposals"][0]["rows"]}
        self.assertEqual(rows["fast-a"]["latest"], "10.0.0")
        self.assertEqual(rows["fast-a"]["scanState"], "done")
        self.assertEqual(rows["fast-a"]["status"], "proposed", "theoretical PLANNING_ONLY target must reach plan.json")
        self.assertEqual(rows["fast-a"]["target"], "10.0.0")
        self.assertEqual(rows["fast-b"]["status"], "proposed")
        self.assertEqual(rows["fast-c"]["status"], "proposed")
        self.assertEqual(rows["slow-hanging"]["scanState"], "attempted")
        self.assertEqual(rows["slow-hanging"]["latest"], "registry unavailable")
        self.assertEqual(rows["zz-never-started"]["scanState"], "pending")
        self.assertEqual(rows["zz-never-started"]["latest"], "registry unavailable")

        reasons = {u["package"]: u["reason"] for u in plan["unknowns"]}
        self.assertIn("interrupted", reasons["slow-hanging"], reasons)
        self.assertIn("not attempted", reasons["zz-never-started"], reasons)
        self.assertNotEqual(reasons["slow-hanging"], reasons["zz-never-started"])
        self.assertEqual(plan["counts"]["not-attempted"], 1, plan["counts"])
        self.assertEqual(plan["counts"]["interrupted"], 1, plan["counts"])
        self.assertEqual(plan["scan"]["inc-abort"]["processed"], 3, plan["scan"])

        prompt = (ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-r11-inc" / "draft" / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("Обработано 3/5", prompt)
        self.assertIn("не начаты: 1", prompt)
        self.assertIn("прерваны deadline: 1", prompt)

        # The late in-flight reader must never mutate the published snapshot:
        # let it fade, then re-hash the atomically written artifacts.
        time.sleep(1.2)
        plan_path = ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-r11-inc" / "draft" / "plan.json"
        hashes = set()
        for _ in range(3):
            hashes.add(hashlib.sha256(plan_path.read_bytes()).hexdigest())
            time.sleep(0.3)
        self.assertEqual(len(hashes), 1, "published plan.json must stay stable after the abort")

    def test_large_registry_response_reads_within_budget_after_chunked_read(self):
        """A multi-MB packument must be read fast (no per-byte loop) and the
        run finishes within the explicit deadline (R11 T4 regression guard)."""
        deps = [("big-lib", "1.0.0", "2.0.0")]
        versions = {"big-lib": ["1.0.0", "1.1.0", "1.2.0", "2.0.0"]}
        times = {"big-lib": {"1.0.0": "2025-01-01T00:00:00Z", "2.0.0": "2026-09-01T00:00:00Z"}}
        with _MockRegistry(deps, versions, times=times, big_file=2_500_000) as reg:
            ws = self._fixture("big-lib", deps, registry=reg.url)
            result = self._run(ws, "big-lib",
                               ["--run-id", "run-r11-big", "--workspace-id", "ws-big", "--project-id", "big-lib",
                                "--mode", "draft", "--draft-deadline-seconds", "6"],
                               osv_base=reg.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = self._manifest(ws, "run-r11-big")
        self.assertEqual(manifest["metadata"]["metadataKnown"], 1, result.stderr)
        self.assertIn(manifest["status"], ("DRAFT_READY", "DRAFT_PARTIAL"), result.stderr)
        self.assertLess(manifest["elapsedMs"], 6000,
                        "chunked read must not blow the deadline on a multi-MB packument")


class DraftReadDeadlineSafetyTests(unittest.TestCase):
    """T4: the chunked body read still aborts on a slow trickle under the
    absolute supervisor cutoff (byte loop removed)."""

    def test_slow_body_trickle_is_aborted_by_the_deadline(self):
        class _Trickle:
            def __init__(self, url):
                self.url = url

        outer = {}

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = b"{" + b'"x": "' + b"1234567890" * 2 + b'"}'  # ~25 bytes
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for chunk in [body[i:i + 1] for i in range(len(body))]:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        time.sleep(0.12)  # ~3s natural transfer
                except OSError:
                    pass

            def log_message(self, *args):  # noqa: N802
                pass

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        registry_url = f"http://127.0.0.1:{server.server_address[1]}"

        client = roadmap.LiveDataClient(registry_url, timeout=30, batch_size=4, sleep_sec=0.0, use_system_proxy=False)
        clock = roadmap.DeadlineClock(2.0)
        client.set_deadline(clock)
        started = time.monotonic()
        with self.assertRaises(roadmap.DraftBudgetExceeded) as ctx:
            client.fetch_npm_metadata("trickle-pkg")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5, f"trickle aborted too late: {elapsed:.2f}s")
        self.assertEqual(ctx.exception.phase, "network-read")


class DraftFinalAssignmentPlanningOnlyTests(unittest.TestCase):
    """R11 acceptance guard for the FULL Draft path (not only the abort path):
    planning-only registry evidence must never look like a target drift.

    A full 77-dependency Draft originally crashed at the final assignment pass
    with FINAL_PROVEN_ASSIGNMENT_REGISTRY_DRIFT for every planned update,
    because enrich_registry_target_evidence rejected the "planning-only" probe
    that Draft intentionally substitutes for the physical tarball check.
    """

    def _client(self):
        client = roadmap.LiveDataClient(
            "https://registry.example.test", timeout=30, batch_size=4,
            sleep_sec=0.0, use_system_proxy=False,
        )
        client.set_draft_run("run-r11-final")
        return client

    def _meta(self):
        return {
            "name": "pkg-a",
            "dist-tags": {"latest": "2.0.0"},
            "versions": {"1.0.0": {"version": "1.0.0"}, "2.0.0": {"version": "2.0.0"}},
        }

    def _row(self, name="pkg-a", current="1.0.0", target="2.0.0"):
        row = roadmap.DependencyRow(
            project="proj", package_dir="/proj", name=name, kind="runtime",
            requested_spec="^1.0.0", current_version=current, current_source="lockfile",
            latest_version=target, current_vulns="none", min_no_critical=target,
            min_no_high=target, min_no_vuln=target, min_lag_12m=target,
            min_lag_9m=target, min_lag_6m=target, min_lag_3m=target,
            group=1, reason="test", notes="",
        )
        row.target_default = target
        row.target_yellow = target
        row.target_green = target
        return row

    def test_final_assignment_accepts_planning_only_in_draft(self):
        client = self._client()
        client.npm_cache["pkg-a"] = self._meta()
        row = self._row()
        row.registry_artifacts["2.0.0"] = client.registry_version_artifact("pkg-a", self._meta(), "2.0.0")
        rows_by_project = {"proj": [row]}
        # The strict final-assignment pass (allow_target_mutation=False) raised
        # FINAL_PROVEN_ASSIGNMENT_REGISTRY_DRIFT before the Draft-aware skip.
        roadmap.enrich_registry_target_evidence(rows_by_project, client, allow_target_mutation=False)
        self.assertEqual(row.registry_artifacts["2.0.0"]["status"], "planning-only")
        self.assertEqual(row.target_yellow, "2.0.0", "planning-only must never mutate the target away")
        # ...and the peer-assignment prover must accept the metadata candidate.
        self.assertTrue(
            roadmap._candidate_registry_installable(row, "2.0.0", client),
            "draft must accept registry-metadata candidates as installable",
        )
        self.assertEqual(
            roadmap.validate_final_peer_assignment(rows_by_project, client), [],
            "a full Draft with planned updates must not report registry drift",
        )


if __name__ == "__main__":
    unittest.main()
