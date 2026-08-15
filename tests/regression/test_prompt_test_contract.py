from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import dependency_live_roadmap_generator as roadmap


class PromptTestContractRegression(unittest.TestCase):
    def test_compact_and_full_prompt_keep_verification_docs_contract(self) -> None:
        row = roadmap.DependencyRow(
            project="Demo", package_dir=".", name="react-router-dom", kind="runtime", requested_spec="^6.0.0",
            current_version="6.0.0", current_source="package-lock.json", latest_version="7.0.0",
            current_vulns="0", min_no_critical="6.0.0", min_no_high="6.0.0", min_no_vuln="6.0.0",
            min_lag_12m="7.0.0", min_lag_9m="7.0.0", min_lag_6m="7.0.0", min_lag_3m="7.0.0",
            group=3, reason="router migration", notes="", subgroup="router", lag_threshold_months=12,
            target_default="7.0.0", target_yellow="7.0.0", target_green="7.0.0",
            target_default_reason="lag", target_yellow_reason="lag", target_green_reason="lag",
        )
        row.release_by_target["7.0.0"] = roadmap.ReleaseIntelligence(
            target="7.0.0",
            status="breaking-confirmed",
            summary="Router API migration is required",
            coverage="6.0.0 -> 7.0.0",
            breaking_changes=["Removed legacy navigation API"],
            migration_notes=["Replace legacy route adapter"],
            deprecations=["Old router facade is deprecated"],
            requirements=["React 18 or newer"],
            sources=[{"url": "https://example.test/router-v7", "kind": "migration-guide"}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "report.html"
            roadmap.write_html(
                {"Demo": [row]}, out,
                history_dir=root / "history",
                project_specs={"Demo": roadmap.ProjectSpec("Demo", root / "demo", source_branch="main")},
                roadmap_json_path=root / "roadmap.json",
                history_snapshots=[],
                knowledge_log_path=root / "dependency-knowledge.json",
                knowledge_entries=[{
                    "id": "router-migration", "recordedAt": "2026-07-14", "packages": ["react-router-dom"],
                    "title": "Router migration proof", "symptom": "legacy API fails", "cause": "major API change",
                    "guidance": "Use the supported adapter", "verification": ["run focused router test"],
                    "confidence": "verified",
                }],
            )
            html = out.read_text(encoding="utf-8")
            required_fragments = [
                "testPolicyForRow",
                "focused-check-required",
                "standard-check-allowed",
                "Verification policy in the manifest",
                "not generated regression tests",
                "Do not create new dependency-regression tests",
                "verificationGap",
                "Verification / gap",
                "docsUpdated",
                "MIGRATION_DOCUMENTATION",
                "CHANGE_RATIONALE_REQUIRED",
                "docs/dependency-update-review-notes.md",
                "docs/dependency-upgrades.md",
                "docs/dependency-update-summary.md",
                "critical bugs or vulnerabilities were actually fixed",
                "benefit for product/business/developer workflow",
                "newly available capabilities",
                "what broke during migration and how it was repaired",
                "why the change was necessary",
                "why it is not unrelated refactoring",
                "NO_REFACTORING",
                "REFACTOR_REQUIRED",
                "broad formatting/autofix-only diffs",
                "--require-final-status",
                "Mandatory protocol",
                "It is mandatory",
                "action=update|remove",
                "MERGE_TARGET_REGRESSION",
                "recommendedActionForRow",
                "sourceCheckoutVerified",
                "sourceCommit",
                "SOURCE_COMMIT_CHANGED_AFTER_ROADMAP",
                "criticalReleaseDossierForRows",
                "Critical release dossier",
                "Критичный BREAKING / migration dossier",
                "Removed legacy navigation API",
                "Replace legacy route adapter",
                "Old router facade is deprecated",
                "React 18 or newer",
                "The dossier is mandatory, not decorative",
                "priorMigrationKnowledge",
                "router-migration",
                "knowledgePolicy",
                "Package migration knowledge",
                "targetArtifactStatus=available",
                "REGISTRY_TARGET_UNAVAILABLE",
                "FOREIGN_REGISTRY_URL",
                "Metadata/maintainers from",
                "do not choose another version, revert the package, or edit the immutable manifest",
                "Storybook/cohort constraints are also immutable",
                "registryArtifactForTarget",
                "compatibilityCohort",
                "scopeHashVersion: 5",
                "scopeExcluded",
                "exclusionReason",
                "action=excluded",
                "detailedCodeComments",
                "codeCommentPolicy",
                "detailed-why-comments",
                "minimal-comments",
                r"Carry \`codeCommentPolicy=",
            ]
            for fragment in required_fragments:
                self.assertIn(fragment, html)
            forbidden_fragments = [
                "testPolicy=required",
                "TEST_CONTRACT_BLOCKED",
                "defaultCollectedRegressionFiles",
                "regressionValidatorPath",
                "validate_dependency_regression.py",
                "failureProbe",
                "testOrigin=existing|generated",
                "tautological assertions",
                "one package per generated test/gate file",
                "changePolicy=minimal-compatibility-only",
                "refactoringPerformed=false",
                "Reusable protocol: ${runbookPath} (optional",
            ]
            for fragment in forbidden_fragments:
                self.assertNotIn(fragment, html)
            import re
            scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
            js = root / "report.js"
            js.write_text("\n".join(scripts), encoding="utf-8")
            result = subprocess.run(["node", "--check", str(js)], text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
