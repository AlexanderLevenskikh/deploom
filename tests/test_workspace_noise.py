from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workspace_noise import is_ignorable_workspace_path, relevant_porcelain_entries


class WorkspaceNoiseTests(unittest.TestCase):
    def test_known_editor_and_os_paths_are_ignored_anywhere(self) -> None:
        for path in (
            '.idea/workspace.xml', 'src/.vs/cache.bin', '.vscode/settings.json',
            'foo/.fleet/state.json', 'x/.history/file.ts', 'src/file.ts.swp',
            'project.suo', 'dir/.DS_Store', 'Thumbs.db', 'desktop.ini', 'file.ts~',
        ):
            self.assertTrue(is_ignorable_workspace_path(path), path)
        self.assertFalse(is_ignorable_workspace_path('src/App.tsx'))

    def test_filter_ignores_tracked_and_untracked_ide_noise(self) -> None:
        status = '?? .idea/workspace.xml\n M .vscode/settings.json\nM  src/App.tsx\n?? notes.txt\n'
        self.assertEqual(
            ['M  src/App.tsx', '?? notes.txt'],
            relevant_porcelain_entries(status),
        )

    def test_z_porcelain_filter(self) -> None:
        status = '?? .idea/workspace.xml\0 M src/App.tsx\0?? .vs/cache.bin\0'
        self.assertEqual([' M src/App.tsx'], relevant_porcelain_entries(status, nul=True))


if __name__ == '__main__':
    unittest.main()
