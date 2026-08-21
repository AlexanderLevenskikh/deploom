from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_proof import (
    SourceIdentityUnavailable,
    VerificationProofStore,
    bind_resolved_state_identity,
    build_verification_proof_identity,
    emit_verification_event,
    environment_snapshot_fingerprint,
    source_snapshot_fingerprint,
)
from resolved_dependency_state import (
    capture_resolved_dependency_state,
    resolved_state_metadata,
)


class VerificationProofIdentityTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        (root / "package.json").write_text(
            json.dumps({
                "packageManager": "npm@11.0.0",
                "dependencies": {"demo": "^1.0.0"},
                "scripts": {"typecheck": "tsc"},
            }),
            encoding="utf-8",
        )
        (root / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3, "packages": {}}),
            encoding="utf-8",
        )
        (root / "src").mkdir()
        (root / "src" / "index.ts").write_text("export const value = 1\n", encoding="utf-8")
        return root

    def _identity(
        self,
        project: Path,
        *,
        assignment: dict[str, str] | None = None,
        commands: tuple[str, ...] = ("npm run typecheck",),
        registry: str = "https://registry.example.test/npm/",
        environment: dict[str, str] | None = None,
    ):
        env = dict(environment or {"PATH": os.environ.get("PATH", ""), "HOME": str(project)})
        return build_verification_proof_identity(
            project,
            assignment=assignment or {"demo": "2.0.0"},
            remove_packages=(),
            manager="npm",
            manager_executable=sys.executable,
            registry=registry,
            project_checks="adaptive",
            commands=commands,
            environment=env,
        )

    def test_assignment_changes_resolver_preparation_and_project_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            first = self._identity(project, assignment={"demo": "2.0.0"})
            second = self._identity(project, assignment={"demo": "3.0.0"})
            self.assertNotEqual(first.resolver_input_key, second.resolver_input_key)
            self.assertNotEqual(first.preparation_proof_key, second.preparation_proof_key)
            self.assertNotEqual(first.project_proof_key, second.project_proof_key)

    def test_source_only_change_keeps_resolver_key_but_invalidates_project_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            first = self._identity(project)
            (project / "src" / "index.ts").write_text("export const value = 2\n", encoding="utf-8")
            second = self._identity(project)
            self.assertEqual(first.resolver_input_key, second.resolver_input_key)
            self.assertNotEqual(first.source_snapshot_key, second.source_snapshot_key)
            self.assertNotEqual(first.preparation_proof_key, second.preparation_proof_key)
            self.assertNotEqual(first.project_proof_key, second.project_proof_key)

    def test_project_command_change_does_not_invalidate_resolver_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            first = self._identity(project, commands=("npm run typecheck",))
            second = self._identity(project, commands=("npm run build",))
            self.assertEqual(first.resolver_input_key, second.resolver_input_key)
            self.assertEqual(first.preparation_proof_key, second.preparation_proof_key)
            self.assertNotEqual(first.project_proof_key, second.project_proof_key)

    def test_environment_change_invalidates_resolver_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            first = self._identity(project, environment={"PATH": "A", "HOME": str(project)})
            second = self._identity(project, environment={"PATH": "B", "HOME": str(project)})
            self.assertNotEqual(first.environment_key, second.environment_key)
            self.assertNotEqual(first.resolver_input_key, second.resolver_input_key)

    def test_environment_fingerprint_never_exposes_secret_value(self) -> None:
        secret = "super-secret-token"
        fingerprint = environment_snapshot_fingerprint({"NPM_TOKEN": secret, "PATH": "x"})
        self.assertNotIn(secret, fingerprint)
        self.assertEqual(32, len(fingerprint))

    def test_jsonl_telemetry_is_structured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "telemetry.jsonl"
            emit_verification_event(path, "verify.test", durationMs=12, resolverInputKey="abc")
            line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual("verify.test", line["event"])
            self.assertEqual(12, line["durationMs"])
            self.assertEqual("abc", line["resolverInputKey"])

    def test_monorepo_sibling_change_invalidates_project_source_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "packages" / "app"
            lib = root / "packages" / "lib"
            app.mkdir(parents=True)
            lib.mkdir(parents=True)
            (app / "package.json").write_text('{"name":"app"}\n', encoding="utf-8")
            (lib / "index.ts").write_text("export const value = 1\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "proof@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Proof Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, stdout=subprocess.DEVNULL)

            first = source_snapshot_fingerprint(app)
            (lib / "index.ts").write_text("export const value = 2\n", encoding="utf-8")
            second = source_snapshot_fingerprint(app)
            self.assertNotEqual(first, second)

    def test_source_snapshot_capture_failure_is_fail_closed(self) -> None:
        # Block X removed Git status/diff as the authoritative source identity.
        # VerificationProof now adapts the captured-content SourceSnapshot
        # boundary. Capture uncertainty must still fail closed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                '{"name":"source-capture-failure"}\n',
                encoding="utf-8",
            )
            SourceCaptureError = __import__("source_snapshot").SourceCaptureError
            with patch(
                "verification_proof.captured_source_snapshot_fingerprint",
                side_effect=SourceCaptureError(
                    "SOURCE_FILE_UNREADABLE: simulated capture failure"
                ),
            ):
                with self.assertRaisesRegex(
                    SourceIdentityUnavailable,
                    "SOURCE_FILE_UNREADABLE",
                ):
                    source_snapshot_fingerprint(root)

    def test_pass_only_proof_store_round_trip_and_corruption_is_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            self._project(project)
            identity = self._identity(project)
            store = VerificationProofStore(root / "cas")
            observed = {"demo": "2.0.0"}
            observed_hash = __import__("hashlib").sha256(
                json.dumps(observed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            state = capture_resolved_dependency_state(
                project,
                manager="npm",
                resolver_input_key=identity.resolver_input_key,
                observed_resolved_hash=observed_hash,
                proof_cache_dir=store.root,
            )
            identity = bind_resolved_state_identity(
                identity,
                state.key,
                project_checks="adaptive",
                commands=("npm run typecheck",),
            )
            metadata = {
                "observedResolvedVersions": observed,
                "observedResolvedHash": observed_hash,
                **resolved_state_metadata(state),
            }

            self.assertIsNone(store.lookup_pass("project", identity.project_proof_key))
            self.assertTrue(store.publish_pass(
                "project", identity.project_proof_key, identity, metadata=metadata
            ))
            record = store.lookup_pass("project", identity.project_proof_key)
            self.assertIsNotNone(record)
            self.assertEqual(identity.project_proof_key, record.key)

            record_path = root / "cas" / "project" / f"{identity.project_proof_key}.json"
            record_path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(store.lookup_pass("resolver", identity.resolver_input_key))

    def test_resolver_cache_requires_self_consistent_observed_tree_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            self._project(project)
            identity = self._identity(project)
            store = VerificationProofStore(root / "cas")
            observed = {"demo": "2.0.0"}
            observed_hash = __import__("hashlib").sha256(
                json.dumps(observed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            state = capture_resolved_dependency_state(
                project,
                manager="npm",
                resolver_input_key=identity.resolver_input_key,
                observed_resolved_hash=observed_hash,
                proof_cache_dir=store.root,
            )
            identity = bind_resolved_state_identity(
                identity,
                state.key,
                project_checks="adaptive",
                commands=("npm run typecheck",),
            )
            self.assertTrue(store.publish_pass(
                "resolver",
                identity.resolver_input_key,
                identity,
                metadata={
                    "observedResolvedVersions": observed,
                    "observedResolvedHash": observed_hash,
                    **resolved_state_metadata(state),
                },
            ))
            self.assertIsNotNone(store.lookup_pass("resolver", identity.resolver_input_key))

            path = root / "cas" / "resolver" / f"{identity.resolver_input_key}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["metadata"]["observedResolvedHash"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(store.lookup_pass("resolver", identity.resolver_input_key))

    def test_proof_store_rejects_invalid_key_and_unknown_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = VerificationProofStore(Path(tmp) / "cas")
            self.assertIsNone(store.lookup_pass("project", "not-a-hash"))
            self.assertIsNone(store.lookup_pass("failure", "a" * 32))


    def test_tool_cache_does_not_change_source_or_project_proof_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            first = self._identity(project)
            cache_file = project / ".dependency-roadmap" / "cache" / "baseline-proofs" / "x.json"
            cache_file.parent.mkdir(parents=True)
            cache_file.write_text("{}", encoding="utf-8")
            second = self._identity(project)
            self.assertEqual(first.source_snapshot_key, second.source_snapshot_key)
            self.assertEqual(first.project_proof_key, second.project_proof_key)


    def test_resolved_state_binding_invalidates_preparation_and_project_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            base = self._identity(project)
            first = bind_resolved_state_identity(
                base,
                "a" * 64,
                project_checks="adaptive",
                commands=("npm run typecheck",),
            )
            second = bind_resolved_state_identity(
                base,
                "b" * 64,
                project_checks="adaptive",
                commands=("npm run typecheck",),
            )
            self.assertEqual(base.resolver_input_key, first.resolver_input_key)
            self.assertEqual(base.resolver_input_key, second.resolver_input_key)
            self.assertEqual("a" * 64, first.resolved_state_key)
            self.assertNotEqual(first.preparation_proof_key, second.preparation_proof_key)
            self.assertNotEqual(first.project_proof_key, second.project_proof_key)

    def test_parent_yarnrc_outside_project_invalidates_resolver_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "parent"
            project = parent / "repo"
            project.mkdir(parents=True)
            (project / "package.json").write_text(
                json.dumps({"packageManager": "yarn@1.22.22", "dependencies": {"demo": "^1.0.0"}}),
                encoding="utf-8",
            )
            (project / "yarn.lock").write_text("", encoding="utf-8")
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root / "home")}
            first = build_verification_proof_identity(
                project,
                assignment={"demo": "2.0.0"},
                remove_packages=(),
                manager="yarn",
                manager_executable=sys.executable,
                registry="https://registry.example.test/npm",
                project_checks="adaptive",
                commands=("yarn test",),
                environment=env,
            )
            (parent / ".yarnrc").write_text(
                'registry "https://registry.example.test/other"\\n',
                encoding="utf-8",
            )
            second = build_verification_proof_identity(
                project,
                assignment={"demo": "2.0.0"},
                remove_packages=(),
                manager="yarn",
                manager_executable=sys.executable,
                registry="https://registry.example.test/npm",
                project_checks="adaptive",
                commands=("yarn test",),
                environment=env,
            )
            self.assertNotEqual(first.resolver_input_key, second.resolver_input_key)


if __name__ == "__main__":
    unittest.main()
