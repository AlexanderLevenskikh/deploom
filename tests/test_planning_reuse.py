"""Real process/HTTP boundaries for planning reuse; never proof fixtures."""
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import planning_cache as cache
from migration_research import collect, github_repository
from tests.test_registry_metadata_cache import _client, PACKUMENT

ROOT = Path(__file__).resolve().parents[1]


class PlanningReuseTests(unittest.TestCase):
    def test_unsupported_manager_never_expands_workspace_topology(self):
        from lockfile_preflight import probe_candidate
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root/"yarn.lock").write_text("fixture")
            for manager in ("npm@10.0.0", "yarn@1.22.22", "yarn@2.4.3"):
                (root/"package.json").write_text(json.dumps({"packageManager": manager}))
                with patch("lockfile_preflight.resolve_project_topology", side_effect=AssertionError("expensive topology")):
                    self.assertIsNone(probe_candidate(root, root, {}, {}, apply=lambda *a: self.fail("apply"), run=lambda *a: self.fail("run")))

    def test_old_major_release_uses_exact_tag_and_skips_redundant_fallback(self):
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            if "/tags/" in url:
                return {"tag_name": "v19.0.0", "body": "Official migration notes", "html_url": "https://github.com/example/package/releases/tag/v19.0.0"}, 1, "", "github"
            return [], 1, "", "github"
        with tempfile.TemporaryDirectory() as raw:
            for _ in range(2):
                result = collect("package", "18.0.0", "19.0.0", repository="https://github.com/example/package", fetch=fetch, root=Path(raw))
                self.assertEqual([r["tag"] for r in result["releases"]], ["v19.0.0"])
                self.assertEqual(result["unknown"], [])
            self.assertEqual(len(calls), 2, "one list and one exact tag, reused on second lookup")

    def test_public_network_has_wall_budget_beyond_socket_read_timeout(self):
        from migration_research import _public_json
        release, finished = threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            release.wait(5)
            finished.set()
            return {"late": True}, 1, "", "fixture"
        try:
            with patch("migration_research._fetch_public_json", side_effect=slow):
                data, _, failure, _ = _public_json("https://api.github.com/fixture", wall_timeout=.02)
            self.assertIsNone(data)
            self.assertEqual(failure, "source-timeout")
            self.assertFalse(finished.is_set(), "caller must return while the socket worker is blocked")
        finally:
            release.set()

    def test_exact_unavailable_probe_stops_before_heavy_trial_copy(self):
        import iterative_migration as migration
        from types import SimpleNamespace
        from tests.regression.test_iterative_materialize_retry_budget import _run_dict, _config_dict, _checkpoint_dict
        with tempfile.TemporaryDirectory() as raw:
            run = Path(raw)
            (run/"trial").mkdir(); (run/"checkpoints").mkdir()
            config = _config_dict(targets={"pkg-a": "2.0.0"})
            migration.save_run(run, _run_dict())
            migration.save_config(run, config)
            migration.save_checkpoint(run, _checkpoint_dict())
            migration._plan_next_locked(run, migration.load_run(run), config)
            failure = subprocess.CompletedProcess([], 1, "YN0082: pkg-a@npm:2.0.0: No candidates found", "")
            with patch.object(migration, "run_budget_ok", return_value=True), patch.object(migration, "_assert_runtime_unchanged"), patch.object(migration, "_open_checkpoint_source", return_value=SimpleNamespace(root=run, project_path=run)), patch("lockfile_preflight.probe_candidate", return_value=failure), patch.object(migration, "_materialize_trial_tree") as copy, patch("iterative_trial_workspace.remove_trial_workspace") as retire:
                self.assertEqual(0, migration._materialize_locked(run, migration.load_run(run), config, SimpleNamespace(timeout_seconds=1200)))
                copy.assert_not_called(); retire.assert_not_called()
            self.assertEqual("REJECTED", migration.load_candidate(run)["stage"])
            self.assertEqual("VERIFIED", migration.load_checkpoint(run, "C0")["status"])

    def test_small_probe_excludes_large_source_and_dependency_trees(self):
        from types import SimpleNamespace
        from lockfile_preflight import probe_candidate
        profile = SimpleNamespace(family="yarn-berry", declared_version="4.0.0")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)/"source"; root.mkdir()
            run = Path(raw)/"run"; run.mkdir()
            (root/"package.json").write_text(json.dumps({"packageManager": "yarn@4.0.0", "dependencies": {"fixture": "1.0.0"}}))
            (root/"yarn.lock").write_text("__metadata:\n  version: 8\n")
            (root/".yarnrc.yml").write_text("enableGlobalCache: true\n")
            for name in ("src", "node_modules", ".git", ".artifacts"):
                (root/name).mkdir(); (root/name/"large").write_bytes(b"x"*100000)
            topology = SimpleNamespace(profile=profile, package_manager_root=root,
                                       workspace_member_manifests=(), lockfile=root/"yarn.lock")
            def native(selected):
                self.assertEqual({p.name for p in selected.iterdir()}, {"package.json", "yarn.lock", ".yarnrc.yml"})
                return subprocess.CompletedProcess([], 0, "", "")
            with patch("lockfile_preflight.resolve_project_topology", return_value=topology):
                result = probe_candidate(root, run, {}, {}, apply=lambda *a: None, run=native)
                self.assertEqual(0, result.returncode)
                (root/".yarnrc.yml").write_text("enableGlobalCache: false\n")
                self.assertIsNone(probe_candidate(root, run, {}, {}, apply=lambda *a: self.fail("local-cache apply"), run=lambda *a: self.fail("local-cache run")))
                (root/".yarnrc.yml").write_text("enableGlobalCache: true\n")
                (root/"package.json").write_text(json.dumps({"packageManager": "yarn@4.0.0", "dependencies": {"fixture": "file:../local"}}))
                self.assertIsNone(probe_candidate(root, run, {}, {}, apply=lambda *a: self.fail("unsupported apply"), run=lambda *a: self.fail("unsupported run")))
            self.assertEqual(list(run.iterdir()), [])

    def test_yarn_target_deferral_requires_exact_selected_target(self):
        from iterative_target_deferrals import record_unavailable_targets
        candidate = {"candidateId": "c", "delta": {"changed": {"@scope/pkg": "2.0.0"}}}
        config = {"targets": {"@scope/pkg": "2.0.0"}}
        message = "➤ YN0082: @scope/pkg@npm:2.0.0: No candidates found"
        ledger = {}
        self.assertTrue(record_unavailable_targets({}, {}, config, candidate, message.replace("YN0082: ", "YN0082: │ ")))
        self.assertTrue(record_unavailable_targets(ledger, {}, config, candidate, message))
        self.assertEqual(ledger["targetAvailabilityDeferrals"][0]["package"], "@scope/pkg")
        for unrelated in ("YN0082: no candidates found", message.replace("2.0.0", "3.0.0"), "YN0041: Invalid authentication"):
            self.assertFalse(record_unavailable_targets({}, {}, config, candidate, unrelated))

    def test_three_processes_share_one_registry_request_across_project_caches(self):
        hits = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                hits.append(self.path)
                time.sleep(.15)
                body = json.dumps(PACKUMENT).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temp:
            code = "from pathlib import Path; import sys; from tests.test_registry_metadata_cache import _client; assert _client(sys.argv[1],Path(sys.argv[2])).fetch_npm_metadata('pkg-a')['name']=='pkg-a'"
            env = {**os.environ, "DEPLOOM_PLANNING_CACHE_DIR": str(Path(temp)/"machine")}
            processes = [subprocess.Popen([sys.executable, "-c", code, f"http://127.0.0.1:{server.server_port}", str(Path(temp)/str(i))], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
            try:
                for process in processes:
                    out, error = process.communicate(timeout=35)
                    self.assertEqual(process.returncode, 0, error.decode(errors="replace"))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill(); process.wait()
            self.assertEqual(len(hits), 1, "three live clients must produce one network fetch")

    def test_expired_subject_revalidates_with_etag_without_body(self):
        headers = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                headers.append(self.headers.get("If-None-Match"))
                if headers[-1] == '"revision-1"':
                    self.send_response(304); self.end_headers(); return
                body = json.dumps(PACKUMENT).encode()
                self.send_response(200); self.send_header("ETag", '"revision-1"')
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"DEPLOOM_PLANNING_CACHE_DIR": "0"}):
            url = f"http://127.0.0.1:{server.server_port}"
            self.assertEqual(_client(url, Path(temp), ttl=.01).fetch_npm_metadata("pkg-a"), PACKUMENT)
            time.sleep(.03)
            self.assertEqual(_client(url, Path(temp), ttl=.01).fetch_npm_metadata("pkg-a"), PACKUMENT)
            self.assertEqual(headers, [None, '"revision-1"'])

    def test_dead_producer_releases_mutex(self):
        with tempfile.TemporaryDirectory() as temp:
            ready = Path(temp)/"ready"
            code = "import sys,time; from pathlib import Path; from planning_cache import planning_lease; c=planning_lease('crash-fixture'); c.__enter__(); Path(sys.argv[1]).write_text('ready'); time.sleep(60)"
            process = subprocess.Popen([sys.executable, "-c", code, str(ready)], cwd=ROOT)
            try:
                stop = time.monotonic()+10
                while not ready.exists() and time.monotonic()<stop:
                    time.sleep(.05)
                self.assertTrue(ready.exists())
            finally:
                process.kill(); process.wait(timeout=10)
            with cache.planning_lease("crash-fixture", timeout=1) as acquired:
                self.assertTrue(acquired)

    def test_wait_budget_is_checked_and_failure_never_persisted(self):
        def stopped(): raise TimeoutError("budget")
        with self.assertRaisesRegex(TimeoutError, "budget"):
            with cache.planning_lease("budget", check=stopped): pass
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def fetch():
                calls.append(1); return None, 1, "timeout", "fixture"
            for _ in range(2): cache.cached_query("failure", fetch, root=Path(temp))
            self.assertEqual(len(calls), 2)
            self.assertEqual(list(Path(temp).glob("*.json")), [])

    def test_expired_budget_rejects_a_warm_cache_hit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache.write_entry(root, 'warm', {'version': '2.0.0'})
            def expired():
                raise TimeoutError('expired fixture')
            with self.assertRaisesRegex(TimeoutError, 'expired fixture'):
                cache.cached_query('warm', lambda: self.fail('must not fetch'), root=root, check=expired)

    def test_corrupt_cache_is_a_miss_and_contexts_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache.write_entry(root, ["registry-a", "context-a"], {"version": 1})
            self.assertIsNone(cache.read_entry(root, ["registry-a", "context-b"], 600))
            file = root/(cache.key_digest("broken")+".json")
            file.write_text("{broken", encoding="utf-8")
            self.assertIsNone(cache.read_entry(root, "broken", 600))

    def test_public_research_is_bounded_exact_transition_and_cached(self):
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            if "osv" in url:
                return {"results": [{"vulns": [{"id": "GHSA-fixture", "modified": "2026-01-01"}]}]}, 1, "", "osv"
            return [{"tag_name": tag, "body": "x"*9000, "html_url": "https://github.com/example/package/releases"} for tag in ("v3.0.0", "v2.1.0", "v1.0.0", "other@2.0.0")], 1, "", "github"
        with tempfile.TemporaryDirectory() as temp:
            kwargs = dict(repository="https://github.com/example/package.git", include_osv=True, fetch=fetch, root=Path(temp))
            first = collect("package", "1.0.0", "2.1.0", **kwargs)
            second = collect("package", "1.0.0", "2.1.0", **kwargs)
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 3)
            self.assertEqual([r["tag"] for r in first["releases"]], ["v2.1.0"])
            self.assertEqual(len(first["releases"][0]["notes"]), 2500)
            self.assertEqual(first["authority"], "guidance-only")
            self.assertIn("not the configured audit", first["osv"]["coverage"])
            self.assertIsNone(github_repository("https://github.com.evil/example/package"))

    def test_osv_failure_or_pagination_never_becomes_clean_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            result = collect("package", "1.0.0", "2.0.0", include_osv=True, root=Path(temp), fetch=lambda *a, **k: ({"results": [{"next_page_token": "more"}]}, 1, "", "osv"))
            self.assertFalse(result["osv"]["complete"])
            self.assertIn("OSV pagination incomplete", result["unknown"])

    def test_lockfile_only_failure_does_not_materialize_or_verify(self):
        import iterative_migration as migration
        from types import SimpleNamespace
        from contextlib import nullcontext
        profile = SimpleNamespace(family="yarn-berry", declared_version="4.0.0")
        commands = []
        def run(argv, *args, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 1, "YN0082: no candidates found", "")
        with patch.object(migration, "_manager_and_install", return_value=("yarn", "fixture-yarn", ["install", "--mode=skip-build"])), patch.object(migration, "resolve_project_topology", return_value=SimpleNamespace(profile=profile)), patch.object(migration, "_run", side_effect=run), patch("cross_process_io.shared_io_slot", return_value=nullcontext()):
            result = migration._run_install(ROOT, timeout_seconds=30, progress_label="fixture", lockfile_only=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(commands, [["fixture-yarn", "install", "--mode=update-lockfile"]])

    def test_lockfile_success_still_requires_materialization(self):
        import iterative_migration as migration
        from types import SimpleNamespace
        from contextlib import nullcontext
        profile = SimpleNamespace(family="yarn-berry", declared_version="3.8.7")
        commands = []
        def run(argv, *args, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(migration, "_manager_and_install", return_value=("yarn", "fixture-yarn", ["install", "--mode=skip-build"])), patch.object(migration, "resolve_project_topology", return_value=SimpleNamespace(profile=profile)), patch.object(migration, "_run", side_effect=run), patch("cross_process_io.shared_io_slot", return_value=nullcontext()):
            migration._run_install(ROOT, timeout_seconds=30, progress_label="fixture")
        self.assertEqual(commands, [["fixture-yarn", "install", "--mode=skip-build"]])
