"""Real local Git/npm/Node delivery and cold-storage demos, increasing tree size.
No migrated user repository, paid agent, fake verifier or external registry used.
"""
import contextlib
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import iterative_delivery as d
import iterative_migration as core
from source_snapshot import capture_durable_source_snapshot, open_source_snapshot
import snapshot_compaction as cold
import storage_cleanup_inventory as storage


@unittest.skipUnless(shutil.which('git') and shutil.which('node') and (shutil.which('npm') or shutil.which('npm.cmd')), 'Git/Node/npm required')
class StorageDeliveryDemoTests(unittest.TestCase):
    def test_small_to_large_real_delivery_audit_and_lossless_cleanup(self):
        archives = {}
        for version in ['1.0.0','2.0.0']:
            data = io.BytesIO()
            with tarfile.open(fileobj=data,mode='w:gz') as tar:
                for name, content in {'package/package.json':json.dumps({'name':'deploom-demo-package','version':version}).encode(),
                                      'package/index.js':f'module.exports = {{ capability: () => "{version}" }}'.encode()}.items():
                    info=tarfile.TarInfo(name);info.size=len(content);tar.addfile(info,io.BytesIO(content))
            archives[version]=data.getvalue()
        class Registry(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def send(self,data):
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def do_GET(self):
                if self.path.startswith('/tar/'):
                    self.send(archives[self.path.split('/')[-1]]);return
                host=f'http://127.0.0.1:{self.server.server_port}'
                now=datetime.now(timezone.utc).isoformat()
                versions={v:{'name':'deploom-demo-package','version':v,'dist':{'tarball':host+'/tar/'+v,'shasum':hashlib.sha1(archives[v]).hexdigest()}} for v in archives}
                self.send(json.dumps({'name':'deploom-demo-package','dist-tags':{'latest':'2.0.0'},'versions':versions,'time':{v:now for v in versions}}).encode())
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                self.send(b'{}')
        server=ThreadingHTTPServer(('127.0.0.1',0),Registry)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        registry=f'http://127.0.0.1:{server.server_port}'
        npm=shutil.which('npm') or shutil.which('npm.cmd')
        for label, count, megabytes in [('small',10,1),('medium',200,8),('large',2000,64)]:
            with self.subTest(size=label), tempfile.TemporaryDirectory(prefix='deploom-size-demo-') as temp:
                base=Path(temp);repo=base/'original';repo.mkdir()
                workspace=base/'workspace';run_dir=workspace/'.dependency-roadmap/iterative/demo';run_dir.mkdir(parents=True)
                (repo/'.gitignore').write_text('node_modules/\n')
                (repo/'.gitattributes').write_text('*.txt text eol=lf\n')
                (repo/'.npmrc').write_text(f'registry={registry}\naudit=false\n')
                def install(version):
                    (repo/'package.json').write_text(json.dumps({'name':'deploom-size-demo','version':'1.0.0','private':True,'dependencies':{'deploom-demo-package':version}}))
                    result=subprocess.run([npm,'install','--ignore-scripts','--no-fund'],cwd=repo,capture_output=True,text=True,timeout=120)
                    self.assertEqual(result.returncode,0,result.stderr)
                install('1.0.0')
                (repo/'migration-mode.txt').write_text('1.0.0')
                (repo/'check.cjs').write_text("if(require('deploom-demo-package').capability()!==require('fs').readFileSync('migration-mode.txt','utf8'))process.exit(1)")
                data=repo/'data';data.mkdir()
                for index in range(count): (data/f'{index:05}.txt').write_bytes(b'unchanged fixture\r\n')
                (data/'large.bin').write_bytes(b'unchanged-binary-'*(megabytes*1024*1024//17))
                d.git(repo,'init','-b','demo-base');d.git(repo,'config','user.name','Demo');d.git(repo,'config','user.email','demo@example.invalid')
                d.git(repo,'add','.');d.git(repo,'commit','-m','initial demo');head=d.git(repo,'rev-parse','HEAD')
                targets=base/'targets.json';targets.write_text(json.dumps({'deploom-demo-package':'2.0.0'}))
                verification=base/'verify.json';verification.write_text(json.dumps({'commands':['node check.cjs'],'projectChecks':'adaptive','registry':registry}))
                def cli(*args, allowed=(0,)):
                    result=subprocess.run([sys.executable,'-X','utf8',str(Path(core.__file__)), '--run-dir',str(run_dir), *args],
                                          env={**os.environ,'npm_config_registry':registry},capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=600)
                    self.assertIn(result.returncode,allowed, f'{args}: {result.stdout[-5000:]} {result.stderr[-2000:]}')
                    print(f'DEMO {label}: {args[0]} exit={result.returncode}',flush=True)
                    return result
                started=time.monotonic()
                cli('begin','--project-dir',str(repo),'--project-name','Demo','--targets-file',str(targets),'--verify-config',str(verification),'--run-budget-minutes','15')
                self.assertEqual(core.load_checkpoint(run_dir,'C0')['status'],'VERIFIED')
                initial_checkpoint=core.load_checkpoint(run_dir,'C0');initial=Path(initial_checkpoint['sourceSnapshotContainer'])
                cli('plan-next');cli('materialize',allowed=(0,2,4));cli('precheck',allowed=(0,2,4))
                candidate=core.load_candidate(run_dir)
                trial=Path(candidate['materializationRefs']['workspaceRoot'])/candidate['materializationRefs']['projectRelative']
                (trial/'migration-mode.txt').write_text('2.0.0')
                feedback=base/'feedback.json'
                feedback.write_text(json.dumps({'schemaVersion':1,'runId':candidate['runId'],'candidateId':candidate['candidateId'],
                    'baseCheckpointId':candidate['baseCheckpointId'],'attemptId':candidate['attemptId'],'kind':'READY_FOR_VERIFY',
                    'changedFiles':['migration-mode.txt'],'reason':'adapt capability','diagnosticsRefs':[],'proposedScope':{},'proposedConstraints':{}}))
                cli('apply-feedback','--feedback-file',str(feedback));cli('verify-exact')
                self.assertEqual(core.load_run(run_dir)['activeCheckpointId'],'C1')
                self.assertTrue(cold.has_packed_source(initial))
                cli('audit');cli('finish')
                self.assertEqual(core.load_run(run_dir)['terminal'],'COMPLETE')
                latest=Path(core.load_checkpoint(run_dir,'C1')['sourceSnapshotContainer'])
                state=d.delivery_prepare(run_dir,{'branch':'codex/demo-'+label});root=Path(state['workspaceRoot'])
                d.git(root,'add','package.json','package-lock.json');d.git(root,'commit','-m','deps: upgrade demo package')
                d.git(root,'add','migration-mode.txt');d.git(root,'commit','-m','refactor: adapt capability contract')
                d.git(root,'add','docs');d.git(root,'commit','-m','docs: developer guide and migration report')
                with contextlib.redirect_stdout(io.StringIO()):
                    delivered=d.delivery_verify(run_dir,{})
                    scan_records = []
                    original_scan = storage.scan
                    def record_scan(path, *args, **kwargs):
                        records = []
                        class RecordedDigest:
                            def __init__(self): self.digest = hashlib.sha256()
                            def update(self, value):
                                records.append(value.decode()); self.digest.update(value)
                            def hexdigest(self): return self.digest.hexdigest()
                        with patch.object(storage, 'hashlib', SimpleNamespace(sha256=RecordedDigest)):
                            result = original_scan(path, *args, **kwargs)
                        scan_records.append((str(path), records, result['signature']))
                        return result
                    with patch.object(storage, 'scan', record_scan):
                        plan=storage.inspect(None,[str(workspace)]);cleaned=storage.clean(None,[str(workspace)],plan)
                self.assertEqual(delivered['status'],'done')
                self.assertEqual(delivered['audit']['status'],'PASS',delivered['audit'])
                self.assertEqual(core.load_run(run_dir)['terminal'],'COMPLETE')
                if cleaned['removed'] < 1:
                    import materialization_retention as retention
                    evidence = {'cleanup':cleaned, 'sourceValidation':[]}
                    failed_path = next((r['path'] for r in cleaned['results'] if r.get('reason') == 'changed-after-preview'), str(trial))
                    evidence['scanPaths'] = [path for path, _, _ in scan_records]
                    evidence['trialPath'] = str(trial)
                    trial_scans = [(records, digest) for path, records, digest in scan_records if path == failed_path]
                    evidence['scanSignatures'] = [digest for _, digest in trial_scans]
                    if len(trial_scans) >= 2:
                        before, after = trial_scans[0][0], trial_scans[-1][0]
                        evidence['scanRemoved'] = sorted(set(before) - set(after))[:10]
                        evidence['scanAdded'] = sorted(set(after) - set(before))[:10]
                        evidence['scanCounts'] = [len(before), len(after)]
                        evidence['scanFirstMismatch'] = next(([a,b] for a,b in zip(before,after) if a != b), None)
                    for cp, source, manifest in retention.preserved_checkpoints(run_dir):
                        actual_files, actual_dirs = retention.fingerprint(trial)
                        expected_files, expected_dirs = retention.manifest_fingerprint(manifest)
                        diff = [name for name in set(actual_files) | set(expected_files) if actual_files.get(name) != expected_files.get(name)]
                        record = {'checkpoint':cp['checkpointId'], 'filesDiffer':sorted(diff)[:10], 'dirsDiffer':sorted(actual_dirs ^ expected_dirs)[:10]}
                        try: retention.validate_source(source,cp['sourceSnapshotKey']); record['validation']='passed'
                        except Exception as exc: record['validation']=str(exc)
                        evidence['sourceValidation'].append(record)
                    print('DEMO cleanup failure evidence: '+json.dumps(evidence),flush=True)
                self.assertGreaterEqual(cleaned['removed'],1, json.dumps(cleaned))
                self.assertGreater(cleaned['reclaimedBytes'],megabytes*1024*1024//2)
                self.assertFalse((initial/'tree').exists());self.assertTrue((latest/'tree').exists());self.assertTrue(root.exists())
                self.assertEqual(open_source_snapshot(initial,expected_key=initial_checkpoint['sourceSnapshotKey']).key,initial_checkpoint['sourceSnapshotKey'])
                self.assertEqual(d.git(repo,'rev-parse','HEAD'),head)
                print(f'DEMO {label}: files={count}, payloadMiB={megabytes}, delivery=done, audit=PASS, terminal=COMPLETE, reclaimedBytes={cleaned["reclaimedBytes"]}, seconds={time.monotonic()-started:.1f}')
