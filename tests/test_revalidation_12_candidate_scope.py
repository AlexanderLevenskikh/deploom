"""Revalidation #12 (R12): candidate-scope truncation must not hide reachable
lag targets, and an explicit search limit must be reported honestly as
`candidate-search-truncated` instead of `no-target`.

Production repro (v0.2.129, 77-dependency project): the Draft completed
77/77 and issued DRAFT_READY, but the yellow plan reached only 21/76
(27.6%) lag-OK against an 80% gate (61/76 required, 65 with the +5% planning
reserve) -- 53 of 63 lag blockers were `no-target`. Root cause: Draft's
default `draft-max-candidates=3` truncated the candidate list to the first
three versions AT OR ABOVE the installed version BEFORE `min_by_lag` ran, so
an old dependency whose first three versions are equally old was reported as
having "no installable target in the configured registry" even though fresh
versions existed in the same packument (metadata the run itself had already
read). This file keeps that regression pinned and adds the honest
`candidate-search-truncated` reason for deliberately limited searches.
"""

from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
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


def _mk_meta(versions, times):
    return {
        "name": "pkg",
        "dist-tags": {"latest": versions[-1]},
        "versions": {v: {"name": "pkg", "version": v} for v in versions},
        "time": dict(times),
    }


class CandidateScopeTruncationUnitTests(unittest.TestCase):
    """Pure-function reproduction of the truncation defect (R12)."""

    VERSIONS = [f"1.0.{i}" for i in range(11)]  # 1.0.0 .. 1.0.10
    TIMES = {**{v: "2023-05-01T00:00:00Z" for v in ["1.0.0", "1.0.1", "1.0.2", "1.0.3", "1.0.4", "1.0.5", "1.0.6"]},
             **{v: "2026-08-01T00:00:00Z" for v in ["1.0.7", "1.0.8", "1.0.9", "1.0.10"]}}

    def test_min_by_lag_cap_hides_fresh_version_while_full_list_finds_it(self):
        meta = _mk_meta(self.VERSIONS, self.TIMES)
        capped, note = roadmap.versions_from_current(meta, "1.0.0", False, 3)
        self.assertEqual(capped, ["1.0.0", "1.0.1", "1.0.2"])
        self.assertTrue(note)
        blocked = roadmap.min_by_lag(meta, capped, 12, is_available=lambda v: True)
        self.assertNotIn("1.0.", blocked, "the capped list must hide every fresh version")
        full, _note = roadmap.versions_from_current(meta, "1.0.0", False, 0)
        found = roadmap.min_by_lag(meta, full, 12, is_available=lambda v: True)
        self.assertIn("1.0.7", found, "the full local metadata range must find the fresh target")


