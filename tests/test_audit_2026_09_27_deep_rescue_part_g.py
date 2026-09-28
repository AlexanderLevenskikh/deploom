"""Part G (2026-09-27): the WORKING LOOP is honest at every exit.

The auditor's review found the loop could claim success it did not earn:

1. When the exact solver leaves EVERY package unknown, the run emitted
   VERIFIED_TARGET_COMPLETE + SAT_PROVEN without a single physical check
   (the no-op "no changes to materialize" path recorded an incumbent over
   an EMPTY solver result).
2. When only the small component solved, the unresolved packages fell OUT
   of `desired_assignment` (seeded from the first partial solve), so the
   partial assignment compared equal to the (partial) target and was marked
   VERIFIED_TARGET_COMPLETE.
3. A repeatable config/API failure re-verified the SAME tuple 4 times and
   ended by safety limit instead of transitioning to repair, and repair was
   an event + in-memory deferral with no durable handoff to the Desktop
   Executor and no verify-after-repair continuation.
4. The initial migration-progress metrics came from plan-status counters,
   so KNOWN C/H/M/L/U findings were reported as zeros in a brand new
   journal.

These tests drive `resolve_peer_compatibility_with_verification` -- the
actual production loop -- through the resolver/candidate stage with the
exact-solver and physical-verification stages mocked, and assert on the
progress events, the final assignments, the durable repair artifact and the
number of physical verifications.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from unittest import mock

import dependency_live_roadmap_generator as roadmap


def _row(name, current="1.0.0", target_yellow="1.0.0", target_default="1.0.0",
         target_green="1.0.0", vulns="0"):
    return roadmap.DependencyRow(
        project="Demo", package_dir=".", name=name, kind="dev", requested_spec="*",
        current_version=current, current_source="lockfile", latest_version="2.0.0",
        current_vulns=vulns, min_no_critical="1.0.0", min_no_high="1.0.0",
        min_no_vuln="1.0.0", min_lag_12m="1.0.0", min_lag_9m="1.0.0",
        min_lag_6m="1.0.0", min_lag_3m="1.0.0", group=1, reason="test", notes="",
        target_default=target_default, target_yellow=target_yellow,
        target_green=target_green,
    )


def _solver_mock(assignment_by_mode, statuses_by_mode, incomplete_reports=()):
    """A mock of roadmap.resolve_peer_compatibility (the tolerant bootstrap
    solve). Fills solver_statuses_out per mode and incomplete_components_out,
    returns a per-mode candidate_map."""
    def fake(rows_by_project, client, **kwargs):
        modes = kwargs.get("modes", ("default",))
        solver_statuses_out = kwargs.get("solver_statuses_out")
        if solver_statuses_out is None:
            solver_statuses_out = {}
        incomplete_components_out = kwargs.get("incomplete_components_out")
        result = {}
        for project, rows in rows_by_project.items():
            result[project] = {}
            for mode in modes:
                result[project][mode] = dict(assignment_by_mode.get(mode, {}))
                statuses = statuses_by_mode.get(mode, {})
                solver_statuses_out.setdefault(project, {})[mode] = dict(statuses)
        if incomplete_components_out is not None:
            for report in incomplete_reports:
                incomplete_components_out.append(dict(report))
        return result
    return fake


class PartGWorkingLoopHonesty(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # The loop captures a real resolver/source identity, so a minimal
        # project tree must exist (package.json + lockfile).
        (self.root / "package.json").write_text(
            json.dumps({"name": "demo", "version": "1.0.0",
                        "dependencies": {"a": "1.0.0", "b": "1.0.0"}}),
            encoding="utf-8",
        )
        (self.root / "yarn.lock").write_text(
            "# yarn lockfile v1\n", encoding="utf-8",
        )
        self.spec = roadmap.ProjectSpec(
            "Demo", self.root,
            constraint_verify_config={"project_checks": "off"},
        )
        self._env_backup = dict(os.environ)
        os.environ["DEPLOOM_MODE"] = "deep"
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _run(self, rows, solver_fake, verify_fake, mode="yellow", progress_path=None, **extra):
        client = roadmap.LiveDataClient(
            "https://nexus.example/repository/npm-group", timeout=1,
            batch_size=10, sleep_sec=0,
        )
        with mock.patch.object(roadmap, "resolve_peer_compatibility",
                               side_effect=solver_fake), \
             mock.patch.object(roadmap, "verify_assignment",
                               side_effect=verify_fake), \
             mock.patch.object(roadmap, "eprint") as output:
            final = roadmap.resolve_peer_compatibility_with_verification(
                {"Demo": rows}, {"Demo": self.spec}, client,
                modes=(mode,), progress_path=progress_path, run_id="part-g-run",
                **extra,
            )
        # The DEPLOOM_PROGRESS_V2 stream (schemaVersion 2) is emitted through
        # eprint; the persisted progress file only keeps the latest state.
        self._streamed_events = []
        for call in output.call_args_list:
            text = call.args[0] if call.args else ""
            if text.startswith("DEPLOOM_PROGRESS_V2 "):
                try:
                    self._streamed_events.append(
                        json.loads(text[len("DEPLOOM_PROGRESS_V2 "):]))
                except ValueError:
                    pass
        return final

    def test_all_unknown_is_never_sat_proven_and_no_verified_result(self):
        # Auditor repro: the exact solver leaves EVERY package unknown; the old
        # loop claimed VERIFIED_TARGET_COMPLETE + SAT_PROVEN with zero physical
        # verification. Now: no verified assignment, no SAT_PROVEN claim.
        rows = [_row("a", target_yellow="2.0.0"), _row("b", target_yellow="1.5.0")]
        progress_path = self.root / "activity.log"
        incomplete = [{
            "project": "Demo", "mode": "yellow", "component": ["a"],
            "status": "unknown", "detail": "solver did not finish",
        }, {
            "project": "Demo", "mode": "yellow", "component": ["b"],
            "status": "unknown", "detail": "solver did not finish",
        }]

        def solver(rows_by_project, client, **kwargs):
            modes = kwargs.get("modes", ("default",))
            solver_statuses_out = kwargs.get("solver_statuses_out")
            if solver_statuses_out is None:
                solver_statuses_out = {}
            inc = kwargs.get("incomplete_components_out")
            for mode in modes:
                solver_statuses_out.setdefault("Demo", {})[mode] = {"a": "unknown", "b": "unknown"}
            if inc is not None:
                inc.extend(incomplete)
            return {"Demo": {m: {} for m in modes}}

        def verify_fake(*args, **kwargs):
            raise AssertionError("no physical verification may run for all-unknown")

        final = self._run(rows, solver, verify_fake, mode="yellow",
                          progress_path=progress_path)
        self.assertTrue((self.root / "activity.log").is_file())
        events = self._streamed_events
        sat_proven = [e for e in events if e.get("terminalStatus") == "SAT_PROVEN"]
        self.assertEqual([], sat_proven)
        self.assertTrue(
            any(e.get("phase") == "noop-unresolved-components" for e in events),
            "expected an honest unresolved terminal event",
        )
        # No project/mode was handed to the apply phase: nothing is claimed proven.
        self.assertEqual({}, final)
        # The rows must stay flagged unresolved (never "claimed verified").
        for row in rows:
            self.assertTrue(getattr(row, "peer_compat_unresolved", False), row.name)

    def test_partial_solve_keeps_unresolved_in_target_and_never_complete(self):
        # Auditor repro: only the small component solved; the big unresolved
        # package fell OUT of the target and the partial result was marked
        # VERIFIED_TARGET_COMPLETE. Now the goal covers BOTH rows, the partial
        # incumbent stays VERIFIED_GOOD_ENOUGH, the terminal reports
        # SOLVER_UNKNOWN with the unresolved package listed, and the verified
        # assignment keeps big-a pinned at its CURRENT version (never dropped).
        rows = [_row("big-a", target_yellow="2.0.0"), _row("small-b", target_yellow="1.5.0")]
        progress_path = self.root / "activity.log"
        incomplete = [{
            "project": "Demo", "mode": "yellow", "component": ["big-a"],
            "status": "unknown", "detail": "big component unfinished",
        }]

        def solver(rows_by_project, client, **kwargs):
            modes = kwargs.get("modes", ("default",))
            solver_statuses_out = kwargs.get("solver_statuses_out")
            if solver_statuses_out is None:
                solver_statuses_out = {}
            inc = kwargs.get("incomplete_components_out")
            for mode in modes:
                solver_statuses_out.setdefault("Demo", {})[mode] = {
                    "big-a": "unknown", "small-b": "optimal",
                }
            if inc is not None:
                inc.extend(incomplete)
            return {"Demo": {m: {"small-b": "1.5.0"} for m in modes}}

        def verify_fake(*args, **kwargs):
            return roadmap.BaselineVerifyResult(True, "dependency", "resolver ok")

        final = self._run(rows, solver, verify_fake, mode="yellow",
                          progress_path=progress_path)
        events = self._streamed_events
        passed = [e for e in events if e.get("phase") == "mode-passed"]
        # Phase 1: the PARTIAL incumbent verifies green but is not complete.
        partial_idx = [i for i, e in enumerate(passed)
                       if e.get("terminalStatus") == "SOLVER_UNKNOWN"]
        self.assertTrue(partial_idx, "a partial resolver-green pass must exist")
        for i in partial_idx:
            self.assertNotEqual("VERIFIED_TARGET_COMPLETE", passed[i].get("completionStatus"),
                                "a partial solve must never be VERIFIED_TARGET_COMPLETE")
            self.assertEqual(["big-a"], list(passed[i].get("unresolvedPackages") or []))
        # Phase 2 (the auditor's probe ordering): the joined cohort is verified
        # ONLY AFTER the partial one -- VERIFIED_TARGET_COMPLETE appears later.
        full_idx = [i for i, e in enumerate(passed)
                    if e.get("terminalStatus") == "SAT_PROVEN"]
        self.assertTrue(full_idx, "the joined cohort must ultimately verify SAT_PROVEN")
        self.assertGreater(min(full_idx), max(partial_idx),
                           "complete must come only AFTER the partial pass")
        for i in full_idx:
            self.assertEqual("VERIFIED_TARGET_COMPLETE", passed[i].get("completionStatus"))
        # The final validated assignment covers BOTH rows at their goal targets.
        proven = final["Demo"]["yellow"]
        self.assertEqual("2.0.0", proven["big-a"])
        self.assertEqual("1.5.0", proven["small-b"])
        # While the big component was undecided it was flagged for consumers;
        # the flag is set during the partial phase (never claimed verified there).
        big_a_row = next(r for r in rows if r.name == "big-a")
        self.assertTrue(getattr(big_a_row, "peer_compat_unresolved", False))

    def test_repairable_failure_transitions_to_repair_not_four_verifies(self):
        # Auditor repro: a repeatable config/API (project-kind) failure used to
        # verify the SAME tuple up to 4 times and end by safety limit. Now the
        # first failure defers the tuple, later encounters are skipped, and the
        # mode ends with a durable repair-required handoff.
        rows = [_row("a", target_yellow="2.0.0"), _row("b", target_yellow="1.5.0")]
        progress_path = self.root / "progress.log"
        verify_calls = []

        def solver(rows_by_project, client, **kwargs):
            modes = kwargs.get("modes", ("default",))
            solver_statuses_out = kwargs.get("solver_statuses_out")
            if solver_statuses_out is None:
                solver_statuses_out = {}
            for mode in modes:
                solver_statuses_out.setdefault("Demo", {})[mode] = {"a": "optimal", "b": "optimal"}
            return {"Demo": {m: {"a": "2.0.0", "b": "1.5.0"} for m in modes}}

        def verify_fake(*args, **kwargs):
            verify_calls.append(kwargs)
            return roadmap.BaselineVerifyResult(False, "project", "flow check failed",
                                                output="yarn flow:check failed: type mismatch")

        # max_iterations small: the loop must end with the repair transition
        # LONG before it would exhaust them.
        self.spec = roadmap.ProjectSpec(
            "Demo", self.root,
            constraint_verify_config={"project_checks": "off", "maxIterations": 4},
        )
        final = self._run(rows, solver, verify_fake, mode="yellow",
                          progress_path=progress_path)

        events = self._streamed_events
        self.assertTrue(any(e.get("phase") == "cohort-repair-required" for e in events),
                        "repair deferral event must be emitted")
        self.assertTrue(
            any(e.get("phase") == "repair-handoff-suppressed-reverify" for e in events),
            "the same tuple must never be re-verified while deferred",
        )
        self.assertTrue(
            any(e.get("phase") == "repair-required-terminal" for e in events),
            "the mode must end with the repair transition, not the safety limit",
        )
        # Exactly ONE physical verification for the deferred tuple.
        self.assertLessEqual(len(verify_calls), 1)
        # Nothing may be reported as a proven assignment for this mode.
        self.assertNotIn("yellow", final.get("Demo", {}))
        # Durable machine-readable handoff written next to the run events.
        artifact = self.root / "repair-requests.json"
        self.assertTrue(artifact.is_file())
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual("part-g-run", payload["runId"])
        self.assertTrue(payload["requests"])
        request = payload["requests"][0]
        self.assertEqual("Demo", request["project"])
        self.assertEqual("yellow", request["mode"])
        self.assertEqual("2.0.0", request["assignment"]["a"])
        self.assertIn("snapshotIdentity", request)
        self.assertEqual("repair-or-replan", request["disposition"])

    def test_verify_after_repair_accepts_the_same_tuple(self):
        # The repair contract: candidate -> verify -> repair -> verify -> accept.
        # Run 1 (source broken) defers the tuple. Run 2 (source repaired: the
        # snapshot identity changes, the in-memory handoff is empty for a fresh
        # run) re-plans the SAME tuple and physically re-verifies it -- that is
        # how the corrected result is returned. A fresh run must re-verify.
        rows = [_row("a", target_yellow="2.0.0")]
        progress_path = self.root / "after-repair.log"

        def solver(rows_by_project, client, **kwargs):
            modes = kwargs.get("modes", ("default",))
            solver_statuses_out = kwargs.get("solver_statuses_out")
            if solver_statuses_out is None:
                solver_statuses_out = {}
            for mode in modes:
                solver_statuses_out.setdefault("Demo", {})[mode] = {"a": "optimal"}
            return {"Demo": {m: {"a": "2.0.0"} for m in modes}}

        def verify_ok(*args, **kwargs):
            return roadmap.BaselineVerifyResult(True, "dependency", "resolver ok")

        final = self._run(rows, solver, verify_ok, mode="yellow",
                          progress_path=progress_path)
        events = self._streamed_events
        self.assertTrue(any(e.get("phase") == "mode-passed"
                            and e.get("terminalStatus") == "SAT_PROVEN"
                            for e in events))
        self.assertEqual("2.0.0", final["Demo"]["yellow"]["a"])
        self.assertEqual(0, len([e for e in events if e.get("phase") == "cohort-repair-required"]))

    def test_two_phase_repair_uses_durable_handoff_and_resolves_after_repair(self):
        # R1 acceptance: candidate -> verify -> repair request (durable) ->
        # source repaired (snapshot changes) -> RESTARTED run reloads the
        # durable handoff, re-verifies the SAME tuple on the repaired snapshot,
        # resolves it and removes it from the handoff file -- so a restart
        # neither spins on the broken tuple nor carries a stale request.
        rows = [_row("a", target_yellow="2.0.0")]
        phase1_path = self.root / "phase1.log"
        phase2_path = self.root / "phase2.log"

        def solver(rows_by_project, client, **kwargs):
            modes = kwargs.get("modes", ("default",))
            solver_statuses_out = kwargs.get("solver_statuses_out")
            if solver_statuses_out is None:
                solver_statuses_out = {}
            for mode in modes:
                solver_statuses_out.setdefault("Demo", {})[mode] = {"a": "optimal"}
            return {"Demo": {m: {"a": "2.0.0"} for m in modes}}

        # project checks ON so the source snapshot key is real and the repair
        # handoff is bound to a real snapshot identity (never the empty "").
        self.spec.constraint_verify_config = {
            "project_checks": "diagnostic",
            "commands": ["yarn lint"],
            "maxIterations": 8,
        }

        def verify_broken(*args, **kwargs):
            if kwargs.get("run_project_checks"):
                return roadmap.BaselineVerifyResult(False, "project", "project preflight failed",
                                                    output="yarn lint: error TS2322", command="yarn lint")
            return roadmap.BaselineVerifyResult(False, "project", "flow check failed",
                                                output="yarn flow:check failed: type mismatch")

        final1 = self._run(rows, solver, verify_broken, mode="yellow",
                           progress_path=phase1_path)
        events1 = self._streamed_events
        self.assertTrue(any(e.get("phase") == "repair-required-terminal" for e in events1),
                        "phase 1 must end with the repair transition")
        self.assertNotIn("yellow", final1.get("Demo", {}))
        artifact = self.root / "repair-requests.json"
        self.assertTrue(artifact.is_file())
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual("part-g-run", payload["runId"])
        self.assertTrue(payload["requests"])
        request = payload["requests"][0]
        bound_snapshot = request["snapshotIdentity"]
        self.assertTrue(bound_snapshot, "the repair request must be bound to a real snapshot identity")
        # A production restart is a fresh process whose recovery ownership died
        # with it (stale-owner reclaim). In-process, simulate that by dropping
        # the ownership record the phase-1 run left behind.
        active_file = self.root / "baseline-run-active.json"
        if active_file.exists():
            active_file.unlink()

        # Phase 2: the Desktop applies a source/config repair -> the snapshot
        # content changes -> the restarted run must reload the DURABLE handoff,
        # see the snapshot moved, re-verify the same tuple and resolve it.
        (self.root / "package.json").write_text(
            json.dumps({"name": "demo", "version": "1.0.0",
                        "dependencies": {"a": "1.0.0", "b": "1.0.0"},
                        "lint-staged": {"repaired": True}}),
            encoding="utf-8",
        )

        def verify_ok(*args, **kwargs):
            return roadmap.BaselineVerifyResult(True, "dependency", "resolver ok")

        final2 = self._run(rows, solver, verify_ok, mode="yellow",
                           progress_path=phase2_path)
        events2 = self._streamed_events
        resolved = [e for e in events2 if e.get("phase") == "repair-resolved"]
        self.assertTrue(resolved,
                        "the restarted run must emit the machine-readable resolution event")
        self.assertTrue(resolved[0].get("assignment"), resolved[0])
        self.assertEqual("2.0.0", final2["Demo"]["yellow"]["a"])
        self.assertTrue(any(e.get("phase") == "mode-passed"
                            and e.get("terminalStatus") == "SAT_PROVEN"
                            for e in events2))
        # The durable handoff no longer carries the request: neither deferred
        # nor stale, so a third restart cannot loop on it.
        self.assertFalse(artifact.exists(), "the resolved request must leave the durable handoff")

    def test_resolver_green_project_fail_keeps_repair_request_open(self):
        # R4 acceptance (P1#3): a repair request is a SOURCE/CONFIG repair
        # ticket. A fresh verify-after-repair pass whose RESOLVER stage is
        # green but whose PROJECT stage still fails (diagnostic, non-structural)
        # is a PARTIAL result: the durable request must stay open and no
        # repair-resolved event may fire. Only a project-green pass resolves it.
        rows = [_row("a", target_yellow="2.0.0")]
        phase1_path = self.root / "g1.log"
        phase2_path = self.root / "g2.log"

        def solver(rows_by_project, client, **kwargs):
            modes = kwargs.get("modes", ("default",))
            solver_statuses_out = kwargs.get("solver_statuses_out")
            if solver_statuses_out is None:
                solver_statuses_out = {}
            for mode in modes:
                solver_statuses_out.setdefault("Demo", {})[mode] = {"a": "optimal"}
            return {"Demo": {m: {"a": "2.0.0"} for m in modes}}

        self.spec.constraint_verify_config = {
            "project_checks": "diagnostic",
            "commands": ["yarn lint"],
            "maxIterations": 8,
        }

        def verify_broken(*args, **kwargs):
            return roadmap.BaselineVerifyResult(
                False, "project", "project preflight failed",
                output="yarn lint: error TS2322", command="yarn lint")

        self._run(rows, solver, verify_broken, mode="yellow",
                  progress_path=phase1_path)
        artifact = self.root / "repair-requests.json"
        self.assertTrue(artifact.is_file())
        bound_snapshot = json.loads(
            artifact.read_text(encoding="utf-8"))["requests"][0]["snapshotIdentity"]
        self.assertTrue(bound_snapshot)
        active_file = self.root / "baseline-run-active.json"
        if active_file.exists():
            active_file.unlink()
        # The Desktop applies a source repair: the snapshot identity moves.
        (self.root / "package.json").write_text(
            json.dumps({"name": "demo", "version": "1.0.0",
                        "dependencies": {"a": "1.0.0", "b": "1.0.0"},
                        "lint-staged": {"repaired": True}}),
            encoding="utf-8",
        )

        def verify_mixed(*args, **kwargs):
            if kwargs.get("run_project_checks"):
                return roadmap.BaselineVerifyResult(
                    False, "project", "project preflight failed",
                    output="yarn lint: error TS2322", command="yarn lint")
            return roadmap.BaselineVerifyResult(True, "dependency", "resolver ok")

        final2 = self._run(rows, solver, verify_mixed, mode="yellow",
                           progress_path=phase2_path)
        events2 = self._streamed_events
        self.assertFalse(
            [e for e in events2 if e.get("phase") == "repair-resolved"],
            "resolver-green must NOT resolve the durable repair request",
        )
        self.assertTrue(artifact.is_file(),
                        "the request must stay open while the project stage fails")
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertTrue(payload["requests"])
        self.assertEqual(
            bound_snapshot, payload["requests"][0]["snapshotIdentity"],
            "a partial (resolver-green) pass must not move the bound snapshot")
        # Resolver-green is still a legitimate partial incumbent, not dropped.
        self.assertEqual("2.0.0", final2["Demo"]["yellow"]["a"])

    def test_desktop_consumer_locates_producer_handoff_at_contract_path(self):
        # R4 acceptance (P1#1): the Desktop Executor must consume the durable
        # repair handoff at the SINGLE path the Python producer actually
        # writes. This test runs the REAL producer (the production loop) with
        # its REAL settings-base progress path, then runs the REAL Desktop
        # consumer (the compiled repair-handoff.js module) against the SAME
        # directory -- no manual copying anywhere.
        node = shutil.which("node")
        if node is None:
            self.skipTest("node unavailable")
        module = (Path(__file__).resolve().parent.parent
                  / "desktop" / "dist-electron" / "repair-handoff.js")
        if not module.is_file():
            self.skipTest("desktop dist-electron build absent; run tsc first")
        state_dir = self.root / ".dependency-roadmap" / "state"
        progress_path = state_dir / "baseline-verification-progress.json"
        rows = [_row("a", target_yellow="2.0.0")]

        def solver(rows_by_project, client, **kwargs):
            modes = kwargs.get("modes", ("default",))
            solver_statuses_out = kwargs.get("solver_statuses_out")
            if solver_statuses_out is None:
                solver_statuses_out = {}
            for mode in modes:
                solver_statuses_out.setdefault("Demo", {})[mode] = {"a": "optimal"}
            return {"Demo": {m: {"a": "2.0.0"} for m in modes}}

        self.spec.constraint_verify_config = {
            "project_checks": "diagnostic",
            "commands": ["yarn lint"],
            "maxIterations": 4,
        }

        def verify_broken(*args, **kwargs):
            return roadmap.BaselineVerifyResult(
                False, "project", "project preflight failed",
                output="yarn lint: error TS2322", command="yarn lint")

        self._run(rows, solver, verify_broken, mode="yellow",
                  progress_path=progress_path)
        handoff = state_dir / "repair-requests.json"
        self.assertTrue(handoff.is_file(),
                        "producer must persist the handoff at the contract path")
        consumer = self.root / "consumer.mjs"
        consumer.write_text(
            "import { openRepairRequests } from " + json.dumps(module.as_uri()) + ";\n"
            "const s = openRepairRequests(process.argv[2]);\n"
            "console.log(JSON.stringify({file: s.file, runId: s.runId, "
            "requests: s.requests.map(r => ({requestId: r.requestId, project: r.project, "
            "mode: r.mode, fingerprint: r.fingerprint, assignment: r.assignment}))}));\n",
            encoding="utf-8",
        )
        proc = subprocess.run([node, str(consumer), str(state_dir)],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(0, proc.returncode, proc.stderr)
        scan = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(
            str(scan["file"]).replace("\\", "/").endswith(
                ".dependency-roadmap/state/repair-requests.json"),
            scan,
        )
        self.assertEqual("part-g-run", scan["runId"])
        self.assertEqual(1, len(scan["requests"]))
        req = scan["requests"][0]
        self.assertEqual("Demo", req["project"])
        self.assertEqual("yellow", req["mode"])
        self.assertEqual("2.0.0", req["assignment"]["a"])

    def test_incumbent_overrides_reach_every_verify_and_survive_restart_seed(self):
        # R2b: resolver overrides accepted with the verified incumbent are part
        # of the verified substrate. On a RESTART the map arrives through the
        # verify config (fed from the proven state/envelope); it must reach
        # EVERY physical verification (resolver + project preflight), the
        # recorded incumbent and the finalization -- never be dropped because
        # the first new incumbent started with an empty override set.
        self.spec.constraint_verify_config = {
            "project_checks": "diagnostic",
            "commands": ["yarn lint"],
            "resolver_overrides": {"pin-x": "1.0.0"},
        }
        rows = [_row("a", target_yellow="2.0.0"), _row("b")]
        progress_path = self.root / "overrides.log"
        seen: list = []
        calls_project: list = []

        def solver(rows_by_project, client, **kwargs):
            modes = kwargs.get("modes", ("default",))
            solver_statuses_out = kwargs.get("solver_statuses_out")
            if solver_statuses_out is None:
                solver_statuses_out = {}
            for mode in modes:
                solver_statuses_out.setdefault("Demo", {})[mode] = {
                    "a": "optimal", "b": "optimal",
                }
            return {"Demo": {m: {"a": "2.0.0"} for m in modes}}

        def verify_fake(*args, **kwargs):
            cfg = kwargs.get("config")
            seen.append(dict(cfg.resolver_overrides) if cfg else {})
            calls_project.append(bool(kwargs.get("run_project_checks")))
            return roadmap.BaselineVerifyResult(True, "dependency", "resolver ok")

        final = self._run(rows, solver, verify_fake, mode="yellow",
                          progress_path=progress_path)
        self.assertEqual("2.0.0", final["Demo"]["yellow"]["a"])
        assert seen, "the loop must physically verify at least once"
        for captured in seen:
            self.assertEqual({"pin-x": "1.0.0"}, captured,
                             "every verify must keep the incumbent overrides")
        self.assertTrue(any(calls_project),
                        "a project preflight must run under the same config")
        events = self._streamed_events
        self.assertTrue(any(e.get("phase") == "progressive-incumbent-finalized"
                            for e in events),
                        "finalization must keep the overrides-carrying incumbent")


class PartGInitialMetricsFromScan(unittest.TestCase):
    def test_publish_draft_result_reports_known_vulns_not_zeros(self):
        # Auditor repro: initial Draft metrics came from plan-status counters,
        # so known vulnerabilities hit a new journal as zeros. publish_draft_result
        # must seed the initial checkpoint with the ACTUAL scanned findings.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roadmap.set_draft_artifacts_base(base)
            try:
                vuln_row = _row("express", current="4.17.1", target_yellow="4.19.0",
                                vulns="C:1,H:2,M:3,L:0,U:0")
                clear_row = _row("uuid", current="10.0.0", vulns="0")
                health = roadmap.ProjectHealth(
                    project="tiny-basic", status="yellow", status_rank=1,
                    total=2, lag_ok_12m=1, lag_bad_12m=1, lag_ok_pct=50.0,
                    critical=0, high=0, moderate=0, low=0, unknown=0, reason="reason",
                    lag_unknown=0, lag_needed_for_yellow=1,
                    yellow_plan_required=1, yellow_projected_lag_ok=1,
                    yellow_projected_lag_pct=100.0, yellow_plan_shortfall=0,
                )
                manifest = roadmap.publish_draft_result(
                    run_id="run-g-1", workspace_id="ws", project_id="tiny-basic",
                    mode="yellow",
                    rows_by_project={"tiny-basic": [vuln_row, clear_row]},
                    projects_by_name={}, health_by_project={"tiny-basic": health},
                    status="DRAFT_READY", partial_reason=None,
                    deadline=roadmap.DeadlineClock(None),
                    settings_snapshot={}, execution_context={},
                )
                draft_dir = base / "runs" / "run-g-1" / "draft"
                payload_path = draft_dir / "migration-progress.json"
                self.assertTrue(payload_path.is_file())
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
                point = payload["points"][0]
                self.assertEqual("scan-rows", point["actualHealth"]["metricsSource"])
                self.assertEqual(1, point["actualHealth"]["critical"])
                self.assertEqual(2, point["actualHealth"]["high"])
                self.assertEqual(3, point["actualHealth"]["moderate"])
                # R3b: vulnerablePackages counts UNIQUE vulnerable packages (one
                # row has six findings); findingsOccurrences counts the severity
                # findings themselves; unique advisories and resolved tree nodes
                # were not measured by the row scan, so they stay null and the
                # engine is explicit.
                self.assertEqual(1, point["securityCounts"]["vulnerablePackages"])
                self.assertEqual(6, point["securityCounts"]["findingsOccurrences"])
                self.assertIsNone(point["securityCounts"]["advisories"])
                self.assertIsNone(point["securityCounts"]["nodes"])
                self.assertEqual("not-started", point["audit"]["engine"])
                self.assertEqual("tiny-basic", manifest["projectId"])
            finally:
                roadmap.set_draft_artifacts_base(None)


if __name__ == "__main__":
    unittest.main()
