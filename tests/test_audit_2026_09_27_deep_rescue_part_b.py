"""Part B of the libjs deep rescue (2026-09-27): the Draft prompt becomes a
standalone useful migration instruction.

Acceptance tested against the REAL `build_draft_prompt` output (RU and EN), not
by grepping the module source:
  B1 - execution context (paths, source identity, manager/runtime, sanitized
       registry), exact policy limits (0 preserved), commands + composite test
       rule, accumulative algorithm, NOT_VERIFIED/PLANNING_ONLY discipline,
       user-excluded vs search-deferred separation.
  B2 - migration-progress.json + migration-journal.md emitted at publish time
       with the initial NOT_VERIFIED measurement point; machine/man-readable.
  B3 - the ready PowerShell manual_dependency_audit.py command with real
       substituted values, credentials stripped, engine/accuracy rationale.
  B4 - detailed-why-comments policy and the two developer documents with the
       exact run-scoped paths.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import dependency_live_roadmap_generator as roadmap

REGISTRY = "https://user:secret@nexus.local/repository/npm"


def _ctx(**overrides) -> dict:
    ctx = {
        "projectPath": r"C:\Users\kovalev\Desktop\projects\libjs",
        "projectName": "libjs",
        "toolRoot": r"C:\tools\deploom",
        "registry": REGISTRY,
        "source": {"branch": "master", "commit": "d4b8b6d85ad8"},
        "packageManager": "yarn 1.22.22",
        "runtime": "node 20",
        "commands": {
            "yarn flow:check": {"available": True},
            "yarn lint": {"available": True},
            "yarn test:unit": {"available": None},
        },
        "scripts": {"test": "yarn test:unit && yarn test:build", "test:unit": "jest"},
        "inputHashes": {"source": "abc123", "lock": "def456"},
        "policy": {
            "targetLevel": "yellow", "lagPolicyMonths": 12, "minLagOkPct": 80,
            "maxKnownCritical": 0, "maxKnownHigh": 1, "maxKnownModerate": 5, "maxKnownLow": 10,
        },
    }
    ctx.update(overrides)
    return ctx


def _snapshot(**overrides) -> dict:
    snap = {
        "targetLevel": "yellow", "lagPolicyMonths": 12, "minLagOkPct": 80,
        "maxKnownCritical": 0, "maxKnownHigh": 1, "maxKnownModerate": 5, "maxKnownLow": 10,
    }
    snap.update(overrides)
    return snap


def _plan() -> dict:
    return {
        "projects": ["libjs"],
        "proposals": [{
            "project": "libjs",
            "health": {
                "lag_ok_12m": 72, "total": 110, "lag_unknown": 5,
                "critical": 1, "high": 4, "moderate": 9, "low": 20, "unknown": 3,
            },
            "rows": [],
        }],
        "unknowns": [],
        "conflicts": [],
        "scan": {},
    }


def _prompt(language: str, ctx: dict | None, snapshot: dict | None, plan: dict | None = None) -> str:
    return roadmap.build_draft_prompt(
        "run-b-1", "ws-b", "pj-b", "yellow", "pol-hash-b",
        plan if plan is not None else _plan(),
        {},
        language=language,
        snapshot=snapshot,
        execution_context=ctx,
        artifacts_dir=r"C:\ws\.dependency-roadmap\artifacts\runs\run-b-1\draft",
    )


class PartBPromptRu(unittest.TestCase):
    def test_real_prompt_ru_contains_part_b_contracts(self):
        md = _prompt("ru", _ctx(), _snapshot())
        # B1 execution context
        self.assertIn("## Контекст выполнения", md)
        self.assertIn(r"`C:\Users\kovalev\Desktop\projects\libjs`", md)
        self.assertIn("`C:\\tools\\deploom`", md)
        self.assertIn("commit `d4b8b6d85ad8`", md)
        self.assertIn("source=abc123", md)
        self.assertIn("`yarn 1.22.22`", md)
        # B3 audit command with real substituted values and NO credentials
        self.assertIn("manual_dependency_audit.py", md)
        self.assertIn("--project-dir 'C:\\Users\\kovalev\\Desktop\\projects\\libjs'", md)
        self.assertIn("--registry 'https://nexus.local/repository/npm'", md)
        self.assertNotIn("user:secret@", md)
        self.assertIn("--max-known-high 1", md)
        self.assertIn("--yarn-audit-engine auto", md)
        self.assertIn("npm-lock-bridge", md)
        self.assertIn("yarn-inventory", md)
        # B2 progress files
        self.assertIn("migration-progress.json", md)
        self.assertIn("migration-journal.md", md)
        # B4 deliverables
        self.assertIn("DEVELOPER_UPGRADE_GUIDE.md", md)
        self.assertIn("MIGRATION_REPORT.md", md)
        self.assertIn("detailed-why-comments", md)
        # composite test rule for libjs
        self.assertIn("test:build", md)
        self.assertIn("test:unit", md)
        # scope separation user-excluded vs search-deferred
        self.assertIn("user-excluded", md)
        self.assertIn("search-deferred", md)
        # algorithm + NOT_VERIFIED discipline
        self.assertIn("## Алгоритм миграции", md)
        self.assertIn("NOT_VERIFIED", md)
        self.assertIn("## Промежуточные отчёты", md)
        self.assertIn("actual Lag OK", md)

    def test_policy_zero_is_preserved(self):
        ctx = _ctx(policy={
            "targetLevel": "yellow", "lagPolicyMonths": 12, "minLagOkPct": 80,
            "maxKnownCritical": 0, "maxKnownHigh": 0, "maxKnownModerate": 5, "maxKnownLow": 10,
        })
        snap = _snapshot(maxKnownHigh=0)
        md = _prompt("ru", ctx, snap)
        self.assertIn("Critical <= 0", md)
        self.assertIn("High <= 0", md)
        self.assertNotIn("Critical <= не задан", md)

    def test_policy_explicit_limits_match_user_not_constants(self):
        policy = {
            "targetLevel": "green", "lagPolicyMonths": 6, "minLagOkPct": 100,
            "maxKnownCritical": 0, "maxKnownHigh": 0, "maxKnownModerate": 0, "maxKnownLow": 0,
        }
        md = _prompt("ru", _ctx(policy=policy), _snapshot(**policy))
        self.assertIn("Уровень: `green`", md)
        self.assertIn("lag-policy месяцев: `6`", md)
        self.assertIn("`100%`", md)
        self.assertIn("--target-level 'green'", md)
        self.assertIn("--lag-months 6", md)
        self.assertIn("--min-lag-ok-pct 100", md)
        self.assertIn("--max-known-high 0", md)

    def test_missing_context_degrades_honestly(self):
        md = _prompt("ru", None, None)
        self.assertIn("Путь проекта: `—`", md)
        self.assertIn("Хеши входа: не переданы", md)
        self.assertIn("registry: —", md)
        self.assertIn("<candidate-project>", md)


class PartBPromptEn(unittest.TestCase):
    def test_real_prompt_en_contains_part_b_contracts(self):
        md = _prompt("en", _ctx(), _snapshot())
        self.assertIn("## Execution context", md)
        self.assertIn(r"`C:\Users\kovalev\Desktop\projects\libjs`", md)
        self.assertIn("--project-dir 'C:\\Users\\kovalev\\Desktop\\projects\\libjs'", md)
        self.assertIn("--registry 'https://nexus.local/repository/npm'", md)
        self.assertNotIn("user:secret@", md)
        self.assertIn("## Run policy", md)
        self.assertIn("Critical <= 0", md)
        self.assertIn("## Migration algorithm", md)
        self.assertIn("## Intermediate reports", md)
        self.assertIn("migration-progress.json", md)
        self.assertIn("DEVELOPER_UPGRADE_GUIDE.md", md)
        self.assertIn("MIGRATION_REPORT.md", md)
        self.assertIn("detailed-why-comments", md)
        self.assertIn("test:build", md)
        self.assertIn("user-excluded", md)
        self.assertIn("search-deferred", md)


class PartBProgressStore(unittest.TestCase):
    def test_append_migration_progress_roundtrip_and_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            draft_dir = Path(tmp)
            record = roadmap.initial_migration_progress_record(
                run_id="run-b-1", workspace_id="ws", project_id="pj", mode="yellow",
                policy_hash="pol-hash-b", ctx=_ctx(),
                plan_counts={"total": 110, "lag_ok_12m": 72, "critical": 1, "high": 4,
                             "moderate": 9, "low": 20, "unknown": 3},
                settings_snapshot=_snapshot(),
            )
            roadmap.append_migration_progress(draft_dir, record, journal_language="ru")
            payload_path = draft_dir / "migration-progress.json"
            journal_path = draft_dir / "migration-journal.md"
            self.assertTrue(payload_path.is_file())
            self.assertTrue(journal_path.is_file())
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            self.assertEqual([record["timestamp"]], [p["timestamp"] for p in payload["points"]])
            point = payload["points"][0]
            self.assertEqual("run-b-1", point["runId"])
            self.assertEqual("pol-hash-b", point["policyHash"])
            self.assertEqual({"source": "abc123", "lock": "def456"}, point["inputHashes"])
            self.assertEqual([], point["acceptedCohortIds"])
            self.assertEqual(72, point["actualHealth"]["lagOk"])
            self.assertEqual(110, point["actualHealth"]["lagTotal"])
            self.assertEqual("not-started", point["audit"]["engine"])
            unit_packages = point["securityCounts"]["vulnerablePackages"]
            self.assertEqual(1 + 4 + 9 + 20, unit_packages)
            journal = journal_path.read_text(encoding="utf-8")
            self.assertIn("NOT_VERIFIED", journal)
            self.assertIn("run-b-1", journal)
            # second point appends, first point is never lost
            record2 = dict(record, timestamp="later")
            roadmap.append_migration_progress(draft_dir, record2, journal_language="en")
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            self.assertEqual(2, len(payload["points"]))


class PartBEndToEndPublish(unittest.TestCase):
    def test_publish_draft_result_emits_migration_progress_and_prompt_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roadmap.set_draft_artifacts_base(base)
            try:
                row = roadmap.DependencyRow(
                    project="tiny-basic", package_dir="C:/projects/tiny-basic", name="uuid",
                    kind="runtime", requested_spec="10.0.0", current_version="10.0.0",
                    current_source="package-lock.json", latest_version="registry unavailable",
                    current_vulns="unknown", min_no_critical="unknown", min_no_high="unknown",
                    min_no_vuln="unknown", min_lag_12m="-", min_lag_9m="-",
                    min_lag_6m="-", min_lag_3m="-", group=2, reason="runtime/API hygiene",
                    notes="",
                )
                health = roadmap.ProjectHealth(
                    project="tiny-basic", status="yellow", status_rank=1,
                    total=1, lag_ok_12m=0, lag_bad_12m=0, lag_ok_pct=100.0,
                    critical=0, high=0, moderate=0, low=0, unknown=1, reason="reason",
                    lag_unknown=1, lag_needed_for_yellow=0,
                    yellow_plan_required=0, yellow_projected_lag_ok=0,
                    yellow_projected_lag_pct=100.0, yellow_plan_shortfall=0,
                )
                exec_ctx = _ctx(artifactsDir=str(base / "runs" / "run-b-2" / "draft"))
                manifest = roadmap.publish_draft_result(
                    run_id="run-b-2", workspace_id="ws", project_id="tiny-basic", mode="yellow",
                    rows_by_project={"tiny-basic": [row]},
                    projects_by_name={},
                    health_by_project={"tiny-basic": health},
                    status="DRAFT_READY", partial_reason=None,
                    deadline=roadmap.DeadlineClock(None),
                    settings_snapshot=_snapshot(),
                    execution_context=exec_ctx,
                )
                draft_dir = base / "runs" / "run-b-2" / "draft"
                self.assertTrue((draft_dir / "migration-progress.json").is_file())
                self.assertTrue((draft_dir / "migration-journal.md").is_file())
                self.assertEqual(
                    str(draft_dir / "migration-progress.json"),
                    manifest["artifacts"]["migrationProgress"],
                )
                self.assertEqual(
                    str(draft_dir / "migration-journal.md"),
                    manifest["artifacts"]["migrationJournal"],
                )
                prompt = (draft_dir / "prompt.md").read_text(encoding="utf-8")
                self.assertIn("migration-progress.json", prompt)
                self.assertIn("DEVELOPER_UPGRADE_GUIDE.md", prompt)
                self.assertIn("MIGRATION_REPORT.md", prompt)
                self.assertIn("detailed-why-comments", prompt)
                progress = json.loads((draft_dir / "migration-progress.json").read_text(encoding="utf-8"))
                self.assertEqual(1, len(progress["points"]))
                self.assertEqual("NOT_VERIFIED", manifest["verificationStatus"])
            finally:
                roadmap.set_draft_artifacts_base(None)


if __name__ == "__main__":
    unittest.main()
