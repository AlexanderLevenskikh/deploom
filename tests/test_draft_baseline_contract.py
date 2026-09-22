from pathlib import Path
import ast
import os
import sys
import tempfile
import unittest

import dependency_live_roadmap_generator as roadmap

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
DESKTOP_MAIN = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
DIALOG = (ROOT / "desktop" / "src" / "components" / "BaselineIntentDialog.tsx").read_text(encoding="utf-8")
TYPES = (ROOT / "desktop" / "src" / "types.ts").read_text(encoding="utf-8")


def load_tests(
    loader: unittest.TestLoader,
    standard_tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    """Make the free ``test_*`` functions visible to unittest.

    run_tool_tests.py loads modules via spec_from_file_location + exec_module
    and collects with unittest's loadTestsFromModule, which only collects
    unittest.TestCase subclasses -- standalone pytest-style functions were
    silently dropped from the CI unit suite. This hook wraps them explicitly.
    """
    del loader
    del pattern
    free_tests: list[unittest.TestCase] = []
    for name, value in list(globals().items()):
        if (
            name.startswith("test_")
            and callable(value)
            and getattr(value, "__module__", None) == __name__
            and value is not None
        ):
            free_tests.append(unittest.FunctionTestCase(value))
    return unittest.TestSuite([standard_tests, *free_tests])


def test_free_functions_are_collectable_by_unittest_loader() -> None:
    collected = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    assert collected.countTestCases() >= 5, "unittest must collect every Draft contract test"


def test_draft_generator_python_syntax_is_valid() -> None:
    ast.parse(GENERATOR)


def test_draft_cli_and_authority_markers_are_explicit() -> None:
    assert '"--draft-baseline"' in GENERATOR
    assert "--capture-baseline and --draft-baseline are mutually exclusive" in GENERATOR
    assert '"verificationStatus": "NOT_VERIFIED"' in GENERATOR
    assert '"authority": "PLANNING_ONLY"' in GENERATOR
    assert '"compatibility": "UNKNOWN"' in GENERATOR


def test_draft_static_path_skips_physical_verification_and_proof_publication() -> None:
    assert "if args.draft_baseline:" in GENERATOR
    assert "resolve_peer_compatibility(" in GENERATOR
    assert "Draft Baseline: ProofEnvelope publication skipped" in GENERATOR
    assert 'proven_dependency_state = {"schemaVersion": 1, "projects": {}}' in GENERATOR
    assert "existing verified proof state left untouched" in GENERATOR


def test_desktop_draft_is_invocation_local_and_does_not_advance_flow() -> None:
    assert "export type BaselineProofMode = 'VERIFIED' | 'DRAFT'" in TYPES
    assert "delete durable.proofMode" in DESKTOP_MAIN
    assert "job.action === 'baseline' && job.baselineProofMode === 'DRAFT'" in DESKTOP_MAIN
    assert "DRAFT_BASELINE_AUTOPILOT_FORBIDDEN" in DESKTOP_MAIN
    # Draft finalizes exclusively through its run-scoped artifacts: the import
    # is scoped to the exact run/project and a missing manifest is a typed
    # FAILED result instead of a silent exit-0 success (R1).
    assert "if (job.baselineProofMode === 'DRAFT')" in DESKTOP_MAIN
    assert "importDraftResult(job.workspace, job.projectName, job.runId)" in DESKTOP_MAIN
    assert "DRAFT_RESULT_MISSING" in DESKTOP_MAIN
    assert "DRAFT_RUN_ID_MISSING" in DESKTOP_MAIN
    # The verified snapshot path stays separate: Draft never snapshots legacy
    # roadmap outputs into the shared verified UI cache (R5).
    assert "if (job.baselineProofMode === 'DRAFT')" in DESKTOP_MAIN
    assert "PROJECT_ARTIFACT_SNAPSHOT_FAILED" in DESKTOP_MAIN
    assert "baselineProofMode === 'DRAFT' && job.runId ? { runId: job.runId }" in DESKTOP_MAIN


def test_desktop_draft_writer_and_reader_resolve_the_same_artifact_root() -> None:
    # R6: writer passes --artifacts-dir <ws>/.dependency-roadmap/artifacts and
    # the reader must resolve the SAME path, regardless of where
    # settings.project.json lives (root-level vs .dependency-roadmap).
    assert "'--artifacts-dir', join(workspace.path, '.dependency-roadmap', 'artifacts')" in DESKTOP_MAIN
    assert "return join(workspace.path, '.dependency-roadmap', 'artifacts')" in DESKTOP_MAIN
    assert "function draftManifestPath" in DESKTOP_MAIN


def test_generator_draft_skips_legacy_roadmap_writes() -> None:
    # R5: a Draft publishes ONLY run-scoped artifacts; the legacy MD/JSON/HTML
    # reports must not be written so a planning-only result cannot shadow a
    # verified roadmap/dashboard.
    assert "if not args.draft_baseline:" in GENERATOR
    assert "writing roadmap artifacts" in GENERATOR
    assert "R5: a Draft publishes ONLY its run-scoped artifacts" in GENERATOR


def test_generator_draft_honours_deadline_at_finalization() -> None:
    # R2: an expiry right before/at publication must publish DRAFT_PARTIAL,
    # not crash the run without an artifact.
    assert 'deadline_clock.check("draft-plane")' in GENERATOR
    assert "Draft deadline exceeded at finalization" in GENERATOR


def test_generator_draft_builds_local_inventory_before_network() -> None:
    # R2: local manifest+lockfile inventory is gathered before any deadline-
    # sensitive work so an early expiry publishes the complete local
    # dependency list, not an empty plan with zero unknowns.
    assert "def _draft_local_inventory_rows" in GENERATOR
    assert "_draft_local_inventory_rows(project, overrides)" in GENERATOR
    assert "inventory из локального manifest/lockfile" in GENERATOR


def test_generator_draft_tolerates_unsupported_package_managers() -> None:
    # R7: pnpm / Yarn Berry are a Verified-step capability boundary, not a ban
    # on a theoretical planning-only Draft plan.
    assert "PACKAGE_MANAGER_PNPM_UNSUPPORTED" in GENERATOR
    assert "PACKAGE_MANAGER_YARN_BERRY_UNSUPPORTED" in GENERATOR
    assert "manifest-only precision" in GENERATOR


def test_generator_osv_unavailable_never_becomes_zero() -> None:
    # R3: unavailable OSV evidence must read as UNKNOWN, not as an empty
    # finding list. Safe targets are not invented from missing evidence.
    assert "safe target по уязвимостям не вычислялся" in GENERATOR
    assert 'current_summary = "unknown"' in GENERATOR
    assert 'min_nv = "неизвестно"' in GENERATOR


def test_draft_ui_and_external_agent_handoff_are_visible() -> None:
    assert "Create Draft and show prompt" in DIALOG
    assert "Build Draft for agent handoff" in DIALOG
    assert "DRAFT BASELINE / PLANNING ONLY" in GENERATOR


def _make_row(**overrides):
    base = dict(
        project="tiny-basic",
        package_dir="C:/projects/tiny-basic",
        name="uuid",
        kind="runtime",
        requested_spec="10.0.0",
        current_version="10.0.0",
        current_source="package-lock.json",
        latest_version="registry unavailable",
        current_vulns="unknown",
        min_no_critical="неизвестно", min_no_high="неизвестно", min_no_vuln="неизвестно",
        min_lag_12m="—", min_lag_9m="—", min_lag_6m="—", min_lag_3m="—",
        group=2,
        reason="runtime/API hygiene",
        notes="",
    )
    base.update(overrides)
    return roadmap.DependencyRow(**base)


def _make_health(**overrides) -> roadmap.ProjectHealth:
    base = dict(
        project="tiny-basic", status="yellow", status_rank=1,
        total=1, lag_ok_12m=0, lag_bad_12m=0, lag_ok_pct=100.0,
        critical=0, high=0, moderate=0, low=0, unknown=1,
        reason="lag-policy target неизвестен для 1 зависимостей",
        lag_unknown=1, lag_needed_for_yellow=0,
        yellow_plan_required=0, yellow_projected_lag_ok=0,
        yellow_projected_lag_pct=100.0, yellow_plan_shortfall=0,
    )
    base.update(overrides)
    return roadmap.ProjectHealth(**base)


def test_draft_deadline_clock_enforces_and_reports_phase() -> None:
    import time

    clock = roadmap.DeadlineClock(0.001)
    time.sleep(0.01)
    with _expect_budget_exceeded("phase-boundary"):
        clock.check("phase-boundary")
    disabled = roadmap.DeadlineClock(None)
    disabled.check("any-phase")
    snap = disabled.as_dict()
    assert snap["deadlineSeconds"] is None
    assert snap["remainingMs"] is None


def _expect_budget_exceeded(phase: str):
    class _Raise:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, _traceback):
            assert exc_type is roadmap.DraftBudgetExceeded, f"expected DraftBudgetExceeded, got {exc_type}"
            assert exc_value.phase == phase
            return True

    return _Raise()


