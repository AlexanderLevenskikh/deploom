"""Cooperative controls at safe storage-maintenance filesystem boundaries."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import time


class StorageCancelled(BaseException):
    """Unwind cleanup locks without being mistaken for an item failure."""


class MaintenanceControl:
    def __init__(self, path, operation_id, owner_pid=None, owner_nonce=None):
        self.path = Path(path)
        self.operation_id = operation_id
        self.owner_pid = owner_pid
        self.owner_nonce = owner_nonce
        self.last_check = 0.0
        self.last_owner_check = 0.0
        self.paused = False

    def mode(self):
        try:
            if self.path.stat().st_size > 4096:
                raise StorageCancelled('Storage control is invalid')
            value = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if value.get('operationId') != self.operation_id or value.get('state') not in {'run', 'pause', 'cancel'}:
                raise StorageCancelled('Storage control identity changed')
            if self.owner_nonce is not None and value.get('ownerNonce') != self.owner_nonce:
                raise StorageCancelled('Storage control owner changed')
            return value['state']
        except (OSError, ValueError, TypeError):
            raise StorageCancelled('Storage control is unavailable')

    def emit(self, state):
        print('STORAGE_CONTROL_V1 ' + json.dumps({'operationId': self.operation_id, 'state': state}), flush=True)

    def checkpoint(self, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_check < 0.05:
            return
        self.last_check = now
        while True:
            if self.owner_pid and now - self.last_owner_check >= 2:
                self.last_owner_check = now
                from verification_storage_maintenance import owner_alive
                if not owner_alive(self.owner_pid):
                    raise StorageCancelled('Storage owner exited')
            mode = self.mode()
            if mode == 'cancel':
                raise StorageCancelled('Storage maintenance canceled')
            if mode == 'run':
                if self.paused:
                    self.paused = False
                    self.emit('running')
                return
            if not self.paused:
                self.paused = True
                self.emit('paused')
            time.sleep(0.1)
            now = time.monotonic()


_current = ContextVar('storage_maintenance_control', default=None)


def checkpoint(*, force=False):
    control = _current.get()
    if control is not None:
        control.checkpoint(force=force)


@contextmanager
def maintenance_control(payload):
    path, operation = payload.get('controlFile'), payload.get('operationId')
    control = MaintenanceControl(path, operation, payload.get('ownerPid'), payload.get('ownerNonce')) if path and operation else None
    token = _current.set(control)
    try:
        checkpoint(force=True)
        yield
    finally:
        _current.reset(token)
