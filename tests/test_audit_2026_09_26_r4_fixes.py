from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dependency_live_roadmap_generator as roadmap
from deploom_failure import build_failure, classify_failure, write_diagnostic_artifact
from peer_solver_model import ExactSolveResult


REGISTRY = "https://nexus.example/repository/npm-group"


def _make_row(name="uuid", **overrides):
    base = dict(
        project="tiny-basic",
        package_dir="C:/projects/tiny-basic",
        name=name,
        kind="runtime",
        requested_spec="10.0.0",
        current_version="10.0.0",
        current_source="package-lock.json",
        latest_version="10.0.1",
        current_vulns="C:0;H:0;M:0;L:0",
        min_no_critical="10.0.0", min_no_high="10.0.0", min_no_vuln="10.0.0",
        min_lag_12m="10.0.0", min_lag_9m="10.0.0", min_lag_6m="10.0.0", min_lag_3m="10.0.0",
        group=2,
        reason="runtime/API hygiene",
        notes="",
        target_default=roadmap.NO_ACTION,
        target_yellow=roadmap.NO_ACTION,
        target_green=roadmap.NO_ACTION,
        target_default_reason="display only",
        target_yellow_reason="display only",
        target_green_reason="display only",
    )
    base.update(overrides)
    return roadmap.DependencyRow(**base)


def _make_health(**overrides):
    base = dict(
        project="tiny-basic", status="yellow", status_rank=1,
        total=1, lag_ok_12m=0, lag_bad_12m=0, lag_ok_pct=100.0,
        critical=0, high=0, moderate=0, low=0, unknown=1,
        reason="lag-policy target",
        lag_needed_for_yellow=0, yellow_plan_required=0,
        yellow_plan_shortfall=0, yellow_projected_lag_ok=0,
        yellow_projected_lag_pct=100.0,
    )
    base.update(overrides)
    return roadmap.ProjectHealth(**base)


