"""Physical regression for the v0.2.71 the reported production project release blocker.

A guarded lower reaches its dependency tree through NTFS junctions that point
at the sealed PreparedArtifact, which by construction lives outside the trial
root. `observed_resolved_assignment` resolved each candidate package.json and
required the result to stay inside the trial root, so every junctioned package
looked like a boundary escape. On that project it fired on the first sorted
managed direct dependency, `@babel/cli`, and surfaced as
BASELINE_VERIFY_UNKNOWN_ERROR.

These tests use real junctions, not mocks, and they also pin the guard's
remaining strength: an unauthorized target, a poisoned target and a version
drift must all still be rejected.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import baseline_constraint_verifier as verifier
from prepared_workspace_fastpath import (
    cleanup_guarded_clone,
    dependency_root_manifest,
    guarded_clone_authorized_roots,
    try_materialize_guarded_clone,
)


def _junction(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )


@unittest.skipUnless(os.name == "nt", "NTFS junction physical acceptance")
class GuardedLowerAssignmentEscapeRegression(unittest.TestCase):
    """The exact production incident shape, with a scoped package."""

    def _fixture(self, root: Path, *, version: str = "7.24.8") -> tuple[Path, Path]:
        sealed = root / "sealed"
        (sealed / "node_modules" / "@babel" / "cli").mkdir(parents=True)
        (sealed / "node_modules" / "@babel" / "cli" / "package.json").write_text(
            json.dumps({"name": "@babel/cli", "version": version}), encoding="utf-8"
        )
        clone = root / "project-check-001"
        clone.mkdir(parents=True)
        (clone / "package.json").write_text(
            json.dumps({"devDependencies": {"@babel/cli": version}}), encoding="utf-8"
        )
        _junction(
            clone / "node_modules" / "@babel" / "cli",
            sealed / "node_modules" / "@babel" / "cli",
        )
        return sealed, clone

    def test_junctioned_scoped_package_is_not_an_escape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            sealed, clone = self._fixture(Path(raw))
            observed = verifier.observed_resolved_assignment(
                clone,
                {"@babel/cli": "7.24.8"},
                package_manager_root=clone,
                authorized_roots=(sealed,),
            )
            self.assertEqual(observed, {"@babel/cli": "7.24.8"})

    def test_unauthorized_junction_target_is_still_an_escape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _sealed, clone = self._fixture(Path(raw))
            with self.assertRaisesRegex(
                verifier.ObservedResolutionError,
                "OBSERVED_RESOLVED_ASSIGNMENT_ESCAPE",
            ):
                verifier.observed_resolved_assignment(
                    clone, {"@babel/cli": "7.24.8"}, package_manager_root=clone
                )

    def test_poisoned_junction_target_outside_authorized_roots_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _sealed, clone = self._fixture(root)
            unrelated = root / "some-other-artifact"
            unrelated.mkdir()
            with self.assertRaisesRegex(
                verifier.ObservedResolutionError,
                "OBSERVED_RESOLVED_ASSIGNMENT_ESCAPE",
            ):
                verifier.observed_resolved_assignment(
                    clone,
                    {"@babel/cli": "7.24.8"},
                    package_manager_root=clone,
                    authorized_roots=(unrelated,),
                )

    def test_authorized_root_still_enforces_exact_assignment(self) -> None:
        """An artifact from another assignment must not pass just by living in
        an authorized root."""
        with tempfile.TemporaryDirectory() as raw:
            sealed, clone = self._fixture(Path(raw), version="7.24.8")
            (sealed / "node_modules" / "@babel" / "cli" / "package.json").write_text(
                json.dumps({"name": "@babel/cli", "version": "7.0.0"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                verifier.ObservedResolutionError,
                "OBSERVED_RESOLVED_ASSIGNMENT_DRIFT",
            ):
                verifier.observed_resolved_assignment(
                    clone,
                    {"@babel/cli": "7.24.8"},
                    package_manager_root=clone,
                    authorized_roots=(sealed,),
                )


@unittest.skipUnless(os.name == "nt", "NTFS junction physical acceptance")
class GuardedLowerAuthorizedRootsWiring(unittest.TestCase):
    """End-to-end: the real Ω materializer must publish usable authorized roots."""

    def test_materialized_guarded_clone_observes_its_own_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prepared = root / "prepared"
            package = prepared / "node_modules" / "@babel" / "cli"
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps({"name": "@babel/cli", "version": "7.24.8"}), encoding="utf-8"
            )
            (prepared / "package.json").write_text(
                json.dumps({"devDependencies": {"@babel/cli": "7.24.8"}}),
                encoding="utf-8",
            )
            target = root / "clone"
            project = try_materialize_guarded_clone(
                source_project=root / "source",
                prepared_workspace_root=prepared,
                project_relative=Path("."),
                target=target,
                dependency_roots=dependency_root_manifest(prepared),
            )
            if project is None:
                self.skipTest("guarded lower unavailable in this environment")
            try:
                authorized = guarded_clone_authorized_roots(target)
                self.assertTrue(authorized, "materializer published no authorized roots")
                observed = verifier.observed_resolved_assignment(
                    project,
                    {"@babel/cli": "7.24.8"},
                    package_manager_root=project,
                    authorized_roots=authorized,
                )
                self.assertEqual(observed, {"@babel/cli": "7.24.8"})
            finally:
                cleanup_guarded_clone(target)


if __name__ == "__main__":
    unittest.main()
