"""Write-ahead publication of one controller metadata transition.

Filesystem artifacts and proof records remain at their owning boundaries. This
journal only makes the candidate/ledger/run/repair-request pointer writes
recoverable, preserving the previous incumbent and spent budgets.
"""
import contextvars
import copy
import json
from pathlib import Path

from verification_stage_checkpoint import digest, write_atomic, file_digest, command_tool_closure
from substrate_identity import tool_build_id

TRANSACTION = 'iteration-transition.json'
_ACTIVE = contextvars.ContextVar('iteration_state_transaction', default=None)
_MISSING = object()


def staged_read(path):
    current = _ACTIVE.get()
    value = current.writes.get(str(Path(path).resolve()), _MISSING) if current else _MISSING
    return value is not _MISSING, copy.deepcopy(value) if value is not _MISSING else None


def stage_write(path, value):
    current = _ACTIVE.get()
    if current is None:
        return False
    path = Path(path).resolve()
    if not path.is_relative_to(current.root) or path.name == TRANSACTION:
        return False
    current.writes[str(path)] = copy.deepcopy(value)
    return True


def state_context(root, run, config, candidate):
    import os
    from iterative_stage_checkpoint import trial_key, operation_environment
    environment = operation_environment(config)
    trial = (Path(root) / 'trial' / 'workspace').resolve()
    cp = Path(root) / 'checkpoints' / (str(candidate['baseCheckpointId']) + '.json')
    base = json.loads(cp.read_text(encoding='utf-8-sig')) if cp.exists() else {}
    return {'runId': run['runId'], 'configHash': digest(config), 'toolBuildId': tool_build_id(),
            'environmentHash': digest(environment),
            'runtimeClosure': command_tool_closure('node', Path(config['projectDir']), environment),
            'baseCheckpointId': candidate['baseCheckpointId'],
            'baseSourceKey': base.get('sourceSnapshotKey'), 'candidateId': candidate['candidateId'],
            'trialRoot': str(trial), 'trialKey': trial_key(trial) if trial.is_dir() else ''}


class StateTransaction:
    def __init__(self, root, context, write_json):
        self.root = Path(root).resolve()
        self.context = context
        self.write_json = write_json
        self.writes = {}

    def __enter__(self):
        if (self.root / TRANSACTION).exists():
            raise ValueError('ITERATION_TRANSITION_PENDING')
        self.token = _ACTIVE.set(self)
        return self

    def __exit__(self, kind, value, tb):
        _ACTIVE.reset(self.token)
        if kind is not None:
            return False
        writes = []
        for path, after in self.writes.items():
            item = Path(path)
            before = json.loads(item.read_text(encoding='utf-8-sig')) if item.exists() else None
            writes.append({'path': item.relative_to(self.root).as_posix(), 'beforeHash': digest(before),
                           'afterHash': digest(after), 'after': after})
        record = {'schema': 1, 'context': self.context, 'writes': writes}
        record['digest'] = digest(record)
        write_atomic(self.root / TRANSACTION, record)
        for item in writes:
            self.write_json(self.root / item['path'], item['after'])
        (self.root / TRANSACTION).unlink()
        return False


def recover_transition(root, run, config, *, write_json, validate_source):
    import os
    root = Path(root).resolve()
    path = root / TRANSACTION
    if not path.exists():
        return False
    record = json.loads(path.read_text(encoding='utf-8-sig'))
    checksum = record.pop('digest', None)
    context = record.get('context') or {}
    from iterative_stage_checkpoint import operation_environment
    environment = operation_environment(config)
    if checksum != digest(record) or record.get('schema') != 1:
        raise ValueError('ITERATION_TRANSITION_IDENTITY_MISMATCH: journal integrity')
    expected = {'runId': run['runId'], 'configHash': digest(config), 'toolBuildId': tool_build_id(),
                'environmentHash': digest(environment),
                'runtimeClosure': command_tool_closure('node', Path(config['projectDir']), environment)}
    changed = [key for key, value in expected.items() if context.get(key) != value]
    if changed:
        raise ValueError('ITERATION_TRANSITION_IDENTITY_MISMATCH: ' + ', '.join(changed))
    from iterative_stage_checkpoint import trial_key
    trial = Path(context.get('trialRoot', '')).resolve()
    if trial != (root / 'trial' / 'workspace').resolve():
        raise ValueError('ITERATION_TRANSITION_TRIAL_INVALID')
    if context.get('trialKey') and (not trial.is_dir() or trial_key(trial) != context['trialKey']):
        raise ValueError('ITERATION_TRANSITION_TRIAL_DRIFT')
    validate_source(context['baseCheckpointId'], context.get('baseSourceKey'))
    writes = record.get('writes')
    if not isinstance(writes, list) or not writes:
        raise ValueError('ITERATION_TRANSITION_INVALID')
    for item in writes:
        relative = Path(item['path'])
        target = (root / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(root) or '..' in relative.parts or target == path:
            raise ValueError('ITERATION_TRANSITION_PATH_INVALID')
        current = json.loads(target.read_text(encoding='utf-8-sig')) if target.exists() else None
        if digest(current) not in {item['beforeHash'], item['afterHash']} or digest(item['after']) != item['afterHash']:
            raise ValueError('ITERATION_TRANSITION_STATE_DRIFT')
    for item in writes:
        write_json(root / item['path'], item['after'])
    path.unlink()
    return True