class _MockRegistry:
    """Hermetic registry + OSV mock.

    ``vuln_by_version`` maps a package version to a CRITICAL OSV finding (used
    to prove that versions beyond the covered candidate set are NEVER assumed
    clean and that a truncated security search is reported honestly).
    """

    def __init__(self, deps, versions, times=None, vuln_by_version=None):
        self.deps = deps
        self.versions = versions
        self.times = times or {}
        self.vuln_by_version = vuln_by_version or {}

    def __enter__(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/osv/vulns/"):
                    body = json.dumps({
                        "id": self.path.rsplit("/", 1)[-1],
                        "database_specific": {"severity": "CRITICAL"},
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                name = self.path.lstrip("/").split("%2F")[0].split("?")[0]
                entry = next((d for d in outer.deps if d[0] == name), None)
                if entry is None:
                    self.send_error(404)
                    return
                if self.path.endswith(".tgz"):
                    _pkg, _cur, latest = entry
                    body = json.dumps({"name": name, "version": latest}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
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
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                if not self.path.startswith("/osv/querybatch"):
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                queries = payload.get("queries") or []
                results = []
                for q in queries:
                    ver = (q.get("version") or "") if isinstance(q, dict) else ""
                    pkg = ((q.get("package") or {}).get("name") or "") if isinstance(q, dict) else ""
                    if ver in outer.vuln_by_version.get(pkg, set()):
                        results.append({"vulns": [{"id": "OSV-CRIT-1"}]})
                    else:
                        results.append({"vulns": []})
                body = json.dumps({"results": results}).encode("utf-8")
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


class DraftCandidateScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="deploom-r12-"))
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

    def _plan(self, ws: Path, run_id: str):
        return _read_json(ws / ".dependency-roadmap" / "artifacts" / "runs" / run_id / "draft" / "plan.json")

    def _prompt(self, ws: Path, run_id: str) -> str:
        return (ws / ".dependency-roadmap" / "artifacts" / "runs" / run_id / "draft" / "prompt.md").read_text(encoding="utf-8")

    def test_draft_default_finds_theoretical_lag_target_beyond_first_three(self):
        """Default settings must NOT hide a lag target that exists in the
        locally read packument beyond the first three versions (R12)."""
        versions = [f"1.0.{i}" for i in range(11)]
        times = {"lag-pkg": {**{v: "2023-05-01T00:00:00Z" for v in versions if int(v.split(".")[-1]) <= 6},
                             **{v: "2026-08-01T00:00:00Z" for v in versions if int(v.split(".")[-1]) > 6}}}
        deps = [("lag-pkg", "1.0.0", "1.0.10")]
        with _MockRegistry(deps, {"lag-pkg": versions}, times=times) as reg:
            ws = self._fixture("lag-default", deps, registry=reg.url)
            result = self._run(ws, "lag-default",
                               ["--run-id", "run-r12-lag", "--workspace-id", "ws-r12", "--project-id", "lag-default",
                                "--mode", "draft", "--draft-deadline-seconds", "30"],
                               osv_base=reg.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = self._plan(ws, "run-r12-lag")
        row = next(r for r in plan["proposals"][0]["rows"] if r["package"] == "lag-pkg")
        self.assertEqual(row["status"], "proposed",
                         "a fresh version beyond the first three must produce a theoretical target; stderr:\n" + result.stderr)
        self.assertTrue(row["target"] and "1.0.7" in str(row["target"]), row)
        self.assertEqual(plan["counts"]["proposed"], 1, plan["counts"])
        self.assertEqual(plan["counts"]["no-target"], 0, plan["counts"])
        self.assertEqual(plan["scan"]["lag-default"]["processed"], 1, plan["scan"])

    def test_explicit_candidate_limit_marks_candidate_search_truncated(self):
        """An explicitly bounded candidate search whose safe version was not
        investigated reports `candidate-search-truncated`, never `no-target`
        or a proven absence of an installable target (R12)."""
        versions = [f"1.0.{i}" for i in range(11)]
        times = {"crit-pkg": {v: "2026-08-01T00:00:00Z" for v in versions}}
        # current 1.0.4 is fresh (lag-OK) but CRITICAL; the only safe versions
        # (1.0.7+) sit beyond the explicitly limited covered set.
        crit = {v for i, v in enumerate(versions) if i <= 6 or i == 10}
        deps = [("crit-pkg", "1.0.4", "1.0.10")]
        with _MockRegistry(deps, {"crit-pkg": versions}, times=times,
                           vuln_by_version={"crit-pkg": crit}) as reg:
            ws = self._fixture("crit-lim", deps, registry=reg.url)
            result = self._run(ws, "crit-lim",
                               ["--run-id", "run-r12-trunc", "--workspace-id", "ws-r12", "--project-id", "crit-lim",
                                "--mode", "draft", "--draft-deadline-seconds", "30",
                                "--draft-max-candidates", "3"],
                               osv_base=reg.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = self._plan(ws, "run-r12-trunc")
        row = next(r for r in plan["proposals"][0]["rows"] if r["package"] == "crit-pkg")
        self.assertNotEqual(row["status"], "no-target",
                            "a bounded search can never claim a proven absence of a safe target; stderr:\n" + result.stderr)
        self.assertEqual(row["status"], "candidate-search-truncated",
                         "status must reflect the explicit candidate limit; stderr:\n" + result.stderr)
        self.assertEqual(plan["counts"]["candidate-search-truncated"], 1, plan["counts"])
        self.assertEqual(plan["counts"]["no-target"], 0, plan["counts"])
        unknown_reasons = {u["package"]: u["reason"] for u in plan["unknowns"]}
        self.assertIn("candidate search", unknown_reasons.get("crit-pkg", ""), unknown_reasons)

    def test_full_range_lag_search_closes_all_blockers_with_zero_shortfall(self):
        """End-to-end consistency: with the default candidate limit the full
        search finds every lag blocker, the published proposed count and the
        post-plan lag-OK agree, and the yellow shortfall is 0."""
        versions = [f"1.0.{i}" for i in range(11)]
        old = {v for i, v in enumerate(versions) if i <= 6}
        fresh = {v for i, v in enumerate(versions) if i > 6}
        times = {dep: {**{v: "2023-05-01T00:00:00Z" for v in old},
                       **{v: "2026-08-01T00:00:00Z" for v in fresh}}
                 for dep in ("klag-a", "klag-b", "klag-c")}
        deps = [("klag-a", "1.0.0", "1.0.10"), ("klag-b", "1.0.0", "1.0.10"), ("klag-c", "1.0.0", "1.0.10")]
        versions_map = {dep: versions for dep, _c, _l in deps}
        with _MockRegistry(deps, versions_map, times=times) as reg:
            ws = self._fixture("lag-close", deps, registry=reg.url)
            result = self._run(ws, "lag-close",
                               ["--run-id", "run-r12-close", "--workspace-id", "ws-r12", "--project-id", "lag-close",
                                "--mode", "draft", "--draft-deadline-seconds", "30"],
                               osv_base=reg.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = self._plan(ws, "run-r12-close")
        rows = {r["package"]: r for r in plan["proposals"][0]["rows"]}
        self.assertEqual({r["status"] for r in plan["proposals"][0]["rows"]}, {"proposed"},
                         "every lag blocker must get a full-range theoretical target")
        for pkg in ("klag-a", "klag-b", "klag-c"):
            self.assertIn("1.0.7", str(rows[pkg]["target"]), rows[pkg])
        self.assertEqual(plan["counts"]["proposed"], 3, plan["counts"])
        self.assertEqual(plan["counts"]["no-target"], 0, plan["counts"])
        self.assertEqual(plan["counts"]["candidate-search-truncated"], 0, plan["counts"])
        health = plan["proposals"][0]["health"]
        self.assertEqual(health["postPlanLagOk"], 3, health)
        self.assertEqual(health["postPlanShortfall"], 0, health)
        self.assertEqual(plan["scan"]["lag-close"]["processed"], 3, plan["scan"])
        prompt = self._prompt(ws, "run-r12-close")
        self.assertIn("shortfall: 0", prompt)

    def test_truncated_security_search_reports_critical_blocker_with_evidence(self):
        """A security safety target hidden by an explicit limit is a blocker
        with evidence in the unknowns and the health Critical counter, never a
        silent claim that the plan is clean."""
        versions = [f"1.0.{i}" for i in range(11)]
        times = {
            "lag-ok-pkg": {v: "2026-08-01T00:00:00Z" for v in versions},
            "crit-pkg2": {v: "2026-08-01T00:00:00Z" for v in versions},
        }
        # lag-ok-pkg: fresh current, no lag issue (uses the lag path only).
        # crit-pkg2: current fresh (lag-OK) but CRITICAL; safe versions live
        # at positions >= 7, beyond the explicitly limited covered network.
        crit = {v for i, v in enumerate(versions) if i <= 6 or i == 10}
        deps = [("lag-ok-pkg", "1.0.4", "1.0.10"), ("crit-pkg2", "1.0.4", "1.0.10")]
        versions_map = {dep: versions for dep, _c, _l in deps}
        with _MockRegistry(deps, versions_map, times=times,
                           vuln_by_version={"crit-pkg2": crit}) as reg:
            ws = self._fixture("lag-mixed", deps, registry=reg.url)
            result = self._run(ws, "lag-mixed",
                               ["--run-id", "run-r12-mixed", "--workspace-id", "ws-r12", "--project-id", "lag-mixed",
                                "--mode", "draft", "--draft-deadline-seconds", "30",
                                "--draft-max-candidates", "3"],
                               osv_base=reg.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = self._plan(ws, "run-r12-mixed")
        rows = {r["package"]: r for r in plan["proposals"][0]["rows"]}
        # crit-pkg2 is fresh so it needs no lag update -- but its safe version
        # is hidden by the limit; lag-ok-pkg is healthy (ok in R12 taxonomy).
        self.assertEqual(rows["crit-pkg2"]["status"], "candidate-search-truncated", rows["crit-pkg2"])
        self.assertIn("candidate search", rows["crit-pkg2"]["reason"], rows["crit-pkg2"])
        self.assertEqual(plan["counts"]["candidate-search-truncated"], 1, plan["counts"])
        health = plan["proposals"][0]["health"]
        # The lag goal is fully met (both rows are already fresh), so the
        # remaining blocker is the SECURITY dimension: Critical stays open and
        # its safe target was never proven because the search was truncated.
        self.assertGreaterEqual(int(health.get("critical") or 0), 1, health)
        self.assertEqual(health["postPlanLagOk"], 2, health)
        unknown_reasons = {u["package"]: u["reason"] for u in plan["unknowns"]}
        self.assertIn("candidate search", unknown_reasons.get("crit-pkg2", ""), unknown_reasons)


if __name__ == "__main__":
    unittest.main()
