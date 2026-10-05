"""Local fixture: exact missing-peer declaration, real npm install and verifier.

No external package registry, agent or real migrated project is used.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import shutil
import subprocess
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from baseline_constraint_verifier import (BaselineVerifyConfig, _apply_assignment, _run,
    _validate_assignment_materialization, observed_resolved_assignment, verify_assignment)
from iterative_migration import _run_install
from cohort_attempt_telemetry import verification_stages, event_offset
from baseline_constraint_verifier import assignment_fingerprint
from iterative_scope_expansion import apply_expansion_assignment


def package_archive(manifest, source):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        contents={'package.json':json.dumps(manifest),'index.js':source}
        if manifest.get('types'):contents['index.d.ts']='export declare const answer: number;'
        for name, content in contents.items():
            data = content.encode(); entry = tarfile.TarInfo('package/' + name)
            entry.size = len(data); archive.addfile(entry, io.BytesIO(data))
    return buffer.getvalue()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',choices=('peer-delay','runtime-tests','typed-build','opaque-repair'),default='peer-delay')
    args=parser.parse_args()
    manifests = {
        'demo-origin': {'name': 'demo-origin', 'version': '2.0.0', 'main': 'index.js',
                        'types':'index.d.ts','peerDependencies': {'demo-companion': '^1.0.0'}},
        'demo-companion': {'name': 'demo-companion', 'version': '1.0.0', 'main': 'index.js','types':'index.d.ts'},
    }
    archives = {name: package_archive(manifest,
        "module.exports = require('demo-companion');" if name == 'demo-origin' else 'module.exports = {answer:42};')
        for name, manifest in manifests.items()}

    class Registry(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            name = self.path.strip('/').split('/')[0]
            if name not in manifests:
                self.send_error(404); return
            if self.path.endswith('.tgz'):
                body = archives[name]; content_type = 'application/octet-stream'
            else:
                manifest = manifests[name]
                version = {**manifest, 'dist': {
                    'tarball': f'http://127.0.0.1:{self.server.server_port}/{name}/-/{name}.tgz',
                    'shasum': hashlib.sha1(archives[name]).hexdigest()}}
                body = json.dumps({'name':name,'dist-tags':{'latest':manifest['version']},
                                   'versions':{manifest['version']:version}}).encode()
                content_type = 'application/json'
            self.send_response(200); self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Registry)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    project = ROOT / '.dependency-roadmap' / 'qa' / ('peer-install-' + args.profile + '-' + time.strftime('%Y%m%d-%H%M%S'))
    project.mkdir(parents=True, exist_ok=False)
    registry = f'http://127.0.0.1:{server.server_port}'
    try:
        (project/'package.json').write_text(json.dumps({'name':'isolated-peer-install-demo','private':True,
            'dependencies':{'demo-origin':'1.0.0'},'scripts':{'test':'node check.cjs'}}), encoding='utf-8')
        (project/'.npmrc').write_text(f'registry={registry}\naudit=false\nfund=false\n', encoding='utf-8')
        (project/'check.cjs').write_text("const assert=require('node:assert/strict'); assert.equal(require('demo-origin').answer,42); setTimeout(()=>console.log('peer runtime check passed'),5000);", encoding='utf-8')
        commands=('node check.cjs',)
        valid_source="import {answer} from 'demo-origin'; export function result(): number { return answer*2; }\n"
        if args.profile in ('typed-build','opaque-repair'):
            compiler=ROOT/'desktop'/'node_modules'/'typescript'/'bin'/'tsc'
            if not compiler.exists():raise RuntimeError('Desktop TypeScript dependency is required for this owned fixture')
            (project/'src').mkdir()
            (project/'src'/'app.ts').write_text(valid_source if args.profile=='typed-build' else "import {answer} from 'demo-origin'; export const result: string = answer*2;\n",encoding='utf-8')
            (project/'tsconfig.json').write_text(json.dumps({'compilerOptions':{'strict':True,'module':'commonjs','target':'ES2022','outDir':'dist','rootDir':'src'},'files':['src/app.ts']}),encoding='utf-8')
            (project/'check.cjs').write_text("require('node:child_process').execFileSync(process.execPath,"+json.dumps([compiler.as_posix()])+",{stdio:'inherit'}); const assert=require('node:assert/strict'); assert.equal(require('./dist/app.js').result(),84); setTimeout(()=>console.log('compiled runtime passed'),5000);",encoding='utf-8')
            for script,flags in (('typecheck.cjs',['--noEmit']),('build.cjs',[])):
                (project/script).write_text("require('node:child_process').execFileSync(process.execPath,"+json.dumps([compiler.as_posix(),*flags])+",{stdio:'inherit'});",encoding='utf-8')
            commands=('node typecheck.cjs','node build.cjs','node check.cjs')
        elif args.profile=='runtime-tests':
            (project/'runtime.test.cjs').write_text("const {test}=require('node:test'); const assert=require('node:assert/strict'); test('installed peer API',async()=>{assert.equal(require('demo-origin').answer,42); await new Promise(r=>setTimeout(r,5000));});",encoding='utf-8')
            commands=('node --test runtime.test.cjs',)
        (project/'.gitignore').write_text('node_modules/\ndist/\nproof-cache/\nverification-telemetry.jsonl*\nRESULT.json\n',encoding='utf-8')
        assignment = {'demo-origin':'2.0.0','demo-companion':'1.0.0'}
        changed = apply_expansion_assignment(project, assignment, {'demo-companion':'1.0.0'},
                                            _apply_assignment, _validate_assignment_materialization)
        # Native npm creates the fixture's canonical lockfile; topology guards
        # stay enabled for the production install and verifier.
        lock = _run([shutil.which('npm'), 'install', '--package-lock-only', '--ignore-scripts'], project, timeout_seconds=90)
        if lock.returncode: raise RuntimeError(lock.stdout + lock.stderr)
        for argv in (['git','init'], ['git','add','.'],
                     ['git','-c','user.name=DepLoom fixture','-c','user.email=fixture@example.invalid','commit','-m','Local fixture']):
            child = subprocess.run(argv, cwd=project, capture_output=True, text=True)
            if child.returncode: raise RuntimeError(child.stderr)
        install_started=time.monotonic()
        installed = _run_install(project, timeout_seconds=90, progress_label='isolated companion fixture')
        if installed.returncode: raise RuntimeError(installed.stdout + installed.stderr)
        install_seconds=time.monotonic()-install_started
        observed = observed_resolved_assignment(project, assignment)
        if observed != assignment: raise RuntimeError(f'Installed assignment drift: {observed}')
        started = time.monotonic()
        telemetry=project.parent/(project.name+'-verification-telemetry.jsonl')
        config=BaselineVerifyConfig(project_checks='strict', commands=commands,
                registry=registry, timeout_seconds=90, attempt_timeout_seconds=180,
                verification_purpose='intermediate-candidate', proof_cache_dir=str(project/'proof-cache'),telemetry_path=str(telemetry))
        offset=event_offset(telemetry)
        first=verify_assignment(project, assignment,config=config,run_project_checks=True)
        first_stages=verification_stages(telemetry,offset,assignment_fingerprint(assignment))
        repair_seconds=None
        result=first
        if args.profile=='opaque-repair':
            if first.ok or first.kind!='project':raise RuntimeError(f'Expected physical typecheck rejection: {first.kind}: {first.summary}')
            repair_started=time.monotonic()
            (project/'src'/'app.ts').write_text(valid_source,encoding='utf-8')
            repair_seconds=time.monotonic()-repair_started
            offset=event_offset(telemetry)
            result=verify_assignment(project,assignment,config=config,run_project_checks=True)
        stages=verification_stages(telemetry,offset,assignment_fingerprint(assignment))
        if not result.ok: raise RuntimeError(f'{result.kind}: {result.summary}')
        evidence = {'scope':'owned-production-like-real-install-and-verification','profile':args.profile,
                    'installSeconds':install_seconds,'stages':stages,'firstStages':first_stages,
                    'firstVerdict':{'ok':first.ok,'kind':first.kind},'scriptedRepairSeconds':repair_seconds,'agentSeconds':None, 'changed':changed,
                    'observed':observed,'verified':result.ok,'kind':result.kind,
                    'checkCommands':commands,'checkDelaySeconds':5,
                    'verificationSeconds':round(time.monotonic()-started,3),
                    'resolvedStateKey':result.resolved_state_key,
                    'preparationProofKey':result.preparation_proof_key}
        (project/'RESULT.json').write_text(json.dumps(evidence,indent=2)+'\n', encoding='utf-8')
        print(json.dumps(evidence)); print('RESULT ' + str(project/'RESULT.json'))
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)

if __name__ == '__main__': main()
