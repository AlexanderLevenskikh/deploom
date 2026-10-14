"""Retained filesystem demos of manifest, workspace and installation boundaries.

These are executable scenario contracts, not a project-green migration verdict.
Only physical-* cases run npm. All package bytes come from a loopback registry.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from baseline_constraint_verifier import (
    _apply_assignment, AssignmentMaterializationError, observed_resolved_assignment,
    ObservedResolutionError, discover_baseline_project_checks)
from iterative_stage_checkpoint import observe_materialized_assignment
from iterative_resume import reopen_terminal_run
from project_topology import resolve_project_topology, ProjectTopologyError


@dataclass(frozen=True)
class Demo:
    name: str
    title: str
    group: str
    run: object


def require(value, explanation):
    if not value:
        raise AssertionError(explanation)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def command(argv, cwd, *, env=None):
    executable = shutil.which(argv[0])
    require(executable, f"Required runtime is unavailable: {argv[0]}")
    args = [executable, *argv[1:]]
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        args = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", subprocess.list2cmdline(args)]
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


def repo(base, *, manager="npm", workspaces=None):
    root = base / "repo"
    root.mkdir(parents=True)
    manifest = {"name":"demo-root", "private":True, "version":"1.0.0",
                "dependencies":{"demo-lib":"1.0.0"}}
    if workspaces is not None:
        manifest["workspaces"] = workspaces
    manifest["packageManager"] = {"npm":"npm@11.0.0", "yarn1":"yarn@1.22.22",
                                  "yarn4":"yarn@4.0.0", "pnpm":"pnpm@9.0.0"}[manager]
    write(root / "package.json", manifest)
    if manager == "npm":
        write(root / "package-lock.json", {"name":"demo-root", "lockfileVersion":3,
                                           "packages":{"":manifest}})
    elif manager.startswith("yarn"):
        (root / "yarn.lock").write_text("# yarn lockfile v1\n" if manager == "yarn1" else "__metadata:\n  version: 8\n", encoding="utf-8")
        if manager == "yarn4":
            (root / ".yarnrc.yml").write_text("nodeLinker: node-modules\n", encoding="utf-8")
    else:
        (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n", encoding="utf-8")
    result = command(["git","init","-q"],root)
    require(result.returncode == 0,"git fixture initialization failed")
    return root.resolve()


def app(root, relative="packages/app", *, spec="1.0.0"):
    project = root / relative
    write(project / "package.json", {"name":"demo-"+relative.replace("/","-"), "version":"1.0.0",
                                     "private":True,"dependencies":{"demo-lib":spec}})
    return project


def reject(call, error, text, *, unchanged=None):
    before = unchanged.read_bytes() if unchanged else None
    try:
        call()
    except error as exc:
        require(text in str(exc), f"Expected {text}, observed {exc}")
        if unchanged:
            require(unchanged.read_bytes() == before,"Rejected operation changed the manifest")
        return type(exc).__name__ + ": " + text
    raise AssertionError("Operation unexpectedly succeeded: " + text)


def topology_demo(base, *, manager="npm", selection="child", shape="array", independent=False):
    patterns = ["packages/*"] if shape != "brace" else ["packages/{app,sibling}"]
    workspaces = {"packages":patterns} if shape == "object" else patterns
    root = repo(base,manager=manager,workspaces=workspaces)
    child = app(root); sibling = app(root,"packages/sibling")
    if independent:
        write(child / "package-lock.json", {"lockfileVersion":3,"packages":{"":read(child/'package.json')}})
    selected = root if selection == "root" else child
    expected_manager = child if independent and selected == child else root
    topology = resolve_project_topology(selected,allow_discovery=False,require_supported=True)
    require(topology.package_root == selected.resolve(),"wrong selected manifest")
    require(topology.package_manager_root == expected_manager.resolve(),"wrong package-manager owner")
    paths = [root/'package.json',child/'package.json',sibling/'package.json',topology.lockfile]
    before = {str(path.relative_to(root)):path.read_bytes() for path in paths}
    _apply_assignment(selected,{'demo-lib':'2.0.0'})
    changed = [str(path.relative_to(root)) for path in paths if path.read_bytes()!=before[str(path.relative_to(root))]]
    require(changed == [str((selected/'package.json').relative_to(root))],"manifest update escaped selected package")
    require(read(sibling/'package.json')['dependencies']['demo-lib']=='1.0.0',"sibling mutated")
    return {'selectedManifest':str((selected/'package.json').relative_to(root)),
            'managerRoot':str(expected_manager.relative_to(root)) or '.',
            'lockfile':str(topology.lockfile.relative_to(root)), 'changedFiles':changed,
            'scope':'topology + manifest mutation; install has not been run',
            'beforeVersions':{'root':'1.0.0','child':'1.0.0'},
            'afterVersions':{'root':read(root/'package.json')['dependencies']['demo-lib'],
                             'child':read(child/'package.json')['dependencies']['demo-lib']}}


def format_demo(base, *, style):
    root = repo(base)
    manifest = read(root/'package.json')
    manifest['description']='Пример café'; manifest['custom']={'preserve':True}
    raw = json.dumps(manifest,ensure_ascii=False,indent=None if style=='minified' else 2)
    encoding = 'utf-8-sig' if style=='bom' else 'utf-8'
    (root/'package.json').write_bytes(raw.replace('\n','\r\n').encode(encoding) if style=='crlf' else raw.encode(encoding))
    if style == 'invalid-json':
        (root/'package.json').write_text('{"name":"demo", // comment\n}',encoding='utf-8')
        code = reject(lambda:_apply_assignment(root,{'demo-lib':'2.0.0'}),
                      json.JSONDecodeError,'Expecting property name',unchanged=root/'package.json')
        return {'expectedRejection':code,'scope':'invalid JSON rejected without writing'}
    _apply_assignment(root,{'demo-lib':'2.0.0'})
    actual = read(root/'package.json')
    require(actual['custom']=={'preserve':True} and actual['description']=='Пример café','custom keys lost')
    require(actual['dependencies']['demo-lib']=='2.0.0','assignment not applied')
    return {'format':style,'customKeysPreserved':True,'scope':'UTF-8 JSON parsed and rewritten by production manifest mutator'}


def selector_demo(base, *, spec, fixed=False, section='dependencies'):
    root = repo(base); manifest = read(root/'package.json')
    manifest.pop('dependencies'); manifest[section]={'demo-lib':spec}
    write(root/'package.json',manifest)
    if fixed:
        code = reject(lambda:_apply_assignment(root,{'demo-lib':'2.0.0'}),
                      AssignmentMaterializationError,'ASSIGNMENT_TARGETS_FIXED_INPUT',unchanged=root/'package.json')
        return {'selector':spec,'section':section,'expectedRejection':code,'scope':'fixed input cannot become a registry assignment'}
    _apply_assignment(root,{'demo-lib':'2.0.0'})
    require(read(root/'package.json')[section]['demo-lib']=='2.0.0','wrong dependency section')
    return {'selector':spec,'section':section,'materializedSelector':'2.0.0','scope':'manifest transformation, not install proof'}


def rejection_demo(base, *, kind):
    if kind == 'ambiguous-discovery':
        root=base/'repo'; root.mkdir(); app(root,'a'); app(root,'b')
        code=reject(lambda:resolve_project_topology(root),ProjectTopologyError,'PROJECT_PACKAGE_ROOT_AMBIGUOUS')
    else:
        root=repo(base,manager='pnpm' if kind=='pnpm' else 'yarn4' if kind=='pnp' else 'npm',workspaces=['packages/*'])
        child=app(root)
        selected=child
        if kind=='unowned':
            manifest=read(root/'package.json');manifest['workspaces']=['other/*'];write(root/'package.json',manifest)
        elif kind=='ambiguous-lock':
            (root/'yarn.lock').write_text('# yarn lockfile v1\n',encoding='utf-8')
        elif kind=='pnp':
            (root/'.yarnrc.yml').write_text('nodeLinker: pnp\n',encoding='utf-8')
        elif kind=='pnpm':
            selected=root
        key={'unowned':'CANONICAL_LOCKFILE_MISSING','ambiguous-lock':'LOCKFILE_AMBIGUOUS',
             'pnp':'UNSUPPORTED','pnpm':'UNSUPPORTED'}[kind]
        code=reject(lambda:resolve_project_topology(selected,allow_discovery=False,require_supported=True),
                    (ProjectTopologyError,RuntimeError),key)
    return {'expectedRejection':code,'scope':'unsupported or ambiguous topology is not accepted as a verified project'}


def observed_demo(base, *, placement):
    root=repo(base,workspaces=['packages/*']); child=app(root,spec='2.0.0')
    target=(child if placement=='local' else root)/'node_modules/demo-lib/package.json'
    if placement != 'missing':
        write(target,{'name':'demo-lib','version':'2.0.0'})
    if placement == 'shadow':
        write(child/'node_modules/demo-lib/package.json',{'name':'demo-lib','version':'1.0.0'})
    observe=lambda:observe_materialized_assignment(child,root,{'demo-lib':'2.0.0'},observed_resolved_assignment)
    if placement in {'shadow','missing'}:
        code=reject(observe,ObservedResolutionError,'ASSIGNMENT_'+('MISSING' if placement=='missing' else 'DRIFT'))
        return {'placement':placement,'expectedRejection':code,'scope':'physical installed package files; no install subprocess'}
    require(observe()=={'demo-lib':'2.0.0'},'installed assignment differs')
    return {'placement':placement,'observedVersion':'2.0.0','scope':'physical installed package files; no install subprocess'}


def recovery_demo(base):
    run={'phase':'TERMINAL','terminal':'NO_VERIFIED_UPGRADE','activeCheckpointId':'C0',
         'budgetLedger':{'candidateAttempts':7},'runId':'demo-run'}
    checkpoint={'status':'VERIFIED','checkpointId':'C0','sourceSnapshotKey':'demo-source'}
    write(base/'run-before.json',run);write(base/'checkpoint.json',checkpoint)
    resumed=reopen_terminal_run(read(base/'run-before.json'),read(base/'checkpoint.json'),None)
    write(base/'run-after.json',resumed)
    require(resumed['activeCheckpointId']=='C0' and resumed['budgetLedger']==run['budgetLedger'],'budget or base reset')
    require(resumed['phase']=='READY','terminal did not reopen')
    reject(lambda:reopen_terminal_run(run,{**checkpoint,'status':'UNKNOWN'},None),ValueError,'RESUME_CHECKPOINT_NOT_VERIFIED')
    return {'retainedCheckpoint':'C0','retainedCandidateAttempts':7,'scope':'durable coordination transition; source/proof validation still required by CLI'}


def package_tarball(version, broken_lifecycle=False):
    stream=io.BytesIO()
    manifest={'name':'demo-lib','version':version,'main':'index.cjs'}
    if broken_lifecycle:
        manifest['scripts']={'postinstall':'node -e "process.exit(17)"'}
    with tarfile.open(fileobj=stream,mode='w:gz') as archive:
        for name,data in [('package.json',json.dumps(manifest).encode()),('index.cjs',('module.exports='+json.dumps(version)+';').encode())]:
            entry=tarfile.TarInfo('package/'+name);entry.size=len(data);entry.mtime=0
            archive.addfile(entry,io.BytesIO(data))
    return manifest,stream.getvalue()


@contextlib.contextmanager
def local_registry(broken_lifecycle=False):
    packages={version:package_tarball(version,broken_lifecycle and version=='2.0.0') for version in ['1.0.0','2.0.0']}
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            requests.append(self.path)
            if self.path=='/demo-lib':
                versions={version:{**manifest,'dist':{'tarball':f'http://127.0.0.1:{self.server.server_port}/demo-lib/-/{version}.tgz',
                          'shasum':hashlib.sha1(payload).hexdigest()}} for version,(manifest,payload) in packages.items()}
                payload=json.dumps({'name':'demo-lib','dist-tags':{'latest':'2.0.0'},'versions':versions}).encode()
                content_type='application/json'
            elif self.path.startswith('/demo-lib/-/') and self.path.rsplit('/',1)[-1].removesuffix('.tgz') in packages:
                payload=packages[self.path.rsplit('/',1)[-1].removesuffix('.tgz')][1];content_type='application/octet-stream'
            else:
                self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}',requests
    finally: server.shutdown();server.server_close();thread.join(timeout=3)


def physical_demo(base, *, selection):
    root=repo(base,workspaces=['packages/*']);child=app(root);sibling=app(root,'packages/sibling')
    # npm creates the governing lockfile itself; no synthetic lock data is used.
    (root/'package-lock.json').unlink()
    env={key:value for key,value in os.environ.items() if not key.upper().startswith('NPM_CONFIG_')}
    for kind in ['user','global']:
        config=base/(kind+'.npmrc');config.write_text('',encoding='utf-8');env['NPM_CONFIG_'+kind.upper()+'CONFIG']=str(config.resolve())
    broken=selection=='lifecycle-failure'
    with local_registry(broken) as (url,requests):
        (root/'.npmrc').write_text('registry='+url+'\n',encoding='utf-8')
        argv=['npm','install','--ignore-scripts','--no-audit','--no-fund','--fetch-retries=0','--cache',str(base/'cache')]
        initial=command(argv,root,env=env);require(initial.returncode==0,'initial local-registry install failed: '+initial.stderr[-800:])
        selected=root if selection=='root' else child
        untouched=sibling/'package.json';before=untouched.read_bytes()
        _apply_assignment(selected,{'demo-lib':'2.0.0'})
        installed=command(argv,root,env=env);require(installed.returncode==0,'updated local-registry install failed: '+installed.stderr[-800:])
        expected={'root':'2.0.0' if selection=='root' else '1.0.0','child':'1.0.0' if selection=='root' else '2.0.0','sibling':'1.0.0'}
        actual={}
        for label,path in [('root',root),('child',child),('sibling',sibling)]:
            result=command(['node','-p',"require('demo-lib')"],path,env=env)
            require(result.returncode==0,'Node resolution failed: '+result.stderr[-400:]);actual[label]=result.stdout.strip()
            require(observed_resolved_assignment(path,{'demo-lib':expected[label]},package_manager_root=root)=={'demo-lib':expected[label]},'physical installed assignment differs')
        require(actual==expected,'Node loaded the wrong package version')
        require(untouched.read_bytes()==before,'sibling manifest changed')
        result={'selectedManifest':str((selected/'package.json').relative_to(root)), 'runtimeVersions':actual,
                'lockfile':'package-lock.json','registryRequests':requests,'scope':'real npm resolver/install + real Node module resolution; no public registry'}
        if broken:
            lifecycle=command(['npm','rebuild','demo-lib','--foreground-scripts','--cache',str(base/'cache')],root,env=env)
            require(lifecycle.returncode!=0,'broken postinstall unexpectedly passed')
            result.update(lifecycleExitCode=lifecycle.returncode,projectVerified=False,
                          scope='real resolver PASS followed by lifecycle failure; no project-green verdict')
        return result


def demos():
    result=[]
    for manager in ['npm','yarn1','yarn4']:
        for selection in ['root','child']:
            result.append(Demo(f'{manager}-{selection}',f'{manager}: обновление {"корня" if selection=="root" else "подпроекта"}', 'topology',partial(topology_demo,manager=manager,selection=selection)))
    for shape in ['object','brace']:
        result.append(Demo('workspaces-'+shape,'Workspaces: '+shape,'topology',partial(topology_demo,shape=shape)))
    result.append(Demo('independent-child','Подпроект со своим lockfile','topology',partial(topology_demo,independent=True)))
    for style in ['minified','bom','crlf','unicode','invalid-json']:
        result.append(Demo('json-'+style,'package.json: '+style,'manifest',partial(format_demo,style=style)))
    for spec in ['1.0.0','^1.0.0','~1.0.0','>=1 <3','latest']:
        result.append(Demo('selector-'+str(len(result)),'Селектор '+spec,'manifest',partial(selector_demo,spec=spec)))
    for spec in ['workspace:*','file:../lib','link:../lib','npm:other-lib@1.0.0','git+https://example.invalid/demo.git#fixed']:
        result.append(Demo('fixed-'+str(len(result)),'Фиксированный источник '+spec,'manifest',partial(selector_demo,spec=spec,fixed=True)))
    for section in ['devDependencies','optionalDependencies','peerDependencies']:
        result.append(Demo('section-'+section,'Секция '+section,'manifest',partial(selector_demo,spec='^1',section=section)))
    for kind in ['unowned','ambiguous-discovery','ambiguous-lock','pnp','pnpm']:
        result.append(Demo('reject-'+kind,'Защитное отклонение: '+kind,'rejection',partial(rejection_demo,kind=kind)))
    for placement in ['hoisted','local','shadow','missing']:
        result.append(Demo('installed-'+placement,'Установленные зависимости: '+placement,'observation',partial(observed_demo,placement=placement)))
    result.append(Demo('resume-budget','Продолжение сохраняет checkpoint и бюджеты','recovery',recovery_demo))
    for selection in ['root','child','lifecycle-failure']:
        result.append(Demo('physical-'+selection,'Настоящий npm: '+selection,'physical',partial(physical_demo,selection=selection)))
    return result


def run_demo(demo, directory):
    directory.mkdir(parents=True,exist_ok=False)
    started=time.monotonic()
    row={'id':demo.name,'title':demo.title,'group':demo.group}
    try: row.update(status='PASS',evidence=demo.run(directory))
    except Exception as exc: row.update(status='FAIL',error=type(exc).__name__+': '+str(exc))
    row['seconds']=round(time.monotonic()-started,3)
    write(directory/'evidence.json',row)
    return row


def run_suite(directory, *, core_only=False, names=None):
    directory.mkdir(parents=True,exist_ok=False)
    selected=[demo for demo in demos() if (not core_only or demo.group!='physical') and (not names or demo.name in names)]
    require(selected,'No matching demo cases')
    rows=[run_demo(demo,directory/demo.name) for demo in selected]
    report={'schema':'real-world-workspace-demos-v1','scope':'scenario contracts; not a verified migration of a user project',
            'passed':sum(row['status']=='PASS' for row in rows),'failed':sum(row['status']=='FAIL' for row in rows),'cases':rows}
    write(directory/'results.json',report)
    lines=['# Демо реальных структур проектов','',f"Пройдено: {report['passed']}; ошибок: {report['failed']}.",'',
           'PASS означает, что сценарий повёл себя ожидаемо, включая защитные отклонения. Это не подтверждение зелёной миграции проекта.','',
           '| Сценарий | Итог | Время, с | Доказательства |','| --- | --- | ---: | --- |']
    lines.extend(f"| {row['title']} | {row['status']} | {row['seconds']} | [{row['id']}]({row['id']}/evidence.json) |" for row in rows)
    lines.extend(['','## Корень и подпроект','',
                  'В сценариях root меняется корневой manifest; manifest подпроекта и соседей сохраняется. В child меняется только выбранный подпроект. Общий lockfile и cwd установки остаются у владельца workspace.', '',
                  'Physical-сценарии проверяют эти правила настоящим npm и Node через локальный registry. Остальные явно указывают границу проверки в evidence.scope.'])
    (directory/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return report
