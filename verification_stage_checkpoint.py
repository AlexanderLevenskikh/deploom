"""Write-ahead stage records and source-bound command PASS receipts.

Receipts are eligible only after the authoritative verifier has checked its
prepared artifact, supervised command and guards. They never replace these
preconditions and never turn failed/interrupted work into success.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import sys
import time
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def write_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with tmp.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 9:
                    raise
                time.sleep(min(.02 * 2**attempt, .2))
    finally:
        tmp.unlink(missing_ok=True)


class StageJournal:
    def __init__(self, directory, identity):
        self.directory = Path(directory)
        self.identity = identity

    def path(self, stage):
        return self.directory / (digest({'identity': self.identity, 'stage': stage}) + '.json')

    def read(self, stage):
        try:
            row = json.loads(self.path(stage).read_text(encoding='utf-8'))
            checksum = row.pop('digest')
            if checksum == digest(row) and row.get('schema') == 1 and row.get('identity') == self.identity and row.get('stage') == stage:
                return row
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return None

    def write(self, stage, status, evidence=None):
        previous = self.read(stage) or {}
        attempts = int(previous.get('attempts') or 0) + (status == 'RUNNING')
        history = list(previous.get('history') or [])
        if previous:
            history.append({'status': previous.get('status'), 'at': previous.get('updatedAt'), 'attempt': previous.get('attempts')})
        row = {'schema': 1, 'authority': 'STAGE_EVIDENCE', 'identity': self.identity, 'stage': stage,
               'status': status, 'attempts': attempts, 'history': history[-256:],
               'updatedAt': time.time(), 'evidence': evidence or {}}
        row['digest'] = digest(row)
        write_atomic(self.path(stage), row)
        return str(self.path(stage))

    def pass_record(self, stage):
        row = self.read(stage)
        return row if row and row.get('status') == 'PASS' else None


def _tokens(command):
    return [part.strip('"\'') for part in shlex.split(command, posix=os.name != 'nt')]


def command_tool_closure(command, project, environment, manager_root=None):
    """Conservative Node command closure; unknown external tools are replayed.

    Scripts, configuration, local binaries and package code are covered by the
    sealed prepared tree. Node, the manager implementation and shell are hashed
    separately; every environment value participates through an opaque digest.
    """
    from migration_validation import internal_command_argv
    project = Path(project)
    manager_root = Path(manager_root or project)
    internal = internal_command_argv(command)
    adapters = []
    if internal:
        command = internal[internal.index('--command') + 1]
        adapters = [str(Path(__file__).with_name('migration_validation.py')), sys.executable]
    try:
        tokens = _tokens(command)
    except ValueError:
        return None
    if not tokens or any(mark in command for mark in ("&&", "||", ";", "|", "`", "$(", ">", "<")):
        return None
    manager = Path(tokens[0]).stem.lower()
    if manager not in {'node', 'npm', 'yarn', 'pnpm'}:
        return None
    # A declared npm/Yarn script cannot hide an unknown external CLI in a
    # pre/post script. Node/package binaries come from the sealed dependency tree.
    seen = set()
    def known_script(name):
        if name in seen:
            return False
        seen.add(name)
        try:
            manifest = json.loads((project / 'package.json').read_text(encoding='utf-8-sig'))
            scripts = manifest.get('scripts') or {}
            body = scripts[name]
            for script in (scripts.get('pre' + name, ''), body, scripts.get('post' + name, '')):
                for fragment in script.replace('&&', ';').replace('||', ';').split(';'):
                    parts = _tokens(fragment)
                    if not parts:
                        continue
                    executable = parts[0]
                    if executable == 'node':
                        continue
                    if executable in {'npm', 'yarn', 'pnpm'}:
                        rest = parts[1:]
                        if rest and rest[0] == 'run':
                            rest = rest[1:]
                        if not rest or not known_script(rest[0]):
                            return False
                        continue
                    if any((root / 'node_modules' / '.bin' / (executable + suffix)).is_file()
                           for root in (project, *project.parents) if root.is_relative_to(manager_root)
                           for suffix in ('', '.cmd', '.ps1')):
                        continue
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if manager != 'node':
        rest = tokens[1:]
        if rest and rest[0] == 'run':
            rest = rest[1:]
        if not rest or not known_script(rest[0]):
            # Direct known local manager executors such as `yarn vitest`.
            if manager not in {'yarn', 'pnpm'} or not rest or not any(
                (root / 'node_modules' / '.bin' / (rest[0] + suffix)).is_file()
                for root in (project, manager_root) for suffix in ('', '.cmd')):
                return None
    files = set(Path(item) for item in adapters)
    search_path = environment.get('PATH', os.environ.get('PATH'))
    for tool in {'node', manager, 'npm', 'yarn', 'pnpm'}:
        executable = shutil.which(tool, path=search_path)
        if not executable:
            if tool in {'node', manager}:
                return None
            continue
        path = Path(executable).resolve()
        files.add(path)
        if tool != 'node':
            roots = {path.parent / 'node_modules' / tool, path.parent / 'node_modules' / 'corepack'}
            for ancestor in path.parents:
                manifest = ancestor / 'package.json'
                if manifest.is_file():
                    try:
                        if json.loads(manifest.read_text(encoding='utf-8-sig')).get('name') in {tool, 'corepack'}:
                            roots.add(ancestor)
                            break
                    except (OSError, ValueError, AttributeError):
                        return None
            for root in roots:
                if root.is_dir():
                    files.update(p for p in root.rglob('*') if p.is_file() and not p.is_symlink())
    if os.name == 'nt':
        shell = environment.get('COMSPEC') or os.environ.get('COMSPEC')
        if not shell or not Path(shell).is_file():
            return None
        files.add(Path(shell))
    try:
        hashes = {str(path): file_digest(path) for path in sorted(files, key=str)}
    except OSError:
        return None
    return digest({'files': hashes, 'platform': platform.platform(), 'environment': dict(environment)})


def adapter_artifacts(command):
    from migration_validation import internal_command_argv
    argv = internal_command_argv(command)
    paths = []
    if argv:
        for option in ('--baseline', '--observation'):
            if option in argv:
                paths.append(Path(argv[argv.index(option) + 1]))
    return {str(path): file_digest(path) for path in paths}


class CommandCheckpoint:
    def __init__(self, directory, proof_identity, prepared, commands, environment):
        self.directory = Path(directory)
        self.proof = proof_identity.event_fields()
        self.prepared = prepared
        self.commands = list(commands)
        self.environment = dict(environment)
        self.inflight = {}

    def identity(self, index, command):
        if not self.prepared:
            return None
        root = Path(self.prepared['workspaceRoot'])
        project = root / self.prepared['projectRelative']
        closure = command_tool_closure(command, project, self.environment, root)
        if not closure:
            return None
        return {'proof': self.proof, 'preparedArtifactContentKey': self.prepared['artifactContentKey'],
                'commandPrefix': self.commands[:index], 'command': command, 'toolClosure': closure}

    def journal(self, index, command):
        identity = self.identity(index, command)
        return StageJournal(self.directory, identity) if identity else None

    def lookup(self, index, command):
        journal = self.journal(index, command)
        row = journal.pass_record('project-command') if journal else None
        if not row:
            return None
        evidence = row.get('evidence') or {}
        if evidence.get('exitCode') != 0 or evidence.get('supervision') != 'guaranteed-tree':
            return None
        try:
            if evidence.get('adapterArtifacts', {}) != adapter_artifacts(command):
                return None
        except (OSError, ValueError):
            return None
        return str(journal.path('project-command'))

    def begin(self, index, command):
        journal = self.journal(index, command)
        if journal:
            self.inflight[(index, command)] = journal
            return journal.write('project-command', 'RUNNING', {'commandIndex': index})
        return ''

    def complete(self, index, command, result, safe):
        journal = self.inflight.pop((index, command), None)
        if journal is None:
            return ''
        current_identity = self.identity(index, command)
        safe = safe and current_identity == journal.identity
        status = 'PASS' if safe and result.returncode == 0 else 'FAIL' if result.returncode > 0 else 'UNKNOWN'
        if current_identity != journal.identity:
            status = 'UNKNOWN'
        try:
            artifacts = adapter_artifacts(command) if status == 'PASS' else {}
        except (OSError, ValueError):
            status, artifacts = 'UNKNOWN', {}
        supervision = str(getattr(getattr(result, 'supervision', None), 'quality', ''))
        if status == 'PASS' and supervision != 'guaranteed-tree':
            status = 'UNKNOWN'
        evidence = {'exitCode': result.returncode, 'supervision': supervision,
                    'output': str(result.stdout or '')[-131072:], 'adapterArtifacts': artifacts,
                    'preparedArtifact': str(self.prepared['workspaceRoot'])}
        path = journal.write('project-command', status, evidence)
        return path if status == 'PASS' else ''

def capture_recovery_source(project, directory, *, preferred_destination=None, timeout_seconds=1800, progress=None):
    """Resume sealed source capture while preserving the ordinary live guard."""
    import dataclasses
    from source_snapshot import (capture_durable_source_snapshot, open_source_snapshot,
        source_snapshot_fingerprint, source_snapshot_live_source_continuity, SourceCaptureError, _subject_layout)
    from substrate_identity import tool_build_id
    project = Path(project).resolve()
    directory = Path(directory).resolve()
    journal = StageJournal(directory / 'records', {'project': str(project), 'toolBuildId': tool_build_id(),
        'sourceScope': os.environ.get('DEPLOOM_SOURCE_SCOPE_MODE', 'repository')})
    previous = journal.pass_record('source-sealed')
    root, relative, head = _subject_layout(project)
    def bind_live(snapshot):
        return dataclasses.replace(snapshot, original_project_path=project, capture_root=root,
            project_relative=relative, project_path=snapshot.root / relative,
            git_root=str(root) if head else '', durable_recovery=True)
    if previous:
        evidence = previous.get('evidence') or {}
        container = Path(str(evidence.get('container') or '')).resolve()
        owned = container.is_relative_to(directory) or (preferred_destination is not None and container == Path(preferred_destination).resolve())
        if owned and container.is_dir():
            try:
                if progress:
                    progress('Validating preserved sealed source against current source')
                live_key = source_snapshot_fingerprint(project)
                if live_key == evidence.get('key'):
                    snapshot = bind_live(open_source_snapshot(container, expected_key=live_key, timeout_seconds=timeout_seconds, progress=progress))
                    if source_snapshot_live_source_continuity(snapshot, timeout_seconds=timeout_seconds, progress=progress):
                        if progress:
                            progress('SourceSnapshot checkpoint HIT; sealed source copy reused')
                        return snapshot, True
            except (SourceCaptureError, OSError):
                pass
    journal.write('source-sealed', 'RUNNING')
    destination = Path(preferred_destination).resolve() if preferred_destination else directory / 'sources' / uuid.uuid4().hex
    if destination.exists():
        # Keep historical evidence and incomplete captures; never overwrite them.
        destination = directory / 'sources' / uuid.uuid4().hex
    snapshot = bind_live(capture_durable_source_snapshot(project, destination, timeout_seconds=timeout_seconds, progress=progress))
    if not source_snapshot_live_source_continuity(snapshot, timeout_seconds=timeout_seconds, progress=progress):
        raise SourceCaptureError('SOURCE_CAPTURE_UNSTABLE: live inputs changed during durable publication')
    journal.write('source-sealed', 'PASS', {'container': str(snapshot.container), 'key': snapshot.key})
    return snapshot, False
