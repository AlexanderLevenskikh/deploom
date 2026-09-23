# F1 cross-language chain: the ordinary audit producer
# (manual_dependency_audit.build_report) writes the report the desktop reader
# (acceptance-policy.acceptanceVerdictFromManualAudit) enforces, and the two
# implementations must agree on the dependency-input identity that binds the
# evidence together. This file is the end-to-end proof of that chain: real
# producer -> real JSON file -> real Node verdict.
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import manual_dependency_audit as audit

NODE_VERDICT_SCRIPT = """\
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
const [distPath, reportPath, projectPath, policyArg] = process.argv.slice(2)
const { acceptanceVerdictFromManualAudit, dependencyInputIdentity } = await import(pathToFileURL(distPath).href)
const report = JSON.parse(readFileSync(reportPath, 'utf8'))
const policy = policyArg ? JSON.parse(policyArg) : undefined
const identity = dependencyInputIdentity(projectPath)
const verdict = acceptanceVerdictFromManualAudit(report, identity.hash, policy)
console.log(JSON.stringify({ status: verdict.status, accepted: verdict.accepted, reasons: verdict.reasons, lagOkPct: verdict.lagOkPct, identityHash: identity.hash }))
"""


def fake_audit(complete: bool = True) -> dict:
    return {
        "engine": "npm-native",
        "command": ["npm", "audit", "--json"],
        "exitCode": 0,
        "complete": complete,
        "packages": {},
        "totals": {"critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0},
        "packageTotals": {"critical": 0, "high": 0, "moderate": 0, "low": 0, "unknown": 0},
        "notes": [],
    }


def fake_lag(*counts: int) -> list:
    """Build a lag list with `counts` = (ok, lagging, unknown) items."""
    items = []
    for kind, count in zip(("ok", "lagging", "unknown"), counts):
        for index in range(count):
            items.append({
                "section": "dependencies",
                "name": f"{kind}-{index}",
                "spec": "^1.0.0",
                "current": "1.0.0",
                "source": "package.json",
                "policyMonths": 12,
                "status": kind,
                "latest": "2.0.0",
                "currentPublishedAt": "2023-01-01T00:00:00+00:00",
                "latestPublishedAt": "2025-01-01T00:00:00+00:00",
                "error": "",
            })
    return items


class F1AcceptanceEvidenceChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="f1-chain-"))
        self.project = self.tmp / "project"
        self.project.mkdir()
        self._write_project(["left-pad@1.0.0"])
        self.verdict_script = self.tmp / "verdict.mjs"
        self.verdict_script.write_text(NODE_VERDICT_SCRIPT, encoding="utf-8")
        self.dist = str(ROOT / "desktop" / "dist-electron" / "acceptance-policy.js").replace("\\", "/")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_project(self, dependencies) -> None:
        (self.project / "package.json").write_text(
            json.dumps({"name": "demo", "dependencies": {name.split("@")[0]: f"^{name.split('@')[1]}" for name in dependencies}}),
            encoding="utf-8",
        )

    def _produce_report(self, target_level="yellow", min_lag_ok_pct=80, max_known_high=1, lag_count=(10, 0, 0)) -> dict:
        with (
            mock.patch.object(audit, "run_audit", return_value=fake_audit()),
            mock.patch.object(audit, "check_lag", return_value=fake_lag(*lag_count)),
        ):
            return audit.build_report(
                self.project,
                "demo",
                registry="https://registry.npmjs.org/",
                lag_months=12,
                target_level=target_level,
                min_lag_ok_pct=min_lag_ok_pct,
                max_known_high=max_known_high,
            )

    def _run_verdict(self, report_path: Path, policy: dict | None = None) -> dict:
        command = ["node", str(self.verdict_script), self.dist, str(report_path), str(self.project)]
        if policy is not None:
            command.append(json.dumps(policy))
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
        if completed.returncode != 0:
            raise AssertionError(f"node verdict failed: {completed.stdout}\n{completed.stderr}")
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_producer_report_carries_goal_policy_lag_pct_and_identity(self) -> None:
        report = self._produce_report()
        self.assertEqual(10, report["audit"]["lagOk"])
        self.assertEqual(10, report["audit"]["lagTotal"])
        self.assertEqual(0, report["audit"]["lagUnknown"])
        self.assertAlmostEqual(100.0, report["audit"]["lagOkPct"], places=1)
        self.assertEqual(
            {"targetLevel": "yellow", "minLagOkPct": 80, "maxKnownCritical": 0, "maxKnownHigh": 1, "lagPolicyMonths": 12},
            report["policy"],
        )
        self.assertTrue(report["complete"])
        # The report is bound to the dependency inputs it actually inspected.
        self.assertEqual(audit.dependency_input_identity(self.project)[0], report["dependencyInputHash"])

    def test_identity_hash_parity_between_python_producer_and_node_reader(self) -> None:
        # Both implementations must derive the same digest from the same inputs,
        # otherwise a report the producer signed cannot be matched to the inputs
        # the desktop reader recomputes at release time.
        python_hash = audit.dependency_input_identity(self.project)[0]
        report = self._produce_report()
        report_path = self.tmp / "manual-audit-demo.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        verdict = self._run_verdict(report_path)
        self.assertEqual(python_hash, verdict["identityHash"])
        self.assertEqual(python_hash, report["dependencyInputHash"])

    def test_complete_fresh_yellow80_evidence_reaches_accepted_through_real_producer(self) -> None:
        report = self._produce_report(lag_count=(10, 0, 0))
        report_path = self.tmp / "manual-audit-demo.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        verdict = self._run_verdict(report_path)
        self.assertEqual("ACCEPTED", verdict["status"], verdict["reasons"])

    def test_known_lag_gap_below_goal_is_remediation_not_unknown_or_accepted(self) -> None:
        # 3 of 10 with lag evidence; 7 unknown. The producer reports 30%
        # (unknown rows ARE in the denominator), and the reader must classify
        # the known shortfall as REMEDIATION_REQUIRED, never as "can't tell".
        report = self._produce_report(lag_count=(3, 0, 7))
        report_path = self.tmp / "manual-audit-demo.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        verdict = self._run_verdict(report_path)
        self.assertAlmostEqual(30.0, verdict["lagOkPct"], places=1)
        self.assertEqual("REMEDIATION_REQUIRED", verdict["status"])
        self.assertTrue(any("below the target gate" in reason for reason in verdict["reasons"]))

    def test_input_change_invalidates_previous_evidence_as_unknown(self) -> None:
        report = self._produce_report(lag_count=(10, 0, 0))
        report_path = self.tmp / "manual-audit-demo.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        # The inputs change after the audit was produced: the reader must
        # refuse the stale evidence instead of passing on old numbers.
        self._write_project(["left-pad@1.0.0", "right-pad@2.0.0"])
        verdict = self._run_verdict(report_path)
        self.assertEqual("UNKNOWN", verdict["status"])
        self.assertTrue(any("changed after the audit" in reason for reason in verdict["reasons"]))

    def test_goal_policy_mismatch_fails_closed_as_unknown(self) -> None:
        report = self._produce_report(target_level="green", min_lag_ok_pct=100, max_known_high=0, lag_count=(10, 0, 0))
        report_path = self.tmp / "manual-audit-demo.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        green_intent = {"maxKnownCritical": 0, "maxKnownHigh": 0, "targetLevel": "green", "minLagOkPct": 100}
        verdict = self._run_verdict(report_path, green_intent)
        self.assertEqual("ACCEPTED", verdict["status"], verdict["reasons"])
        # The user now pursues yellow90: evidence produced under green100 must
        # not vouch for a different goal.
        intent_policy = {"maxKnownCritical": 0, "maxKnownHigh": 1, "targetLevel": "yellow", "minLagOkPct": 90}
        verdict = self._run_verdict(report_path, intent_policy)
        self.assertEqual("UNKNOWN", verdict["status"])
        self.assertTrue(any("different goal policy" in reason for reason in verdict["reasons"]))

    def test_green_goal_aligns_with_green_policy_and_rejects_low_lag_as_remediation(self) -> None:
        report = self._produce_report(target_level="green", min_lag_ok_pct=100, max_known_high=0, lag_count=(3, 0, 7))
        report_path = self.tmp / "manual-audit-demo.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        intent_policy = {"maxKnownCritical": 0, "maxKnownHigh": 0, "targetLevel": "green", "minLagOkPct": 100}
        verdict = self._run_verdict(report_path, intent_policy)
        self.assertEqual("REMEDIATION_REQUIRED", verdict["status"])
        self.assertTrue(any("below the target gate 100%" in reason for reason in verdict["reasons"]))


if __name__ == "__main__":
    unittest.main()