def test_draft_row_without_registry_metadata_is_unknown_and_gets_no_invented_target() -> None:
    row = _make_row()
    assert row.latest_version == "registry unavailable"
    assert roadmap._row_metadata_known(row) is False
    chosen = roadmap._draft_target_for_major(row)
    assert chosen == roadmap.NO_ACTION
    assert roadmap._draft_row_status(row, chosen) == "unknown-metadata"
    plan = roadmap.build_draft_plan(
        {"tiny-basic": [row]},
        {},
        {"tiny-basic": _make_health()},
    )
    assert plan["counts"]["unknown-metadata"] == 1
    assert plan["counts"]["proposed"] == 0
    assert [u["package"] for u in plan["unknowns"]] == ["uuid"]
    row_entry = plan["proposals"][0]["rows"][0]
    assert row_entry["target"] is None
    assert row_entry["latest"] == "registry unavailable"


def test_draft_publish_writes_run_scoped_artifacts_atomically() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        roadmap.set_draft_artifacts_base(base)
        try:
            row = _make_row()
            manifest = roadmap.publish_draft_result(
                run_id="run-test-1",
                workspace_id="ws-test",
                project_id="tiny-basic",
                mode="draft",
                rows_by_project={"tiny-basic": [row]},
                projects_by_name={},
                health_by_project={"tiny-basic": _make_health()},
                status="DRAFT_READY",
                partial_reason=None,
                deadline=roadmap.DeadlineClock(None),
            )
            draft_dir = base / "runs" / "run-test-1" / "draft"
            for name in ("result.json", "plan.json", "prompt.md", "summary.md"):
                assert (draft_dir / name).is_file(), f"missing {name}"
            assert manifest["status"] == "DRAFT_READY"
            assert manifest["verificationStatus"] == "NOT_VERIFIED"
            assert manifest["authority"] == "PLANNING_ONLY"
            assert manifest["compatibility"] == "UNKNOWN"
            assert manifest["metadata"]["unknownPackages"] == ["uuid"]
            assert manifest["metadata"]["unknown"] == 1
            assert manifest["proposals"]["unknown"] == 1
            prompt = (draft_dir / "prompt.md").read_text(encoding="utf-8")
            assert "DRAFT BASELINE / PLANNING ONLY" in prompt
            assert "NOT_VERIFIED" in prompt
        finally:
            roadmap.set_draft_artifacts_base(None)


