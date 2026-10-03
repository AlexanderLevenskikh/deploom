"""Serialized, display-only progress for long iterative migration operations."""
import threading
import time


class MigrationProgress:
    def __init__(self, emit, *, operation, message, run_id="", candidate_id="", interval=15):
        self.emit = emit
        self.operation = operation
        self.message = message
        self.run_id = run_id
        self.candidate_id = candidate_id
        self.interval = interval
        self.started = time.monotonic()
        self.last = self.started
        self.sequence = 0
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None

    def _publish(self, heartbeat=False):
        self.sequence += 1
        self.last = time.monotonic()
        self.emit({"event": "migration.progress", "runId": self.run_id,
                   "candidateId": self.candidate_id, "operation": self.operation,
                   "message": self.message, "heartbeat": heartbeat,
                   "sequence": self.sequence, "elapsedSeconds": round(self.last - self.started, 1)})

    def __call__(self, message):
        with self.lock:
            if self.stop.is_set():
                return
            self.message = str(message)
            self._publish()

    def _heartbeat(self):
        while not self.stop.wait(self.interval):
            with self.lock:
                if not self.stop.is_set() and time.monotonic() - self.last >= self.interval:
                    self._publish(heartbeat=True)

    def __enter__(self):
        with self.lock:
            self._publish()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop.set()
        self.thread.join()
        # Never manufacture completion or success; the caller owns the result.
