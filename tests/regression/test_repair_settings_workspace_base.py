from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as roadmap


def norm(path: Path) -> str:
    # Windows 8.3 short names (LEVENS~1 vs levenskikh) can differ between
    # resolve() calls in different directory levels; compare normalized forms.
    from os import path as ospath

    return ospath.normcase(str(path.resolve()).replace("\\", "/")).rstrip("/")


# R7 review P1#1: the episode's repair settings file must keep the SAME
# workspace base as the original settings. The generator resolves the base from
# the settings file's directory, with a special case ONLY for the immediate
# `.dependency-roadmap` folder. A repair settings file placed under
# `.../.dependency-roadmap/state/` shifts the base one level down, silently
# moving every relative path -- most critically the durable repair handoff the
# Desktop reads (`<base>/.dependency-roadmap/state/repair-requests.json`) and
# the baseline progress path (settings_base/.dependency-roadmap/state/
# baseline-verification-progress.json), so the re-verification would write its
# handoff somewhere the Desktop never reads and a green re-verify would not
# close the original requests.
class RepairSettingsWorkspaceBaseTests(unittest.TestCase):
    def make_workspace(self, root: Path, project_path: str) -> Path:
        ws = root / "ws"
        settings_dir = ws / ".dependency-roadmap"
        settings_dir.mkdir(parents=True)
        settings_dir.joinpath("settings.project.json").write_text(
            json.dumps({
                "schemaVersion": 1,
                "projects": [{"name": "Demo", "path": project_path, "git": {"sourceBranch": "master"}}],
            }),
            encoding="utf-8",
        )
        state_dir = ws / ".dependency-roadmap" / "state"
        state_dir.mkdir(parents=True)
        state_dir.joinpath("repair-requests.json").write_text(
            json.dumps({
                "schemaVersion": 1,
                "runId": "r1",
                "requests": [{
                    "requestId": "rq",
                    "project": "Demo",
                    "mode": "yellow",
                    "assignment": {"a": "1"},
                    "fingerprint": "fp1",
                    "snapshotIdentity": "s1",
                    "failingCommands": [{"command": "yarn lint", "exitCode": 1}],
                    "reason": "project-source-config-repair",
                }],
            }),
            encoding="utf-8",
        )
        return ws

    def test_repair_settings_next_to_original_keeps_base_and_shared_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self.make_workspace(root, str(root / "src"))
            original = ws / ".dependency-roadmap" / "settings.project.json"
            repair = ws / ".dependency-roadmap" / "repair-settings-Demo.json"
            # The production repair settings: same directory, project path remapped
            # to the isolated repair checkout (what buildRepairSettingsFile emits).
            repair.write_text(
                original.read_text(encoding="utf-8").replace(str(root / "src"), str(root / "repair-checkout")),
                encoding="utf-8",
            )

            base_original = roadmap.settings_workspace_base(original)
            base_repair = roadmap.settings_workspace_base(repair)
            self.assertEqual(norm(base_original), norm(ws))  # SAME base (P1#1)
            self.assertEqual(norm(base_repair), norm(base_original))

            # The verifier derives its progress path from the base (generator
            # L26469); the durable handoff sits next to that progress file. Both
            # settings must yield the SAME handoff -- the one the Desktop's
            # openRepairRequests reads under <ws>/.dependency-roadmap/state.
            handoff_original = (
                base_original / ".dependency-roadmap" / "state" / "baseline-verification-progress.json"
            ).parent / "repair-requests.json"
            handoff_repair = (
                base_repair / ".dependency-roadmap" / "state" / "baseline-verification-progress.json"
            ).parent / "repair-requests.json"
            self.assertEqual(norm(handoff_original), norm(handoff_repair))
            self.assertEqual(norm(handoff_repair), norm(ws / ".dependency-roadmap" / "state" / "repair-requests.json"))

            # And the Desktop's open handoff is exactly what the verifier writes.
            self.assertTrue(handoff_repair.exists())

    def test_repair_settings_under_state_dir_shifts_the_base(self) -> None:
        # Negative control (the R7 reproduction): placing the repair settings
        # under .../.dependency-roadmap/state/ changes the base and the handoff.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self.make_workspace(root, str(root / "src"))
            original = ws / ".dependency-roadmap" / "settings.project.json"
            broken = ws / ".dependency-roadmap" / "state" / "repair-settings-Demo.json"
            broken.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "projects": [{"name": "Demo", "path": str(root / "repair-checkout"), "git": {"sourceBranch": "master"}}],
                }),
                encoding="utf-8",
            )
            base_broken = roadmap.settings_workspace_base(broken)
            self.assertEqual(norm(base_broken), norm(ws / ".dependency-roadmap" / "state"))
            handoff_broken = (
                base_broken / ".dependency-roadmap" / "state" / "baseline-verification-progress.json"
            ).parent / "repair-requests.json"
            # The exact R7 reproduction: the verifier would write its durable
            # handoff one level deeper than the Desktop reads it.
            self.assertEqual(
                norm(handoff_broken),
                norm(ws / ".dependency-roadmap" / "state" / ".dependency-roadmap" / "state" / "repair-requests.json"),
            )
            self.assertNotEqual(
                norm(handoff_broken),
                norm(ws / ".dependency-roadmap" / "state" / "repair-requests.json"),
            )


if __name__ == "__main__":
    unittest.main()
