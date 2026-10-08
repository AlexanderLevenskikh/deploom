"""Regression: explicit project/CI Node runtime (D2-D4).

The resolver must turn the requested value into ONE concrete installed runtime
by a real version probe; an unavailable requested version is an explicit
ENVIRONMENT_UNAVAILABLE outcome with the discovered versions listed — never a
silent PATH fallback. engines.node matching uses npm semver (OR ranges, minimal
minor/patch, prerelease) against the EXACT effective version, not a hand-made
string comparison. The runtime contract hash lets identity-bearing layers bind
to the exact toolchain.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from project_runtime import (  # noqa: E402
    engine_mismatch_detail,
    node_range_satisfied,
    normalize_node_version,
    probe_node_version,
    resolve_requested_node,
    runtime_env,
)


def _fake_dir(tmp: Path, version: str) -> Path:
    """A directory with a fake `node` that answers --version with the version."""
    directory = tmp / ("node-" + version)
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        inner = "import sys; sys.stdout.write('v%s\\n')" % version
        body = "@echo off\r\npython -c \"" + inner + "\"\r\n"
        executable = directory / "node.cmd"
        executable.write_text(body, encoding="utf-8")
    else:
        executable = directory / "node"
        executable.write_text(f"#!/bin/sh\nprintf 'v{version}\\n'\n", encoding="utf-8")
        executable.chmod(0o755)
    (directory / "npm.cmd" if os.name == "nt" else directory / "npm").write_text("", encoding="utf-8")
    return directory


class ProjectRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls._tmp = Path(tempfile.mkdtemp(prefix="project-runtime-test-"))

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil

        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_normalize_node_version(self) -> None:
        self.assertEqual("22.18.0", normalize_node_version("v22.18.0"))
        self.assertEqual("22.18.0", normalize_node_version("node v22.18.0\n"))
        self.assertEqual("24.11.0", normalize_node_version("v24.11.0 (x64)"))
        self.assertEqual("", normalize_node_version("not-a-version"))
        self.assertEqual("", normalize_node_version(""))

    def test_resolve_exact_installed(self) -> None:
        fake = _fake_dir(self._tmp, "22.18.0")
        resolution = resolve_requested_node(
            "22.18.0",
            discovered=[(str(fake / ("node.cmd" if os.name == "nt" else "node")), "22.18.0")],
        )
        self.assertTrue(resolution.found)
        self.assertEqual("22.18.0", resolution.effective_version)
        self.assertEqual("installed-exact", resolution.source)
        self.assertTrue(resolution.npm_path)

    def test_resolve_major_alias_to_exact_installed_version(self) -> None:
        node = "node.cmd" if os.name == "nt" else "node"
        resolution = resolve_requested_node(
            "22",
            discovered=[
                (str(Path("a") / node), "22.17.4"),
                (str(Path("b") / node), "22.18.0"),
                (str(Path("c") / node), "24.11.0"),
            ],
        )
        self.assertTrue(resolution.found)
        self.assertEqual("22.18.0", resolution.effective_version)
        self.assertEqual("major-alias", resolution.source)

    def test_unavailable_requested_is_environment_unavailable_no_fallback(self) -> None:
        node = "node.cmd" if os.name == "nt" else "node"
        resolution = resolve_requested_node(
            "20.11.0",
            discovered=[
                (str(Path("a") / node), "22.18.0"),
                (str(Path("b") / node), "24.11.0"),
            ],
        )
        self.assertFalse(resolution.found)
        self.assertEqual("", resolution.effective_version)
        self.assertEqual(("22.18.0", "24.11.0"), resolution.available_versions)
        self.assertIn("20.11.0", resolution.unavailable_reason)
        self.assertIn("22.18.0", resolution.unavailable_reason)

    def test_empty_request_is_not_resolved(self) -> None:
        resolution = resolve_requested_node("", discovered=[])
        self.assertFalse(resolution.found)
        self.assertEqual("", resolution.effective_version)
        self.assertEqual("EMPTY", resolution.unavailable_reason)

    def test_runtime_env_prepends_node_dir(self) -> None:
        node = "node.cmd" if os.name == "nt" else "node"
        # A Windows drive-letter path can never be a PATH entry on POSIX (':' is
        # the separator there), so the fixture must be platform-appropriate —
        # otherwise splitting the joined PATH truncates the node dir.
        runtime_dir = "X:/runtimes/22.18.0" if os.name == "nt" else "/opt/runtimes/22.18.0"
        resolution = resolve_requested_node(
            "22.18.0",
            discovered=[(str(Path(runtime_dir) / node), "22.18.0")],
        )
        extra = ["C:/other", "D:/bin"] if os.name == "nt" else ["/usr/other", "/usr/bin"]
        env = runtime_env(resolution, base_env={"PATH": os.pathsep.join(extra), "npm_config_registry": "https://r"})
        entries = env["PATH"].split(os.pathsep)
        node_dir = str(Path(resolution.node_path).parent)
        self.assertEqual(os.path.normcase(node_dir), os.path.normcase(entries[0]))
        for entry in extra:
            self.assertIn(os.path.normcase(entry), [os.path.normcase(e) for e in entries])
        self.assertEqual("https://r", env["npm_config_registry"])

    def test_contract_hash_binds_runtime(self) -> None:
        node = "node.cmd" if os.name == "nt" else "node"
        a = resolve_requested_node("22.18.0", discovered=[(str(Path("r1") / node), "22.18.0")])
        b = resolve_requested_node("22.18.0", discovered=[(str(Path("r1") / node), "22.18.0")])
        c = resolve_requested_node("22.17.4", discovered=[(str(Path("r1") / node), "22.17.4")])
        self.assertEqual(a.contract_hash(), b.contract_hash())
        self.assertNotEqual(a.contract_hash(), c.contract_hash())

    def test_node_range_satisfied_spaced_npm_comparators(self) -> None:
        for spec in (">= 18", ">= 0.8.0", ">= 14", "^ 22.18.0 || >= 24.11.0"):
            self.assertTrue(node_range_satisfied(spec, "24.18.0"), spec)
        self.assertFalse(node_range_satisfied("< 24", "24.18.0"))
        self.assertFalse(node_range_satisfied(">= unknown", "24.18.0"))

    def test_node_range_satisfied_or_range_boundaries(self) -> None:
        spec = "^22.18.0 || >=24.11.0"
        self.assertTrue(node_range_satisfied(spec, "22.18.0"))
        self.assertFalse(node_range_satisfied(spec, "22.17.4"))
        self.assertTrue(node_range_satisfied(spec, "24.11.0"))
        self.assertFalse(node_range_satisfied(spec, "24.10.9"))
        self.assertFalse(node_range_satisfied(spec, "20.11.0"))
        self.assertFalse(node_range_satisfied(spec, "not-a-version"))

    def test_node_range_minor_and_prerelease(self) -> None:
        self.assertTrue(node_range_satisfied(">=22.18.0", "22.19.0"))
        self.assertFalse(node_range_satisfied("22.x", "24.0.0"))
        self.assertFalse(node_range_satisfied(">22", "22.0.0"))

    def test_engine_mismatch_detail_is_structured(self) -> None:
        node = "node.cmd" if os.name == "nt" else "node"
        resolution = resolve_requested_node(
            "20.11.0",
            discovered=[(str(Path("r1") / node), "22.18.0")],
        )
        detail = engine_mismatch_detail("es5-ext", "0.10.63", "^22.18.0 || >=24.11.0", resolution)
        self.assertEqual("NODE_ENGINE_MISMATCH", detail["kind"])
        self.assertEqual("es5-ext", detail["package"])
        self.assertEqual("^22.18.0 || >=24.11.0", detail["requiredNodeRange"])
        self.assertEqual("20.11.0", detail["requestedNode"])
        self.assertEqual("registry metadata engines.node", detail["evidence"])

    def test_probe_node_version_runs_real_executable(self) -> None:
        fake_dir = _fake_dir(self._tmp, "22.18.0")
        executable = fake_dir / ("node.cmd" if os.name == "nt" else "node")
        version = probe_node_version(str(executable))
        self.assertEqual("22.18.0", version)
        self.assertEqual("", probe_node_version(str(Path("no-such-node-here"))))

    def test_installed_runtimes_json_shape(self) -> None:
        from project_runtime import installed_runtimes_json

        payload = json.loads(installed_runtimes_json())
        self.assertIsInstance(payload, list)
        for entry in payload:
            self.assertIn("path", entry)
            self.assertIn("version", entry)


if __name__ == "__main__":
    unittest.main()
