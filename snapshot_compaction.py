"""Lossless cold storage for sealed migration snapshots; never shared writable files."""
from __future__ import annotations
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

MARKER = "packed-source.json"
SCHEMA = "deploom-packed-source-v1"
MIN_BYTES = 1024 * 1024


def store_root(container):
    for parent in Path(container).resolve().parents:
        if parent.name == ".dependency-roadmap":
            return parent / "snapshot-objects"
    raise ValueError("snapshot outside owned workspace")


def plain(path, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("storage link")
    if directory:
        if not stat.S_ISDIR(info.st_mode): raise ValueError("storage directory missing")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("storage file is shared or special")
    return info


def ancestors(path):
    for parent in [path, *path.parents]: plain(parent, True)


def manifest(container):
    plain(container / "manifest.json")
    data = (container / "manifest.json").read_bytes()
    raw = json.loads(data)
    if raw.get("type") != "deploom-source-snapshot" or raw.get("schemaVersion") != 1:
        raise ValueError("unsupported snapshot")
    seen = set()
    for entry in raw["entries"]:
        name = entry["path"]
        relative = Path(name)
        if not name or relative.is_absolute() or relative.drive or ".." in relative.parts or any(":" in part for part in relative.parts) or name in seen:
            raise ValueError("unsafe snapshot member")
        seen.add(name)
        if entry["kind"] == "file":
            if not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"]) or not isinstance(entry["size"], int) or entry["size"] < 0:
                raise ValueError("invalid object identity")
        elif entry["kind"] != "directory": raise ValueError("unsupported snapshot member")
    return raw, hashlib.sha256(data).hexdigest()


def object_path(store, digest):
    return store / digest[:2] / (digest + ".gz")


def read_object(store, entry, output=None):
    path = object_path(store, entry["sha256"])
    ancestors(path.parent); plain(path)
    digest = hashlib.sha256(); size = 0
    with gzip.open(path, "rb") as source:
        while chunk := source.read(min(1024 * 1024, entry["size"] - size + 1)):
            size += len(chunk)
            if size > entry["size"]: raise ValueError("object size mismatch")
            digest.update(chunk)
            if output is not None: output.write(chunk)
    if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
        raise ValueError("object content mismatch")


def has_packed_source(container):
    return (Path(container) / MARKER).is_file()


def validate_packed(container, progress=lambda path: None):
    container = Path(container); ancestors(container)
    raw, identity = manifest(container)
    plain(container / MARKER)
    marker = json.loads((container / MARKER).read_bytes())
    if marker != {"schema": SCHEMA, "manifestSha256": identity}:
        raise ValueError("packed manifest changed")
    store = store_root(container)
    for entry in raw["entries"]:
        if entry["kind"] == "file": read_object(store, entry)
        progress(container / entry["path"])
    from source_snapshot import _canonical_hash, SOURCE_SNAPSHOT_SCHEMA, SourceInputPolicy
    if raw.get('policyKey') != SourceInputPolicy().key: raise ValueError('unsupported source policy')
    producer = raw.get('toolBuildId')
    if not isinstance(producer, str) or not producer: raise ValueError('missing producer identity')
    manifest_key = _canonical_hash({'schema': SOURCE_SNAPSHOT_SCHEMA, 'toolBuildId': producer,
                                   'policyKey': raw['policyKey'], 'entries': raw['entries']}, length=64)
    relative = Path(str(raw.get('projectRelative') or '.'))
    if relative.is_absolute() or relative.drive or '..' in relative.parts: raise ValueError('unsafe project path')
    key = _canonical_hash({'schema': SOURCE_SNAPSHOT_SCHEMA, 'toolBuildId': producer,
                          'policyKey': raw['policyKey'], 'manifestKey': manifest_key,
                          'projectRelative': relative.as_posix() or '.'}, length=32)
    if raw.get('manifestKey') != manifest_key or raw.get('sourceSnapshotKey') != key:
        raise ValueError('packed source identity mismatch')
    return raw


def restore(container, progress=lambda path: None):
    """Rebuild private bytes in place, then the normal source verifier hashes them."""
    container = Path(container)
    if (container / "tree").exists(): return
    raw = validate_packed(container, progress)
    from storage_capacity import require_capacity
    require_capacity(container, sum(e.get("size", 0) for e in raw["entries"]), "snapshot restore")
    stage = Path(tempfile.mkdtemp(prefix=".restore-", dir=container))
    try:
        for entry in raw["entries"]:
            target = stage / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry["kind"] == "directory": target.mkdir(exist_ok=True)
            else:
                with target.open("xb") as output: read_object(store_root(container), entry, output)
                target.chmod(0o555 if entry.get("executable") else 0o444)
            progress(target)
        # Never overwrite a tree that appeared concurrently.
        if (container / "tree").exists(): raise ValueError("snapshot restored concurrently")
        from prepared_workspace_fastpath import apply_tree_write_protection
        apply_tree_write_protection(stage, readonly=True)
        os.rename(stage, container / "tree")
    finally:
        if stage.exists():
            from source_snapshot import _force_rmtree
            _force_rmtree(stage)


def compact(container, advance=lambda size: None, progress=lambda path: None, packed_progress=lambda done, total: None):
    """Publish verified objects and manifest receipt BEFORE retiring original bytes."""
    from source_snapshot import open_source_snapshot
    from storage_cleanup_inventory import scan, remove_tree
    container = Path(container); ancestors(container)
    raw, identity = manifest(container)
    root = container / "tree"
    measure = scan(root, "validate")
    if measure["unsafe"]: raise ValueError("snapshot links or shared files")
    # Exact membership prevents dropping an extra user file omitted by source policy.
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*")}
    if actual != {e["path"] for e in raw["entries"]}: raise ValueError("unregistered snapshot files")
    open_source_snapshot(container, expected_key=raw["sourceSnapshotKey"], allow_build_upgrade=True, timeout_seconds=0)
    store = store_root(container); store.mkdir(exist_ok=True); ancestors(store)
    added = done = 0
    file_total = sum(e["kind"] == "file" for e in raw["entries"])
    for entry in raw["entries"]:
        if entry["kind"] != "file": continue
        path = object_path(store, entry["sha256"])
        path.parent.mkdir(exist_ok=True); ancestors(path.parent)
        if path.exists(): read_object(store, entry)
        else:
            from storage_capacity import require_capacity
            require_capacity(path, entry["size"], "snapshot object")
            fd, temp = tempfile.mkstemp(prefix=".object-", dir=path.parent)
            temporary = Path(temp)
            try:
                with os.fdopen(fd, "wb") as output, gzip.GzipFile(fileobj=output, mode="wb", mtime=0, compresslevel=1) as compressed:
                    with (root / entry["path"]).open("rb") as source: shutil.copyfileobj(source, compressed, 1024 * 1024)
                # Validate the new compressed object before publication.
                with gzip.open(temporary, "rb") as source:
                    h = hashlib.sha256(); size = 0
                    while chunk := source.read(1024 * 1024): h.update(chunk); size += len(chunk)
                if h.hexdigest() != entry["sha256"] or size != entry["size"]: raise ValueError("source changed while packing")
                if path.exists(): read_object(store, entry)
                else:
                    os.rename(temporary, path); added += path.stat().st_size
                    path.chmod(0o444)
            finally:
                temporary.unlink(missing_ok=True)
        done += 1
        packed_progress(done, file_total)
        progress(root / entry["path"])
    receipt = {"schema": SCHEMA, "manifestSha256": identity}
    temporary = container / (MARKER + ".tmp")
    if temporary.exists(): raise ValueError("packing transaction pending")
    with temporary.open("x", encoding="utf-8") as output: json.dump(receipt, output)
    os.replace(temporary, container / MARKER)
    validate_packed(container, progress)
    if scan(root, "validate")["signature"] != measure["signature"]:
        raise ValueError("snapshot changed during packing")
    retired = container / ".packed-tree"
    if retired.exists(): raise ValueError("packing transaction pending")
    os.rename(root, retired)
    # Receipt and all verified objects survive an interrupted removal.
    remove_tree(retired, advance)
    return max(0, measure["bytes"] - added)


def locations(run, archive=None):
    """Keep the current usable checkpoint hot; retain all historical bytes cold."""
    history = archive or run
    try:
        state = json.loads((run / "run.json").read_text(encoding="utf-8-sig"))
        if state.get("activeCandidateId") or state.get("activeCandidate") or state.get("bootstrapRefs"): return
        if not state.get('runId') or state.get("phase") not in {"READY", "TERMINAL"}: return
        if (run / "trial/agent-lease.json").exists() or (run / "restart-archive-transaction.json").exists(): return
        if (history/'trial/agent-lease.json').exists(): return
        agent = history/'delivery-agent.json'
        if agent.exists() and json.loads(agent.read_text(encoding='utf-8-sig')).get('status') == 'running': return
        hot = str(state.get("activeCheckpointId") or "")
        containers = [(p, 'checkpoint') for p in sorted((history / "sources").glob("C*"))]
        containers += [(p, 'upgrade') for p in sorted((history / "checkpoint-build-upgrades").glob("*-source"))]
        for container, kind in containers:
            if kind == 'checkpoint' and (not re.fullmatch(r"C\d+", container.name) or (archive is None and container.name == hot)): continue
            if kind == 'upgrade' and not re.fullmatch(r"[a-f0-9]{64}-source", container.name): continue
            if not (container / "tree").is_dir(): continue
            try:
                raw, _ = manifest(container)
                size = sum(e.get("size", 0) for e in raw["entries"])
                if size >= MIN_BYTES: yield container, "snapshot-compaction", "", run, archive
            except (OSError, ValueError, KeyError, TypeError): continue
    except (OSError, ValueError, TypeError): return


def compact_superseded(run, progress=lambda path: None):
    """Called after an atomic accepted-pointer switch, before the next cohort."""
    result = {'compacted': 0, 'reclaimedBytes': 0, 'errors': []}
    for container, *unused in locations(Path(run)):
        # Verifier upgrades have their own proof/receipt lifecycle.
        if container.parent.name != 'sources': continue
        try:
            result['reclaimedBytes'] += compact(container, progress=progress)
            result['compacted'] += 1
        except Exception as exc:
            result['errors'].append({'path': str(container), 'error': str(exc)[:500]})
    return result


def residue_reason(container, path, *, verify_bytes=False):
    """A crashed packing/restoration transaction may leave only verified bytes."""
    try:
        container, path = Path(container), Path(path)
        ancestors(path)
        if path.parent != container or not (path.name == '.packed-tree' or re.fullmatch(r'\.restore-[a-z0-9_]+', path.name)):
            return 'unknown-ownership'
        raw, _ = manifest(container)
        if not has_packed_source(container): return 'missing-packed-source'
        if not verify_bytes: return ''
        validate_packed(container)
        entries = {e['path']: e for e in raw['entries']}
        for directory, children, files in os.walk(path, followlinks=False):
            plain(Path(directory), True)
            for name in children + files:
                member = Path(directory) / name
                relative = member.relative_to(path).as_posix()
                expected = entries.get(relative)
                if expected is None: return 'unpreserved-materialization-edits'
                info = plain(member, expected['kind'] == 'directory')
                if expected['kind'] == 'file':
                    h = hashlib.sha256()
                    with member.open('rb') as stream:
                        while chunk := stream.read(1024 * 1024): h.update(chunk)
                    if info.st_size != expected['size'] or h.hexdigest() != expected['sha256']:
                        return 'unpreserved-materialization-edits'
        return ''
    except (OSError, ValueError, KeyError, TypeError): return 'unknown-materialization-evidence'


def source_locations(run, archive=None):
    history = archive or run
    compactable = {str(entry[0]): entry for entry in locations(run, archive)}
    for source in sorted((history / 'sources').iterdir()):
        from verification_storage_maintenance import _plain_directory
        if not _plain_directory(source): continue
        residues = [p for p in source.iterdir() if p.name == '.packed-tree' or p.name.startswith('.restore-')]
        if residues:
            for residue in residues:
                if _plain_directory(residue):
                    yield residue, 'packed-tree-garbage', residue_reason(source, residue), run, archive
            # Recompaction must wait until an interrupted transaction is closed.
        else:
            yield compactable.get(str(source), (source, 'current-run' if archive is None else 'archive-history',
                  'current-checkpoint-or-run-data' if archive is None else 'archive-checkpoints-and-evidence', None, None))
