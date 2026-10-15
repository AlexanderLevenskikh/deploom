"""Real Windows Git read-only objects must not strand the next private trial."""
import ctypes
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from iterative_trial_workspace import remove_trial_workspace, TrialWorkspaceCleanupError


class IterativeTrialCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        self.trial = self.run / 'trial' / 'workspace'
        self.trial.mkdir(parents=True)
        self.checkpoint = self.run / 'checkpoints' / 'C1.json'
        self.checkpoint.parent.mkdir()
        self.checkpoint.write_bytes(b'verified checkpoint preserved')

    def test_readonly_git_objects_are_removed_before_fresh_copy(self):
        original = self.root / 'original'; original.mkdir()
        (original / 'keep').write_bytes(b'original source')
        obj = self.trial / '.git' / 'objects' / 'ab' / 'cdef'
        obj.parent.mkdir(parents=True); obj.write_bytes(b'git object')
        obj.chmod(stat.S_IREAD)
        remove_trial_workspace(self.run)
        self.assertFalse(self.trial.exists())
        self.assertEqual(b'verified checkpoint preserved', self.checkpoint.read_bytes())
        self.assertEqual(b'original source', (original / 'keep').read_bytes())
        self.trial.mkdir(); (self.trial / 'package.json').write_text('{}')
        remove_trial_workspace(self.run)
        remove_trial_workspace(self.run)

    def test_root_link_is_rejected_without_touching_external_data(self):
        external = self.root / 'external'; external.mkdir(); (external / 'keep').write_bytes(b'keep')
        self.trial.rmdir()
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(self.trial), str(external)], check=True, capture_output=True)
        else:
            self.trial.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(TrialWorkspaceCleanupError, 'reparse/outside-owned-root'):
            remove_trial_workspace(self.run)
        self.assertEqual(b'keep', (external / 'keep').read_bytes())

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing and readonly semantics')
    def test_locked_file_preserves_specific_error_and_checkpoint(self):
        locked = self.trial / 'locked'; locked.write_bytes(b'busy')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateFileW(str(locked), 0x80000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            with self.assertRaisesRegex(TrialWorkspaceCleanupError, 'CLEANUP_BLOCKED.*locked') as caught:
                remove_trial_workspace(self.run)
            self.assertIsInstance(caught.exception.__cause__, PermissionError)
            self.assertEqual(b'verified checkpoint preserved', self.checkpoint.read_bytes())
        finally:
            kernel.CloseHandle(handle)
        remove_trial_workspace(self.run)
        self.assertFalse(self.trial.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows readonly unlink semantics')
    def test_shared_readonly_file_is_not_unprotected_through_a_hardlink(self):
        original = self.root / 'original-object'; original.write_bytes(b'original')
        os.link(original, self.trial / 'alias')
        original.chmod(stat.S_IREAD)
        try:
            with self.assertRaises(TrialWorkspaceCleanupError):
                remove_trial_workspace(self.run)
            self.assertFalse(original.stat().st_mode & stat.S_IWRITE)
            self.assertEqual(b'original', original.read_bytes())
        finally:
            original.chmod(stat.S_IWRITE | stat.S_IREAD)
