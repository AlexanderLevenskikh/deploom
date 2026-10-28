from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from draft_input_identity import aggregate_semantic_inputs, capture_draft_semantic_inputs


class DraftInputIdentityTests(unittest.TestCase):
    def fixture(self, path: Path):
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
        self.write_json(path / "package.json", {"name": "synthetic-root", "private": True, "packageManager": "npm@10.0.0", "workspaces": ["packages/*"]})
        self.write_json(path / "package-lock.json", {"lockfileVersion": 3, "packages": {"": {"name": "synthetic-root"}, "packages/app": {"name": "synthetic-app"}}})
        self.write_json(path / "packages/app/package.json", {"name": "synthetic-app", "dependencies": {"example-library": "1.0.0"}})
        self.write_json(path / "packages/peer/package.json", {"name": "synthetic-peer", "version": "1.0.0"})
        return path / "packages/app"

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_child_identity_binds_parent_lock_root_and_sibling_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            child = self.fixture(root)
            captured = capture_draft_semantic_inputs(child)
            self.assertEqual("package-lock.json", captured["topology"]["canonicalLockfile"])
            coverage = captured["inputCoverage"]
            self.assertEqual("PARTIAL", coverage["status"])
            self.assertEqual(["packages/app/package.json"], coverage["evaluatedManifests"])
            self.assertEqual(["package.json", "packages/peer/package.json"], coverage["unevaluatedManifests"])
            self.assertFalse(coverage["jointSolver"])
            before = captured["semanticInputHash"]
            for name in ("package-lock.json", "package.json", "packages/peer/package.json"):
                path = root / name
                original = path.read_bytes()
                value = json.loads(original)
                value["syntheticChange"] = True
                self.write_json(path, value)
                with self.subTest(input=name):
                    self.assertNotEqual(before, capture_draft_semantic_inputs(child)["semanticInputHash"])
                path.write_bytes(original)
            (root / ".npmrc").write_text("strict-peer-deps=true\n", encoding="utf-8")
            self.assertNotEqual(before, capture_draft_semantic_inputs(child)["semanticInputHash"])

    def test_relocation_run_output_and_state_path_do_not_change_semantic_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            captures = []
            for index in (1, 2):
                root = base / f"checkout-{index}"
                child = self.fixture(root)
                settings = base / f"run-{index}" / "settings.json"
                state = base / f"run-{index}" / "state" / "dashboard-state.json"
                self.write_json(settings, {"root": str(root), "projects": [{"name": "synthetic-app", "path": str(child)}], "artifactsDir": str(base / f"run-{index}" / "artifacts"), "dashboardState": str(state), "maxCandidates": 3})
                self.write_json(state, {"packageOverrides": {}, "updatedAt": f"invocation-{index}"})
                captures.append(capture_draft_semantic_inputs(child, settings_sources=[settings], dashboard_state_path=state))
            self.assertEqual(captures[0]["semanticInputHash"], captures[1]["semanticInputHash"])
            # Renaming a project display label also does not change aggregation.
            self.assertEqual(aggregate_semantic_inputs({"first-label": captures[0]})["semanticInputHash"], aggregate_semantic_inputs({"second-label": captures[1]})["semanticInputHash"])
            state = base / "run-2/state/dashboard-state.json"
            self.write_json(state, {"packageOverrides": {"synthetic-app": {"example-library": {"scopeExcluded": True}}}})
            changed = capture_draft_semantic_inputs(base / "checkout-2/packages/app", settings_sources=[base / "run-2/settings.json"], dashboard_state_path=state)
            self.assertNotEqual(captures[1]["semanticInputHash"], changed["semanticInputHash"])

    def test_effective_overrides_change_identity_without_hashing_ui_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            child = self.fixture(root)
            first = capture_draft_semantic_inputs(child, effective_overrides={"example-library": {"scopeExcluded": False}})
            second = capture_draft_semantic_inputs(child, effective_overrides={"example-library": {"scopeExcluded": True}})
            self.assertNotEqual(first["semanticInputHash"], second["semanticInputHash"])

    def test_root_planning_does_not_claim_workspace_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            self.fixture(root)
            result = capture_draft_semantic_inputs(root)
            self.assertEqual(["package.json"], result["inputCoverage"]["evaluatedManifests"])
            self.assertEqual("PARTIAL", result["inputCoverage"]["status"])
            self.assertFalse(result["inputCoverage"]["jointSolver"])
            self.assertEqual("PARTIAL", aggregate_semantic_inputs({"root": result})["inputCoverage"]["status"])

    def test_conflicting_package_manager_profile_publishes_unknown_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_json(root / "package.json", {"name": "synthetic-conflict", "packageManager": "yarn@1.22.22"})
            self.write_json(root / "package-lock.json", {"lockfileVersion": 3, "packages": {}})
            result = capture_draft_semantic_inputs(root)
            self.assertEqual("UNKNOWN", result["inputCoverage"]["status"])
            self.assertEqual("PACKAGE_MANAGER_DECLARATION_LOCK_CONFLICT", result["inputCoverage"]["topologyError"])

    def test_external_manifest_is_read_at_actual_path_and_does_not_claim_bound_topology(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            child = self.fixture(root)
            external = Path(tmp) / "external-member" / "package.json"
            self.write_json(external, {"name": "synthetic-external", "version": "1.0.0"})
            topology = {
                "packageManagerRoot": str(root), "packageRoot": str(child),
                "sourceRoot": str(root), "canonicalLockfile": str(root / "package-lock.json"),
                "workspaceMemberManifests": [str(external)],
            }
            first = capture_draft_semantic_inputs(child, topology=topology)
            self.assertEqual("UNKNOWN", first["inputCoverage"]["status"])
            record = next(r for r in first["files"] if Path(r.get("path", "")).resolve() == external.resolve())
            self.assertEqual("PRESENT", record["status"])
            self.write_json(external, {"name": "synthetic-external", "version": "2.0.0"})
            second = capture_draft_semantic_inputs(child, topology=topology)
            self.assertNotEqual(first["semanticInputHash"], second["semanticInputHash"])

    def test_missing_or_unreadable_required_input_stays_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            child = self.fixture(root)
            captured = capture_draft_semantic_inputs(child)
            topology = {
                "packageManagerRoot": str(root), "packageRoot": str(child),
                "sourceRoot": str(root), "canonicalLockfile": str(root / "package-lock.json"),
                "workspaceMemberManifests": captured["inputCoverage"]["discoveredManifests"],
                "workspacePatterns": ["packages/*"],
            }
            original = Path.read_bytes
            lock = root / "package-lock.json"
            def deny(path):
                if path.resolve() == lock.resolve():
                    raise PermissionError("synthetic unreadable lock")
                return original(path)
            with patch.object(Path, "read_bytes", deny):
                unavailable = capture_draft_semantic_inputs(child, topology=topology)
            self.assertEqual("UNKNOWN", unavailable["inputCoverage"]["status"])
            self.assertIn("package-lock.json", unavailable["inputCoverage"]["missingOrUnreadableInputs"])
            self.assertNotEqual(captured["semanticInputHash"], unavailable["semanticInputHash"])
            lock.unlink()
            missing = capture_draft_semantic_inputs(child)
            self.assertEqual("UNKNOWN", missing["inputCoverage"]["status"])
            self.assertFalse(missing["topology"]["topologyBound"])


if __name__ == "__main__":
    unittest.main()