class R4D1DraftPartialSolverTests(unittest.TestCase):
    """D1: an UNFINISHED exact solver in a Draft run marks rows unresolved and
    publishes a partial plan instead of dying with exit 3; proven outcomes
    (unsat / unavailable) still stop the run."""

    def make_client(self):
        return roadmap.LiveDataClient(REGISTRY, timeout=1, batch_size=10, sleep_sec=0)

    def add_package(self, client, name, versions):
        records = {}
        for version, extra in versions.items():
            records[version] = {
                **extra,
                "dist": {"tarball": f"{REGISTRY}/artifact/{name}/{version}.tgz"},
            }
            client.registry_artifact_cache[(name, version)] = {
                "status": "available",
                "tarballUrl": f"{REGISTRY}/artifact/{name}/{version}.tgz",
            }
        client.npm_cache[name] = {"versions": records}

    @staticmethod
    def solver_row(name, current="1.0.0", desired="2.0.0"):
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

    def prepare(self, names, versions):
        client = self.make_client()
        for name, vers in versions.items():
            self.add_package(client, name, vers)
        rows = [self.solver_row(name) for name in names]
        by_project = {"Demo": rows}
        roadmap.capture_desired_targets(by_project)
        roadmap.enrich_registry_target_evidence(by_project, client)
        return client, rows, by_project

    def test_unknown_component_is_marked_unresolved_and_returned_not_raised(self):
        client, (row_a,), by_project = self.prepare(["a"], {"a": {"1.0.0": {}, "2.0.0": {}}})
        incomplete = []

        with mock.patch.object(
            roadmap, "solve_z3_exact",
            return_value=ExactSolveResult(backend="z3", status="unknown", detail="no reason given"),
        ):
            assignments = roadmap.resolve_peer_compatibility(
                by_project,
                client,
                modes=("default",),
                apply_results=False,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                partial_on_incomplete=True,
                incomplete_components_out=incomplete,
                run_context={"runId": "audit-r4-d1"},
            )

        self.assertTrue(row_a.peer_compat_unresolved)
        self.assertIn("status=unknown", row_a.peer_compat_unresolved_note)
        self.assertEqual({}, assignments["Demo"]["default"])  # component skipped, nothing invented
        self.assertEqual(1, len(incomplete))
        entry = incomplete[0]
        self.assertEqual("Demo", entry["project"])
        self.assertEqual("default", entry["mode"])
        self.assertEqual(["a"], entry["component"])
        self.assertEqual(1, entry["componentCount"])
        self.assertEqual("unknown", entry["status"])
        self.assertEqual("no reason given", entry["detail"])
        self.assertFalse(entry["confirmedTimeout"])
        self.assertEqual("SOLVER_UNKNOWN", entry["terminalStatus"])
        self.assertEqual("no reason given", entry["reasonUnknown"])
        self.assertEqual("30000", entry["timeoutMs"])

    def test_confirmed_timeout_is_recorded_not_called_no_reason(self):
        client, (row_a,), by_project = self.prepare(["a"], {"a": {"1.0.0": {}, "2.0.0": {}}})
        incomplete = []

        with mock.patch.object(
            roadmap, "solve_z3_exact",
            return_value=ExactSolveResult(backend="z3", status="unknown", detail="timeout; inside C-coded loop"),
        ):
            roadmap.resolve_peer_compatibility(
                by_project,
                client,
                modes=("default",),
                apply_results=False,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                partial_on_incomplete=True,
                incomplete_components_out=incomplete,
            )

        self.assertTrue(row_a.peer_compat_unresolved)
        self.assertIn("timeout", row_a.peer_compat_unresolved_note)
        self.assertTrue(incomplete[0]["confirmedTimeout"])
        self.assertIn("timeout", incomplete[0]["reasonUnknown"])

    def test_unresolved_status_budget_also_collects_not_raises(self):
        client, (row_a,), by_project = self.prepare(["a"], {"a": {"1.0.0": {}, "2.0.0": {}}})
        incomplete = []
        with mock.patch.object(
            roadmap, "solve_z3_exact",
            return_value=ExactSolveResult(
                backend="z3", status="unknown_refinement_budget",
                detail="registry refinement budget 3 exhausted",
            ),
        ):
            assignments = roadmap.resolve_peer_compatibility(
                by_project,
                client,
                modes=("default",),
                apply_results=False,
                shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                partial_on_incomplete=True,
                incomplete_components_out=incomplete,
            )
        self.assertTrue(row_a.peer_compat_unresolved)
        self.assertEqual("BUDGET_EXHAUSTED", incomplete[0]["terminalStatus"])
        self.assertEqual({}, assignments["Demo"]["default"])

    def test_proven_unsat_still_stops_even_in_partial_mode(self):
        client, _rows, by_project = self.prepare(["a"], {"a": {"1.0.0": {}, "2.0.0": {}}})
        incompletes = []
        with mock.patch.object(
            roadmap, "solve_z3_exact",
            return_value=ExactSolveResult(backend="z3", status="unsat", detail="proven"),
        ):
            with self.assertRaises(roadmap.BaselineConstraintVerificationError) as raised:
                roadmap.resolve_peer_compatibility(
                    by_project,
                    client,
                    modes=("default",),
                    apply_results=False,
                    shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                    partial_on_incomplete=True,
                    incomplete_components_out=incompletes,
                )
        self.assertEqual("EXACT_SOLVER_UNSAT_PROVEN", raised.exception.stop_code)
        self.assertEqual("UNSAT_PROVEN", raised.exception.terminal_status)
        self.assertEqual([], incompletes)

    def test_proven_unavailable_still_stops_even_in_partial_mode(self):
        client, _rows, by_project = self.prepare(["a"], {"a": {"1.0.0": {}, "2.0.0": {}}})
        incompletes = []
        with mock.patch.object(
            roadmap, "solve_z3_exact",
            return_value=ExactSolveResult(backend="z3", status="unavailable", detail="z3-solver is not installed"),
        ):
            with self.assertRaises(roadmap.BaselineConstraintVerificationError) as raised:
                roadmap.resolve_peer_compatibility(
                    by_project,
                    client,
                    modes=("default",),
                    apply_results=False,
                    shadow_solver_config_by_project={"Demo": {"solverBackend": "z3"}},
                    partial_on_incomplete=True,
                    incomplete_components_out=incompletes,
                )
        self.assertEqual("EXACT_SOLVER_UNAVAILABLE", raised.exception.stop_code)
        self.assertEqual("SOLVER_UNAVAILABLE", raised.exception.terminal_status)


