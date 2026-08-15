from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DesktopCheckInventoryRegression(unittest.TestCase):
    def test_every_desktop_check_script_is_wired_to_npm_and_ci(self) -> None:
        scripts = sorted(path.stem.removeprefix("check-") for path in (ROOT / "desktop" / "scripts").glob("check-*.mjs"))
        package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        npm_scripts = package.get("scripts", {})
        ci_sources = [
            (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        ]
        ci = "\n".join(ci_sources)
        missing_npm = [name for name in scripts if f"check:{name}" not in npm_scripts]
        missing_ci = [name for name in scripts if f"npm run check:{name}" not in ci]
        self.assertEqual([], missing_npm, f"Desktop check scripts missing npm command: {missing_npm}")
        self.assertEqual([], missing_ci, f"Desktop checks missing CI invocation: {missing_ci}")
