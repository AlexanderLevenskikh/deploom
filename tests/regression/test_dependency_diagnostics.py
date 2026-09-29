# -*- coding: utf-8 -*-
"""D4.5/D4.6: non-fatal dependency diagnostics over the project manifest.

``_dependency_diagnostics`` reports resolution-pin warnings (a pin that is not a
version-looking spec, or that has no direct consumer) and deterministically
malformed Git dependency URLs as STRUCTURED, SEPARATE lists. They are
diagnostics only — they never become install constraints, never block planning,
and a missing/unreadable manifest yields ``manifestReadError`` instead of an
exception.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from iterative_migration import (  # noqa: E402
    _dependency_diagnostics,
    _git_dependency_issue,
    _is_git_dependency,
)


def _codes(entries):
    return sorted(str(entry.get("code")) for entry in entries)


class DependencyDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _project(self, manifest: dict) -> Path:
        project = self.root / "project"
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        return project

    def test_resolution_pin_and_git_diagnostics_are_separate(self) -> None:
        project = self._project({
            "name": "demo",
            "dependencies": {
                "good-git": "git+ssh://git@github.com:org/repo.git#v1.0.0",
                "realpkg": "^1.0.0",
                "react": "^18.0.0",
            },
            "resolutions": {
                "realpkg": "1.2.3",
                "unused": "1.0.0",
                "bogus": "not-a-version",
            },
            "overrides": {"react": "^18.2.0"},
        })
        result = _dependency_diagnostics(str(project))
        self.assertEqual(result["manifestReadError"], "")
        self.assertEqual(
            _codes(result["resolutionWarnings"]),
            sorted(["PIN_SPEC_NOT_VERSION", "PIN_WITHOUT_DIRECT_CONSUMER", "PIN_WITHOUT_DIRECT_CONSUMER"]),
        )
        pairs = {(entry["pin"], entry["code"]) for entry in result["resolutionWarnings"]}
        self.assertIn(("unused", "PIN_WITHOUT_DIRECT_CONSUMER"), pairs)
        self.assertIn(("bogus", "PIN_SPEC_NOT_VERSION"), pairs)
        self.assertIn(("bogus", "PIN_WITHOUT_DIRECT_CONSUMER"), pairs)
        self.assertNotIn(("realpkg", "PIN_WITHOUT_DIRECT_CONSUMER"), pairs)
        # No git URLs in this manifest -> no git warnings.
        self.assertEqual(result["gitUrlWarnings"], [])

    def test_malformed_git_urls_are_reported_but_never_fatal(self) -> None:
        project = self._project({
            "name": "demo",
            "dependencies": {
                "good": "git+https://github.com/org/repo.git#v1.0.0",
                "bad-ssh": "git@github.comorg/repo",
                "bad-scheme": "git+foo://bar",
                "bad-host": "git+https://#v1.0.0",
                "bad-spaces": "git+ssh://git@github.com:o/re.git#x y",
            },
        })
        result = _dependency_diagnostics(str(project))
        self.assertEqual(
            _codes(result["gitUrlWarnings"]),
            ["GIT_PLUS_NO_SCHEME", "GIT_SSH_WITHOUT_COLON", "GIT_URL_NO_HOST", "GIT_URL_WHITESPACE"],
        )
        packages = {entry["package"] for entry in result["gitUrlWarnings"]}
        self.assertEqual(
            packages,
            {"bad-ssh", "bad-scheme", "bad-host", "bad-spaces"},
        )

    def test_missing_manifest_is_a_reported_read_error_not_a_crash(self) -> None:
        project = self.root / "no-manifest"
        project.mkdir(parents=True, exist_ok=True)
        result = _dependency_diagnostics(str(project))
        self.assertTrue(result["manifestReadError"])
        self.assertEqual(result["resolutionWarnings"], [])
        self.assertEqual(result["gitUrlWarnings"], [])

    def test_direct_consumers_span_all_dependency_fields(self) -> None:
        project = self._project({
            "name": "demo",
            "dependencies": {"a": "^1.0.0"},
            "devDependencies": {"b": "^2.0.0"},
            "peerDependencies": {"c": "^3.0.0"},
            "optionalDependencies": {"d": "^4.0.0"},
            "resolutions": {"a": "1.1.0", "b": "2.1.0", "c": "3.1.0", "d": "4.1.0", "e": "5.1.0"},
        })
        result = _dependency_diagnostics(str(project))
        pins = [entry["pin"] for entry in result["resolutionWarnings"]]
        self.assertEqual(pins, ["e"])
        self.assertEqual(result["resolutionWarnings"][0]["code"], "PIN_WITHOUT_DIRECT_CONSUMER")

    def test_git_matcher_and_issue_direct_bounds(self) -> None:
        self.assertTrue(_is_git_dependency("git+ssh://git@h:p.git#v1"))
        self.assertTrue(_is_git_dependency("github:org/repo"))
        self.assertTrue(_is_git_dependency("git://h/p.git"))
        self.assertFalse(_is_git_dependency("^1.0.0"))
        self.assertFalse(_is_git_dependency("npm:@x/y@^1"))
        self.assertEqual(_git_dependency_issue("p", "git+ssh://git@h:o/r.git#v1"), {})


if __name__ == "__main__":
    unittest.main()
