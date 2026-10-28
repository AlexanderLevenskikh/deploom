from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import dependency_live_roadmap_generator as core


class RequiredPlannerIntegrationTests(unittest.TestCase):
    """Synthetic full Core solver path; no registry/network/package install."""

    def setUp(self):
        self._env = patch.dict(os.environ, {
            "DEPLOOM_BASELINE_INTENT_JSON": json.dumps({
                "policies": {"sample-router": "required"},
                "targetMode": "pinned",
                "objective": "minimum-change-to-policy",
            }),
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        core._BASELINE_INTENT_CACHE_RAW = "<unset>"
        self.client = core.LiveDataClient(
            "https://registry.example.invalid", timeout=1, batch_size=4, sleep_sec=0
        )
        self._network = patch.object(
            self.client.session, "request",
            side_effect=AssertionError("synthetic fixture must not access network")
        )
        self._network.start()
        self.addCleanup(self._network.stop)

    def test_required_target_passes_real_peer_solver_without_invalid_current_endpoint(self):
        name = "sample-router"
        current, target = "1.0.0", "1.5.0"
        row = core.DependencyRow(
            project="synthetic", package_dir=".", name=name, kind="dev",
            requested_spec="^1.0.0", current_version=current,
            current_source="lockfile", latest_version=target, current_vulns="0",
            min_no_critical=current, min_no_high=current, min_no_vuln=current,
            min_lag_12m=current, min_lag_9m=current, min_lag_6m=current,
            min_lag_3m=current, group=1, reason="synthetic required update",
            notes="", target_default=target, target_yellow=target,
            target_green=target, draft_scan_state="done",
            vuln_evidence_by_version={current: "0", target: "0"},
        )
        versions = {}
        for version in (current, target):
            tarball = f"https://registry.example.invalid/{name}/{version}.tgz"
            versions[version] = {"dist": {"tarball": tarball}}
            self.client.registry_artifact_cache[(name, version)] = {
                "status": "available", "tarballUrl": tarball
            }
        self.client.npm_cache[name] = {"versions": versions}
        rows = {"synthetic": [row]}
        core._apply_baseline_intent_scope(rows)
        core.capture_desired_targets(rows)
        core.enrich_registry_target_evidence(rows, self.client)
        core.resolve_peer_compatibility(
            rows, self.client, modes=("yellow",),
            apply_results=True, partial_on_incomplete=True,
        )
        self.assertEqual(
            target, row.target_yellow,
            "valid required target must survive the real solver and transition grouping"
        )
        self.assertFalse(
            getattr(row, "peer_compat_unresolved", False),
            "required update must not be reported as an unresolved peer transition"
        )


if __name__ == "__main__":
    unittest.main()