class R4D1DraftPlanSurfaceTests(unittest.TestCase):
    """D1: the unresolved component reaches the Draft plan/manifest status and
    reads as DECIDED-NOTHING, never as a silent default."""

    def test_draft_row_status_peer_compat_unresolved_takes_priority(self):
        row = _make_row(peer_compat_unresolved=True, peer_compat_unresolved_note="n")
        self.assertEqual("peer-compat-unresolved", roadmap._draft_row_status(row, roadmap.NO_ACTION))

    def test_build_draft_plan_surfaces_unresolved_rows(self):
        row = _make_row(
            name="eslint",
            peer_compat_unresolved=True,
            peer_compat_unresolved_note=(
                "peer compatibility not decided by the exact solver (status=unknown); "
                "reason: no reason given; further review required"
            ),
        )
        plan = roadmap.build_draft_plan(
            {"tiny-basic": [row]},
            {},
            {"tiny-basic": _make_health()},
        )
        self.assertEqual(1, plan["counts"]["peer-compat-unresolved"])
        self.assertEqual(1, len(plan["unresolved"]))
        unres = plan["unresolved"][0]
        self.assertEqual("eslint", unres["package"])
        self.assertEqual("peer-compat-unresolved", unres["status"])
        self.assertIn("no reason given", unres["reason"])
        entry = plan["proposals"][0]["rows"][0]
        self.assertTrue(entry["peerCompatUnresolved"])
        self.assertIsNone(entry["target"])  # nothing invented as a decision
        self.assertEqual([u["package"] for u in plan["unknowns"]], ["eslint"])
        unknown = plan["unknowns"][0]
        self.assertEqual("peer-compat", unknown["clarity"])
        self.assertIn("no reason given", unknown["reason"])

    def test_publish_draft_result_manifest_carries_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roadmap.set_draft_artifacts_base(base)
            try:
                row = _make_row(
                    name="eslint",
                    peer_compat_unresolved=True,
                    peer_compat_unresolved_note="peer compatibility not decided (status=unknown)",
                )
                manifest = roadmap.publish_draft_result(
                    run_id="run-r4-d1",
                    workspace_id="ws-test",
                    project_id="tiny-basic",
                    mode="draft",
                    rows_by_project={"tiny-basic": [row]},
                    projects_by_name={},
                    health_by_project={"tiny-basic": _make_health()},
                    status="DRAFT_PARTIAL",
                    partial_reason="peer compatibility not decided: 1 package(s)",
                    deadline=roadmap.DeadlineClock(None),
                )
                self.assertEqual("DRAFT_PARTIAL", manifest["status"])
                self.assertEqual(1, len(manifest["unresolved"]))
                self.assertEqual("eslint", manifest["unresolved"][0]["package"])
                plan_path = base / "runs" / "run-r4-d1" / "draft" / "plan.json"
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                self.assertEqual(1, plan["counts"]["peer-compat-unresolved"])
                self.assertEqual(1, len(plan["unresolved"]))
            finally:
                roadmap.set_draft_artifacts_base(None)


class R4D2SolverClassificationTests(unittest.TestCase):
    """D2: classification by structured stop codes, never by the package name
    inside the message; project-check category requires check evidence."""

    def solver_failure(self, component):
        exc = roadmap._baseline_terminal_error(
            roadmap.BaselineTerminalStatus.SOLVER_UNKNOWN,
            "EXACT_SOLVER_UNKNOWN",
            f"fixture/default: component={component} status=unknown; detail=no reason given; "
            "unfinished exact proof is not a dependency decision",
            source="z3",
        )
        return build_failure(exc, context=exc.failure_context).to_envelope()

    def test_solver_unknown_is_package_name_independent(self):
        for component in ("package-a", "eslint", "typescript", "libjs", "eslint-plugin-x", "src/lint/foo"):
            envelope = self.solver_failure(component)
            self.assertEqual("SOLVER_UNKNOWN", envelope["category"], component)
            self.assertNotEqual("PROJECT_INCOMPATIBLE", envelope["category"], component)

    def test_project_incompatible_requires_check_evidence(self):
        message = "CANDIDATE_REJECTED: command=yarn lint:types; exitCode=2"
        with_evidence = build_failure(
            ValueError(message),
            check_evidence={"phase": "project-check", "command": "yarn lint:types", "exitCode": "2"},
        )
        self.assertEqual("PROJECT_INCOMPATIBLE", with_evidence.to_envelope()["category"])
        without_evidence = build_failure(ValueError(message))
        self.assertNotEqual("PROJECT_INCOMPATIBLE", without_evidence.to_envelope()["category"])
        self.assertEqual("UNKNOWN", without_evidence.to_envelope()["category"])

    def test_message_level_timeout_still_classifies_as_budget(self):
        category, _, _ = classify_failure(
            roadmap._baseline_terminal_error(
                roadmap.BaselineTerminalStatus.SOLVER_UNKNOWN,
                "EXACT_SOLVER_UNKNOWN",
                "x: component=a status=unknown; detail=timeout; unfinished",
                source="z3",
            )
        )
        self.assertEqual("SOLVER_BUDGET_EXHAUSTED", category)

    def test_structured_budget_and_unsat_and_unavailable_categories(self):
        budget = roadmap._baseline_terminal_error(
            roadmap.BaselineTerminalStatus.BUDGET_EXHAUSTED,
            "EXACT_SOLVER_BUDGET_EXHAUSTED",
            "x: component=a; detail=exact refinement budget 3 exhausted",
            source="z3",
        )
        self.assertEqual("SOLVER_BUDGET_EXHAUSTED", classify_failure(budget)[0])

        unsat = roadmap._baseline_terminal_error(
            roadmap.BaselineTerminalStatus.UNSAT_PROVEN,
            "EXACT_SOLVER_UNSAT_PROVEN",
            "x: component=a; the authoritative finite-domain component has no satisfying assignment",
            source="z3",
        )
        self.assertEqual("EXACT_UNSAT_PROVEN", classify_failure(unsat)[0])

        unavailable = roadmap._baseline_terminal_error(
            roadmap.BaselineTerminalStatus.SOLVER_UNAVAILABLE,
            "EXACT_SOLVER_UNAVAILABLE",
            "x: component=a; no heuristic fallback was used",
            source="z3",
        )
        self.assertEqual("SOLVER_UNAVAILABLE", classify_failure(unavailable)[0])


