from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from proven_dependency_state import (
    RESOLVER_PROOF_STATUS_NOT_REQUIRED_NO_OP,
    build_proven_dependency_envelope,
    proof_envelope_key,
    validate_proven_dependency_envelope,
    write_proven_dependency_state,
)


class ProvenDependencyStateTests(unittest.TestCase):
    def envelope(self):
        return build_proven_dependency_envelope(
            project="Demo",
            mode="yellow",
            proof_schema="baseline-proof-v3",
            source_head="abc123",
            source_snapshot_key="source-key",
            assignment_key="assignment-key",
            resolver_input_key="resolver-key",
            fixed_resolver_inputs_key="d" * 64,
            preparation_proof_key="preparation-key",
            project_proof_key="project-key",
            observed_resolved_hash="a" * 64,
            resolved_state_key="b" * 64,
            resolved_lockfile_path="yarn.lock",
            resolved_lockfile_hash="c" * 64,
            assignment={"a": "2.0.0", "@types/a": "1.0.0"},
            removals=("@types/a",),
            verification_commands=("yarn lint:types",),
            project_checks="adaptive",
            resolver_proof_status="passed",
            preparation_proof_status="passed",
            project_proof_status="diagnostic-red",
        )

    def test_key_is_deterministic_and_covers_removals(self):
        first = self.envelope()
        second = self.envelope()
        self.assertEqual(first["envelopeKey"], second["envelopeKey"])
        self.assertEqual(first["envelopeKey"], proof_envelope_key(first))

        changed = dict(first)
        changed["removals"] = []
        self.assertNotEqual(first["envelopeKey"], proof_envelope_key(changed))

    def test_tampering_is_detected(self):
        envelope = self.envelope()
        valid, reason = validate_proven_dependency_envelope(envelope)
        self.assertTrue(valid, reason)

        envelope["exactDirectAssignment"]["a"] = "2.1.0"
        valid, reason = validate_proven_dependency_envelope(envelope)
        self.assertFalse(valid)
        self.assertIn("key mismatch", reason)

    def test_resolver_pass_requires_observed_tree_hash(self):
        envelope = self.envelope()
        broken = dict(envelope)
        broken["observedResolvedHash"] = ""
        broken["envelopeKey"] = proof_envelope_key(broken)
        valid, reason = validate_proven_dependency_envelope(broken)
        self.assertFalse(valid)
        self.assertIn("observedResolvedHash", reason)

        failed = dict(envelope)
        failed["resolverProofStatus"] = "failed"
        failed["envelopeKey"] = proof_envelope_key(failed)
        valid, reason = validate_proven_dependency_envelope(failed)
        self.assertFalse(valid)
        self.assertIn("resolver proof status", reason)


    def test_resolver_pass_requires_explicit_resolved_state(self):
        envelope = self.envelope()
        broken = dict(envelope)
        broken["resolvedStateKey"] = ""
        broken["envelopeKey"] = proof_envelope_key(broken)
        valid, reason = validate_proven_dependency_envelope(broken)
        self.assertFalse(valid)
        self.assertIn("resolvedStateKey", reason)

    def test_atomic_state_write_preserves_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "proven-dependency-state.json"
            envelope = self.envelope()
            payload = write_proven_dependency_state(
                path, {"Demo": {"yellow": envelope}}
            )
            self.assertEqual(
                envelope["envelopeKey"],
                payload["projects"]["Demo"]["yellow"]["envelopeKey"],
            )
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                "deploom-proven-dependency-state",
                loaded["type"],
            )

    def test_non_git_noop_source_is_allowed_but_actionable_source_requires_git(self) -> None:
        import dependency_live_roadmap_generator as roadmap

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            head, clean = roadmap._proof_source_head_and_clean(
                root,
                require_git=False,
            )
            self.assertEqual("non-git", head)
            self.assertTrue(clean)

            with self.assertRaisesRegex(
                roadmap.BaselineConstraintVerificationError,
                "actionable dependency proof requires a git HEAD",
            ):
                roadmap._proof_source_head_and_clean(
                    root,
                    require_git=True,
                )


    def test_true_noop_round_trips_through_same_status_model(self):
        envelope = build_proven_dependency_envelope(
            project="Demo",
            mode="default",
            proof_schema="baseline-proof-v5-resolved-state",
            source_head="abc123",
            source_snapshot_key="source-key",
            assignment_key="assignment-key",
            resolver_input_key="resolver-key",
            fixed_resolver_inputs_key="d" * 64,
            preparation_proof_key="preparation-key",
            project_proof_key="project-key",
            observed_resolved_hash="",
            resolved_state_key="",
            resolved_lockfile_path="",
            resolved_lockfile_hash="",
            assignment={},
            removals=(),
            verification_commands=(),
            project_checks="off",
            resolver_proof_status=RESOLVER_PROOF_STATUS_NOT_REQUIRED_NO_OP,
            preparation_proof_status="not-required",
            project_proof_status="not-required",
        )
        valid, reason = validate_proven_dependency_envelope(envelope)
        self.assertTrue(valid, reason)

    def test_builder_rejects_legacy_or_unknown_status_before_artifact_exists(self):
        with self.assertRaisesRegex(ValueError, "resolver proof status invalid"):
            build_proven_dependency_envelope(
                project="Demo", mode="default", proof_schema="p", source_head="h",
                source_snapshot_key="s", assignment_key="a", resolver_input_key="r",
                fixed_resolver_inputs_key="d" * 64, preparation_proof_key="prep", project_proof_key="project",
                observed_resolved_hash="", resolved_state_key="",
                resolved_lockfile_path="", resolved_lockfile_hash="",
                assignment={}, removals=(), verification_commands=(), project_checks="off",
                resolver_proof_status="not-required-no-fixed-inputs",
                preparation_proof_status="not-required", project_proof_status="not-required",
            )


if __name__ == "__main__":
    unittest.main()
