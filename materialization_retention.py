"""Recognize disposable copies while preserving authoritative source snapshots."""
from pathlib import Path
import hashlib
import json
import os
import re


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def fingerprint(root, progress=lambda path: None):
    from verification_storage_maintenance import _plain_directory
    files = {}; dirs = set(); excluded = {'.git', 'node_modules', '.dependency-roadmap'}
    for current, children, names in os.walk(root, followlinks=False):
        children[:] = sorted(n for n in children if n not in excluded)
        if not _plain_directory(Path(current)): raise OSError('non-private source directory')
        for name in children:
            path=Path(current)/name
            if not _plain_directory(path): raise OSError('source directory link')
            dirs.add(path.relative_to(root).as_posix())
        for name in sorted(names):
            if name == '.git': continue
            path=Path(current)/name
            if path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&0x400: raise OSError('source link')
            h=hashlib.sha256()
            with path.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
            files[path.relative_to(root).as_posix()]=h.hexdigest()
        progress(current)
    return files, dirs


def manifest_fingerprint(manifest):
    files={};dirs=set()
    for item in manifest['entries']:
        path=Path(item['path'])
        if any(p in {'.git','node_modules','.dependency-roadmap'} for p in path.parts): continue
        if item['kind']=='file': files[path.as_posix()]=item['sha256']
        elif item['kind']=='directory': dirs.add(path.as_posix())
        else: raise ValueError('unsupported source entry')
    return files,dirs


def preserved_checkpoints(history):
    from verification_storage_maintenance import _plain_directory
    for record in sorted((history/'checkpoints').glob('*.json')):
        cp=read(record);identity=cp.get('checkpointId','')
        if not isinstance(identity,str) or not re.fullmatch(r'C\d+',identity): continue
        source=history/'sources'/identity
        from snapshot_compaction import has_packed_source
        if not _plain_directory(source/'tree') and not has_packed_source(source): continue
        manifest=read(source/'manifest.json');key=cp.get('sourceSnapshotKey')
        if key and manifest.get('type')=='deploom-source-snapshot' and manifest.get('sourceSnapshotKey')==key:
            yield cp,source,manifest


def validate_source(source,key):
    from source_snapshot import SourceCaptureError,open_source_snapshot
    from snapshot_compaction import has_packed_source, validate_packed
    if not (source/'tree').exists() and has_packed_source(source):
        raw = validate_packed(source)
        if raw.get('sourceSnapshotKey') != key: raise ValueError('packed source key mismatch')
        return
    try: open_source_snapshot(source,expected_key=key,allow_build_upgrade=True,timeout_seconds=0)
    except SourceCaptureError as exc:
        if not str(exc).startswith('SOURCE_SNAPSHOT_CONTENT_MISMATCH:'): raise
        from checkpoint_index_recovery import inspect_checkpoint_index_drift
        inspect_checkpoint_index_drift(source,key)


def reason(run,archive,path,category, *, verify_bytes=False,progress=lambda path:None):
    from storage_cleanup_inventory import plain_ancestors,references
    from substrate_identity import tool_build_id
    history=archive or run
    if not plain_ancestors(path): return 'unsafe-path'
    try:
        state=read(history/'run.json')
        if not state.get('runId'): return 'unknown-ownership'
        if archive:
            from storage_cleanup_inventory import archive_reason
            guarded=archive_reason(run,archive,path)
            if guarded: return guarded
        if (history/'trial/agent-lease.json').exists(): return 'agent-session'
        if (run/'restart-archive-transaction.json').exists(): return 'restart-pending'
        if category=='obsolete-upgrade-copy':
            if state.get('phase')!='TERMINAL' or state.get('activeCandidateId') or state.get('activeCandidate'): return 'active-materialization'
            name=path.name
            if not re.fullmatch(r'[a-f0-9]{64}-source',name): return 'unknown-ownership'
            record=read(path.parent/(name[:-7]+'.json'));identity=record['identity']
            identity_key=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if identity_key!=name[:-7]: return 'unknown-ownership'
            if record.get('verification',{}).get('status')!='passed': return 'unknown-ownership'
            if identity.get('toolBuildId')==tool_build_id():
                delivery=history/'delivery-state.json'
                if not delivery.exists() or read(delivery).get('status')!='done': return 'current-verifier-copy'
            matches=[(cp,source,manifest) for cp,source,manifest in preserved_checkpoints(history)
                     if cp['sourceSnapshotKey']==identity.get('sourceSnapshotKey')]
            if not matches: return 'missing-archive-source'
            manifest=read(path/'manifest.json')
            if not record.get('key') or manifest.get('sourceSnapshotKey')!=record['key']: return 'unknown-ownership'
            if verify_bytes:
                if manifest_fingerprint(matches[0][2])!=manifest_fingerprint(manifest): return 'unpreserved-materialization-edits'
                validate_source(matches[0][1],matches[0][0]['sourceSnapshotKey'])
                validate_source(path,record['key'])
            return ''
        if state.get('phase')!='TERMINAL' or state.get('activeCandidateId') or state.get('activeCandidate'):
            return 'active-materialization'
        if (history/'trial/candidate.json').exists() or state.get('bootstrapRefs'):
            return 'active-materialization'
        # Handoffs can pin a private working tree after a completed operation.
        for name in ['repair-handoff.json','restart-archive-transaction.json']:
            record=history/name
            if record.exists() and references(read(record),path): return 'current-run-reference'
        security=history/'security-state.json'
        if security.exists():
            saved=read(security)
            if saved.get('status') not in {'accepted','done'} and references(saved,path): return 'active-materialization'
        if not verify_bytes: return ''
        actual=fingerprint(path,progress)
        for cp,source,manifest in preserved_checkpoints(history):
            if manifest_fingerprint(manifest)==actual:
                validate_source(source,cp['sourceSnapshotKey']);return ''
        return 'unpreserved-materialization-edits'
    except (OSError,ValueError,KeyError,TypeError,RuntimeError): return 'unknown-materialization-evidence'


def locations(run,archive=None):
    history=archive or run
    upgrade=history/'checkpoint-build-upgrades'
    if upgrade.is_dir():
        for path in sorted(upgrade.glob('*-source')):
            if path.is_dir(): yield path,'obsolete-upgrade-copy',reason(run,archive,path,'obsolete-upgrade-copy'),run,archive
    for name in ['trial/workspace','bootstrap/workspace']:
        path=history/name
        if path.is_dir(): yield path,'retired-materialization',reason(run,archive,path,'retired-materialization'),run,archive
    for path in sorted((history/'security').glob('attempt-*/workspace')):
        if path.is_dir(): yield path,'retired-materialization',reason(run,archive,path,'retired-materialization'),run,archive
