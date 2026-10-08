"""Recoverable archival for an explicit fresh migration; never touches a project."""
from pathlib import Path
import json
import os
import uuid

TRANSACTION = 'restart-archive-transaction.json'
HISTORY = 'run-archive'

class RestartArchiveError(ValueError):
    pass

def _history(run_dir):
    history = run_dir / HISTORY
    if history.is_symlink() or (history.exists() and history.resolve().parent != run_dir.resolve()):
        raise RestartArchiveError('RESTART_HISTORY_UNSAFE')
    return history

def recover_restart_archive(run_dir, write_json):
    """Caller holds the run lock. Uncommitted moves roll back after interruption."""
    journal_path = run_dir / TRANSACTION
    if not journal_path.exists():
        return
    journal = json.loads(journal_path.read_text(encoding='utf-8'))
    history = _history(run_dir)
    archive_name = journal.get('archiveName', '')
    if not archive_name or Path(archive_name).name != archive_name or archive_name in ('.', '..'):
        raise RestartArchiveError('RESTART_JOURNAL_INVALID')
    archive = history / archive_name
    if archive.is_symlink():
        raise RestartArchiveError('RESTART_HISTORY_UNSAFE')
    if journal.get('phase') != 'committed':
        for name in reversed(journal['entries']):
            if Path(name).name != name or name in ('.', '..', HISTORY, TRANSACTION):
                raise RestartArchiveError('RESTART_JOURNAL_INVALID')
            source, target = run_dir / name, archive / name
            if target.exists() or target.is_symlink():
                if source.exists() or source.is_symlink():
                    raise RestartArchiveError(f'RESTART_RECOVERY_CONFLICT: {name}')
                os.rename(target, source)
    journal_path.unlink()

def archive_for_restart(run_dir, lock_filename, write_json):
    recover_restart_archive(run_dir, write_json)
    # An agent session must be resumed/closed, never abandoned by moving its trial.
    if (run_dir / 'trial' / 'agent-lease.json').exists():
        raise RestartArchiveError('RESTART_AGENT_PENDING: продолжите и завершите сессию агента перед новым запуском')
    delivery_lease = run_dir / 'delivery-agent.json'
    if delivery_lease.exists():
        lease = json.loads(delivery_lease.read_text(encoding='utf-8-sig'))
        if lease.get('status') == 'running':
            raise RestartArchiveError('RESTART_AGENT_PENDING: возобновите и завершите подготовку результата перед новым запуском')
    history = _history(run_dir)
    entries = [entry.name for entry in sorted(run_dir.iterdir())
               if entry.name not in {lock_filename, HISTORY, 'repair-archive', TRANSACTION}]
    if not entries:
        return {'archived': False}
    archive = history / str(uuid.uuid4())
    archive.mkdir(parents=True, exist_ok=False)
    journal = {'phase': 'moving', 'archiveName': archive.name, 'entries': entries}
    write_json(run_dir / TRANSACTION, journal)
    try:
        for name in entries:
            os.rename(run_dir / name, archive / name)
        write_json(run_dir / TRANSACTION, {**journal, 'phase': 'committed'})
    except Exception:
        recover_restart_archive(run_dir, write_json)
        raise
    (run_dir / TRANSACTION).unlink(missing_ok=True)
    return {'archived': True, 'archiveDir': str(archive)}
