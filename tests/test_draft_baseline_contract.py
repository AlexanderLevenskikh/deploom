from pathlib import Path
import ast
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import dependency_live_roadmap_generator as roadmap

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
DESKTOP_MAIN = (ROOT / "desktop" / "electron" / "main.ts").read_text(encoding="utf-8")
DRAFT_READER = (ROOT / "desktop" / "electron" / "draft-artifact-reader.ts").read_text(encoding="utf-8")
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
    # FAILED result instead of a silent exit-0 success (R1/T1).
    assert "if (job.baselineProofMode === 'DRAFT')" in DESKTOP_MAIN
    assert "importDraftResult(job.workspace, job.projectName, job.runId)" in DESKTOP_MAIN
    assert "importDraftResult" in DESKTOP_MAIN
    assert "draftReadFailureText" in DESKTOP_MAIN
    assert "DRAFT_RESULT_MISSING" in DRAFT_READER
    assert "DRAFT_RUN_ID_MISSING" in DESKTOP_MAIN
    # The verified snapshot path stays separate: Draft never snapshots legacy
    # roadmap outputs into the shared verified UI cache (R5).
    assert "if (job.baselineProofMode === 'DRAFT')" in DESKTOP_MAIN
    assert "PROJECT_ARTIFACT_SNAPSHOT_FAILED" in DESKTOP_MAIN
    assert "baselineProofMode === 'DRAFT' && job.runId ? { runId: job.runId }" in DESKTOP_MAIN


def test_desktop_draft_writer_and_reader_resolve_the_same_artifact_root() -> None:
    # R6: writer passes --artifacts-dir <ws>/.dependency-roadmap/artifacts and
    # the reader (production module used by main.ts) must resolve the SAME
    # path, regardless of where settings.project.json lives (root-level vs
    # .dependency-roadmap). Both sides are the `.dependency-roadmap/artifacts`
    # convention spelled identically in the two languages.
    assert "'--artifacts-dir', join(workspace.path, '.dependency-roadmap', 'artifacts')" in DESKTOP_MAIN
    assert "join(workspacePath, '.dependency-roadmap', 'artifacts')" in DRAFT_READER
    assert "draftArtifactsRoot" in DESKTOP_MAIN
    assert "draftManifestPath" in DRAFT_READER
    assert "draftReadStrict(workspace" in DESKTOP_MAIN


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


def test_draft_publish_binds_manifest_to_inventory_input_hashes_not_recompute() -> None:
    """F4: a partial/ready publish must record the INVENTORY-time identity the
    plan was derived from, never re-hash the inputs at publication time.
    Otherwise a manifest/package.json/settings edit landing between inventory
    and publish would silently bind the plan to other data."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        project = roadmap.ProjectSpec(name="tiny-basic", path=base / "proj", source_branch="main")
        project.path.mkdir(parents=True)
        (project.path / "package.json").write_text('{"name":"proj","version":"1.0.0"}', encoding="utf-8")
        (project.path / "package-lock.json").write_text("{}", encoding="utf-8")
        (project.path / ".dependency-roadmap").mkdir(parents=True, exist_ok=True)
        (project.path / ".dependency-roadmap" / "settings.project.json").write_text(
            '{"projects":[{"name":"proj","path":"."}]}', encoding="utf-8")
        inventory_hash = roadmap.draft_input_hash(project)
        # Inputs move AFTER inventory, before publish (lockfile edited).
        (project.path / "package-lock.json").write_text('{"changed": true}', encoding="utf-8")
        changed_hash = roadmap.draft_input_hash(project)
        assert inventory_hash != changed_hash

        roadmap.set_draft_artifacts_base(base)
        try:
            row = _make_row()
            manifest = roadmap.publish_draft_result(
                run_id="run-f4-inventory",
                workspace_id="ws-test",
                project_id="tiny-basic",
                mode="draft",
                rows_by_project={"tiny-basic": [row]},
                projects_by_name={},
                health_by_project={"tiny-basic": _make_health()},
                status="DRAFT_READY",
                partial_reason=None,
                deadline=roadmap.DeadlineClock(None),
                input_hashes={"tiny-basic": inventory_hash},
            )
            # The manifest carries the identity captured BEFORE the input moved:
            # the reader compares against today's files and reports stale,
            # instead of the writer silently re-binding the plan to new data.
            assert manifest["inputHashes"]["tiny-basic"] == inventory_hash
            assert manifest["inputHashes"]["tiny-basic"] != changed_hash
        finally:
            roadmap.set_draft_artifacts_base(None)
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

    80 -> 90 moves the real yellow gate (and the planning closure) and the
    policy hash and the prompt's numeric goal; the old hardcoded-80 contract is
    gone from the planner. Green always closes at 100% (T2): the green label
    == "every library matches its own lag policy", identical to the actual
    green status criterion (lag_bad == 0), so the projection is not softer.
    """
    from unittest import mock

    def _ratio(total: int, pct: int) -> int:
        return roadmap.required_ratio_count(total, (pct, 100))

    with mock.patch.object(roadmap, "EFFECTIVE_MIN_LAG_OK_PCT", 80):
        assert _ratio(10, roadmap.health_yellow_ratio()[0] / roadmap.health_yellow_ratio()[1] * 100) == 8
        assert roadmap.required_ratio_count(10, roadmap.health_yellow_ratio()) == 8
        assert roadmap.required_ratio_count(10, roadmap.health_green_ratio()) == 10
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
    assert "boundedInteger(nextMinLagOkPct, 80, 0, 100)" in DIALOG
    assert "targetLevel === 'green' ? 'green' : 'yellow'" in DIALOG
    assert "DEPLOOM_BASELINE_TARGET_LEVEL: effectiveIntent.targetLevel" in DESKTOP_MAIN
    assert "DEPLOOM_BASELINE_MIN_LAG_OK_PCT: String(effectiveIntent.minLagOkPct" in DESKTOP_MAIN
    assert "minLagOkPct?: number" in TYPES
    assert "targetLevel?: 'yellow' | 'green'" in TYPES
    assert "--target-level" in GENERATOR
    assert "--min-lag-ok-pct" in GENERATOR
    assert "EFFECTIVE_MIN_LAG_OK_PCT" in GENERATOR


