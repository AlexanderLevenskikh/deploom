"""WS6: expected domain failures must not surface as raw Python tracebacks.

The reported incident ended with a bare traceback plus
`BASELINE_VERIFY_UNKNOWN_ERROR: ... OBSERVED_RESOLVED_ASSIGNMENT_ESCAPE ...`
as the primary user-facing result. That is a runtime UX defect: the failure was
an expected domain outcome, and it was also misclassified as UNKNOWN when the
message plainly carried a precise cause.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploom_failure import (
    FAILURE_SCHEMA,
    build_failure,
    classify_failure,
    write_diagnostic_artifact,
)

INCIDENT = (
    "BASELINE_VERIFY_UNKNOWN_ERROR: demo/yellow: adaptive screen yarn lint:types: "
    "OBSERVED_RESOLVED_ASSIGNMENT_ESCAPE: @babel/cli: "
    r"...\project-check-001\node_modules\@babel\cli\package.json"
)


class RuntimeFailureEnvelope(unittest.TestCase):
    def test_generic_wrapper_does_not_mask_the_precise_cause(self) -> None:
        """The incident message must classify by its specific code, not UNKNOWN."""
        category, retryability, _recovery = classify_failure(RuntimeError(INCIDENT))
        self.assertEqual(category, "ASSIGNMENT_DRIFT")
        self.assertEqual(retryability, "retry-after-rebuild")

    def test_envelope_has_every_required_field(self) -> None:
        failure = build_failure(
            RuntimeError(INCIDENT),
            context={
                "project": "demo",
                "mode": "yellow",
                "phase": "guarded-lower precheck",
                "command": "yarn lint:types",
                "assignment": "2881d1cca9acfbf4",
            },
        )
        envelope = failure.to_envelope()
        self.assertEqual(envelope["schemaVersion"], FAILURE_SCHEMA)
        for field in (
            "code",
            "category",
            "severity",
            "retryability",
            "project",
            "mode",
            "assignment",
            "phase",
            "command",
            "rootCause",
            "proofImpact",
            "recoveryAction",
        ):
            self.assertIn(field, envelope)
        self.assertEqual(envelope["category"], "ASSIGNMENT_DRIFT")
        self.assertEqual(
            envelope["code"],
            "OBSERVED_RESOLVED_ASSIGNMENT_ESCAPE",
            "the generic wrapper masked the precise code",
        )
        self.assertEqual(envelope["command"], "yarn lint:types")
        self.assertTrue(str(envelope["recoveryAction"]).strip())

    def test_human_summary_is_readable_and_names_the_stage(self) -> None:
        failure = build_failure(
            RuntimeError(INCIDENT),
            context={"project": "demo", "mode": "yellow", "phase": "guarded-lower precheck"},
        )
        text = failure.human_summary()
        self.assertIn("Baseline stopped safely: ASSIGNMENT_DRIFT", text)
        self.assertIn("guarded-lower precheck", text)
        self.assertIn("Recovery:", text)
        self.assertNotIn("Traceback (most recent call last)", text)

    def test_only_unexpected_errors_are_tool_internal(self) -> None:
        expected, _r, _a = classify_failure(RuntimeError(INCIDENT), expected=True)
        defect, _r2, _a2 = classify_failure(RuntimeError("boom"), expected=False)
        self.assertNotEqual(expected, "TOOL_INTERNAL_ERROR")
        self.assertEqual(defect, "TOOL_INTERNAL_ERROR")

    def test_traceback_is_preserved_in_the_diagnostic_artifact(self) -> None:
        """Not hiding errors: the traceback still exists, just not as the answer."""
        try:
            raise RuntimeError(INCIDENT)
        except RuntimeError as exc:
            failure = build_failure(exc)
            with tempfile.TemporaryDirectory() as raw:
                path = write_diagnostic_artifact(exc, failure, directory=Path(raw))
                self.assertTrue(path, "no diagnostic artifact written")
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertIn("Traceback (most recent call last)", payload["traceback"])
        self.assertEqual(payload["category"], "ASSIGNMENT_DRIFT")

    def test_source_and_substrate_failures_get_distinct_categories(self) -> None:
        cases = {
            "SOURCE_SNAPSHOT_CONTENT_MISMATCH: expected=a observed=b": "SOURCE_INTEGRITY",
            "PREPARED_ARTIFACT_CONTENT_UNSTABLE: x": "ARTIFACT_INTEGRITY",
            "SUBSTRATE_ASSIGNMENT_DRIFT: cmd: mismatch": "ASSIGNMENT_DRIFT",
            "SOURCE_REMOTE_NOT_FOUND: demo: remote 'origin' is not configured": "ENVIRONMENT",
        }
        for message, expected_category in cases.items():
            with self.subTest(message=message):
                category, _r, _a = classify_failure(RuntimeError(message))
                self.assertEqual(category, expected_category)

    def test_process_exits_without_a_traceback_on_a_domain_failure(self) -> None:
        """End-to-end: the boundary must convert the exception into a result."""
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from deploom_failure import build_failure, write_diagnostic_artifact\n"
            "import dataclasses, json\n"
            "exc = RuntimeError(%r)\n"
            "f = build_failure(exc, expected=True)\n"
            "print(json.dumps(f.to_envelope()))\n"
            "print(f.human_summary(), file=sys.stderr)\n"
            "sys.exit(3)\n" % (str(ROOT), INCIDENT)
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback (most recent call last)", result.stderr)
        self.assertIn("Baseline stopped safely", result.stderr)


if __name__ == "__main__":
    unittest.main()
