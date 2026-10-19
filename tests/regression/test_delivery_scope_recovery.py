"""Real filesystem/Git boundaries for recoverable delivery and monorepo repair."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import iterative_delivery as delivery
from delivery_audit_recovery import restore_audit_copies
from security_workspace_scope import blocked_mutations


@unittest.skipUnless(shutil.which("git"), "Git required")
class WorkspaceScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deploom-scope-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.reference = self.base / "reference"
        self.reference.mkdir()
        self.write(self.reference, "package.json", {"name": "root", "private": True, "workspaces": ["packages/*"], "scripts": {"test": "check"}})
        self.write(self.reference, "package-lock.json", {"lockfileVersion": 3, "packages": {}})
        for name, dependencies in [("app", {"local-lib": "1.0.0"}), ("local-lib", {"external": "1.0.0"}), ("unrelated", {"other": "1.0.0"})]:
            self.write(self.reference, f"packages/{name}/package.json", {"name": name, "dependencies": dependencies, "scripts": {"test": "check"}})
        (self.reference / ".gitignore").write_text("node_modules/\n.husky/_/\n", encoding="utf-8")
        delivery.git(self.reference, "init", "-b", "fixture")
        delivery.git(self.reference, "add", ".")
        self.root = self.base / "workspace"
        shutil.copytree(self.reference, self.root)
        self.before = delivery.inventory(self.root)

    def write(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def check(self):
        after = delivery.inventory(self.root)
        changed = {n for n in set(self.before) | set(after) if self.before.get(n) != after.get(n)}
        return blocked_mutations(self.root, "packages/app", "scope-fixture", self.before, changed, self.reference)

    def test_shared_lock_override_and_reachable_dependency_are_allowed(self):
        self.write(self.root, "package-lock.json", {"lockfileVersion": 3, "packages": {"new": {"version": "2.0.0"}}})
        manifest = delivery.read(self.root / "package.json")
        manifest["overrides"] = {"external": "2.0.0"}
        self.write(self.root, "package.json", manifest)
        manifest = delivery.read(self.root / "packages/local-lib/package.json")
        manifest["dependencies"]["external"] = "2.0.0"
        self.write(self.root, "packages/local-lib/package.json", manifest)
        self.assertEqual(self.check(), [])

    def test_unrelated_root_selector_stays_blocked(self):
        manifest = delivery.read(self.root / "package.json")
        manifest["overrides"] = {"other": "2.0.0"}
        self.write(self.root, "package.json", manifest)
        self.assertEqual(self.check(), ["package.json"])

    def test_wildcard_and_parent_qualified_selectors_stay_blocked(self):
        for field, selector in [("resolutions", "**/external"), ("resolutions", "local-lib/external"), ("overrides", "local-lib@1")]:
            with self.subTest(field=field, selector=selector):
                manifest = delivery.read(self.reference / "package.json")
                manifest[field] = {selector: "2.0.0"}
                self.write(self.root, "package.json", manifest)
                self.assertEqual(self.check(), ["package.json"])

    def test_shared_consumer_does_not_gain_repair_authority(self):
        for root in (self.reference, self.root):
            manifest = delivery.read(root / "packages/unrelated/package.json")
            manifest["dependencies"]["external"] = "1.0.0"
            self.write(root, "packages/unrelated/package.json", manifest)
        self.before = delivery.inventory(self.root)
        manifest = delivery.read(self.root / "package.json")
        manifest["overrides"] = {"external": "2.0.0"}
        self.write(self.root, "package.json", manifest)
        self.assertEqual(self.check(), ["package.json"])

    def test_unrelated_manifests_and_root_scripts_stay_blocked(self):
        manifest = delivery.read(self.root / "package.json")
        manifest["scripts"] = {"test": "skip"}
        self.write(self.root, "package.json", manifest)
        manifest = delivery.read(self.root / "packages/unrelated/package.json")
        manifest["dependencies"]["other"] = "2.0.0"
        self.write(self.root, "packages/unrelated/package.json", manifest)
        self.assertEqual(self.check(), ["package.json", "packages/unrelated/package.json"])

    def test_reachable_manifest_cannot_change_scripts_or_dependency_names(self):
        name = "packages/local-lib/package.json"
        original = delivery.read(self.root / name)
        for changed in [{**original, "scripts": {"test": "skip"}}, {**original, "dependencies": {"replacement": "2.0.0"}}]:
            self.write(self.root, name, changed)
            self.assertEqual(self.check(), [name])

    def test_agent_cannot_expand_membership_or_reachability(self):
        manifest = delivery.read(self.root / "packages/app/package.json")
        manifest["dependencies"]["unrelated"] = "1.0.0"
        self.write(self.root, "packages/app/package.json", manifest)
        self.write(self.root, "packages/unrelated/package.json", {"name": "unrelated", "dependencies": {"other": "9.0.0"}})
        self.assertEqual(self.check(), ["packages/unrelated/package.json"])

    def test_untracked_ignored_husky_output_only(self):
        path = self.root / ".husky/_/pre-commit"
        path.parent.mkdir(parents=True)
        path.write_text("custom hook", encoding="utf-8")
        self.assertEqual(self.check(), [".husky/_/pre-commit"])
        path.write_bytes(b'#!/usr/bin/env sh\n. "$(dirname "$0")/h"')
        self.assertEqual(self.check(), [])
        delivery.git(self.root, "add", "-f", ".husky/_/pre-commit")
        self.assertEqual(self.check(), [".husky/_/pre-commit"])

    def test_corrupt_reference_fails_closed(self):
        manifest = delivery.read(self.root / "package.json")
        manifest["overrides"] = {"external": "2.0.0"}
        self.write(self.root, "package.json", manifest)
        self.write(self.reference, "package.json", manifest)
        self.assertEqual(self.check(), ["package.json"])


class AuditRecoveryTests(unittest.TestCase):
    def test_recovery_archives_displaced_bytes_and_never_accepts_new_evidence(self):
        with tempfile.TemporaryDirectory(prefix="deploom-audit-recovery-") as temporary:
            run = Path(temporary)
            root = run / "workspace"
            prefix = "docs/dependency-migration/run-fixture/"
            names = ["pre-fix-audit-report.json", "post-fix-audit-report.json", "ru/pre-fix-audit-report.md", "ru/post-fix-audit-report.md"]
            expected = {}
            for name in names:
                target, reference = root / (prefix + name), run / "reports" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                reference.parent.mkdir(parents=True, exist_ok=True)
                reference.write_bytes(b"controller evidence")
                target.write_bytes(b"agent replacement")
                expected[prefix + name] = hashlib.sha256(reference.read_bytes()).hexdigest()
            state = {"workspaceRoot": str(root), "runId": "run-fixture", "documentLanguage": "ru", "expected": expected}
            restored = restore_audit_copies(run, state)
            self.assertEqual(len(restored), 4)
            for item in restored:
                self.assertEqual(Path(item["archiveRef"]).read_bytes(), b"agent replacement")
                self.assertEqual((root / item["path"]).read_bytes(), b"controller evidence")
            self.assertEqual(restore_audit_copies(run, state), [])
            name = names[0]
            (run / "reports" / name).write_bytes(b"corrupt reference")
            (root / (prefix + name)).write_bytes(b"another replacement")
            self.assertEqual(restore_audit_copies(run, state), [])
            self.assertEqual((root / (prefix + name)).read_bytes(), b"another replacement")

    def test_preflight_recovers_reports_but_source_drift_still_blocks(self):
        from tests.regression.test_iterative_delivery import GitDeliveryTests
        from iterative_delivery_preflight import delivery_preflight
        case = GitDeliveryTests("test_russian_delivery_copies_all_documents_and_untranslated_json")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.config["documentLanguage"] = "ru"
        import iterative_migration as core
        core.save_config(case.run_dir, case.config)
        state = case.prepare()
        root = Path(state["workspaceRoot"])
        name = f"docs/dependency-migration/{state['runId']}/pre-fix-audit-report.json"
        (root / name).write_bytes(b"agent evidence")
        with patch.object(core, "verify_assignment") as verify:
            result = delivery_preflight(case.run_dir, {})
            verify.assert_not_called()
        self.assertEqual(result["restoredAuditReportCount"], 1)
        self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), state["expected"][name])
        (root / "source.txt").write_bytes(b"source drift")
        with self.assertRaisesRegex(RuntimeError, "DELIVERY_SOURCE_CHANGED"):
            delivery_preflight(case.run_dir, {})
