from pathlib import Path
import ast
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
    assert "job.baselineProofMode !== 'DRAFT'" in DESKTOP_MAIN
    assert "DRAFT_BASELINE_AUTOPILOT_FORBIDDEN" in DESKTOP_MAIN


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
