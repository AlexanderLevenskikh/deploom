from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class DependencyGraphDesktopContractTests(unittest.TestCase):
    def graph(self, root):
        module = (ROOT / "desktop/dist-electron/dependency-graph.js").as_uri()
        script = f"import {{buildDependencyGraphSnapshot as build}} from {json.dumps(module)};"
        script += "console.log(JSON.stringify(build(process.argv[1], 'fixture', JSON.parse(process.argv[2]))));"
        plan = {"candidates": [{"name": name, "kind": "runtime", "requestedSpec": "*"}
                               for name in ("a", "b", "missing")],
                "intent": {"policies": {"a": "required"}, "deferredCohorts": [
                    {"id": "hint", "label": "Hint", "packages": ["a"], "authority": "DIAGNOSTIC_HINT"}]}}
        completed = subprocess.run(["node", "--input-type=module", "-e", script, str(root), json.dumps(plan)],
                                   capture_output=True, text=True, timeout=15, cwd=ROOT)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def manifest(self, root, name, value):
        path = root / "node_modules" / name / "package.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_graph_is_explanatory_not_proof_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.manifest(root, "a", {"version": "1.0.0", "dependencies": {"b": "*"}})
            self.manifest(root, "b", {"version": "2.0.0"})
            value = self.graph(root)
            self.assertIs(False, value["authorityBoundary"]["proofAuthority"])
            self.assertEqual("OBSERVED_LOCAL_MANIFEST", value["authorityBoundary"]["relations"])
            self.assertEqual("DIAGNOSTIC_HINT", value["intent"]["deferredCohorts"][0]["authority"])
            self.assertEqual("required", next(row for row in value["packages"] if row["name"] == "a")["policy"])
            self.assertEqual(1, len(value["edges"]))
            self.assertEqual(("a", "b", "OBSERVED_LOCAL_MANIFEST"),
                             tuple(value["edges"][0][key] for key in ("source", "target", "authority")))

    def test_graph_omits_unobserved_relations_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.manifest(root, "a", {"version": "1.0.0", "dependencies": {"b": "*"}})
            self.manifest(root, "b", {"version": "2.0.0"})
            self.assertEqual(2, self.graph(root)["manifestCoverage"]["observed"])
            shutil.rmtree(root / "node_modules" / "a")
            value = self.graph(root)
            self.assertEqual({"observed": 1, "total": 3, "missing": ["a", "missing"]}, value["manifestCoverage"])
            self.assertEqual([], value["edges"], "requested specs cannot manufacture observed edges")


if __name__ == "__main__":
    unittest.main()