def test_r9_green_closure_is_honest_not_partial_as_success() -> None:
    """Green closure (100% of every library's own lag policy, T2) is an
    independent, honest projection: it is never reported as reached just
    because the run published (even though the plan carries both yellow and
    green projections for every row)."""

    health = roadmap.compute_project_health([_make_row(
        current_version="1.0.0",
        min_lag_12m="2.0.0",
        min_lag_9m=roadmap.NO_ACTION,
        min_lag_6m=roadmap.NO_ACTION,
        min_lag_3m=roadmap.NO_ACTION,
    )], "tiny-basic", None)
    # Default 80% gate: this row is 0/1 compliant, so the yellow gate (80%) is
    # still unmet and the green closure (100%) is unmet too; both shortfalls
    # must be visible instead of being collapsed into the run's partial status.
    assert health.green_required == 1
    assert health.green_projected_lag_ok == 0
    assert health.green_plan_shortfall == 1
    assert health.lag_needed_for_yellow == 1
    assert "green_required" in roadmap.dataclasses.asdict(health)
    assert "green_projected_lag_ok" in roadmap.dataclasses.asdict(health)


def _lag_ok_row(**overrides):
    base = dict(
        project="tiny-basic", package_dir="C:/projects/tiny-basic", name="uuid",
        kind="runtime", requested_spec="10.0.0", current_version="10.0.0",
        current_source="package-lock.json", latest_version="10.2.0", current_vulns="0",
        min_no_critical="10.2.0", min_no_high="10.2.0", min_no_vuln="10.2.0",
        min_lag_12m="10.0.0", min_lag_9m="10.0.0", min_lag_6m="10.0.0", min_lag_3m="10.0.0",
        group=2, reason="runtime/API hygiene", notes="",
    )
    base.update(overrides)
    return roadmap.DependencyRow(**base)


def test_t3_green_requires_known_security() -> None:
    # A row whose OSV state is unknown must not produce a green verdict: unknown
    # is never treated as zero/healthy (T3).
    h = roadmap.compute_project_health([_lag_ok_row(current_vulns="unknown")], "tiny-basic", None)
    assert h.total == 1
    assert h.lag_bad_12m == 0
    assert h.unknown == 1
    assert h.security_unknown == 1
    assert h.status != "green"
    assert "security неизвестна" in h.reason
    assessed = roadmap.compute_project_health([_lag_ok_row(current_vulns="0")], "tiny-basic", None)
    assert assessed.status == "green"
    assert assessed.security_unknown == 0


