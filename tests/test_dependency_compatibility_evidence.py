from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baseline_constraint_verifier import BaselineProjectFailure, BaselineVerifyConfig, BaselineVerifyResult
from dependency_compatibility_evidence import (
    CompatibilityEvidence,
    CompatibilityEvidenceAction,
    CompatibilityEvidenceError,
    _materialize_evidence_ref,
    load_compatibility_evidence,
    localize_compatibility_evidence,
)
from source_snapshot import capture_durable_source_snapshot
from substrate_identity import tool_build_id


STRUCTURAL = "config/plugins.ts:1:1 - error TS2307: Cannot find module '@demo/plugin' or its corresponding type declarations. There are types at x, but this result could not be resolved under your current 'moduleResolution' setting. Consider updating to 'node16', 'nodenext', or 'bundler'."


class CompatibilityEvidenceTests(unittest.TestCase):
    def test_loads_v2_sealed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.json"
            path.write_text(json.dumps({
                "schemaVersion": 2,
                "project": "Demo",
                "projectPath": temp,
                "branchRef": "CD-1-cohort-demo",
                "sourceSnapshotLocator": str(Path(temp) / "snapshot"),
                "sourceSnapshotKey": "sealed-key",
                "toolBuildId": tool_build_id(),
                "projectRelative": ".",
                "targetMode": "yellow",
                "commands": ["yarn lint:types"],
                "actions": [
                    {"package": "a", "current": "1.0.0", "target": "2.0.0", "action": "update"},
                ],
            }), encoding="utf-8")
            evidence = load_compatibility_evidence(path)
            self.assertEqual(evidence.project, "Demo")
            self.assertEqual(evidence.actions[0].current, "1.0.0")


    def test_v1_branch_reconstruction_evidence_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "project": "Demo",
                "projectPath": temp,
                "branchRef": "branch",
                "targetMode": "yellow",
                "commands": ["npm test"],
                "actions": [
                    {"package": "a", "current": "1", "target": "2", "action": "update"},
                ],
            }), encoding="utf-8")
            with self.assertRaisesRegex(
                CompatibilityEvidenceError,
                "schemaVersion must be 2",
            ):
                load_compatibility_evidence(path)

    def test_materialization_uses_sealed_snapshot_not_mutated_live_repo(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as state:
            root = Path(temp)
            subprocess.run(["git", "-C", str(root), "init", "-b", "master"], check=True, stdout=subprocess.PIPE)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "evidence@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Evidence"], check=True)
            (root / "package.json").write_text('{"name":"demo","private":true}', encoding="utf-8")
            subject = root / "subject.txt"
            subject.write_text("sealed-post-executor", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, stdout=subprocess.PIPE)

            durable = capture_durable_source_snapshot(
                root,
                Path(state) / "evidence.source-snapshot",
                timeout_seconds=60,
            )
            subject.write_text("mutated-live-checkout", encoding="utf-8")
            evidence = CompatibilityEvidence(
                project="Demo",
                project_path=root,
                branch_ref="master",
                target_mode="yellow",
                commands=("npm test",),
                actions=(CompatibilityEvidenceAction("a", "1", "2"),),
                source_snapshot_locator=durable.container,
                source_snapshot_key=durable.key,
                project_relative=durable.project_relative,
            )
            materialized_root, project = _materialize_evidence_ref(evidence)
            try:
                self.assertEqual(
                    "sealed-post-executor",
                    (project / "subject.txt").read_text(encoding="utf-8"),
                )
            finally:
                import shutil
                shutil.rmtree(materialized_root, ignore_errors=True)

    def test_candidate_vs_control_localizes_target_nogood(self) -> None:
        evidence = CompatibilityEvidence(
            project="Demo",
            project_path=Path("."),
            branch_ref="CD-1-cohort-demo",
            target_mode="yellow",
            commands=("yarn lint:types",),
            actions=(
                CompatibilityEvidenceAction("a", "1.0.0", "2.0.0"),
                CompatibilityEvidenceAction("b", "1.0.0", "2.0.0"),
                CompatibilityEvidenceAction("independent", "1.0.0", "2.0.0"),
            ),
        )
        config = BaselineVerifyConfig(project_checks="adaptive", commands=("yarn lint:types",), parallelism=2, max_delta_checks=12)

        def fake_verify(_project, assignment, *, config, run_project_checks, remove_packages=()):
            del config, run_project_checks, remove_packages
            # Empty assignment means the preserved evidence branch: all targets.
            if not assignment:
                failing = True
            else:
                failing = assignment.get("a") == "2.0.0" and assignment.get("b") == "2.0.0"
            if not failing:
                return BaselineVerifyResult(True, "passed", "green")
            failure = BaselineProjectFailure("yarn lint:types", 2, STRUCTURAL)
            return BaselineVerifyResult(
                False, "project", "project preflight failed: yarn lint:types",
                command="yarn lint:types", output=STRUCTURAL, project_failures=(failure,),
            )

        with patch("dependency_compatibility_evidence._materialize_evidence_ref", return_value=(Path("."), Path("."))), \
             patch("dependency_compatibility_evidence.shutil.rmtree"):
            nogood, signatures = localize_compatibility_evidence(
                evidence, base_config=config, verify=fake_verify, parallelism=2, max_checks=12,
            )
        self.assertEqual(nogood, {"a": "2.0.0", "b": "2.0.0"})
        self.assertEqual(signatures, ("ts-module-resolution:@demo/plugin",))

    def test_preexisting_structural_signature_is_not_learned(self) -> None:
        evidence = CompatibilityEvidence(
            project="Demo", project_path=Path("."), branch_ref="branch", target_mode="yellow",
            commands=("yarn lint:types",),
            actions=(CompatibilityEvidenceAction("a", "1.0.0", "2.0.0"),),
        )
        config = BaselineVerifyConfig(project_checks="adaptive", commands=evidence.commands)

        def always_fails(_project, assignment, *, config, run_project_checks, remove_packages=()):
            del assignment, config, run_project_checks, remove_packages
            failure = BaselineProjectFailure("yarn lint:types", 2, STRUCTURAL)
            return BaselineVerifyResult(False, "project", "red", command="yarn lint:types", output=STRUCTURAL, project_failures=(failure,))

        with patch("dependency_compatibility_evidence._materialize_evidence_ref", return_value=(Path("."), Path("."))), \
             patch("dependency_compatibility_evidence.shutil.rmtree"):
            with self.assertRaisesRegex(RuntimeError, "also present after restoring"):
                localize_compatibility_evidence(evidence, base_config=config, verify=always_fails)

    def test_loads_exact_assignment_from_valid_proof_envelope(self) -> None:
        from proven_dependency_state import build_proven_dependency_envelope

        with tempfile.TemporaryDirectory() as temp:
            envelope = build_proven_dependency_envelope(
                project="Demo",
                mode="yellow",
                proof_schema="baseline-proof-v3",
                source_head="abc123",
                source_snapshot_key="source-key",
                assignment_key="assignment-key",
                resolver_input_key="resolver-key",
                fixed_resolver_inputs_key="e" * 64,
                preparation_proof_key="preparation-key",
                project_proof_key="project-key",
                observed_resolved_hash="b" * 64,
                resolved_state_key="c" * 64,
                resolved_lockfile_path="yarn.lock",
                resolved_lockfile_hash="d" * 64,
                assignment={"a": "2.0.0", "b": "1.0.0"},
                removals=(),
                verification_commands=("yarn lint:types",),
                project_checks="adaptive",
                resolver_proof_status="passed",
                preparation_proof_status="passed",
                project_proof_status="diagnostic-red",
            )
            path = Path(temp) / "evidence.json"
            path.write_text(json.dumps({
                "schemaVersion": 2,
                "project": "Demo",
                "projectPath": temp,
                "branchRef": "CD-1-cohort-demo",
                "sourceSnapshotLocator": str(Path(temp) / "snapshot"),
                "sourceSnapshotKey": "sealed-key",
                "toolBuildId": tool_build_id(),
                "projectRelative": ".",
                "targetMode": "yellow",
                "commands": ["yarn lint:types"],
                "actions": [
                    {"package": "a", "current": "1.0.0", "target": "2.0.0", "action": "update"},
                ],
                "proofEnvelope": envelope,
            }), encoding="utf-8")
            evidence = load_compatibility_evidence(path)
            self.assertEqual(envelope["envelopeKey"], evidence.proof_envelope_key)
            self.assertEqual({"a": "2.0.0", "b": "1.0.0"}, dict(evidence.exact_assignment))



if __name__ == "__main__":
    unittest.main()
