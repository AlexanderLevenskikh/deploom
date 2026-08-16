from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verification_proof import (
    build_verification_proof_identity,
    emit_verification_event,
    environment_snapshot_fingerprint,
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


if __name__ == "__main__":
    unittest.main()