def test_t3_unknown_security_with_known_metadata_reaches_plan_and_prompt() -> None:
    row = _lag_ok_row(current_vulns="unknown")
    health = roadmap.compute_project_health([row], "tiny-basic", None)
    plan = roadmap.build_draft_plan({"tiny-basic": [row]}, {}, {"tiny-basic": health})
    assert plan["counts"]["unknown-security"] == 1
    entry = next(u for u in plan["unknowns"] if u["package"] == "uuid")
    assert entry["clarity"] == "security"
    assert "OSV" in entry["reason"]
    prompt = roadmap.build_draft_prompt(
        "run-t3", "ws", "tiny-basic", "draft", "hash",
        plan, {}, language="ru", snapshot={"targetLevel": "yellow", "minLagOkPct": "80"},
    )
    assert "security coverage" in prompt
    assert "уязвимости неизвестны" in prompt
    assert "OSV" in prompt


def test_t3_empty_known_denominator_is_insufficient_data_not_100() -> None:
    row = _make_row()  # registry unavailable + unknown lag/vuln
    h = roadmap.compute_project_health([row], "tiny-basic", None)
    assert h.total == 0
    assert h.lag_unknown == 1
    assert h.insufficient_data is True
    assert h.lag_ok_pct == 0.0
    assert h.status == "yellow"
    assert "недостаточно данных" in h.reason
    plan = roadmap.build_draft_plan({"tiny-basic": [row]}, {}, {"tiny-basic": h})
    prompt = roadmap.build_draft_prompt(
        "run-t3", "ws", "tiny-basic", "draft", "hash",
        plan, {}, language="ru", snapshot={"targetLevel": "yellow", "minLagOkPct": "80"},
    )
    assert "недостаточно данных" in prompt


def test_t3_keep_current_stays_in_health_but_explicit_exclude_removes() -> None:
    keep = _lag_ok_row(name="is-number", current_vulns="unknown")
    normal = _lag_ok_row(name="uuid")
    rows = [keep, normal]
    with mock.patch.dict(os.environ, {"DEPLOOM_BASELINE_INTENT_JSON": json.dumps(
        {"schemaVersion": 1, "policies": {"is-number": "keep-current"}})}, clear=False):
        roadmap._BASELINE_INTENT_CACHE_RAW = "<unset>"
        roadmap._apply_baseline_intent_scope({"tiny-basic": rows})
    # keep-current defers the update but stays inside the health score (T3).
    assert not keep.scope_excluded
    assert keep.planner_deferred
    h = roadmap.compute_project_health(rows, "tiny-basic", None)
    assert h.excluded == 0
    assert h.total == 2
    assert h.metadata_total == 2
    assert h.security_total == 2
    assert h.security_unknown == 1
    # Explicit exclusion has a reason/size and does remove the row from the score.
    leftout = _lag_ok_row(name="leftout")
    leftout.scope_excluded = True
    leftout.exclusion_reason = "пользователь исключил: отдельный реестр"
    leftout.exclusion_source = "user"
    h2 = roadmap.compute_project_health(rows + [leftout], "tiny-basic", None)
    assert h2.excluded == 1
    assert h2.total == 2
    assert h2.metadata_total == 2
    assert h2.security_total == 2


def test_t3_critical_never_green_even_when_lag_compliant() -> None:
    h = roadmap.compute_project_health([_lag_ok_row(current_vulns="C:1")], "tiny-basic", None)
    assert h.status == "red"
    assert "Critical" in h.reason
    high2 = roadmap.compute_project_health([_lag_ok_row(current_vulns="H:2")], "tiny-basic", None)
    assert high2.status != "green"
    high1 = roadmap.compute_project_health([_lag_ok_row(current_vulns="H:1")], "tiny-basic", None)
    assert high1.status != "green"


