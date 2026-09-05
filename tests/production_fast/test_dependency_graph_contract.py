from __future__ import annotations

import unittest
from pathlib import Path


class DependencyGraphDesktopContractTests(unittest.TestCase):
    def test_graph_is_explanatory_not_proof_authority(self) -> None:
        root = Path(__file__).resolve().parents[2]
        electron = (root / "desktop/electron/dependency-graph.ts").read_text(encoding="utf-8")
        projection = (root / "desktop/src/data/dependencyGraphProjection.ts").read_text(encoding="utf-8")
        component = (root / "desktop/src/components/DependencyGraphWorkspace.tsx").read_text(encoding="utf-8")
        app = (root / "desktop/src/App.tsx").read_text(encoding="utf-8")

        self.assertIn("proofAuthority: false", electron)
        self.assertIn("OBSERVED_LOCAL_MANIFEST", electron)
        self.assertIn("DIAGNOSTIC_HINT", projection)
        self.assertIn("USER_POLICY", projection)
        self.assertIn("Verified Scope", component)
        self.assertIn("Current blockage", component)
        self.assertIn("DependencyGraphWorkspace", app)

    def test_graph_omits_unobserved_relations_instead_of_guessing(self) -> None:
        root = Path(__file__).resolve().parents[2]
        electron = (root / "desktop/electron/dependency-graph.ts").read_text(encoding="utf-8")
        self.assertIn("if (!manifest) continue", electron)
        self.assertIn("manifestCoverage", electron)


if __name__ == "__main__":
    unittest.main()
