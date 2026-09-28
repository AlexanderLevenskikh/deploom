"""R6 (v0.2.140 audit follow-up): per-attempt Draft budget capping and the
run -> component -> attempt operation-id chain.

R6-1: `_draft_solver_attempt_timeout_ms` is evaluated immediately before EVERY
actual solver call (after model build and after each registry refinement);
it never exceeds the configured timeout nor the remaining budget minus the
publication reserve, is never raised by a 1s floor, and returns 0 -- refusing
to start a fresh attempt -- when the budget is already gone. The refused
attempt is reported honestly as `budget_exhausted_before_attempt`, never as a
confirmed timeout of a solver that ran.

R6-2: each executed attempt gets its own observability id generated BEFORE the
solver call, is passed into `solve_z3_exact` so the matching
solver.z3.start/finish events share it, and is returned in the component
report so the artifact links component -> attempts -> real Z3 events.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

import dependency_live_roadmap_generator as roadmap
import peer_solver_z3
from peer_solver_model import ExactSolveResult


REGISTRY = "https://nexus.example/repository/npm-group"


class _Clock:
    """Deterministic deadline stand-in: exposes a mutable `remaining` plus the
    DeadlineClock check surface resolve/supervisor need."""

    def __init__(self, remaining: float) -> None:
        self.deadline_seconds = 100.0
        self._remaining = float(remaining)

    @property
    def remaining(self) -> float:
        return self._remaining

    def set(self, value: float) -> None:
        self._remaining = float(value)

    def check(self, phase: str) -> None:
        if self.remaining <= 0:
            raise roadmap.DraftBudgetExceeded(phase)

    def check_with_reserve(self, phase: str) -> None:
        if self.remaining <= roadmap.DRAFT_FINALIZE_RESERVE_SECONDS:
            raise roadmap.DraftBudgetExceeded(phase)


def _row(name: str, current: str = "1.0.0", desired: str = "2.0.0") -> roadmap.DependencyRow:
    return roadmap.DependencyRow(
        project="Demo",
        package_dir=".",
        name=name,
        kind="dev",
        requested_spec="*",
        current_version=current,
        current_source="lockfile",
        latest_version=desired,
        current_vulns="0",
        min_no_critical=current,
        min_no_high=current,
        min_no_vuln=current,
        min_lag_12m=current,
        min_lag_9m=current,
        min_lag_6m=current,
        min_lag_3m=current,
        group=1,
        reason="model lab",
        notes="",
        target_default=desired,
        target_yellow=desired,
        target_green=desired,
        target_default_reason="desired",
        target_yellow_reason="desired",
        target_green_reason="desired",
    )


def _component_ctx(remaining: float):
    """One-package component with a fresh deadline clock and a registry domain
    that contains current, a mid candidate and the desired target."""
    client = roadmap.LiveDataClient(REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
    client.deadline = _Clock(remaining)
    client.npm_cache["a"] = {"versions": {
        "1.0.0": {"dist": {"tarball": f"{REGISTRY}/artifact/a/1.0.0.tgz"}},
        "1.5.0": {"dist": {"tarball": f"{REGISTRY}/artifact/a/1.5.0.tgz"}},
        "2.0.0": {"dist": {"tarball": f"{REGISTRY}/artifact/a/2.0.0.tgz"}},
    }}
    row = _row("a")
    by_project = {"Demo": [row]}
    roadmap.capture_desired_targets(by_project)
    rows_by_name = {r.name: r for r in by_project["Demo"]}
    domains = {"a": roadmap._candidate_domain(row, "default", client)}
    return client, rows_by_name, domains


class R6_1AttemptTimeoutBoundaryTests(unittest.TestCase):
    """R6-1 acceptance: the available time is recomputed right before every
    solver call; the timeout never exceeds configured nor remaining-minus-
    reserve; no 1s floor; an impossible attempt is refused, not started."""

    def _helper(self, remaining: float, default_ms: int = 30_000):
        client = types.SimpleNamespace(deadline=_Clock(remaining))
        return roadmap._draft_solver_attempt_timeout_ms(client, default_ms)

    def test_no_deadline_returns_none(self):
        client = types.SimpleNamespace(deadline=None)
        self.assertIsNone(roadmap._draft_solver_attempt_timeout_ms(client, 30_000))
        clock = types.SimpleNamespace(deadline=roadmap.DeadlineClock(None))
        self.assertIsNone(roadmap._draft_solver_attempt_timeout_ms(clock, 30_000))

    def test_respects_remaining_minus_reserve_without_floor(self):
        # remaining=1.2s, reserve=1s -> usable 0.2s, net min(30, 0.15) -> 150ms.
        # Never the old 1000ms floor.
        self.assertEqual(150, self._helper(1.2))
        # 1.04s -> usable 0.04s less than the net minimum -> refuse to start.
        self.assertEqual(0, self._helper(1.04))
        # 0.5s -> already inside the reserve -> refuse to start.
        self.assertEqual(0, self._helper(0.5))

    def test_configured_timeout_is_the_upper_bound(self):
        # configured 500ms with a huge remaining budget stays 500ms, not 1000.
        self.assertEqual(500, self._helper(100.0, default_ms=500))

    def test_first_attempt_matches_audit_repro_remaining_ten(self):
        # The audit reproduced first-attempt 8950ms at remaining 10s; the
        # recompute preserves that and only fixes the FOLLOWING attempts.
        self.assertEqual(8950, self._helper(10.0))

    def test_attempt_timeout_recomputed_before_every_refinement_attempt(self):
        client, rows_by_name, domains = _component_ctx(10.0)
        calls = []

        def exact_target(model, timeout_ms=30_000, operation_id=None):
            calls.append({"timeout_ms": timeout_ms, "operation_id": operation_id})
            if len(calls) == 1:
                # Simulate the first attempt spending the budget down to 1.2s.
                client.deadline.set(1.2)
                return ExactSolveResult(
                    backend="z3", status="optimal", assignment={"a": "1.5.0"},
                    score=model.assignment_score({"a": "1.5.0"}),
                )
            return ExactSolveResult(
                backend="z3", status="optimal", assignment={"a": "2.0.0"},
                score=model.assignment_score({"a": "2.0.0"}),
            )

        def installable(row, version, client, *, trusted_target=""):
            return version != "1.5.0"

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target), \
             mock.patch.object(roadmap, "_candidate_registry_installable", side_effect=installable):
            report = roadmap._run_z3_peer_component(
                ["a"], rows_by_name, domains, client, "default", [], None, None,
                budget_capped=True,
            )

        # First attempt started at 10s remaining (8950ms); registry refinement
        # consumed budget to 1.2s, so the SECOND attempt gets a reduced timeout
        # instead of reusing the stale first value.
        self.assertEqual([8950, 150], [c["timeout_ms"] for c in calls])
        # distinct real attempt ids, both recorded in the report
        self.assertEqual(2, len({c["operation_id"] for c in calls}))
        self.assertNotEqual(calls[0]["operation_id"], calls[1]["operation_id"])
        self.assertEqual(2, report["attemptCount"])
        self.assertEqual([c["operation_id"] for c in calls], [a["attemptId"] for a in report["attempts"]])
        self.assertEqual([8950, 150], [a["timeoutMs"] for a in report["attempts"]])
        self.assertEqual({"a": "2.0.0"}, report["assignment"])
        # report timeoutMs = the timeout of the attempt that produced the outcome
        self.assertEqual(150, report["timeoutMs"])

    def test_budget_exhausted_before_attempt_never_starts_solver(self):
        client, rows_by_name, domains = _component_ctx(0.4)

        def forbidden_call(*_args, **_kwargs):
            raise AssertionError("solver must not start when the budget is already gone")

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=forbidden_call):
            report = roadmap._run_z3_peer_component(
                ["a"], rows_by_name, domains, client, "default", [], None, None,
                budget_capped=True,
            )

        self.assertEqual("budget_exhausted_before_attempt", report["status"])
        self.assertEqual(0, report["timeoutMs"])
        self.assertIn("budget exhausted before this exact attempt", report["detail"])
        self.assertEqual(1, report["attemptCount"])
        entry = report["attempts"][0]
        self.assertEqual("not_started", entry["status"])
        self.assertEqual("", entry["attemptId"])  # never started -> no fabricated id/finish
        self.assertEqual(0, entry["timeoutMs"])
        self.assertNotIn("elapsedMs", entry)
        self.assertEqual("BUDGET_EXHAUSTED", roadmap._terminal_status_for_exact_solver(report["status"]).value)

    def test_restricted_remaining_after_model_build_is_respected(self):
        # The acceptance wants the post-build remaining checked. `_Clock(2.0)`
        # has usable 1.0s (>=0.05 net), so a normal attempt is allowed; a
        # remaining inside the reserve refuses instead of raising to 1000ms.
        client, rows_by_name, domains = _component_ctx(2.0)
        calls = []

        def exact_target(model, timeout_ms=30_000, operation_id=None):
            calls.append(timeout_ms)
            return ExactSolveResult(backend="z3", status="unknown", detail="no reason given")

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target):
            report = roadmap._run_z3_peer_component(
                ["a"], rows_by_name, domains, client, "default", [], None, None,
                budget_capped=True,
            )
        self.assertEqual([950], calls)  # 2.0 - 1.0 reserve - 0.05 net = 0.95s
        self.assertEqual("unknown", report["status"])

    def test_timely_unresolved_outcome_commits_rows_and_report_under_small_budget(self):
        """R6-1 acceptance: a real supervisor with a small budget -- the timely
        UNFINISHED outcome commits the unresolved flag AND its report together."""
        client = roadmap.LiveDataClient(REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
        client.deadline = roadmap.DeadlineClock(1.35)
        client.npm_cache["a"] = {"versions": {
            "1.0.0": {"dist": {"tarball": f"{REGISTRY}/artifact/a/1.0.0.tgz"}},
            "2.0.0": {"dist": {"tarball": f"{REGISTRY}/artifact/a/2.0.0.tgz"}},
        }}
        client.registry_artifact_cache[("a", "2.0.0")] = {
            "status": "available", "tarballUrl": f"{REGISTRY}/artifact/a/2.0.0.tgz",
        }
        row = _row("a")
        by_project = {"Demo": [row]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)

        def exact_target(model, timeout_ms=30_000, operation_id=None):
            import time
            time.sleep(0.12)
            captured_timeout[0] = timeout_ms
            return ExactSolveResult(backend="z3", status="unknown", detail="timeout")

        captured_timeout = [None]
        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target):
            roadmap.run_supervised_planning(
                "planning",
                client.deadline,
                lambda working: roadmap.resolve_peer_compatibility(
                    working, client, modes=("default",), apply_results=True,
                    shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                    partial_on_incomplete=True,
                    run_context={"runId": "audit-r6-1-supervisor"},
                ),
                by_project,
            )
        # the supervisor splices the deep-copied working rows back: the shared
        # list now holds NEW objects, so re-fetch the committed row
        committed = by_project["Demo"][0]
        self.assertTrue(committed.peer_compat_unresolved)
        report = committed.peer_compat_unresolved_report
        self.assertIsNotNone(report)
        self.assertEqual("unknown", report["status"])
        self.assertEqual(1, report["attemptCount"])
        self.assertNotEqual("", report["attempts"][0]["attemptId"])
        # the actually passed attempt timeout stays within remaining minus reserve
        self.assertLessEqual(int(report["attempts"][0]["timeoutMs"]), 350)
        self.assertIsNotNone(captured_timeout[0])
        self.assertEqual(report["attempts"][0]["timeoutMs"], captured_timeout[0])


class R6_2OperationChainTests(unittest.TestCase):
    """R6-2 acceptance: attempt ids are generated before the solver call, used
    by the instrumentation events, and linked from the component report."""

    def _fake_model(self):
        return types.SimpleNamespace(packages=(), constraints=(), requirements=())

    def test_solve_z3_exact_uses_passed_operation_id_in_start_finish_and_result(self):
        events = []
        original = peer_solver_z3._solve_z3_exact_impl
        peer_solver_z3._solve_z3_exact_impl = (
            lambda model, *, timeout_ms=30_000: ExactSolveResult(
                backend="z3", status="optimal", assignment={}, score=(), elapsed_ms=7
            )
        )
        try:
            with mock.patch.object(
                peer_solver_z3, "emit_observability_event",
                side_effect=lambda event, **fields: events.append((event, fields)),
            ):
                result = peer_solver_z3.solve_z3_exact(
                    self._fake_model(), timeout_ms=1234, operation_id="z3-attempt-1"
                )
        finally:
            peer_solver_z3._solve_z3_exact_impl = original

        self.assertEqual("z3-attempt-1", result.operation_id)
        starts = [fields for event, fields in events if event == "solver.z3.start"]
        finishes = [fields for event, fields in events if event == "solver.z3.finish"]
        self.assertEqual(["z3-attempt-1"], [s["operationId"] for s in starts])
        self.assertEqual(["z3-attempt-1"], [s["operationId"] for s in finishes])
        self.assertEqual([1234], [s["timeoutMs"] for s in starts])
        self.assertEqual(["optimal"], [s["status"] for s in finishes])

    def test_solve_z3_exact_without_operation_id_generates_consistent_one(self):
        events = []
        original = peer_solver_z3._solve_z3_exact_impl
        peer_solver_z3._solve_z3_exact_impl = (
            lambda model, *, timeout_ms=30_000: ExactSolveResult(
                backend="z3", status="unknown", detail="no reason given", elapsed_ms=3
            )
        )
        try:
            with mock.patch.object(
                peer_solver_z3, "emit_observability_event",
                side_effect=lambda event, **fields: events.append((event, fields)),
            ):
                result = peer_solver_z3.solve_z3_exact(self._fake_model(), timeout_ms=5_000)
        finally:
            peer_solver_z3._solve_z3_exact_impl = original

        self.assertTrue(result.operation_id.startswith("z3-"))
        for event, fields in events:
            self.assertEqual(result.operation_id, fields["operationId"])

    def test_component_report_links_real_attempt_ids_and_variants(self):
        client = roadmap.LiveDataClient(REGISTRY, timeout=1, batch_size=10, sleep_sec=0)
        for version in ("1.0.0", "2.0.0"):
            client.npm_cache.setdefault("a", {}).setdefault("versions", {})[version] = {
                "name": "a", "version": version,
                "dist": {"tarball": f"{REGISTRY}/artifact/a/{version}.tgz"},
            }
            client.registry_artifact_cache[("a", version)] = {
                "status": "available", "tarballUrl": f"{REGISTRY}/artifact/a/{version}.tgz",
            }
        row = _row("a")
        by_project = {"Demo": [row]}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        captured = {}

        def exact_target(model, timeout_ms=30_000, operation_id=None):
            captured["operation_id"] = operation_id
            captured["timeout_ms"] = timeout_ms
            return ExactSolveResult(backend="z3", status="unknown", detail="no reason given")

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=exact_target):
            roadmap.resolve_peer_compatibility(
                by_project, client, modes=("default",), apply_results=False,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                partial_on_incomplete=True,
                run_context={"runId": "audit-r6-2", "productMode": "draft"},
            )

        report = row.peer_compat_unresolved_report
        self.assertIsNotNone(report)
        self.assertEqual(1, report["attemptCount"])
        # the artifact's per-attempt record carries the SAME id the real
        # instrumentation used (captured at the solve boundary)
        self.assertEqual(captured["operation_id"], report["attempts"][0]["attemptId"])
        self.assertEqual(captured["timeout_ms"], report["attempts"][0]["timeoutMs"])
        # component / variant / product mode stored separately, existing
        # operationId (component-scoped) semantics unchanged
        self.assertTrue(report["operationId"].startswith("z3-audit-r6-2-"))
        self.assertEqual("default", report["solverVariant"])
        self.assertEqual("draft", report["productMode"])
        self.assertEqual("default", report["mode"])
        self.assertFalse(report["confirmedTimeout"] is True)
        self.assertEqual(30000, report["attempts"][0]["timeoutMs"])


class R6PinnedFeasibleStatusTests(unittest.TestCase):
    """R6 (v0.2.144 re-acceptance): a fully pinned, constraint-consistent
    component is reported as FEASIBLE (decided by the bounded check on the
    residual fallback), never as a fresh "optimal" -- the pinned path did not
    prove global optimality. Consumers still treat it as decided (SAT terminal,
    non-unresolved, alternative-candidate), and the exact solver must not be
    started for an already consistent pin."""

    def test_pinned_consistent_component_is_feasible_not_optimal(self):
        client, rows_by_name, domains = _component_ctx(10.0)

        def forbidden_solve(*_args, **_kwargs):
            raise AssertionError("a consistent fully-pinned component must not start a fresh global solve")

        with mock.patch.object(roadmap, "solve_z3_exact", side_effect=forbidden_solve):
            report = roadmap._run_z3_peer_component(
                ["a"], rows_by_name, domains, client, "default", [], None, None,
                budget_capped=True, residual_targets={"a": "1.0.0"},
            )
        self.assertEqual("feasible", report["status"])
        self.assertNotEqual("optimal", report["status"])
        self.assertTrue(report.get("pinned"))
        self.assertEqual("queued-fallback", report.get("proof"))
        self.assertEqual({"a": "1.0.0"}, report["assignment"])
        self.assertEqual(0, report["attemptCount"])
        self.assertEqual("SAT_PROVEN", roadmap._terminal_status_for_exact_solver(report["status"]).value)

    def test_feasible_maps_to_proven_terminal_never_unknown(self):
        self.assertEqual("SAT_PROVEN", roadmap._terminal_status_for_exact_solver("feasible").value)
        self.assertEqual("SAT_PROVEN", roadmap._terminal_status_for_exact_solver("optimal").value)
        self.assertNotEqual("SAT_PROVEN", roadmap._terminal_status_for_exact_solver("unknown").value)

    def test_resolve_peer_compatibility_keeps_pinned_rows_non_unresolved(self):
        # The verification consumers must NOT push a feasible pinned component
        # into the unresolved bucket: it is decided.
        client, rows_by_name, domains = _component_ctx(10.0)
        row = rows_by_name["a"]
        report = roadmap._run_z3_peer_component(
            ["a"], rows_by_name, domains, client, "default", [], None, None,
            budget_capped=True, residual_targets={"a": "1.0.0"},
        )
        self.assertEqual("feasible", report["status"])
        self.assertFalse(getattr(row, "peer_compat_unresolved", False))