class DraftDeadlineHardBoundTests(unittest.TestCase):
    """T4: the Draft deadline is an absolute monotonic bound, not a hint.

    These run real HTTP (no mocks) against a local server, mirroring the
    validator's slow-trickle reproduction.
    """

    def _trickle_client(self, port: int, deadline_seconds: float):
        client = roadmap.LiveDataClient(
            f"http://127.0.0.1:{port}", timeout=30, batch_size=1, sleep_sec=0.001,
            use_system_proxy=False,
        )
        client.set_deadline(roadmap.DeadlineClock(deadline_seconds))
        return client

    def test_slow_trickle_metadata_is_aborted_by_the_deadline(self) -> None:
        import threading
        import http.server
        import socketserver

        body = json.dumps({
            "name": "uuid", "dist-tags": {"latest": "10.0.0"},
            "versions": {"10.0.0": {"name": "uuid", "version": "10.0.0"}},
        }).encode("utf-8")
        # 24 bytes x 150ms = ~3.6s natural transfer. The per-byte read timeout
        # (1s) is never hit, so only the monotonic deadline can stop the read.

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.15)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):  # noqa: N802
                pass

        with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            started = time.monotonic()
            try:
                with self.assertRaises(roadmap.DraftBudgetExceeded):
                    self._trickle_client(server.server_address[1], 2.0).fetch_npm_metadata("uuid")
            finally:
                server.shutdown()
                server.server_close()
            elapsed = time.monotonic() - started
        # deadline 2.0s with a 1.0s finalize reserve: the slow trickle must be
        # cut off well before the natural ~3.6s transfer (and not return a
        # "success" that silently overran the budget).
        self.assertLess(elapsed, 2.5, f"trickle must abort at the deadline, took {elapsed:.2f}s")

    def test_budgeted_timeout_is_fractional_and_reserves_finalization(self) -> None:
        client = roadmap.LiveDataClient("http://registry.invalid", timeout=30, batch_size=1, sleep_sec=0, use_system_proxy=False)
        client.set_deadline(roadmap.DeadlineClock(9.5))
        connect, read = client._budgeted_timeout()
        # Fractional remainders are preserved (no floor-to-1s) and the
        # publication reserve is withheld from network work.
        self.assertGreater(read, 8.0)
        self.assertLessEqual(read, 9.5 - 1.0 + 1e-9)
        self.assertLess(connect, 9.5)
        # Less than the reserve left -> the call must fail fast instead of
        # starting a request that would eat the publication slice.
        tiny = roadmap.LiveDataClient("http://registry.invalid", timeout=30, batch_size=1, sleep_sec=0, use_system_proxy=False)
        tiny.set_deadline(roadmap.DeadlineClock(0.2))
        with self.assertRaises(roadmap.DraftBudgetExceeded):
            tiny._budgeted_timeout()
        # Bounded sleep cannot outlive the remaining budget either.
        sleeper = roadmap.LiveDataClient("http://registry.invalid", timeout=30, batch_size=1, sleep_sec=0, use_system_proxy=False)
        sleeper.set_deadline(roadmap.DeadlineClock(5.0))
        sleeper._bounded_sleep(60.0)
        self.assertGreaterEqual(sleeper.deadline.remaining, 1.0 - 0.5)

    def test_header_trickle_is_aborted_by_the_deadline(self) -> None:
        import threading
        import http.server
        import socketserver

        # A server that drips the RESPONSE HEADER BLOCK byte-by-byte (the
        # validator reproduction). Natural transfer ~50 x 0.1 = ~5s; the per-recv
        # read timeout never fires because every recv returns quickly. Only an
        # absolute bound on the header phase can stop this within the budget.
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                raw = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 4\r\nConnection: close\r\n\r\n"
                self.wfile.write(raw[:2])
                self.wfile.flush()
                try:
                    for byte in raw[2:]:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):  # noqa: N802
                pass

        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler) as server:
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            started = time.monotonic()
            try:
                with self.assertRaises(roadmap.DraftBudgetExceeded):
                    self._trickle_client(server.server_address[1], 2.0).fetch_npm_metadata("uuid")
            finally:
                server.shutdown()
                server.server_close()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.6, f"header trickle must abort at the deadline, took {elapsed:.2f}s")

    def test_fetch_bytes_binary_trickle_keeps_deadline_semantics(self) -> None:
        import threading
        import http.server
        import socketserver

        # fetch_bytes is used for registry tarballs/type evidence. A binary
        # body dripped one byte at a time must abort the run with
        # DraftBudgetExceeded (not be swallowed into a silent None).
        body = b"\x1f\x8b\x08\x00" * 7  # 28 bytes
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.12)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):  # noqa: N802
                pass

        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler) as server:
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            started = time.monotonic()
            try:
                client = self._trickle_client(server.server_address[1], 2.0)
                with self.assertRaises(roadmap.DraftBudgetExceeded):
                    client.fetch_bytes(f"http://127.0.0.1:{server.server_address[1]}/pkg/-/pkg-1.0.0.tgz")
            finally:
                server.shutdown()
                server.server_close()
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.6, f"binary trickle must abort at the deadline, took {elapsed:.2f}s")

    def test_supervised_worker_is_an_absolute_bound_for_uninterruptible_work(self) -> None:
        # The supervisor bounds CPU-heavy planning the same way it bounds the
        # network header phase: a function that never checks the clock itself
        # still cannot outlive the remaining budget.
        started = time.monotonic()
        with self.assertRaises(roadmap.DraftBudgetExceeded):
            roadmap.run_supervised("planning", roadmap.DeadlineClock(2.0), lambda: time.sleep(60))
        self.assertLess(time.monotonic() - started, 2.6)
        # Without a deadline the same fn runs inline (zero overhead).
        self.assertTrue(roadmap.run_supervised("planning", None, lambda: True))
        # Worker exceptions are re-raised, preserving DraftBudgetExceeded
        # raised inside the worker (e.g. by per-byte checks).
        with self.assertRaises(ValueError):
            roadmap.run_supervised("network", roadmap.DeadlineClock(60.0), lambda: (_ for _ in ()).throw(ValueError("boom")))
        with self.assertRaises(roadmap.DraftBudgetExceeded):
            roadmap.run_supervised("network", roadmap.DeadlineClock(60.0), lambda: (_ for _ in ()).throw(roadmap.DraftBudgetExceeded("network-read")))


