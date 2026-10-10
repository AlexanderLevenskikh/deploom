"""Preview-bound, read-only inspection and explicit safe garbage cleanup."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from contextlib import nullcontext
from verification_storage_maintenance import (
    _plain_directory, _trusted_parent, dead_trial_owner, owner_alive,
    TRASH_NAME, MIN_AGE_SECONDS,
)

PRIVATE = re.compile(r'^dependency-flow-(baseline-verify|resolver-seed|same-run-prepared)-[a-z0-9_]+$')
RETIRED_PRIVATE = re.compile(r'^\.deploom-trial-trash-dependency-flow-(resolver-seed|same-run-prepared)-[a-z0-9_]+-\d+-\d+$')
_last_emit = 0.0


def progress(stage, path='', *, processed=0, total=0, files=0, bytes=0, force=False):
    global _last_emit
    now = time.monotonic()
    if not force and now - _last_emit < 0.25:
        return
    _last_emit = now
    payload = dict(stage=stage, path=str(path), processed=processed, total=total,
                   files=files, bytes=bytes,
                   percent=min(99, int(processed * 100 / total)) if total else None)
    if stage == 'done': payload['percent'] = 100
    print('STORAGE_PROGRESS_V1 ' + json.dumps(payload), flush=True)


def plain_ancestors(path):
    return all(_plain_directory(p) for p in [path, *path.parents])


def scan(path, stage='scan', *, identify=True):
    """Never follow junctions; the signature binds every entry and root object."""
    signature = hashlib.sha256(); files = total = 0; unsafe = False
    stack = [Path(path)]
    while stack:
        folder = stack.pop()
        info = folder.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            unsafe = True; continue
        if identify:
            signature.update(f'D:{folder.relative_to(path)}:{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}'.encode())
        with os.scandir(folder) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                # Windows directory listings can return stale timestamps and
                # omit object/link identity. Bind the preview to fresh no-follow
                # metadata, also used when inspecting each directory below.
                info = Path(entry.path).lstat()
                if identify:
                    signature.update(f'E:{Path(entry.path).relative_to(path)}:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_mode}'.encode())
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    unsafe = True
                elif stat.S_ISDIR(info.st_mode): stack.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    files += 1; total += info.st_size
                    if info.st_nlink > 1: unsafe = True
                else: unsafe = True
        progress(stage, folder, files=files, bytes=total)
    return dict(bytes=total, files=files, signature=signature.hexdigest(), unsafe=unsafe)


def eligible_trial(path):
    if not _plain_directory(path): return 'unsafe-path'
    # Registered process-private copies become disposable when their owner exits.
    # Do not retain new multi-GiB materializations for another day after completion.
    if (PRIVATE.fullmatch(path.name) or RETIRED_PRIVATE.fullmatch(path.name)) and dead_trial_owner(path): return ''
    if time.time() - path.stat().st_mtime < MIN_AGE_SECONDS: return 'recent'
    if TRASH_NAME.fullmatch(path.name):
        stamp = int(path.name.rsplit('-', 1)[1]) / 1e9
        return '' if time.time() - stamp >= MIN_AGE_SECONDS else 'recent'
    if PRIVATE.fullmatch(path.name) or RETIRED_PRIVATE.fullmatch(path.name):
        return '' if dead_trial_owner(path) else 'live-or-unknown-owner'
    return 'unknown-ownership'


def references(payload, target):
    # Resolve actual path values: Windows short/long names identify the same tree.
    stack = [payload]; target_resolved = target.resolve()
    variants = {str(target).replace('\\', '/').lower(), str(target_resolved).replace('\\', '/').lower()}
    while stack:
        value = stack.pop()
        if isinstance(value, dict): stack.extend(value.values())
        elif isinstance(value, list): stack.extend(value)
        elif isinstance(value, str):
            normalized = value.replace('\\', '/').lower()
            if any(needle in normalized for needle in variants): return True
            if f'run-archive/{target.name}' in normalized: return True
            if Path(value).is_absolute():
                try:
                    Path(value).resolve().relative_to(target_resolved)
                    return True
                except (OSError, ValueError): pass
    return False


def cache_trash_reason(path, cache):
    if not plain_ancestors(path): return 'unsafe-path'
    if not re.fullmatch(r'[a-z0-9_-]+\.[a-f0-9]{12}\.\d+\.\d+', path.name): return 'unknown-ownership'
    if time.time() - path.stat().st_mtime < MIN_AGE_SECONDS: return 'recent'
    pid, stamp = path.name.rsplit('.', 2)[1:]
    if not 0 < int(pid) < 2**31: return 'unknown-ownership'
    if time.time() - int(stamp) / 1e9 < MIN_AGE_SECONDS: return 'recent'
    if owner_alive(int(pid)): return 'live-or-unknown-owner'
    try:
        if not plain_ancestors(cache / 'index'): return 'unknown-cache-evidence'
        for record in (cache / 'index').glob('*.json'):
            if record.is_symlink(): return 'unknown-cache-evidence'
            if references(json.loads(record.read_text(encoding='utf-8-sig')), path): return 'published-cache-reference'
        return ''
    except (OSError, ValueError): return 'unknown-cache-evidence'


def archive_reason(run, archive, path):
    # Sources, reports, metadata and last verified checkpoints are never candidates.
    if not plain_ancestors(path): return 'unsafe-path'
    if not (archive / 'run.json').is_file(): return 'unknown-archive'
    if (run / 'restart-archive-transaction.json').exists(): return 'restart-pending'
    if time.time() - archive.stat().st_mtime < MIN_AGE_SECONDS: return 'recent'
    try:
        saved = json.loads((archive / 'run.json').read_text(encoding='utf-8-sig'))
        if not saved.get('runId'): return 'unknown-archive'
        if saved.get('phase') != 'TERMINAL' or saved.get('activeCandidate'):
            return 'unfinished-archive'
        checkpoints = [json.loads(p.read_text(encoding='utf-8-sig')) for p in (archive / 'checkpoints').glob('*.json')]
        verified = [c for c in checkpoints if c.get('status') == 'VERIFIED']
        if not verified: return 'no-verified-archive-checkpoint'
        # A trial can be the sole surviving copy after corruption. Preserve it
        # unless every archived verified checkpoint still has its sealed source.
        for checkpoint in verified:
            key = checkpoint.get('checkpointId', '')
            if not isinstance(key, str) or not re.fullmatch(r'C\d+', key): return 'missing-archive-source'
            source = archive / 'sources' / key
            from snapshot_compaction import has_packed_source
            if not plain_ancestors(source / 'tree') and not (plain_ancestors(source) and has_packed_source(source)):
                return 'missing-archive-source'
            source_key = checkpoint.get('sourceSnapshotKey')
            if not isinstance(source_key, str) or not source_key: return 'missing-archive-source'
            manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8-sig'))
            if manifest.get('type') != 'deploom-source-snapshot' or manifest.get('sourceSnapshotKey') != source_key:
                return 'missing-archive-source'
        if (archive / 'trial' / 'agent-lease.json').exists(): return 'agent-session'
        if (archive / 'trial' / 'candidate.json').exists(): return 'archived-candidate-work'
        # All current metadata is protected; references to this archive block it.
        metadata = list(run.glob('*.json')) + list((run / 'checkpoints').glob('*.json'))
        metadata += list((run / 'trial').glob('*.json')) + list((run / 'security').glob('*.json'))
        metadata += list((run / 'security').glob('attempt-*/*.json')) + list((run / 'delivery').glob('*.json'))
        for p in metadata:
            if references(json.loads(p.read_text(encoding='utf-8-sig')), archive): return 'current-run-reference'
        return ''
    except (OSError, ValueError, TypeError): return 'unknown-archive-evidence'


def materialization_children(run, archive, parent, category, reason):
    from materialization_retention import locations as owned_locations
    owned = {str(p):(kind,why,r,a) for p,kind,why,r,a in owned_locations(run,archive)}
    from snapshot_compaction import locations as compact_locations
    for p,kind,why,r,a in compact_locations(run,archive):
        if str(p) in owned and owned[str(p)][1]: owned[str(p)] = (kind,why,r,a)
    def children(folder):
        for child in sorted(folder.iterdir()):
            if not _plain_directory(child): continue
            entry=owned.get(str(child))
            if entry:
                kind,why,r,a=entry; yield child,kind,why,r,a
            elif folder.name=='security' and child.name.startswith('attempt-'):
                yield from children(child)
            else: yield child,category,reason,None,None
    yield from children(parent)


def locations(root, workspaces):
    if root is not None and _trusted_parent(root):
        for p in sorted((root / 'trials').iterdir()):
            if _plain_directory(p): yield p, 'verification-trials', eligible_trial(p), None, None
        for p in root.iterdir():
            if p.name == 'trials' or not _plain_directory(p): continue
            if p.name == 'baseline-prepared-artifacts' and _plain_directory(p / 'trash'):
                for child in p.iterdir():
                    if child.name == 'trash':
                        for trash in sorted(child.iterdir()):
                            if _plain_directory(trash): yield trash, 'prepared-trash', cache_trash_reason(trash, p), None, None
                    elif _plain_directory(child): yield child, 'verification-cache', 'shared-cache', None, None
            else: yield p, 'verification-cache', 'shared-cache', None, None
    for workspace in dict.fromkeys(workspaces):
        objects = Path(workspace) / '.dependency-roadmap' / 'snapshot-objects'
        if objects.is_dir() and plain_ancestors(objects):
            yield objects, 'snapshot-objects', 'shared-snapshot-objects', None, None
        base = Path(workspace) / '.dependency-roadmap' / 'iterative'
        if not base.is_dir() or not plain_ancestors(base): continue
        for run in sorted(base.iterdir()):
            if not _plain_directory(run): continue
            if run.name == 'deliveries':
                yield run, 'delivery', 'delivery-branch', None, None; continue
            for p in sorted(run.iterdir()):
                if not _plain_directory(p): continue
                if p.name != 'run-archive':
                    if p.name in {'trial','bootstrap','security','checkpoint-build-upgrades'}:
                        yield from materialization_children(run,None,p,'current-run','current-checkpoint-or-run-data')
                    elif p.name == 'sources':
                        from snapshot_compaction import source_locations
                        yield from source_locations(run)
                    else: yield p, 'current-run', 'current-checkpoint-or-run-data', None, None
                    continue
                for archive in sorted(p.iterdir()):
                    if not _plain_directory(archive): continue
                    for child in sorted(archive.iterdir()):
                        if not _plain_directory(child): continue
                        if child.name in {'trial','bootstrap','security','checkpoint-build-upgrades'}:
                            yield from materialization_children(run,archive,child,'archive-history','archive-checkpoints-and-evidence')
                        elif child.name == 'sources':
                            from snapshot_compaction import source_locations
                            yield from source_locations(run, archive)
                        else:
                            yield child, 'archive-history', 'archive-checkpoints-and-evidence', None, None


def inspect(root, workspaces, *, eligible_only=False):
    items = []; volumes = {}
    for path, category, reason, run, archive in locations(root, workspaces):
        if eligible_only and reason: continue
        try:
            if not reason and category in {'obsolete-upgrade-copy','retired-materialization'}:
                from materialization_retention import reason as retention_reason
                reason = retention_reason(run,archive,path,category,verify_bytes=True,
                                          progress=lambda p: progress('validate',p))
            if not reason and category == 'packed-tree-garbage':
                from snapshot_compaction import residue_reason
                reason = residue_reason(path.parent, path, verify_bytes=True)
            # Validation can refresh Git metadata. Bind the preview only after
            # preservation checks; later changes still fail closed in clean().
            measure = scan(path, identify=not reason)
            if not plain_ancestors(path) or measure['unsafe']: reason = 'links-or-shared-files'
            if eligible_only and reason: continue
            item = dict(path=str(path), category=category, reason=reason, eligible=not reason, **measure)
            if run: item.update(run=str(run), archive=str(archive) if archive else None)
            items.append(item)
            drive = path.anchor
            volumes[drive] = dict(path=drive, freeBytes=shutil.disk_usage(path).free)
        except OSError as exc:
            items.append(dict(path=str(path), category=category, reason='unreadable', eligible=False,
                              bytes=0, files=0, signature='', unsafe=True, error=type(exc).__name__))
    progress('done', force=True)
    return dict(root=str(root) if root is not None else '', items=items, volumes=list(volumes.values()),
                eligible=sum(i['eligible'] for i in items),
                eligibleBytes=sum(i['bytes'] for i in items if i['eligible']),
                protectedBytes=sum(i['bytes'] for i in items if not i['eligible']),
                removed=0, failed=0, protected=sum(not i['eligible'] for i in items), reclaimedBytes=0)


def remove_tree(path, advance):
    # Do not chmod until the complete scan has excluded shared files and links.
    for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            file = Path(current) / name
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise OSError('tree changed during cleanup')
            file.chmod(info.st_mode | stat.S_IWRITE); file.unlink(); advance(info.st_size)
        for name in dirs:
            child = Path(current) / name
            if not _plain_directory(child): raise OSError('tree changed during cleanup')
            child.chmod(child.stat().st_mode | stat.S_IWRITE); child.rmdir()
    path.chmod(path.stat().st_mode | stat.S_IWRITE); path.rmdir()


def clean(root, workspaces, plan):
    allowed = {str(p): (reason, run, archive) for p, _, reason, run, archive in locations(root, workspaces)}
    selected = [i for i in plan.get('items', []) if i.get('eligible')]
    total = sum(i['files'] - (1 if i['category'] == 'snapshot-compaction' else 0) for i in selected)
    processed = freed = removed = failed = protected = compacted = 0
    results = []
    for item in selected:
        path = Path(item['path']); entry = allowed.get(str(path)); reason = 'scope-changed'
        def advance(size):
            nonlocal processed, freed
            processed += 1; freed += size
            progress('clean', path, processed=processed, total=total, bytes=freed)
        if entry is not None:
            reason, run, archive = entry
            lock = nullcontext()
            if run:
                from iterative_migration import _RunLock
                class Lock:
                    def __enter__(self): self.lease = _RunLock(run, 'storage-maintenance'); self.lease.acquire()
                    def __exit__(self, *args): self.lease.release()
                lock = Lock()
            try:
                with lock:
                    if run and item['category'] in {'obsolete-upgrade-copy','retired-materialization'}:
                        from materialization_retention import reason as retention_reason
                        reason=retention_reason(run,archive,path,item['category'],verify_bytes=True,
                                                progress=lambda p: progress('validate',p))
                    elif run and item['category'] == 'packed-tree-garbage':
                        from snapshot_compaction import residue_reason
                        reason = residue_reason(path.parent, path, verify_bytes=True)
                    elif run and item['category'] == 'snapshot-compaction':
                        from snapshot_compaction import locations as compact_locations
                        reason = '' if any(p == path for p, *_ in compact_locations(run, archive)) else 'scope-changed'
                    elif run: reason = archive_reason(run, archive, path)
                    elif root is not None and path.parent == root / 'baseline-prepared-artifacts' / 'trash':
                        reason = cache_trash_reason(path, path.parent.parent)
                    else: reason = eligible_trial(path)
                    if not reason and plain_ancestors(path):
                        measure = scan(path, 'validate')
                        if measure['unsafe']: reason = 'links-or-shared-files'
                        elif measure['signature'] != item['signature']: reason = 'changed-after-preview'
                        else:
                            progress('clean', path, processed=processed, total=total, force=True)
                            if item['category'] == 'snapshot-compaction':
                                from snapshot_compaction import compact
                                before, before_processed = freed, processed
                                def packing(done, file_total):
                                    nonlocal processed
                                    processed = before_processed + done
                                    progress('clean', path, processed=processed, total=total, bytes=before)
                                def packed_removed(size):
                                    nonlocal freed
                                    freed += size
                                    progress('clean', path, processed=processed, total=total, bytes=freed)
                                reclaimed = compact(path, packed_removed, lambda p: progress('clean', p, processed=processed, total=total, bytes=freed), packing)
                                freed = before + reclaimed
                                compacted += 1
                                results.append(dict(path=str(path), status='compacted'))
                            else:
                                remove_tree(path, advance); removed += 1
                                results.append(dict(path=str(path), status='removed'))
                            continue
            except Exception as exc:
                # A failed compaction may have allocated objects before a
                # partial removal. Never advertise those bytes as reclaimed.
                if item['category'] == 'snapshot-compaction': freed = 0
                error_reason = 'disk-space-low' if str(exc).startswith('DISK_SPACE_LOW:') else type(exc).__name__
                failed += 1; results.append(dict(path=str(path), status='failed', reason=error_reason))
                continue
        protected += 1; results.append(dict(path=str(path), status='protected', reason=reason))
    progress('done', processed=processed, total=total, bytes=freed, force=True)
    volumes = []
    for old in plan.get('volumes', []):
        try: volumes.append(dict(path=old['path'], freeBytes=shutil.disk_usage(old['path']).free))
        except OSError: pass
    return {**plan, 'removed': removed, 'compacted': compacted, 'failed': failed, 'protected': protected,
            'reclaimedBytes': freed, 'volumes': volumes, 'results': results}


def main():
    from block_vex_storage import verification_storage_profile
    profile = verification_storage_profile()
    payload = json.load(sys.stdin)
    root = profile.root.absolute() if profile.root is not None else None
    workspaces = payload.get('workspaces', [])
    result = inspect(root, workspaces, eligible_only=bool(payload.get('eligibleOnly'))) if sys.argv[1] == 'inspect' else clean(root, workspaces, payload['plan'])
    print('STORAGE_RESULT_V1 ' + json.dumps(result), flush=True)

if __name__ == '__main__': main()
