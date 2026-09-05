from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dependency_live_roadmap_generator as gen
from block_psi_anytime import BaselineAnytimeState, ContinuationReason


class BaselineProgressEnvelopeTests(unittest.TestCase):
    def test_production_continuation_call_with_real_anytime_payload(self):
        # Execute the actual orchestration callsite, not a hand-copied wrapper.
        source = Path(gen.__file__).read_text(encoding="utf-8-sig")
        calls = [n for n in ast.walk(ast.parse(source))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "emit" and len(n.args) == 3
                 and isinstance(n.args[2], ast.Constant)
                 and n.args[2].value == "continuation-required"]
        self.assertEqual(1, len(calls))
        state = BaselineAnytimeState()
        state.observe_candidate(duration_seconds=1900, passed=False,
                                predicate="ts-module-resolution:@vitejs/plugin-react")
        decision = state.continuation_payload(project="mini", mode="yellow", iteration=8,
                     reason=ContinuationReason.AUTOMATIC_BUDGET_EXHAUSTED)
        with tempfile.TemporaryDirectory() as tmp, patch.object(gen, "eprint"):
            target = Path(tmp) / "progress.json"
            reporter = gen.BaselineProgressReporter(target)
            expression = ast.Expression(body=calls[0])
            eval(compile(expression, gen.__file__, "eval"), {
                "progress_reporter": reporter, "project": "mini", "mode": "yellow",
                "iteration": 8, "decision_payload": decision,
            })
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual("BASELINE_CONTINUATION_REQUIRED", payload["event"])
            self.assertEqual("HUMAN_DECISION_REQUIRED", payload["terminalStatus"])
            self.assertEqual("continuation-required", payload["phase"])

    def test_reserved_envelope_and_wrapper_values_win_over_nested_payload(self):
        nested = dict(project="other", mode="other", phase="confirmation",
                      schemaVersion=999, type="other", updatedAt="stale",
                      event="nested", iteration=999)
        with tempfile.TemporaryDirectory() as tmp, patch.object(gen, "eprint") as output:
            target = Path(tmp) / "progress.json"
            gen.BaselineProgressReporter(target).emit(
                "mini", "yellow", "continuation-required", details=nested,
                event="BASELINE_CONTINUATION_REQUIRED", iteration=8)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual("mini", payload["project"])
            self.assertEqual("yellow", payload["mode"])
            self.assertEqual("continuation-required", payload["phase"])
            self.assertEqual(1, payload["schemaVersion"])
            self.assertNotEqual("stale", payload["updatedAt"])
            self.assertNotIn("type", payload)
            self.assertEqual(8, payload["iteration"])
            self.assertEqual("BASELINE_CONTINUATION_REQUIRED", payload["event"])
            self.assertEqual("confirmation", payload["localizationPhase"])
            stream = json.loads(output.call_args.args[0].split(" ", 1)[1])
            self.assertEqual(2, stream["schemaVersion"])
            self.assertEqual("deploom-baseline-progress", stream["type"])
        self.assertEqual("other", nested["project"])

    def test_orchestrator_never_expands_arbitrary_payload_into_emit_keywords(self):
        tree = ast.parse(Path(gen.__file__).read_text(encoding="utf-8-sig"))
        unsafe = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute) and n.func.attr == "emit"
                  and any(k.arg is None for k in n.keywords)]
        self.assertEqual([], unsafe)