def _row_with_variants(**overrides):
    base = dict(
        project="tiny-basic", package_dir="C:/projects/tiny-basic", name="uuid",
        kind="runtime", requested_spec="10.0.0", current_version="10.0.0",
        current_source="package-lock.json", latest_version="10.2.0", current_vulns="0",
        min_no_critical="10.2.0", min_no_high="10.2.0", min_no_vuln="10.2.0",
        min_lag_12m="10.0.0", min_lag_9m="10.0.0", min_lag_6m="10.0.0", min_lag_3m="10.0.0",
        group=2, reason="runtime/API hygiene", notes="",
    )
    base.update(overrides)
    return roadmap.DependencyRow(**base)


def test_t2_draft_target_follows_effective_target_level() -> None:
    """T2: the chosen goal level selects the plan variant.

    Green and yellow planners can legitimately pick different targets; the
    effective level (DEPLOOM_BASELINE_TARGET_LEVEL / --target-level) must pick
    which one becomes the executor handoff. The old code always took
    target_default/yellow, so a "green" run could hand out a yellow plan.
    """
    from unittest import mock

    row = _row_with_variants(
        target_default="10.1.0",
        target_yellow="10.1.0",
        target_green="10.2.0",
    )
    with mock.patch.object(roadmap, "EFFECTIVE_TARGET_LEVEL", "green"):
        assert roadmap._draft_target_for_major(row) == "10.2.0"
    with mock.patch.object(roadmap, "EFFECTIVE_TARGET_LEVEL", "yellow"):
        assert roadmap._draft_target_for_major(row) == "10.1.0"
    # Fallback: when the chosen level planned nothing, the default (legacy
    # handoff) and then the other variant still resolve.
    no_green = _row_with_variants(target_default=roadmap.NO_ACTION, target_yellow="10.3.0", target_green=roadmap.NO_ACTION)
    with mock.patch.object(roadmap, "EFFECTIVE_TARGET_LEVEL", "green"):
        assert roadmap._draft_target_for_major(no_green) == "10.3.0"
    # Deferred / excluded rows stay inert regardless of the level.
    deferred = _row_with_variants(target_green="10.2.0", planner_deferred=True)
    with mock.patch.object(roadmap, "EFFECTIVE_TARGET_LEVEL", "green"):
        assert roadmap._draft_target_for_major(deferred) == roadmap.NO_ACTION


def test_t2_zero_percent_goal_is_not_coerced_to_default() -> None:
    """T2: 0% is a legitimate goal ("no lag-policy slack at the gate").

    One lag-compliant library out of ten must satisfy a 0% gate; the old
    `int(raw) or 80`-style coercion would silently demand the 80% default.
    """
    from unittest import mock

    with mock.patch.object(roadmap, "EFFECTIVE_MIN_LAG_OK_PCT", 0):
        assert roadmap.required_ratio_count(10, roadmap.health_yellow_ratio()) == 0
        assert roadmap.required_ratio_count(10, roadmap.health_green_ratio()) == 10
        health = roadmap.compute_project_health([_lag_ok_row()] + [_lag_hard_row() for _ in range(9)], "tiny-basic", None)
        assert health.lag_ok_12m == 1
        assert health.lag_needed_for_yellow == 0


