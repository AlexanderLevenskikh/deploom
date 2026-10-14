"""Collect every core demo as a filesystem contract and retain failures."""
from pathlib import Path
import shutil
import tempfile
import unittest
from examples.real_world_workspace_demos import demos,run_demo


@unittest.skipUnless(shutil.which('git'),'Git required')
class RealWorldWorkspaceDemoTests(unittest.TestCase):
    def test_all_core_scenarios(self):
        with tempfile.TemporaryDirectory() as temporary:
            for item in demos():
                if item.group=='physical':continue
                with self.subTest(case=item.name):
                    result=run_demo(item,Path(temporary)/item.name)
                    self.assertEqual(result['status'],'PASS',result.get('error'))

    @unittest.skipUnless(shutil.which('npm') and shutil.which('node'),'npm/Node required')
    def test_real_npm_root_child_and_failed_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            for item in demos():
                if item.group!='physical':continue
                with self.subTest(case=item.name):
                    result=run_demo(item,Path(temporary)/item.name)
                    self.assertEqual(result['status'],'PASS',result.get('error'))