class R4D3FailureContextTests(unittest.TestCase):
    """D3: the diagnostic envelope carries the structured solver context and the
    raw check evidence; the human text names the component, the limits and the
    actual times."""

    def test_envelope_carries_solver_context_and_evidence(self):
        exc = roadmap._baseline_terminal_error(
            roadmap.BaselineTerminalStatus.SOLVER_UNKNOWN,
            "EXACT_SOLVER_UNKNOWN",
            "libjs/red: component=eslint,typescript status=unknown; detail=no reason given; "
            "unfinished exact proof is not a dependency decision",
            source="z3",
            project="libjs",
            mode="red",
            extra_context={
                "component": "eslint,typescript",
                "componentCount": "2",
                "candidateCount": "24",
                "timeoutMs": "30000",
                "solverElapsedMs": "4123",
                "reasonUnknown": "no reason given",
                "runId": "audit-r4-d3",
                "phase": "peer-planning",
            },
        )
        envelope = build_failure(exc, context=exc.failure_context).to_envelope()
        self.assertEqual("SOLVER_UNKNOWN", envelope["category"])
        self.assertEqual("libjs", envelope["project"])
        self.assertEqual("red", envelope["mode"])
        self.assertEqual("peer-planning", envelope["phase"])
        ctx = envelope["context"]
        self.assertEqual("eslint,typescript", ctx["component"])
        self.assertEqual("2", ctx["componentCount"])
        self.assertEqual("30000", ctx["timeoutMs"])
        self.assertEqual("4123", ctx["solverElapsedMs"])
        self.assertEqual("audit-r4-d3", ctx["runId"])
        summary = build_failure(exc, context=exc.failure_context).human_summary()
        self.assertIn("Solver:", summary)
        self.assertIn("eslint,typescript", summary)
        self.assertIn("timeoutMs=30000", summary)
        self.assertIn("no reason given", summary)

    def test_diagnostic_artifact_includes_context(self):
        exc = roadmap._baseline_terminal_error(
            roadmap.BaselineTerminalStatus.BUDGET_EXHAUSTED,
            "EXACT_SOLVER_BUDGET_EXHAUSTED",
            "libjs/red: component=eslint; detail=timeout; unfinished",
            source="z3",
            project="libjs",
            mode="red",
            phase="peer-planning",
            command="z3",
            extra_context={
                "component": "eslint",
                "componentCount": "1",
                "timeoutMs": "30000",
                "solverElapsedMs": "30042",
                "reasonUnknown": "timeout",
                "confirmedTimeout": "true",
                "runId": "audit-r4-d3b",
            },
        )
        failure = build_failure(exc, context=exc.failure_context)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_diagnostic_artifact(exc, failure, directory=Path(tmp))
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual("libjs", payload["context"]["project"])
            self.assertEqual("red", payload["context"]["mode"])
            self.assertEqual("eslint", payload["context"]["component"])
            self.assertEqual("30000", payload["context"]["timeoutMs"])
            self.assertEqual("30042", payload["context"]["solverElapsedMs"])
            self.assertEqual("true", payload["context"]["confirmedTimeout"])
            self.assertEqual("audit-r4-d3b", payload["context"]["runId"])
            self.assertEqual("SOLVER_BUDGET_EXHAUSTED", payload.get("category") or classify_failure(exc)[0])


if __name__ == "__main__":
    unittest.main()
