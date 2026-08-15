from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import manual_dependency_audit as manual


class ManualAuditPublishDateRegressionTests(unittest.TestCase):
    def test_separate_npm_view_fallback_recovers_react_dom_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "package.json").write_text(json.dumps({
                "name": "demo",
                "dependencies": {"react-dom": "18.2.0"},
            }), encoding="utf-8")
            (project / "package-lock.json").write_text(json.dumps({
                "lockfileVersion": 3,
                "packages": {"node_modules/react-dom": {"version": "18.2.0"}},
            }), encoding="utf-8")

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_impl = bin_dir / "fake_npm.py"
            fake_impl.write_text("\n".join([
                "import json, sys",
                "args = sys.argv[1:]",
                "if args and args[0] == 'audit':",
                "    print(json.dumps({'vulnerabilities': {}, 'metadata': {'vulnerabilities': {'critical': 0, 'high': 0, 'moderate': 0, 'low': 0, 'unknown': 0}}}))",
                "    raise SystemExit(0)",
                "if args and args[0] == 'view':",
                "    fields = [x for x in args[2:] if not x.startswith('--') and not x.startswith('https://')]",
                "    if fields[:2] == ['time', 'dist-tags']:",
                "        print(json.dumps({'unexpectedProjection': True}))",
                "        raise SystemExit(0)",
                "    if fields and fields[0] == 'time':",
                "        print(json.dumps({'created': '2020-01-01T00:00:00Z', '18.2.0': '2022-06-14T19:46:48.37Z', '19.2.7': '2026-06-01T18:01:02.438Z'}))",
                "        raise SystemExit(0)",
                "    if fields and fields[0] == 'dist-tags':",
                "        print(json.dumps({'latest': '19.2.7'}))",
                "        raise SystemExit(0)",
                "raise SystemExit(2)",
                "",
            ]), encoding="utf-8")

            if os.name == "nt":
                wrapper = bin_dir / "npm.cmd"
                wrapper.write_text(
                    f'@echo off\r\n"{sys.executable}" "{fake_impl}" %*\r\nexit /b %ERRORLEVEL%\r\n',
                    encoding="utf-8",
                )
            else:
                wrapper = bin_dir / "npm"
                wrapper.write_text(
                    f'#!/bin/sh\nexec "{sys.executable}" "{fake_impl}" "$@"\n',
                    encoding="utf-8",
                )
                wrapper.chmod(0o755)

            env = dict(os.environ)
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            result = subprocess.run([
                sys.executable,
                str(Path(manual.__file__)),
                "--project-dir", str(project),
                "--registry", "https://registry.example",
            ], text=True, encoding="utf-8", capture_output=True, check=False, env=env)

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("Completeness: `COMPLETE`", result.stdout)
            self.assertIn("`react-dom`", result.stdout)
            self.assertIn("2022-06-14T19:46:48.370000+00:00", result.stdout)
            self.assertIn("Lagging: **1**; OK: **0**; unknown: **0**.", result.stdout)


    def test_partial_time_map_is_recovered_with_exact_version_queries(self) -> None:
        calls = []

        def fake_view(project: Path, name: str, fields: list[str], registry: str):
            calls.append((name, tuple(fields)))
            if fields == ["time", "dist-tags"]:
                return {
                    "time": {
                        "created": "2020-01-01T00:00:00Z",
                        "modified": "2026-06-01T18:01:02.438Z",
                    },
                    "dist-tags": {"latest": "19.2.7"},
                }, ""
            if fields == ["time[18.2.0]"]:
                return "2022-06-14T19:46:48.370Z", ""
            if fields == ["time[19.2.7]"]:
                return "2026-06-01T18:01:02.438Z", ""
            raise AssertionError((name, fields))

        from unittest.mock import patch
        with patch.object(manual, "_npm_view_json", side_effect=fake_view):
            result = manual.check_lag(
                Path.cwd(),
                [{
                    "section": "dependencies",
                    "name": "react-dom",
                    "spec": "18.2.0",
                    "current": "18.2.0",
                    "source": "yarn.lock",
                    "resolvedExact": True,
                }],
                "https://registry.example",
                12,
                {},
            )

        self.assertEqual("lagging", result[0]["status"])
        self.assertEqual("2022-06-14T19:46:48.370000+00:00", result[0]["currentPublishedAt"])
        self.assertEqual("", result[0]["error"])
        self.assertIn(("react-dom", ("time[18.2.0]",)), calls)
        self.assertIn(("react-dom", ("time[19.2.7]",)), calls)

    def test_publish_metadata_cache_defaults_to_twenty_four_hours(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "metadata-cache.json"
            generated = manual.dt.datetime.now(manual.dt.timezone.utc) - manual.dt.timedelta(hours=2)
            cache_path.write_text(json.dumps({
                "schemaVersion": 1,
                "generatedAt": generated.isoformat(),
                "registry": "https://registry.example",
                "entries": {
                    "react-dom": {"metadata": {"dist-tags": {"latest": "19.2.7"}}, "error": ""}
                },
            }), encoding="utf-8")

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ROADMAP_AUDIT_METADATA_CACHE_HOURS", None)
                cached = manual._metadata_cache_load(cache_path, "https://registry.example")
            self.assertIn("react-dom", cached)

            with patch.dict(os.environ, {"ROADMAP_AUDIT_METADATA_CACHE_HOURS": "1"}):
                expired = manual._metadata_cache_load(cache_path, "https://registry.example")
            self.assertEqual({}, expired)


if __name__ == "__main__":
    unittest.main()
