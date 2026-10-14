"""Localized, deterministic migration documents from artifacts, never acceptance authority."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import string
import subprocess

DOCUMENTS = ('MIGRATION_REPORT.md','DEVELOPER_UPGRADE_GUIDE.md','pre-fix-audit-report.md','post-fix-audit-report.md')
PRIMARY = DOCUMENTS[:2]
CATALOG_ROOT = Path(__file__).with_name('document_locales')


class DocumentError(ValueError): pass


def catalog(language='en'):
    if language not in {'en','ru'}: raise DocumentError('DOCUMENT_LANGUAGE_UNSUPPORTED: '+str(language))
    path=CATALOG_ROOT/(language+'.json')
    try: result=json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError) as exc: raise DocumentError('DOCUMENT_CATALOG_UNAVAILABLE: '+str(path)) from exc
    if not isinstance(result,dict) or any(not isinstance(value,str) or not value for value in result.values()):
        raise DocumentError('DOCUMENT_CATALOG_INVALID: '+str(path))
    return result


def check_catalogs():
    en,ru=catalog('en'),catalog('ru')
    if en.keys()!=ru.keys():raise DocumentError('DOCUMENT_TRANSLATION_KEYS_MISMATCH')
    fields=lambda value:{field for _,field,_,_ in string.Formatter().parse(value) if field}
    for key in en:
        if not isinstance(ru[key],str) or not ru[key] or fields(en[key])!=fields(ru[key]):
            raise DocumentError('DOCUMENT_TRANSLATION_INCOMPLETE: '+key)
    return sorted(en)


def relative_document_names(language='en',primary=False):
    catalog(language)
    return [(('ru/' if language=='ru' else '')+name) for name in (PRIMARY if primary else DOCUMENTS)]


def stable(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n'

def load(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def optional(path):
    try:return load(path) if path and Path(path).is_file() else None
    except (OSError,ValueError):return None

def sha(data):return hashlib.sha256(data).hexdigest()


def store(path,text):
    import os
    path=Path(path)
    if path.is_symlink():raise DocumentError('DOCUMENT_OUTPUT_SYMLINK: '+str(path))
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.write-tmp')
    temporary.write_bytes(text.encode('utf-8'))
    os.replace(temporary,path)

def declarations(manifest):
    if not isinstance(manifest,dict):return {}
    result={}
    for section in ('dependencies','devDependencies','optionalDependencies','peerDependencies'):
        values=(manifest or {}).get(section)
        if isinstance(values,dict):result.update({(section,name):str(value) for name,value in values.items()})
    return result


def checkpoint_source(checkpoint,base=None):
    container=checkpoint.get('sourceSnapshotContainer')
    if not container:return None
    root=Path(container)
    if not root.is_absolute() and base is not None:root=Path(base)/root
    root=root/'tree' if (root/'tree').is_dir() else root
    project=root/str(checkpoint.get('projectRelative') or '.')
    if not project.resolve().is_relative_to(root.resolve()):return None
    return project if (project/'package.json').is_file() else None


def audit_for_checkpoint(run_dir,checkpoint):
    if not checkpoint:return None
    reference=(checkpoint.get('audit') or {}).get('evidenceRef')
    if reference:
        path=Path(reference)
        if not path.is_absolute():path=run_dir/path
        if path.is_dir():path=path/'audit-report.json'
        if path.resolve().is_relative_to(run_dir.resolve()):
            result=optional(path)
            if result is not None:return result
    return optional(run_dir/'audit'/str(checkpoint.get('checkpointId'))/'audit-report.json')


def lock_inventory(path):
    path=Path(path)
    try:
        if path.name=='yarn.lock':
            from manual_dependency_audit import parse_yarn_lock
            return parse_yarn_lock(path)
        payload=load(path)
        packages=payload.get('packages')
        if isinstance(packages,dict):
            return {name:str(row['version']) for name,row in packages.items()
                    if isinstance(row,dict) and row.get('version') is not None}
    except (OSError,ValueError,RuntimeError):pass
    return None


def source_facts(checkpoint,audit=None,base=None):
    project=checkpoint_source(checkpoint,base)
    result={'projectPath':str(project) if project else None,'manifest':None,'manifestHash':None,
            'checkpointId':checkpoint.get('checkpointId'),'sourceHead':checkpoint.get('sourceHead'),
            'checkpointManifestHash':checkpoint.get('manifestHash'),'bindingStatus':'UNKNOWN'}
    if project:
        try:raw=(project/'package.json').read_bytes()
        except OSError:return result
        result.update(manifest=optional(project/'package.json'),manifestHash=sha(raw))
        expected=checkpoint.get('manifestHash')
        if expected:result['bindingStatus']='PASS' if expected==result['manifestHash'] and isinstance(result['manifest'],dict) else 'UNVERIFIED'
        try:
            from project_topology import _select_owned_lockfile
            container=Path(checkpoint['sourceSnapshotContainer'])
            if not container.is_absolute() and base is not None:container=Path(base)/container
            boundary=container/'tree' if (container/'tree').is_dir() else container
            _,lock,_=_select_owned_lockfile(project,source_boundary=boundary)
            result.update(lockfile=str(lock),lockfileHash=sha(lock.read_bytes()),lockInventory=lock_inventory(lock))
            if checkpoint.get('lockfileHash') and checkpoint['lockfileHash']!=result['lockfileHash']:result['bindingStatus']='UNVERIFIED'
        except (OSError,ValueError,RuntimeError):pass
        if checkpoint.get('lockfileHash') and not result.get('lockfileHash') and result['bindingStatus']!='UNVERIFIED':
            result['bindingStatus']='UNKNOWN'
        if audit and audit.get('directDeclarations') is not None:
            result['directDeclarationsActual']=len(declarations(result['manifest']))
            result['directDeclarationsRecorded']=audit['directDeclarations']
            if result['directDeclarationsActual']!=audit['directDeclarations']:result['bindingStatus']='UNVERIFIED'
        if audit and audit.get('dependencyInputHash'):
            try:
                from manual_dependency_audit import dependency_input_identity
                actual,_=dependency_input_identity(project)
                result['dependencyInputHashActual']=actual
                result['dependencyInputHashRecorded']=audit['dependencyInputHash']
                if actual!=audit['dependencyInputHash']:result['bindingStatus']='UNVERIFIED'
            except (OSError,ValueError,RuntimeError):
                result['dependencyInputHashActual']=None
                if result['bindingStatus']!='UNVERIFIED':result['bindingStatus']='UNKNOWN'
    return result


def run_inputs(run_dir,run=None,config=None,checkpoints=None):
    run_dir=Path(run_dir)
    run=run if run is not None else load(run_dir/'run.json')
    config=config if config is not None else load(run_dir/'run-config.json')
    checkpoints=checkpoints if checkpoints is not None else [load(path) for path in sorted((run_dir/'checkpoints').glob('C*.json'))]
    before=next((item for item in checkpoints if item.get('checkpointId')=='C0'),None)
    after=next((item for item in checkpoints if item.get('checkpointId')==run.get('activeCheckpointId')),None)
    pre=audit_for_checkpoint(run_dir,before);post=audit_for_checkpoint(run_dir,after)
    before_facts=source_facts(before or {},pre,run_dir);after_facts=source_facts(after or {},post,run_dir)
    data={'schema':'migration-document-input-v1','run':run,'config':config,'checkpoints':checkpoints,
          'preAudit':pre,'postAudit':post,'before':before_facts,'after':after_facts,
          'ledger':optional(run_dir/'ledger.json') or {},'reconstructed':False}
    delivery=optional(run_dir/'delivery-state.json')
    if delivery and delivery.get('status')=='done' and delivery.get('head') and delivery.get('workspaceRoot'):
        root=Path(delivery['workspaceRoot'])
        relative=(Path(delivery.get('projectRelative') or '.')/'package.json').as_posix()
        try:result=subprocess.run(['git','-C',str(root),'show',delivery['head']+':'+relative],capture_output=True,timeout=30)
        except (OSError,subprocess.TimeoutExpired):result=None
        if result is not None and result.returncode==0:
            data['committedManifestHash']=sha(result.stdout)
            expected=(after or {}).get('manifestHash')
            data['committedManifestBinding']='UNKNOWN' if not expected else 'PASS' if data['committedManifestHash']==expected else 'UNVERIFIED'
    return data


class Document:
    def __init__(self,language,title,data):
        self.strings=catalog(language);self.lines=['# '+self.t(title),''];self.missing={}
        if data.get('reconstructed'):self.lines.extend([self.t('reconstructed_git' if data.get('reconstructionSource')=='git' else 'reconstructed_files'),''])
    def t(self,key,**values):
        try:return self.strings[key].format(**values)
        except KeyError as exc:raise DocumentError('DOCUMENT_TRANSLATION_MISSING: '+str(exc)) from exc
    def section(self,key):self.lines.extend(['','## '+self.t(key),''])
    def value(self,field,value,how='get_source',reason='reason_missing'):
        if value is None or value=='':
            self.missing[field]=(reason,how);return self.t('na')
        if isinstance(value,(dict,list,bool)):return json.dumps(value,ensure_ascii=False,sort_keys=True)
        return str(value)
    def literal(self,field,value,how='get_source'):
        text=self.value(field,value,how)
        return '`'+text.replace('`',r'\`')+'`' if value is not None and value!='' else text
    def table(self,keys,rows):
        self.lines.append('')
        self.lines.extend(['| '+' | '.join(self.t(key) for key in keys)+' |','| '+' | '.join('---' for _ in keys)+' |'])
        for row in rows:self.lines.append('| '+' | '.join(str(cell).replace('|',r'\|').replace('\n','<br>') for cell in row)+' |')
    def raw(self,value,field,how='get_source'):
        if value is None:self.lines.append(self.value(field,None,how));return
        def missing_items(item,path):
            if item is None or item=='':self.value(path,None,how)
            elif isinstance(item,dict):
                for key,child in item.items():missing_items(child,path+'.'+str(key))
            elif isinstance(item,list):
                for index,child in enumerate(item):missing_items(child,path+'['+str(index)+']')
        missing_items(value,field)
        self.lines.extend([self.t('raw'),'','```json',stable(value).rstrip(),'```'])
    def finish(self):
        self.section('missing_title')
        if not self.missing:self.lines.append(self.t('no_missing'))
        for field,(reason,how) in sorted(self.missing.items()):
            self.lines.append(self.t('missing',field='`'+field+'`',reason=self.t(reason).rstrip('.'),how=self.t(how).rstrip('.')+(' `python manual_dependency_audit.py --help`' if how=='get_audit' else '')))
        return '\n'.join(self.lines)+'\n'


def binding(document,facts):
    document.section('evidence')
    rows=[]
    for key in ['projectPath','checkpointId','sourceHead','manifestHash','checkpointManifestHash','lockfile','lockfileHash','bindingStatus','directDeclarationsActual','directDeclarationsRecorded','dependencyInputHashActual','dependencyInputHashRecorded']:
        rows.append(['`'+key+'`',document.value(key,facts.get(key))])
    document.table(['field','value'],rows)
    if facts.get('bindingStatus')=='UNVERIFIED':
        reason=facts.get('bindingReason') or 'reason_mismatch'
        document.value('sourceEvidenceBinding',None,'get_validation' if reason=='reason_no_run' else 'get_source',reason)


def audit_document(report,language='en',*,phase='manual',facts=None,context=None):
    d=Document(language,{'pre':'pre_title','post':'post_title','manual':'manual_title'}[phase],context or {})
    facts=facts or {}
    d.section('metadata');d.table(['field','value'],[['`'+key+'`',d.value(key,(report or {}).get(key),'get_audit')]
        for key in ['projectDir','projectName','packageManager','registry','generatedAt','dependencyInputHash','directDeclarations','auditComplete','lagComplete','complete']])
    audit=(report or {}).get('audit') or {}
    if not report:d.value('audit.'+phase,None,'get_audit')
    if report and audit.get('complete') is not True:d.value('audit.completeEvidence',None,'get_audit')
    d.lines.append(d.t('completeness',status='COMPLETE' if (report or {}).get('complete') is True else 'INCOMPLETE'))
    d.section('vulnerabilities');d.lines.append(d.t('audit_scope'))
    if audit.get('complete') is not True:d.lines.append(d.t('audit_unknown'))
    d.table(['package','status','value'],[[name,str(severity),d.value('audit.packageDetails.'+name,(audit.get('packageDetails') or {}).get(name),'get_audit')]
            for name,severity in sorted((audit.get('packages') or {}).items())])
    d.section('units')
    from manual_dependency_audit import format_counts, SEVERITIES
    inventory=audit.get('engine')=='yarn-inventory'
    summaries=[('versions_summary','packageVersionTotals'),('names_summary','packageTotals')] if inventory else [('nodes_summary','nodeTotals'),('packages_summary','packageTotals')]
    for label,unit in [*summaries,('advisories_summary','advisoryTotals')]:
        totals=audit.get(unit)
        if unit=='nodeTotals' and totals is None:totals=audit.get('totals')
        counts=format_counts(totals) if isinstance(totals,dict) and all(isinstance(totals.get(key),int) for key in SEVERITIES) and audit.get('complete') is True else 'UNKNOWN'
        if counts=='UNKNOWN':d.value('audit.'+unit,None,'get_audit')
        d.lines.append('- '+d.t(label,counts=counts))
    if inventory:d.lines.append(d.t('inventory_scope'))
    rows=[]
    for unit in ['nodeTotals','packageVersionTotals','packageTotals','advisoryTotals']:
        totals=audit.get(unit)
        if unit=='nodeTotals' and totals is None:totals=audit.get('totals')
        severities=[*SEVERITIES,*sorted(set(totals)-set(SEVERITIES))] if isinstance(totals,dict) else SEVERITIES
        for severity in severities:
            value=totals.get(severity) if isinstance(totals,dict) and audit.get('complete') is True else None
            rows.append(['`'+unit+'.'+severity+'`',d.value('audit.'+unit+'.'+severity,value,'get_audit')])
    d.table(['field','value'],rows)
    d.section('lag')
    lag=(report or {}).get('lag')
    if lag is None:d.value('lag',None,'get_audit')
    d.table(['section','package','current_version','latest_version','status','lag_days','policy','published'],[[d.value('lag.section',item.get('section'),'get_audit'),d.literal('lag.name',item.get('name'),'get_audit'),
       d.literal('lag.current',item.get('current'),'get_audit'),d.literal('lag.latest',item.get('latest'),'get_audit'),d.value('lag.status',item.get('status'),'get_audit'),
       d.value('lag.'+str(item.get('name'))+'.lagDays',item.get('lagDays'),'get_audit'),
       d.t('policy_window',months=item['policyMonths']) if item.get('policyMonths') is not None else d.value('lag.policyMonths',None,'get_audit'),
       d.value('lag.currentPublishedAt',item.get('currentPublishedAt'),'get_audit')] for item in (lag or [])])
    d.lines.append(d.t('lag_summary',lagging=d.value('audit.lagLagging',audit.get('lagLagging'),'get_audit'),
                       ok=d.value('audit.lagOk',audit.get('lagOk'),'get_audit'),unknown=d.value('audit.lagUnknown',audit.get('lagUnknown'),'get_audit')))
    d.raw({key:audit.get(key) for key in ['lagOk','lagLagging','lagUnknown','lagTotal','lagOkPct']},'lag.coverage','get_audit')
    d.section('inventory');d.raw(audit.get('canonicalInventory'),'audit.canonicalInventory','get_inventory')
    d.raw(facts.get('lockInventory'),'source.lockInventory','get_inventory')
    d.section('bridge');d.raw(audit.get('bridgeReconciliation'),'audit.bridgeReconciliation','get_inventory')
    d.raw({key:audit.get(key) for key in ['engine','command','exitCode','lockCommand','lockExitCode','lockReused','lockDurationSeconds','auditDurationSeconds','requestedRegistry','effectiveRegistry','notes','rawAuditEvidence']},'audit.details','get_audit')
    if facts:binding(d,facts)
    d.section('reconciliation');d.lines.append(d.t('reconcile_hint'))
    return d.finish()


def migration_document(data,language='en',guide=False):
    d=Document(language,'guide_title' if guide else 'migration_title',data)
    run=data.get('run') or {};config=data.get('config') or {};cps=data.get('checkpoints') or []
    before=data.get('before') or {};after=data.get('after') or {}
    old=declarations(before.get('manifest'));new=declarations(after.get('manifest'))
    d.section('metadata');d.table(['field','value'],[['`'+key+'`',d.value(key,value,'get_run')] for key,value in
        [('runId',run.get('runId')),('projectName',config.get('projectName')),('targetLevel',config.get('targetLevel')),
         ('terminal',run.get('terminal')),('activeCheckpointId',run.get('activeCheckpointId')),('sourceHead',data.get('sourceHead'))]])
    d.lines.append(d.t('scope'))
    d.section('outcome');d.raw(run.get('terminalOutcome'),'terminalOutcome','get_run')
    accepted=(run.get('terminalOutcome') or {}).get('acceptedCheckpoints')
    d.lines.append(d.t('accepted_count',count=d.value('acceptedCheckpoints',accepted,'get_run')))
    d.section('recorded_audit');d.raw(data.get('activeAudit'),'activeCheckpoint.audit','get_audit')
    d.table(['field','value'],[['`'+key+'`',d.value('activeCheckpoint.audit.'+key,(data.get('activeAudit') or {}).get(key),'get_audit')]
            for key in ['status','evidenceRef','lagOkPct','lagOk','lagLagging','lagUnknown','lagTotal','vulnerabilityPackages']])
    d.section('coverage');d.raw({'targets':config.get('targets'),'fullAssignment':data.get('assignment'),
        'deferrals':(data.get('ledger') or {}).get('deferrals')},'policyCoverage','get_run')
    d.raw(data.get('targetCoverage'),'targetCoverage','get_run')
    d.section('direct')
    targets=config.get('targets') or {};rows=[]
    selected=set(old)|set(new)
    known_names={name for _,name in selected}
    recorded_names=set(targets)|set(data.get('assignment') or {})
    selected.update(('',name) for name in recorded_names-known_names)
    for section,name in sorted(selected):
        rows.append([d.value('section.'+name,section),name,d.value('before.'+section+'.'+name,old.get((section,name))),
                     d.value('after.'+section+'.'+name,new.get((section,name))),
                     d.value('target.'+name,targets.get(name),'get_run'),
                     d.value('before.recorded.'+name,(data.get('beforeAssignment') or {}).get(name),'get_validation'),
                     d.value('after.recorded.'+name,(data.get('assignment') or {}).get(name),'get_validation'),
                     'UNVERIFIED' if after.get('bindingStatus')=='UNVERIFIED' or data.get('reconstructed') else d.value('after.bindingStatus',after.get('bindingStatus'))])
    d.table(['section','package','old','new','requested','before_recorded','after_recorded','status'],rows)
    accepted_changes=any(any((cp.get('acceptedDelta') or {}).get(key) for key in ('changed','added','removed')) for cp in data.get('acceptedChain',[]))
    if old==new and not accepted_changes:
        if before.get('manifest') is not None and after.get('manifest') is not None or data.get('acceptedChain') and all(isinstance(cp.get('acceptedDelta'),dict) for cp in data.get('acceptedChain',[])):
            d.lines.append(d.t('no_updates'))
        else:d.value('acceptedVersionChanges',None,'get_source')
    if guide:
        d.section('accepted_upgrades')
        accepted=[{'package':name,'version':version,'checkpointId':cp.get('checkpointId')} for cp in data.get('acceptedChain',[])
                  for name,version in (cp.get('acceptedDelta') or {}).get('changed',{}).items() if cp.get('status')=='VERIFIED']
        by_id={cp.get('checkpointId'):cp for cp in cps}
        rows=[]
        for cp in data.get('acceptedChain',[]):
            if cp.get('status')!='VERIFIED':continue
            parent=by_id.get(cp.get('parentCheckpointId')) or {}
            for name,version in sorted(((cp.get('acceptedDelta') or {}).get('changed') or {}).items()):
                rows.append([name,d.value('accepted.before.'+name,(parent.get('fullAssignment') or {}).get(name),'get_run'),
                             version,cp['checkpointId'],
                             'UNVERIFIED' if after.get('bindingStatus')=='UNVERIFIED' or data.get('committedManifestBinding')=='UNVERIFIED' else
                             'VERIFIED' if after.get('bindingStatus')=='PASS' else 'UNKNOWN'])
        d.table(['package','old','new','checkpoint','status'],rows)
        d.raw(accepted if cps else None,'acceptedUpgrades','get_run')
    d.section('transitive');d.raw(data.get('transitiveChanges'),'transitiveChanges','get_diff')
    d.section('checks');d.raw(config.get('validationProfile') or config.get('verifyConfig'),'validationProfile','get_validation')
    d.raw(config.get('validationScope'),'validationScope','get_validation')
    if (config.get('validationScope') or {}).get('mode')=='test-nonregression':d.lines.append(d.t('nonregression'))
    d.raw([{'checkpointId':cp.get('checkpointId'),'verification':cp.get('verification')} for cp in cps] if cps else None,'checkpointVerification','get_validation')
    d.section('checkpoints')
    d.table(['checkpoint','parent','status','changed','audit'],[[d.value('checkpointId',cp.get('checkpointId'),'get_run'),
       cp.get('parentCheckpointId') or d.t('na'),d.value('checkpoint.status',cp.get('status'),'get_run'),
       json.dumps((cp.get('acceptedDelta') or {}).get('changed',{}),ensure_ascii=False,sort_keys=True),
       d.value('checkpoint.audit',(cp.get('audit') or {}).get('status'),'get_audit')] for cp in cps])
    d.raw([{key:cp.get(key) for key in ['checkpointId','sourceSnapshotKey','manifestHash','lockfileHash','resolvedStateKey','proofRefs']} for cp in cps] if cps else None,'checkpointEvidence','get_run')
    d.section('assignment');d.raw(data.get('assignment'),'fullAssignment','get_run')
    binding(d,after)
    d.table(['field','value'],[['`committedManifestHash`',d.value('committedManifestHash',data.get('committedManifestHash'))],
                             ['`committedManifestBinding`',d.value('committedManifestBinding',data.get('committedManifestBinding'))]])
    if data.get('committedManifestBinding')=='UNVERIFIED':d.value('committedManifestBinding',None,'get_source','reason_mismatch')
    d.section('explanations');d.raw((data.get('ledger') or {}).get('feedback'),'feedback','get_adaptations')
    d.section('capabilities');d.raw(data.get('capabilities'),'versionSpecificCapabilities','get_adaptations')
    d.section('files');d.raw(data.get('changedFiles'),'changedSourceAndConfig','get_diff')
    d.section('limits');d.raw((data.get('ledger') or {}).get('deferrals'),'deferrals','get_run')
    d.section('vulnerabilities');d.raw({'before':(data.get('preAudit') or {}).get('audit'),'after':(data.get('postAudit') or {}).get('audit')},'auditComparison','get_audit')
    if not data.get('preAudit'):d.value('preAudit',None,'get_audit')
    if not data.get('postAudit'):d.value('postAudit',None,'get_audit')
    d.section('rollback');d.lines.append(d.t('rollback_hint'))
    return d.finish()


def complete_inputs(data):
    data=dict(data)
    cps=data.get('checkpoints') or [];run=data.get('run') or {}
    initial=next((cp for cp in cps if cp.get('checkpointId')=='C0'),None)
    if initial:data['beforeAssignment']=initial.get('fullAssignment')
    active=next((cp for cp in cps if cp.get('checkpointId')==run.get('activeCheckpointId')),None)
    if active:
        data['assignment']=active.get('fullAssignment')
        data['activeAudit']=active.get('audit')
        chain=[];seen=set();cursor=active;by_id={cp.get('checkpointId'):cp for cp in cps}
        while cursor and cursor.get('checkpointId') not in seen:
            chain.append(cursor);seen.add(cursor.get('checkpointId'));cursor=by_id.get(cursor.get('parentCheckpointId'))
        data['acceptedChain']=list(reversed(chain))
        data['checkpoints']=data['acceptedChain']+[cp for cp in cps if cp.get('checkpointId') not in seen]
    targets=(data.get('config') or {}).get('targets');assignment=data.get('assignment')
    if isinstance(targets,dict) and isinstance(assignment,dict):
        matched=sum(1 for name,version in targets.items() if assignment.get(name)==str(version))
        data['targetCoverage']={'denominator':len(targets),'matched':matched,'remaining':len(targets)-matched,
                                'matchedPct':round(100*matched/len(targets),2) if targets else None}
    data.setdefault('sourceHead',next((cp.get('sourceHead') for cp in cps if cp.get('checkpointId')=='C0'),None))
    before=(data.get('before') or {}).get('lockInventory');after=(data.get('after') or {}).get('lockInventory')
    if isinstance(before,dict) and isinstance(after,dict):
        data.setdefault('transitiveChanges',[{'lockEntry':key,'before':before.get(key),'after':after.get(key)}
                        for key in sorted(before.keys()|after.keys()) if before.get(key)!=after.get(key)])
    feedback=(data.get('ledger') or {}).get('feedback') or []
    if feedback:data.setdefault('changedFiles',sorted({str(name) for entry in feedback for name in entry.get('changedFiles',[])}))
    return data


def generate_documents(data,output,language='en',*,preserve_primary=False):
    check_catalogs();catalog(language);data=complete_inputs(data)
    output=Path(output);target=output/('ru' if language=='ru' else '')
    target.mkdir(parents=True,exist_ok=True)
    if not target.resolve().is_relative_to(output.resolve()):raise DocumentError('DOCUMENT_OUTPUT_OUTSIDE_ROOT')
    rendered={DOCUMENTS[0]:migration_document(data,language),DOCUMENTS[1]:migration_document(data,language,guide=True),
              DOCUMENTS[2]:audit_document(data.get('preAudit'),language,phase='pre',facts=data.get('before'),context=data),
              DOCUMENTS[3]:audit_document(data.get('postAudit'),language,phase='post',facts=data.get('after'),context=data)}
    for name,text in rendered.items():
        if not preserve_primary or name not in PRIMARY or not (target/name).exists():store(target/name,text)
    for phase,key in [('pre','preAudit'),('post','postAudit')]:
        payload=data.get(key)
        path=output/(phase+'-fix-audit-report.json')
        store(path,stable(payload if payload is not None else {'schemaVersion':1,'status':'UNKNOWN','audit':None,'lag':None}))
    store(output/'documentation-evidence.json',stable(data))
    return [str(target/name) for name in DOCUMENTS]


def generate_run_documents(run_dir,run=None,config=None,checkpoints=None,language=None,*,preserve_primary=False):
    data=run_inputs(run_dir,run,config,checkpoints)
    selected=language if language is not None else (data['config'].get('documentLanguage') or 'en')
    return generate_documents(data,Path(run_dir)/'reports',selected,preserve_primary=preserve_primary)


def write_pre_fix_report(run_dir,run,config,checkpoint):
    language=config.get('documentLanguage') or 'en'
    data=run_inputs(run_dir,run,config,[checkpoint])
    target=Path(run_dir)/'reports'/('ru' if language=='ru' else '')
    target.mkdir(parents=True,exist_ok=True)
    if not target.resolve().is_relative_to((Path(run_dir)/'reports').resolve()):raise DocumentError('DOCUMENT_OUTPUT_OUTSIDE_ROOT')
    store(target/'pre-fix-audit-report.md',audit_document(data['preAudit'],language,phase='pre',facts=data['before']))
    store(Path(run_dir)/'reports/pre-fix-audit-report.json',stable(data['preAudit'] if data['preAudit'] is not None else {'schemaVersion':1,'status':'UNKNOWN','audit':None,'lag':None}))


def reconstruct(before_bytes,after_bytes,*,before_audit=None,after_audit=None,before_lock=None,after_lock=None,history=None):
    before=json.loads(before_bytes.decode('utf-8-sig'));after=json.loads(after_bytes.decode('utf-8-sig'))
    if not isinstance(before,dict) or not isinstance(after,dict):raise DocumentError('MANIFEST_OBJECT_REQUIRED')
    result={'schema':'migration-document-input-v1','reconstructed':True,'reconstructionSource':'git' if history else 'manifests',
            'history':history,'preAudit':before_audit,'postAudit':after_audit,'checkpoints':[], 'ledger':{},
            'before':{'manifest':before,'manifestHash':sha(before_bytes),'lockfileHash':sha(before_lock) if before_lock is not None else None,'bindingStatus':'UNVERIFIED','bindingReason':'reason_no_run'},
            'after':{'manifest':after,'manifestHash':sha(after_bytes),'lockfileHash':sha(after_lock) if after_lock is not None else None,'bindingStatus':'UNVERIFIED','bindingReason':'reason_no_run'},
            'beforeLockHash':sha(before_lock) if before_lock is not None else None,'afterLockHash':sha(after_lock) if after_lock is not None else None,
            'config':{'projectName':after.get('name')},'run':{}}
    for phase,audit in [('before',before_audit),('after',after_audit)]:
        if audit and audit.get('directDeclarations') is not None:
            result[phase]['directDeclarationsActual']=len(declarations(result[phase]['manifest']))
            result[phase]['directDeclarationsRecorded']=audit['directDeclarations']
            if result[phase]['directDeclarationsActual']!=audit['directDeclarations']:result[phase]['bindingReason']='reason_mismatch'
    identity=sha(stable(result).encode())
    result['run']['runId']='reconstructed-'+identity[:12]
    return result


def inventory_bytes(raw,name):
    import tempfile
    with tempfile.TemporaryDirectory() as temporary:
        path=Path(temporary)/Path(name).name
        path.write_bytes(raw)
        return lock_inventory(path)


def main():
    from cli_io import configure_utf8_stdio
    configure_utf8_stdio()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lang',choices=['en','ru'],default=None)
    parser.add_argument('--run-dir',type=Path)
    parser.add_argument('--before-manifest',type=Path);parser.add_argument('--after-manifest',type=Path)
    parser.add_argument('--before-lock',type=Path);parser.add_argument('--after-lock',type=Path)
    parser.add_argument('--before-audit',type=Path);parser.add_argument('--after-audit',type=Path)
    parser.add_argument('--repository',type=Path);parser.add_argument('--range',dest='commit_range')
    parser.add_argument('--manifest-path',default='package.json');parser.add_argument('--lock-path')
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    if args.run_dir:
        if args.commit_range or args.before_manifest or args.after_manifest:parser.error('--run-dir cannot be combined with reconstruction inputs')
        data=run_inputs(args.run_dir);output=args.output_dir or args.run_dir/'reports'
        language=args.lang if args.lang is not None else data['config'].get('documentLanguage','en')
    else:
        history=None
        if args.commit_range:
            if not args.repository or args.commit_range.count('..')!=1 or '...' in args.commit_range:parser.error('Use --repository and --range BASE..HEAD')
            revisions=[]
            for reference in args.commit_range.split('..'):
                result=subprocess.run(['git','-C',str(args.repository),'rev-parse','--verify',reference+'^{commit}'],capture_output=True,text=True,encoding='utf-8',timeout=30)
                if result.returncode:raise DocumentError('RECONSTRUCTION_COMMIT_UNAVAILABLE: '+reference)
                revisions.append(result.stdout.strip())
            def blob(revision,path):
                result=subprocess.run(['git','-C',str(args.repository),'show',revision+':'+path],capture_output=True,timeout=30)
                if result.returncode:raise DocumentError('RECONSTRUCTION_GIT_BLOB_UNAVAILABLE: '+revision+':'+path)
                return result.stdout
            before,after=[blob(revision,args.manifest_path) for revision in revisions]
            old_lock,new_lock=[blob(revision,args.lock_path) if args.lock_path else None for revision in revisions]
            history={'range':args.commit_range,'baseCommit':revisions[0],'headCommit':revisions[1],'manifestPath':args.manifest_path,'lockPath':args.lock_path}
            result=subprocess.run(['git','-C',str(args.repository),'diff','--name-only','..'.join(revisions),'--'],capture_output=True,text=True,encoding='utf-8',timeout=30)
            if result.returncode:raise DocumentError('RECONSTRUCTION_DIFF_UNAVAILABLE')
            history['changedFiles']=sorted(result.stdout.splitlines())
        else:
            if not args.before_manifest or not args.after_manifest:parser.error('Supply both manifests, or --run-dir, or --repository with --range')
            before,after=args.before_manifest.read_bytes(),args.after_manifest.read_bytes()
            old_lock=args.before_lock.read_bytes() if args.before_lock else None
            new_lock=args.after_lock.read_bytes() if args.after_lock else None
        data=reconstruct(before,after,before_audit=optional(args.before_audit),after_audit=optional(args.after_audit),before_lock=old_lock,after_lock=new_lock,history=history)
        if history:
            data['changedFiles']=history['changedFiles']
            data['sourceHead']=history['baseCommit']
            data['before']['sourceHead']=history['baseCommit']
            data['after']['sourceHead']=history['headCommit']
            if old_lock is not None:data['before']['lockInventory']=inventory_bytes(old_lock,args.lock_path)
            if new_lock is not None:data['after']['lockInventory']=inventory_bytes(new_lock,args.lock_path)
        if args.before_lock and args.after_lock:
            data['before']['lockInventory']=lock_inventory(args.before_lock)
            data['after']['lockInventory']=lock_inventory(args.after_lock)
        if not args.output_dir:parser.error('Reconstruction requires --output-dir')
        output=args.output_dir;language=args.lang or 'en'
    paths=generate_documents(data,output,language)
    print(json.dumps({'documents':paths,'runId':data['run'].get('runId')},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
