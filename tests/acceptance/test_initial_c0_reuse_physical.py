"""Real begin/C0 boundary: one capture, private npm check, durable VERIFIED evidence."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

@unittest.skipUnless(shutil.which('git') and shutil.which('npm') and shutil.which('node'), 'Git/npm/Node required')
class InitialC0ReusePhysicalTests(unittest.TestCase):
    def test_begin_checks_the_captured_subject_without_second_capture(self):
        with tempfile.TemporaryDirectory(prefix='deploom-c0-physical-') as raw:
            root = Path(raw) / 'app'; root.mkdir()
            (root / 'package.json').write_text(json.dumps({'name':'c0-acceptance','version':'1.0.0','scripts':{'test':'node check.cjs'}}))
            (root / 'check.cjs').write_text("const fs=require('fs'); if(!fs.existsSync('.env'))process.exit(2); if(fs.existsSync('.claude'))process.exit(3); console.log('physical C0 check passed');")
            (root / '.env').write_text('FIXTURE=1')
            (root / '.claude').mkdir(); (root / '.claude/state.json').write_text('{}')
            for args in [('init','-q'),('config','user.name','Fixture'),('config','user.email','fixture@example.invalid'),('add','.'),('commit','-qm','fixture')]:
                subprocess.run(['git','-C',str(root),*args], check=True, capture_output=True)
            subprocess.run([shutil.which('npm'),'install','--package-lock-only','--ignore-scripts','--offline'], cwd=root, check=True, capture_output=True, timeout=60)
            config = Path(raw) / 'verify.json'; config.write_text(json.dumps({'commands':['npm run test']}))
            result = subprocess.run([sys.executable,str(ROOT / 'iterative_migration.py'),'--run-dir',str(Path(raw) / 'run'),'begin','--project-dir',str(root),'--repair-only','--verify-config',str(config)], cwd=ROOT, capture_output=True, text=True, encoding='utf8', timeout=240)
            self.assertEqual(0, result.returncode, result.stderr[-2000:] + result.stdout[-2000:])
            checkpoint = json.loads((Path(raw) / 'run/checkpoints/C0.json').read_text(encoding='utf8'))
            self.assertEqual('VERIFIED', checkpoint['status'])
            container = Path(checkpoint['sourceSnapshotContainer'])
            manifest = json.loads((container / 'manifest.json').read_text(encoding='utf8'))
            actual = {p.relative_to(container / 'tree').as_posix() for p in (container / 'tree').rglob('*')}
            self.assertEqual({entry['path'] for entry in manifest['entries']}, actual)
            events = [json.loads(line.split(' ',1)[1]) for line in result.stdout.splitlines() if line.startswith('ITERATIVE_MIGRATION_STATUS_V1 ')]
            attempts = [e for e in events if 'SourceSnapshot capture attempt' in e.get('message','') and not e.get('heartbeat')]
            self.assertEqual(1, len(attempts))
            self.assertTrue(any(e.get('operation') == 'source-capture' for e in events))
            self.assertTrue((root / '.claude/state.json').is_file())
            self.assertFalse((root / 'node_modules').exists(), 'Verification must remain private')

if __name__ == '__main__': unittest.main()