def test_generator_tolerates_offline_registry_in_draft_mode() -> None:
    from unittest import mock

    class _BrokenClient:
        def __init__(self) -> None:
            self.npm_cache = {}
            self.registry = "https://registry.invalid/"
            self.timeout = 1.0
            self.batch_size = 1
            self.sleep_sec = 0

        def fetch_npm_metadata(self, pkg: str):
            raise roadmap.RegistryInfrastructureError(f"REGISTRY_METADATA_UNAVAILABLE: {pkg}: offline")

    # The prefetch must degrade (not raise) when tolerance is requested.
    client = _BrokenClient()
    roadmap._prefetch_registry_metadata(
        client,
        [("uuid", "runtime", "10.0.0")],
        tolerate=True,
    )
    assert client.npm_cache.get("uuid") is None
    # Without tolerance the same failure must stay a hard error.
    strict = _BrokenClient()
    try:
        roadmap._prefetch_registry_metadata(
            strict,
            [("uuid", "runtime", "10.0.0")],
            tolerate=False,
        )
        assert False, "registry failure must remain a hard error outside Draft"
    except roadmap.RegistryInfrastructureError:
        pass


def test_draft_manifest_and_command_line_contract_is_run_scoped() -> None:
    assert '"--run-id"' in GENERATOR
    assert '"--workspace-id"' in GENERATOR
    assert '"--project-id"' in GENERATOR
    assert '"--mode"' in GENERATOR
    assert '"--draft-deadline-seconds"' in GENERATOR
    assert '"--artifacts-dir"' in GENERATOR
    assert 'set_draft_artifacts_base(artifacts_dir)' in GENERATOR
    # Desktop threads the same identity through CLI flags and env.
    assert "DEPLOOM_RUN_ID" in DESKTOP_MAIN
    assert "DEPLOOM_WORKSPACE_ID" in DESKTOP_MAIN
    assert "DEPLOOM_PROJECT_ID" in DESKTOP_MAIN
    assert "'--artifacts-dir', join(workspace.path, '.dependency-roadmap', 'artifacts')" in DESKTOP_MAIN
    assert "flow:get-current-draft-result" in DESKTOP_MAIN
    assert "getCurrentDraftResult" in TYPES
    assert "draftResult" in TYPES


