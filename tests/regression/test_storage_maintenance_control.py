"""Real subprocess/FS boundaries for cooperative storage pause and cancellation."""
import contextlib
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import storage_cleanup_inventory as storage
from storage_maintenance_control import StorageCancelled, checkpoint, maintenance_control


class StorageMaintenanceControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='deploom-storage-controls-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'verification'
        (self.root / 'trials').mkdir(parents=True)
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        self.protected = self.workspace / 'source.txt'
        self.protected.write_text('preserved original')
        old = time.time() - 3 * storage.MIN_AGE_SECONDS
        stamp = int(old * 10**9)
        self.trash = self.root / 'trials' / f'.deploom-trial-trash-dependency-flow-baseline-verify-controls-999999-{stamp}'
        self.trash.mkdir()
        for index in range(12): (self.trash / f'{index}.txt').write_text('garbage')
        os.utime(self.trash, (old, old))
        with contextlib.redirect_stdout(io.StringIO()):
            self.plan = storage.inspect(self.root, [str(self.workspace)])
        self.control = self.base / 'operation.json'
        self.payload = {'workspaces': [str(self.workspace)], 'plan': self.plan, 'controlFile': str(self.control), 'operationId': 'fixture-operation', 'ownerPid': os.getpid(), 'ownerNonce': 'fixture-nonce'}

    def mode(self, mode):
        temporary = self.control.with_suffix('.tmp')
        temporary.write_text(json.dumps({'operationId': 'fixture-operation', 'ownerNonce': 'fixture-nonce', 'state': mode}))
        temporary.replace(self.control)

    def child(self):
        child = subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(storage.__file__)), 'clean'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            env={**os.environ, 'DEPLOOM_VERIFICATION_ROOT': str(self.root)})
        def cleanup():
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None: stream.close()
        self.addCleanup(cleanup)
        events = queue.Queue()
        lines = []
        def read():
            for line in child.stdout:
                lines.append(line)
                events.put(line)
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        child.stdin.write(json.dumps(self.payload)); child.stdin.close()
        return child, thread, events, lines

    def paused(self, events):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            line = events.get(timeout=max(0.1, deadline-time.monotonic()))
            if line.startswith('STORAGE_CONTROL_V1 '):
                event = json.loads(line.split(' ', 1)[1])
                if event['state'] == 'paused': return
        self.fail('Python did not acknowledge the safe pause')

    def finish(self, child, thread, lines):
        self.assertEqual(child.wait(timeout=20), 0, child.stderr.read() if child.poll() is not None else '')
        thread.join(timeout=2)
        results = [json.loads(line.split(' ', 1)[1]) for line in lines if line.startswith('STORAGE_RESULT_V1 ')]
        self.assertEqual(len(results), 1)
        return results[0]

    def test_real_cli_pauses_before_mutation_and_resumes_same_operation(self):
        self.mode('pause')
        child, thread, events, lines = self.child()
        self.paused(events)
        self.assertTrue(all((self.trash / f'{index}.txt').is_file() for index in range(12)))
        self.assertIsNone(child.poll())
        self.mode('run')
        result = self.finish(child, thread, lines)
        self.assertFalse(result.get('canceled', False))
        self.assertEqual(result['removed'], 1)
        self.assertFalse(self.trash.exists())
        self.assertEqual(self.protected.read_text(), 'preserved original')
        self.assertTrue(any('"state": "running"' in line for line in lines))

    def test_real_cli_cancel_while_paused_preserves_source_and_unblocks_process(self):
        self.mode('pause')
        child, thread, events, lines = self.child()
        self.paused(events)
        self.mode('cancel')
        result = self.finish(child, thread, lines)
        self.assertTrue(result['canceled'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['removed'], 0)
        self.assertEqual(len(list(self.trash.iterdir())), 12)
        self.assertEqual(self.protected.read_text(), 'preserved original')
        self.assertFalse(any('"percent": 100' in line for line in lines))

    def test_cancel_after_one_delete_keeps_partial_result_and_releases_locks(self):
        self.mode('run')
        original = storage.progress
        def stop_after_first(stage, *args, **kwargs):
            if stage == 'clean' and kwargs.get('processed') == 1:
                self.mode('cancel')
                time.sleep(0.06)
            return original(stage, *args, **kwargs)
        with contextlib.redirect_stdout(io.StringIO()), maintenance_control(self.payload), patch.object(storage, 'progress', side_effect=stop_after_first):
            result = storage.clean(self.root, [str(self.workspace)], self.plan)
        self.assertTrue(result['canceled'])
        self.assertEqual(result['failed'], 0, 'cancellation is not an item failure')
        self.assertEqual(result['removed'], 0)
        self.assertEqual(len(list(self.trash.iterdir())), 11)
        self.assertEqual(self.protected.read_text(), 'preserved original')

    def test_unknown_control_owner_stops_without_mutation(self):
        self.mode('run')
        changed = json.loads(self.control.read_text())
        changed['ownerNonce'] = 'foreign'
        self.control.write_text(json.dumps(changed))
        with self.assertRaises(StorageCancelled):
            with maintenance_control(self.payload): checkpoint(force=True)
        self.assertEqual(len(list(self.trash.iterdir())), 12)

    def test_cancel_during_snapshot_packing_keeps_a_verifiable_source(self):
        from source_snapshot import capture_durable_source_snapshot, open_source_snapshot
        from snapshot_compaction import compact
        source = self.base / 'original'; source.mkdir()
        (source / 'large.bin').write_bytes(b'preserved bytes' * 20000)
        container = self.workspace / '.dependency-roadmap' / 'iterative' / 'demo' / 'sources' / 'C0'
        snapshot = capture_durable_source_snapshot(source, container, timeout_seconds=0)
        self.mode('run')
        def cancel(_path):
            self.mode('cancel')
            checkpoint(force=True)
        with self.assertRaises(StorageCancelled), contextlib.redirect_stdout(io.StringIO()), maintenance_control(self.payload):
            compact(container, progress=cancel)
        self.assertTrue((container / 'tree' / 'large.bin').is_file())
        reopened = open_source_snapshot(container, expected_key=snapshot.key, allow_build_upgrade=True)
        self.assertEqual((reopened.project_path / 'large.bin').read_bytes(), (source / 'large.bin').read_bytes())