def _lag_hard_row(**overrides):
    base = dict(
        project="tiny-basic", package_dir="C:/projects/tiny-basic", name="uuid",
        kind="runtime", requested_spec="9.0.0", current_version="9.0.0",
        current_source="package-lock.json", latest_version="9.9.0", current_vulns="0",
        min_no_critical="9.9.0", min_no_high="9.9.0", min_no_vuln="9.9.0",
        min_lag_12m="9.9.0", min_lag_9m="9.9.0", min_lag_6m="9.9.0", min_lag_3m="9.9.0",
        group=2, reason="runtime/API hygiene", notes="",
    )
    base.update(overrides)
    return roadmap.DependencyRow(**base)


class DraftProgressHeartbeatTests(unittest.TestCase):
    """F5: the Draft progress heartbeat runs independently of blocking work,
    stops at the terminal finalize event, and stage percentages never claim a
    terminal 100 before the run has actually published."""

    @staticmethod
    def _parse(buffer) -> list:
        return [json.loads(line.replace("[draft-progress] ", "", 1)) for line in buffer.splitlines() if "[draft-progress]" in line]

    def test_f5_heartbeat_emits_between_events_and_stops_at_terminal(self) -> None:
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        client = roadmap.LiveDataClient("https://registry.example/", 15, 8, 0.0)
        old_heartbeat = roadmap.DRAFT_PROGRESS_HEARTBEAT_SECONDS
        roadmap.DRAFT_PROGRESS_HEARTBEAT_SECONDS = 0.05
        try:
            client.set_draft_run("run-f5-hb")
            with redirect_stdout(buffer):
                time.sleep(0.18)
                while buffer.getvalue().count("[draft-progress]") < 2:
                    time.sleep(0.02)
                before_terminal = buffer.getvalue()
                client.progress(step="finalize", operation="publish draft", status="DRAFT_READY", completed=10, total=10)
                client.mark_draft_terminal()
                time.sleep(0.22)
                after_terminal = buffer.getvalue()
            lines = self._parse(before_terminal)
            heartbeat_lines = [line for line in lines if line.get("heartbeat") is True]
            self.assertGreaterEqual(len(heartbeat_lines), 1, before_terminal)
            self.assertTrue(all("runId" in line and "elapsedSec" in line for line in heartbeat_lines))
            after_lines = self._parse(after_terminal)
            terminal_lines = [line for line in after_lines if line.get("status") == "DRAFT_READY"]
            self.assertTrue(terminal_lines, after_terminal)
            self.assertEqual(terminal_lines[0].get("pct"), 100)
            self.assertEqual(after_terminal.count("[draft-progress]"),
                             before_terminal.count("[draft-progress]") + 1,
                             "heartbeat must stop after the terminal finalize event")
        finally:
            roadmap.DRAFT_PROGRESS_HEARTBEAT_SECONDS = old_heartbeat
            client.mark_draft_terminal()

    def test_f5_stage_percent_is_capped_and_terminal_only_with_status(self) -> None:
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        client = roadmap.LiveDataClient("https://registry.example/", 15, 8, 0.0)
        try:
            client.set_draft_run("run-f5-pct")
            with redirect_stdout(buffer):
                client.progress(step="scan", completed=10, total=10)
                client.progress(step="scan", completed=1, total=1)
                client.progress(step="finalize", operation="publish draft", completed=10, total=10, status="DRAFT_PARTIAL")
                client.mark_draft_terminal()
            lines = self._parse(buffer.getvalue())
            self.assertIsNone(lines[0].get("status"))
            self.assertEqual(lines[0].get("pct"), 99, "a full measured stage must not claim a terminal 100")
            self.assertEqual(lines[1].get("pct"), 99)
            self.assertEqual(lines[2].get("status"), "DRAFT_PARTIAL")
            self.assertEqual(lines[2].get("pct"), 100, "terminal 100 is allowed only on the finalize/publish event")
        finally:
            client.mark_draft_terminal()