def test_r9_numeric_target_policy_changes_health_gate_and_hash() -> None:
    """R9: the user's freshness goal is effective, not a label.

    80 -> 90 moves the real yellow gate (and the green/planning closures), the
    policy hash and the prompt's numeric goal; the old hardcoded-80 contract is
    gone from the planner.
    """
    from unittest import mock

    def _ratio(total: int, pct: int) -> int:
        return roadmap.required_ratio_count(total, (pct, 100))

    with mock.patch.object(roadmap, "EFFECTIVE_MIN_LAG_OK_PCT", 80):
        assert _ratio(10, roadmap.health_yellow_ratio()[0] / roadmap.health_yellow_ratio()[1] * 100) == 8
        assert roadmap.required_ratio_count(10, roadmap.health_yellow_ratio()) == 8
        assert roadmap.required_ratio_count(10, roadmap.health_green_ratio()) == 9
        assert roadmap.required_ratio_count(10, roadmap.health_planning_ratio()) == 9
    with mock.patch.object(roadmap, "EFFECTIVE_MIN_LAG_OK_PCT", 90):
        assert roadmap.required_ratio_count(10, roadmap.health_yellow_ratio()) == 9
        assert roadmap.required_ratio_count(10, roadmap.health_green_ratio()) == 10
        assert roadmap.required_ratio_count(10, roadmap.health_planning_ratio()) == 10

    env_patcher = mock.patch.dict(
        os.environ,
        {
            "DEPLOOM_BASELINE_MIN_LAG_OK_PCT": "80",
            "DEPLOOM_BASELINE_TARGET_LEVEL": "yellow",
        },
        clear=False,
    )
    env_patcher.start()
    try:
        hash80 = roadmap.draft_policy_hash(roadmap.policy_snapshot_from_env())
    finally:
        env_patcher.stop()
    env_patcher = mock.patch.dict(
        os.environ,
        {
            "DEPLOOM_BASELINE_MIN_LAG_OK_PCT": "90",
            "DEPLOOM_BASELINE_TARGET_LEVEL": "green",
        },
        clear=False,
    )
    env_patcher.start()
    try:
        hash90green = roadmap.draft_policy_hash(roadmap.policy_snapshot_from_env())
        snapshot = roadmap.policy_snapshot_from_env()
    finally:
        env_patcher.stop()
    assert hash80 != hash90green
    assert snapshot["targetLevel"] == "green"
    assert snapshot["minLagOkPct"] == "90"

    prompt = roadmap.build_draft_prompt(
        "run-r9", "ws", "proj", "draft", hash90green,
        {"projects": [], "proposals": [], "counts": {"proposed": 0}, "unknowns": []},
        {},
        language="ru",
        snapshot=snapshot,
    )
    assert "Цель запуска: уровень `green`, минимум актуальности `90%`" in prompt
    assert f"policyHash: `{hash90green}`" in prompt

    # The old source contract forbidding freshness controls is inverted: the
    # dialog now owns the numeric goal and the desktop threads it to the engine.
    assert "Минимум актуальности" in DIALOG
    assert "minLagOkPct: boundedInteger(nextMinLagOkPct, 80, 0, 100)" in DIALOG
    assert "targetLevel === 'green' ? 'green' : 'yellow'" in DIALOG
    assert "DEPLOOM_BASELINE_TARGET_LEVEL: effectiveIntent.targetLevel" in DESKTOP_MAIN
    assert "DEPLOOM_BASELINE_MIN_LAG_OK_PCT: String(effectiveIntent.minLagOkPct" in DESKTOP_MAIN
    assert "minLagOkPct?: number" in TYPES
    assert "targetLevel?: 'yellow' | 'green'" in TYPES
    assert "--target-level" in GENERATOR
    assert "--min-lag-ok-pct" in GENERATOR
    assert "EFFECTIVE_MIN_LAG_OK_PCT" in GENERATOR


def test_r9_green_closure_is_honest_not_partial_as_success() -> None:
    """Green closure (gate + 10) is an independent, honest projection: it is
    never reported as reached just because the run published (even though the
    plan carries both yellow and green projections for every row)."""
    health = roadmap.compute_project_health([_make_row(
        current_version="1.0.0",
        min_lag_12m="2.0.0",
        min_lag_9m=roadmap.NO_ACTION,
        min_lag_6m=roadmap.NO_ACTION,
        min_lag_3m=roadmap.NO_ACTION,
    )], "tiny-basic", None)
    # Default 80% gate: 1/1 is known, so yellow gate (80%) is already met but
    # green closure (90%) is not; the green shortfall must be visible instead of
    # being collapsed into yellow or into the run's partial status.
    assert health.green_required == 1
    assert health.green_projected_lag_ok == 0
    assert health.green_plan_shortfall == 1
    assert health.lag_needed_for_yellow == 1
    assert "green_required" in roadmap.dataclasses.asdict(health)
    assert "green_projected_lag_ok" in roadmap.dataclasses.asdict(health)
