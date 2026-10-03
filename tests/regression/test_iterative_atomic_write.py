import ctypes
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import iterative_migration as migration


class IterativeAtomicWriteTests(unittest.TestCase):
    def test_transient_windows_denial_retries_without_deleting_original(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            target.write_text('{"old":true}', encoding="utf-8")
            original = os.replace
            calls = []
            def replace(source, dest):
                calls.append(source)
                if len(calls) < 3:
                    self.assertEqual(target.read_text(), '{"old":true}')
                    error = PermissionError("sharing violation")
                    error.winerror = 5
                    raise error
                return original(source, dest)
            with patch.object(migration.os, "replace", side_effect=replace), patch.object(migration.time, "sleep"):
                migration._write_json_atomic(target, {"new": True})
            self.assertEqual(len(calls), 3)
            self.assertIn('"new":true', target.read_text())
            self.assertEqual(list(Path(directory).glob("*.tmp-*")), [])

    def test_permanent_failure_keeps_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            target.write_text("old", encoding="utf-8")
            with patch.object(migration.os, "replace", side_effect=PermissionError("ACL denied")) as replace:
                with self.assertRaises(PermissionError): migration._write_json_atomic(target, {})
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(target.read_text(), "old")
            self.assertEqual(list(Path(directory).glob("*.tmp-*")), [])

    @unittest.skipUnless(os.name == "nt", "Windows sharing boundary")
    def test_real_windows_reader_releases_replacement_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            target.write_text("old", encoding="utf-8")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.restype = ctypes.c_void_p
            kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.CreateFileW(str(target), 0x80000000, 1, None, 3, 0, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            timer = threading.Timer(0.1, lambda: kernel.CloseHandle(handle))
            timer.start()
            try: migration._write_json_atomic(target, {"new": True})
            finally: timer.join()
            self.assertIn('"new":true', target.read_text())
