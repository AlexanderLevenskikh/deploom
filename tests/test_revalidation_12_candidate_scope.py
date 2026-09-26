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
from unittest import mock

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


class DraftGoalHonestyTests(unittest.TestCase):
    """R12 follow-up audit (F1-F4): Draft goal numbers must be HONEST.

    F1: the policy gate (user's minLagOkPct: 61/76 at 80%) is never replaced by
    the +5 p.p. planning reserve (65/76); the two have separate required counts
    and separate shortfalls in plan.json/result.json/prompt.
    F2: the cross-project share is sum(projectedLagOk)/sum(scopeTotal), never a
    sum of per-project percentages; each project keeps its OWN numbers.
    F3: post-plan SECURITY is projected on the EXACT chosen/kept versions
    (Critical/High + goalFeasible/unknown/blocked); a target without OSV
    evidence is UNKNOWN, never clean.
    F4: a truncation CAVEAT on an actionable proposed row is NOT a
    clarification -- only targetless/unassessed rows enter "needs
    clarification" (plan.unknowns / manifest metadata.unknown).
    """

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="deploom-r12b-"))
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

    def _run(self, ws: Path, project: str, args: list[str], osv_base: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
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

    def _manifest(self, ws: Path, run_id: str):
        return _read_json(ws / ".dependency-roadmap" / "artifacts" / "runs" / run_id / "draft" / "result.json")

    def _prompt(self, ws: Path, run_id: str) -> str:
        return (ws / ".dependency-roadmap" / "artifacts" / "runs" / run_id / "draft" / "prompt.md").read_text(encoding="utf-8")

    def test_policy_gate_not_replaced_by_planning_reserve_on_76_rows(self):
        """F1: with 76 active rows, 80% gate = 61/76, +5 p.p. reserve = 65/76.
        The plan (74 projected lag-OK) satisfies the POLICY gate; the required
        numbers and shortfalls must stay separate and the prompt must label
        61/76 as the policy requirement and 65/76 as the reserve -- never sign
        the reserve as the policy percentage (65/76 (80%))."""
        base_versions = [f"1.0.{i}" for i in range(5)]
        fresh_times = {v: "2026-08-01T00:00:00Z" for v in base_versions}
        healthy = [(f"h-{i:02d}", "1.0.2", "1.0.4") for i in range(74)]
        stale = [("s-00", "1.0.0", "1.0.0"), ("s-01", "1.0.0", "1.0.0")]
        deps = healthy + stale
        versions_map = {pkg: list(base_versions) for pkg, _c, _l in deps}
        # The two stale rows live in a packument with no newer version at all:
        # no lag target can exist, so they are honest no-target rows (and never
        # report a truncated search -- the whole range is covered).
        versions_map["s-00"] = ["1.0.0"]
        versions_map["s-01"] = ["1.0.0"]
        times = {pkg: dict(fresh_times) for pkg, _c, _l in deps}
        # The stale rows carry NO publish dates at all: their latest version is
        # unknown, so no lag boundary can be computed and no theoretical target
        # exists -- they stay honest no-target rows.
        times["s-00"] = {}
        times["s-01"] = {}
        with _MockRegistry(deps, versions_map, times=times) as reg:
            ws = self._fixture("f1-76", deps, registry=reg.url)
            result = self._run(ws, "f1-76",
                               ["--run-id", "run-r12b-f1", "--workspace-id", "ws-r12b", "--project-id", "f1-76",
                                "--mode", "draft", "--draft-deadline-seconds", "150"],
                               osv_base=reg.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = self._plan(ws, "run-r12b-f1")
        health = plan["proposals"][0]["health"]
        self.assertEqual(health["postPlanScopeTotal"], 76, health)
        self.assertEqual(health["postPlanPolicyRequired"], 61, health)
        self.assertEqual(health["postPlanReserveRequired"], 65, health)
        self.assertEqual(health["postPlanLagOk"], 74, health)
        self.assertEqual(health["postPlanPolicyShortfall"], 0, health)
        self.assertEqual(health["postPlanReserveShortfall"], 0, health)
        self.assertEqual(plan["counts"]["no-target"], 2, plan["counts"])
        prompt = self._prompt(ws, "run-r12b-f1")
        self.assertIn("61/76 (80%)", prompt,
                      "the prompt must state the USER policy gate, never the reserve")
        self.assertNotIn("65/76 (80%)", prompt,
                         "the +5 p.p. reserve must never be signed as the policy percentage")
        self.assertIn("плановый запас", prompt)
        self.assertIn("97.4", prompt)
        self.assertIn("shortfall: 0", prompt)
        manifest = self._manifest(ws, "run-r12b-f1")
        meta = manifest["metadata"]
        self.assertEqual(meta["postPlanPolicyRequired"], 61, meta)
        self.assertEqual(meta["postPlanReserveRequired"], 65, meta)
        self.assertEqual(meta["postPlanLagOk"], 74, meta)
        self.assertEqual(meta["postPlanScopeTotal"], 76, meta)
        self.assertEqual(meta["postPlanLagOkPct"], 97.4, meta)
        self.assertEqual(meta["postPlanPolicyShortfall"], 0, meta)
        self.assertEqual(meta["postPlanReserveShortfall"], 0, meta)
        self.assertEqual(meta["postPlanGoal"], "feasible", meta)
        self.assertEqual(manifest["perProject"]["f1-76"]["postPlanPolicyRequired"], 61, manifest["perProject"])

    def test_aggregate_share_uses_summed_denominator_not_sum_of_percentages(self):
        """F2: two projects with different sizes/policies -- the aggregate share
        is sum(projectedLagOk) / sum(scopeTotal), never the sum of the per-project
        percentages (8/10 + 25/50 = 33/60 = 55.0%, not 80 + 50 = 130)."""
        healths = [
            {"postPlanLagOk": 8, "postPlanScopeTotal": 10, "postPlanPolicyRequired": 8,
             "postPlanReserveRequired": 9, "postPlanPolicyShortfall": 0, "postPlanReserveShortfall": 0,
             "postPlanCritical": 0, "postPlanHigh": 0, "postPlanSecurityKnown": 10,
             "postPlanSecurityUnknown": 0, "postPlanSecurityTotal": 10, "postPlanGoal": "feasible",
             "postPlanTruncatedProposed": 0},
            {"postPlanLagOk": 25, "postPlanScopeTotal": 50, "postPlanPolicyRequired": 25,
             "postPlanReserveRequired": 28, "postPlanPolicyShortfall": 0, "postPlanReserveShortfall": 0,
             "postPlanCritical": 0, "postPlanHigh": 0, "postPlanSecurityKnown": 50,
             "postPlanSecurityUnknown": 0, "postPlanSecurityTotal": 50, "postPlanGoal": "feasible",
             "postPlanTruncatedProposed": 0},
        ]
        aggregate = roadmap.draft_post_plan_aggregate(healths)
        self.assertEqual(aggregate["postPlanLagOk"], 33)
        self.assertEqual(aggregate["postPlanScopeTotal"], 60)
        self.assertEqual(aggregate["postPlanLagOkPct"], 55.0,
                         "the aggregate must be the weighted share, not a sum of percentages")

    @staticmethod
    def _synthetic_row(name, current="1.0.0", latest="1.0.9", vulns="0", min_lag="1.0.9",
                       target_yellow="1.0.9", evidence=None, truncated=False):
        row = roadmap.DependencyRow(
            project="proj", package_dir="proj", name=name, kind="runtime",
            requested_spec="^1.0.0", current_version=current, current_source="package-lock.json",
            latest_version=latest, current_vulns=vulns,
            min_no_critical="1.0.9", min_no_high="1.0.9", min_no_vuln="1.0.9",
            min_lag_12m=min_lag, min_lag_9m=min_lag, min_lag_6m=min_lag, min_lag_3m=min_lag,
            group=0, reason="", notes="", draft_scan_state="done",
            candidate_search_truncated=truncated,
        )
        row.target_yellow = target_yellow
        row.target_green = target_yellow
        row.target_default = target_yellow
        row.vuln_evidence_by_version = dict(evidence or {})
        return row

    def _plan_for(self, rows):
        health = roadmap.compute_project_health(rows, "proj")
        spec = roadmap.ProjectSpec(name="proj", path=Path("proj"))
        return roadmap.build_draft_plan(
            {"proj": rows}, {"proj": spec}, {"proj": health}
        )

    def test_projected_security_shows_target_fixes_current_critical(self):
        """F3: a current Critical eliminated by the exact chosen target must
        project Critical=0 and keep the goal FEASIBLE."""
        row = self._synthetic_row("fixme", current="1.0.0", vulns="C:1",
                                  evidence={"1.0.9": "0"})
        plan = self._plan_for([row])
        health = plan["proposals"][0]["health"]
        self.assertEqual(plan["proposals"][0]["rows"][0]["status"], "proposed", plan)
        self.assertEqual(health["critical"], 1, health)
        self.assertEqual(health["postPlanCritical"], 0, health)
        self.assertEqual(health["postPlanHigh"], 0, health)
        self.assertEqual(health["postPlanGoal"], "feasible", health)

    def test_target_reintroducing_critical_blocks_the_goal(self):
        """F3: a chosen target whose EXACT version again carries a Critical must
        project Critical=1 and mark the goal BLOCKED -- a proposed target never
        implies the C/H is fixed."""
        row = self._synthetic_row("recrit", current="1.0.0", vulns="0",
                                  evidence={"1.0.9": "C:1"})
        plan = self._plan_for([row])
        health = plan["proposals"][0]["health"]
        self.assertEqual(health["critical"], 0, health)
        self.assertEqual(health["postPlanCritical"], 1, health)
        self.assertEqual(health["postPlanGoal"], "blocked", health)

    def test_missing_osv_for_exact_target_is_unknown_not_safe(self):
        """F3: a target without OSV evidence is coverage-UNKNOWN (never clean),
        so the goal is UNKNOWN, not feasible."""
        row = self._synthetic_row("noosv", current="1.0.0", vulns="0", evidence={})
        plan = self._plan_for([row])
        health = plan["proposals"][0]["health"]
        self.assertEqual(health["postPlanSecurityUnknown"], 1, health)
        self.assertEqual(health["postPlanGoal"], "unknown", health)
        self.assertNotEqual(health["postPlanGoal"], "feasible", health)

    def test_r6_draft_counts_unique_package_names_not_findings(self):
        """R6: the Draft post-plan High projection is measured in unique
        PACKAGE NAMES. One package whose exact chosen version carries TWO High
        findings is ONE offender: at maxKnownHigh=1 the goal stays feasible.
        The old finding-summed counter reported 2 and blocked the plan."""
        row = self._synthetic_row("dup", current="1.0.0", vulns="C:0;H:2;M:0;L:0",
                                  evidence={"1.0.9": "C:0;H:2;M:0;L:0"})
        with mock.patch.dict(os.environ, {"DEPLOOM_ACCEPTANCE_POLICY_JSON": json.dumps(
            {"targetLevel": "yellow", "minLagOkPct": 80, "maxKnownCritical": 0, "maxKnownHigh": 1}
        )}):
            plan = self._plan_for([row])
        health = plan["proposals"][0]["health"]
        self.assertEqual(1, health["postPlanHigh"], health)
        self.assertEqual("feasible", health["postPlanGoal"], health)

    def test_policy_gate_counts_are_split_into_policy_and_reserve_shortfalls(self):
        """F1: when projected lag-OK sits between the policy gate and the
        reserve (62 >= 61 but < 65), the POLICY shortfall is 0 while the RESERVE
        shortfall is > 0 -- the old code reported a shortfall against the
        reserve as if it were the gate."""
        row = self._synthetic_row("mid", current="1.0.0", vulns="0",
                                  evidence={"1.0.9": "0"})
        plan = self._plan_for([row])
        health = plan["proposals"][0]["health"]
        self.assertEqual(health["postPlanPolicyRequired"], 1, health)
        self.assertEqual(health["postPlanReserveRequired"], 1, health)
        self.assertEqual(health["postPlanPolicyShortfall"], 0, health)
        self.assertEqual(health["postPlanReserveShortfall"], 0, health)

    def test_truncated_proposed_row_is_both_actionable_and_not_a_clarification(self):
        """F4: a truncation CAVEAT on a row with a concrete target and known
        security is NOT a 'needs clarification' state -- the row stays proposed,
        carries the caveat, and does NOT enter plan.unknowns / manifest.unknown.
        The counters separate it: candidateTruncated=1 (covered versions
        bounded), candidateTruncatedProposed=1 (still actionable),
        candidateTruncatedTargetless=0."""
        versions = [f"1.0.{i}" for i in range(11)]
        old = {v for i, v in enumerate(versions) if i <= 6}
        fresh = {v for i, v in enumerate(versions) if i > 6}
        times = {"f4-pkg": {**{v: "2023-05-01T00:00:00Z" for v in old},
                            **{v: "2026-08-01T00:00:00Z" for v in fresh}}}
        deps = [("f4-pkg", "1.0.0", "1.0.10")]
        with _MockRegistry(deps, {"f4-pkg": versions}, times=times) as reg:
            ws = self._fixture("f4-caveat", deps, registry=reg.url)
            result = self._run(ws, "f4-caveat",
                               ["--run-id", "run-r12b-f4", "--workspace-id", "ws-r12b", "--project-id", "f4-caveat",
                                "--mode", "draft", "--draft-deadline-seconds", "30",
                                "--draft-max-candidates", "3"],
                               osv_base=reg.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = self._plan(ws, "run-r12b-f4")
        rows = plan["proposals"][0]["rows"]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["status"], "proposed", row)
        self.assertTrue(row["candidateSearchTruncated"], row)
        self.assertIn("1.0.7", str(row["target"]), row)
        self.assertEqual(plan["unknowns"], [],
                         "an actionable proposed row with a truncation caveat is not a clarification")
        self.assertEqual(plan["proposals"][0]["health"]["postPlanTruncatedProposed"], 1, plan)
        manifest = self._manifest(ws, "run-r12b-f4")
        meta = manifest["metadata"]
        self.assertEqual(meta["candidateTruncated"], 1, meta)
        self.assertEqual(meta["candidateTruncatedTargetless"], 0, meta)
        self.assertEqual(meta["candidateTruncatedProposed"], 1, meta)
        self.assertEqual(meta["unknown"], 0, meta)
        prompt = self._prompt(ws, "run-r12b-f4")
        self.assertNotIn("`f4-pkg`", prompt.split("## Неизвестные/требуют уточнения у агента")[1]
                         if "## Неизвестные/требуют уточнения у агента" in prompt else "",
                         "the clarification section must not list an actionable proposed row")
        self.assertIn("Примечание: поиск кандидатов ограничен лимитом", prompt)

    # ---- F5: the post-plan goal must speak in the CHOSEN target level ----
    # Pre-fix the verdict always ran `_draft_goal_verdict(post_yellow,
    # yellow_required, ...)` so a targetLevel=green run got the YELLOW gate
    # (80% / maxKnownHigh=1) and could print "цель достижима" while the plan
    # never reached 100% lag / H=0. These tests pin the green gate: 100% of
    # the scope provably lag-OK, C=0/H=0 and the M/L limits of the green
    # status; incomplete OSV/lag data => unknown, never feasible.

    def _policy_ctx(self, policy):
        saved = os.environ.get("DEPLOOM_ACCEPTANCE_POLICY_JSON")
        if policy is None:
            os.environ.pop("DEPLOOM_ACCEPTANCE_POLICY_JSON", None)
        else:
            os.environ["DEPLOOM_ACCEPTANCE_POLICY_JSON"] = json.dumps(policy)

        def _restore():
            if saved is None:
                os.environ.pop("DEPLOOM_ACCEPTANCE_POLICY_JSON", None)
            else:
                os.environ["DEPLOOM_ACCEPTANCE_POLICY_JSON"] = saved

        self.addCleanup(_restore)

    def test_green_goal_uses_hundred_percent_gate_not_yellow(self):
        """F5 repro #1: targetLevel=green, 10 active, 8 projected lag-OK, C/H=0
        -> BLOCKED with required=10, projected=8, shortfall=2 (once the yellow
        default gate silently called the same plan feasible). The 2 non-lag-OK
        rows have no assignable target at all, so the green projection cannot
        count them."""
        self._policy_ctx({"targetLevel": "green"})
        rows = []
        for i in range(8):
            rows.append(self._synthetic_row(f"ok{i}", current="1.0.9", evidence={"1.0.9": "0"}))
        for i in range(2):
            lag = self._synthetic_row(f"lag{i}", current="1.0.0")
            lag.target_yellow = roadmap.NO_ACTION
            lag.target_green = roadmap.NO_ACTION
            lag.target_default = roadmap.NO_ACTION
            rows.append(lag)
        plan = self._plan_for(rows)
        health = plan["proposals"][0]["health"]
        self.assertEqual(health["postPlanTargetLevel"], "green", health)
        self.assertEqual(health["postPlanScopeTotal"], 10, health)
        self.assertEqual(health["postPlanLagOk"], 8, health)
        self.assertEqual(health["postPlanPolicyRequired"], 10, health)
        self.assertEqual(health["postPlanPolicyShortfall"], 2, health)
        self.assertEqual(health["postPlanShortfall"], 2, health)
        self.assertEqual(health["postPlanGoal"], "blocked", health)
        self.assertNotEqual(health["postPlanGoal"], "feasible", health)
        # the rows without a target are no-target, never projected lag-OK
        self.assertEqual([r["status"] for r in plan["proposals"][0]["rows"] if r["package"].startswith("lag")],
                         ["no-target", "no-target"], plan["proposals"][0]["rows"])
        # diagnostic variants keep the other level visible
        self.assertEqual(health["postPlanLagOkYellow"], 8, health)
        self.assertEqual(health["postPlanLagOkGreen"], 8, health)

    def test_green_goal_rejects_high_one_that_yellow_allows(self):
        """F5 repro #2: a 1/1 lag-OK scope with H:1 is BLOCKED for green (green
        requires H=0) even under the same configured maxKnownHigh=1 that makes
        the yellow verdict FEASIBLE."""
        self._policy_ctx({"targetLevel": "green", "maxKnownHigh": 1})
        row = self._synthetic_row("oneh", current="1.0.9", vulns="H:1",
                                  evidence={"1.0.9": "H:1"})
        plan = self._plan_for([row])
        health = plan["proposals"][0]["health"]
        self.assertEqual(health["postPlanLagOk"], 1, health)
        self.assertEqual(health["postPlanHigh"], 1, health)
        self.assertEqual(health["postPlanGoal"], "blocked", health)
        self._policy_ctx({"targetLevel": "yellow", "maxKnownHigh": 1})
        plan_y = self._plan_for([row])
        health_y = plan_y["proposals"][0]["health"]
        self.assertEqual(health_y["postPlanHigh"], 1, health_y)
        self.assertEqual(health_y["postPlanGoal"], "feasible", health_y)

    def test_green_goal_feasible_unknown_and_ml_blocked(self):
        """F5 severity gate: 100% lag + C/H=0 + full OSV => feasible; missing
        OSV evidence => UNKNOWN (never feasible); M beyond the green M/L limits
        => BLOCKED."""
        self._policy_ctx({"targetLevel": "green"})
        rows = [self._synthetic_row(f"g{i}", current="1.0.9", evidence={"1.0.9": "0"}) for i in range(6)]
        plan = self._plan_for(rows)
        health = plan["proposals"][0]["health"]
        self.assertEqual(health["postPlanLagOk"], 6, health)
        self.assertEqual(health["postPlanPolicyRequired"], 6, health)
        self.assertEqual(health["postPlanGoal"], "feasible", health)
        noosv = self._synthetic_row("noosvg", current="1.0.0", evidence={})
        plan_u = self._plan_for([noosv])
        health_u = plan_u["proposals"][0]["health"]
        self.assertEqual(health_u["postPlanGoal"], "unknown", health_u)
        self.assertNotEqual(health_u["postPlanGoal"], "feasible", health_u)
        ml = self._synthetic_row("mlowg", current="1.0.9", vulns="M:25",
                                 evidence={"1.0.9": "M:25"})
        # A green goal demands M/L = 0 by definition, so the M/L limits are
        # zero in the policy; one offender (however many findings it carries)
        # then blocks the goal.
        self._policy_ctx({"targetLevel": "green", "maxKnownModerate": 0, "maxKnownLow": 0})
        plan_m = self._plan_for([ml])
        health_m = plan_m["proposals"][0]["health"]
        # R6: the post-plan Moderate projection is measured in UNIQUE package
        # NAMES. One package carrying M:25 is ONE offender, so the projection
        # reports 1 -- the goal is still red/blocked for a green policy with
        # maxKnownModerate=0, but the counter is names, not findings.
        self.assertEqual(health_m["postPlanModerate"], 1, health_m)
        self.assertEqual(health_m["postPlanGoal"], "blocked", health_m)

    def test_yellow_and_green_pair_on_identical_rows(self):
        """F5: on identical rows the projection follows the CHOSEN level's
        `proposed` assignment -- the same 2 lagging rows get target 1.0.9 for
        yellow (lag-OK, projected) vs target 1.0.5 for green (not lag-OK), so
        postPlanLagOk is 10 vs 8 and the verdict/required flip with the level."""
        rows = []
        for i in range(8):
            rows.append(self._synthetic_row(f"ok{i}", current="1.0.9", evidence={"1.0.9": "0"}))
        for i in range(2):
            lag = self._synthetic_row(f"lag{i}", current="1.0.0", target_yellow="1.0.9",
                                      evidence={"1.0.5": "0", "1.0.9": "0"})
            lag.target_green = "1.0.5"
            lag.target_default = "1.0.9"
            rows.append(lag)
        self._policy_ctx({"targetLevel": "green"})
        plan_g = self._plan_for(rows)
        health_g = plan_g["proposals"][0]["health"]
        self.assertEqual(health_g["postPlanLagOk"], 8, health_g)
        self.assertEqual(health_g["postPlanPolicyRequired"], 10, health_g)
        self.assertEqual(health_g["postPlanGoal"], "blocked", health_g)
        lag_targets_g = [r["target"] for r in plan_g["proposals"][0]["rows"] if r["package"].startswith("lag")]
        self.assertEqual(lag_targets_g, ["1.0.5", "1.0.5"], plan_g["proposals"][0]["rows"])
        self.assertEqual([r["status"] for r in plan_g["proposals"][0]["rows"] if r["package"].startswith("lag")],
                         ["proposed", "proposed"], plan_g["proposals"][0]["rows"])
        self._policy_ctx({"targetLevel": "yellow"})
        plan_y = self._plan_for(rows)
        health_y = plan_y["proposals"][0]["health"]
        self.assertEqual(health_y["postPlanLagOk"], 10, health_y)
        self.assertEqual(health_y["postPlanPolicyRequired"], 8, health_y)
        self.assertEqual(health_y["postPlanGoal"], "feasible", health_y)
        lag_targets_y = [r["target"] for r in plan_y["proposals"][0]["rows"] if r["package"].startswith("lag")]
        self.assertEqual(lag_targets_y, ["1.0.9", "1.0.9"], plan_y["proposals"][0]["rows"])
        self.assertEqual([r["status"] for r in plan_y["proposals"][0]["rows"] if r["package"].startswith("lag")],
                         ["proposed", "proposed"], plan_y["proposals"][0]["rows"])

    def test_green_target_level_subprocess_uses_green_gate(self):
        """F5 integration: `--target-level green` runs the whole pipeline with
        the GREEN gate -- 100% required (76/76), the projection on the same
        assigned versions (74) and an honest verdict. The 2 rows without lag
        data are UNPROVABLE for green closure, so the verdict is UNKNOWN
        (never feaasible/yellow); the manifest, per-project view, RU prompt and
        summary all carry targetLevel=green."""
        base_versions = [f"1.0.{i}" for i in range(5)]
        fresh_times = {v: "2026-08-01T00:00:00Z" for v in base_versions}
        healthy = [(f"h-{i:02d}", "1.0.2", "1.0.4") for i in range(74)]
        stale = [("s-00", "1.0.0", "1.0.0"), ("s-01", "1.0.0", "1.0.0")]
        deps = healthy + stale
        versions_map = {pkg: list(base_versions) for pkg, _c, _l in deps}
        versions_map["s-00"] = ["1.0.0"]
        versions_map["s-01"] = ["1.0.0"]
        times = {pkg: dict(fresh_times) for pkg, _c, _l in deps}
        times["s-00"] = {}
        times["s-01"] = {}
        with _MockRegistry(deps, versions_map, times=times) as reg:
            ws = self._fixture("f5-green", deps, registry=reg.url)
            result = self._run(ws, "f5-green",
                               ["--run-id", "run-r12b-f5", "--workspace-id", "ws-r12b", "--project-id", "f5-green",
                                "--mode", "draft", "--draft-deadline-seconds", "150",
                                "--target-level", "green"],
                               osv_base=reg.url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        plan = self._plan(ws, "run-r12b-f5")
        health = plan["proposals"][0]["health"]
        self.assertEqual(health["postPlanTargetLevel"], "green", health)
        self.assertEqual(health["postPlanLagOk"], 74, health)
        self.assertEqual(health["postPlanPolicyRequired"], 76, health)
        self.assertEqual(health["postPlanPolicyShortfall"], 2, health)
        self.assertEqual(health["postPlanGoal"], "unknown", health)
        self.assertNotEqual(health["postPlanGoal"], "feasible", health)
        manifest = self._manifest(ws, "run-r12b-f5")
        meta = manifest["metadata"]
        self.assertEqual(meta["postPlanTargetLevel"], "green", meta)
        self.assertEqual(meta["postPlanPolicyRequired"], 76, meta)
        self.assertEqual(meta["postPlanLagOk"], 74, meta)
        self.assertEqual(meta["postPlanGoal"], "unknown", meta)
        self.assertEqual(manifest["perProject"]["f5-green"]["postPlanTargetLevel"], "green", manifest["perProject"])
        prompt = self._prompt(ws, "run-r12b-f5")
        self.assertIn("уровень: green", prompt)
        self.assertIn("требуется по политике: 76/76 (100%)", prompt)
        self.assertNotIn("достижима по lag-критерию", prompt)
        summary = (ws / ".dependency-roadmap" / "artifacts" / "runs" / "run-r12b-f5" / "draft" / "summary.md").read_text(
            encoding="utf-8", errors="replace")
        self.assertIn("уровень: green", summary)

    def test_generate_all_keeps_per_project_target_levels(self):
        """F5: in a generate-all run every project keeps its OWN targetLevel --
        the projection/verdict follow the project's policy (yellow vs green) and
        the cross-project aggregate reports level "mixed"."""
        ry = self._synthetic_row("y-ok", current="1.0.9", evidence={"1.0.9": "0"})
        ry.project = "proj-yellow"
        rg = self._synthetic_row("g-ok", current="1.0.9", evidence={"1.0.9": "0"})
        rg.project = "proj-green"
        saved = os.environ.get("DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT")
        os.environ["DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT"] = json.dumps({
            "proj-yellow": {"targetLevel": "yellow"},
            "proj-green": {"targetLevel": "green"},
        })

        def _restore_map():
            if saved is None:
                os.environ.pop("DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT", None)
            else:
                os.environ["DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT"] = saved

        self.addCleanup(_restore_map)
        rows_by = {"proj-yellow": [ry], "proj-green": [rg]}
        health = {n: roadmap.compute_project_health(rs, n) for n, rs in rows_by.items()}
        specs = {n: roadmap.ProjectSpec(name=n, path=Path(n)) for n in rows_by}
        plan = roadmap.build_draft_plan(rows_by, specs, health)
        by_name = {p["project"]: p["health"] for p in plan["proposals"]}
        hy = by_name["proj-yellow"]
        hg = by_name["proj-green"]
        self.assertEqual(hy["postPlanTargetLevel"], "yellow", hy)
        self.assertEqual(hg["postPlanTargetLevel"], "green", hg)
        self.assertEqual(hg["postPlanPolicyRequired"], 1, hg)
        self.assertEqual(hg["postPlanLagOk"], 1, hg)
        self.assertEqual(hg["postPlanGoal"], "feasible", hg)
        aggregate = roadmap.draft_post_plan_aggregate([hy, hg])
        self.assertEqual(aggregate["postPlanTargetLevel"], "mixed", aggregate)
        self.assertEqual(aggregate["postPlanGoal"], "feasible", aggregate)

    def test_green_prompt_labels_level_and_does_not_claim_feasible(self):
        """F5: the RU/EN prompt names the CHOSEN level next to the verdict,
        shows the 100% required share and never prints the yellow "reachable"
        wording for a green-blocked plan."""
        self._policy_ctx({"targetLevel": "green"})
        rows = [self._synthetic_row(f"ok{i}", current="1.0.9", evidence={"1.0.9": "0"}) for i in range(8)]
        for i in range(2):
            lag = self._synthetic_row(f"lag{i}", current="1.0.0")
            lag.target_yellow = roadmap.NO_ACTION
            lag.target_green = roadmap.NO_ACTION
            lag.target_default = roadmap.NO_ACTION
            rows.append(lag)
        plan = self._plan_for(rows)
        spec = roadmap.ProjectSpec(name="proj", path=Path("proj"))
        snapshot = {"targetLevel": "green", "minLagOkPct": "80"}
        prompt = roadmap.build_draft_prompt(
            "run-f5", "ws-f5", "proj", "draft", "hash", plan, {"proj": spec},
            language="ru", snapshot=snapshot,
        )
        self.assertIn("## Цель (projected по этому плану, уровень: green)", prompt)
        self.assertIn("требуется по политике: 10/10 (100%)", prompt)
        self.assertIn("(shortfall по политике: 2)", prompt)
        self.assertIn("Оценка достижимости цели (green): НЕ достижима этим планом", prompt)
        self.assertNotIn("достижима по lag-критерию", prompt)
        prompt_en = roadmap.build_draft_prompt(
            "run-f5", "ws-f5", "proj", "draft", "hash", plan, {"proj": spec},
            language="en", snapshot=snapshot,
        )
        self.assertIn("## Goal (projected by this plan, level: green)", prompt_en)
        self.assertIn("policy requires 10/10 (100%)", prompt_en)
        self.assertIn("Goal feasibility (green): NOT reachable by this plan", prompt_en)
        self.assertNotIn("reachable under the lag criterion", prompt_en)


if __name__ == "__main__":
    unittest.main()
