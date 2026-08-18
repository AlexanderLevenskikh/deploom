#!/usr/bin/env python3
"""Block V-A durable Baseline orchestration recovery.

This module is deliberately *not* proof authority.  It persists orchestration
checkpoints and an append-only journal so a stopped/crashed Baseline can rebuild
its cursor from exact proof/constraint identities.  Every recovered authority
item is still validated by the existing proof/constraint stores.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Optional, Sequence

RECOVERY_SCHEMA_VERSION = 1
RECOVERY_AUTHORITY = "ORCHESTRATION_HINT"
RECOVERY_IDENTITY_SCHEMA = "baseline-run-recovery-v1"


@dataclasses.dataclass(frozen=True)
class RecoveryEpochs:
    # Bump only the component whose producer semantics changed.  A pure UI or
    # crash-handler fix should normally change orchestration only, allowing old
    # exact proof to survive a DepLoom upgrade.
    resolver: str = "resolver-proof-v5"
    preparation: str = "prepared-artifact-v1"
    project: str = "project-proof-v1"
    predicate: str = "structural-predicate-v1"
    constraint: str = "constraint-learning-v1"
    solver: str = "exact-solver-model-v1"
    orchestration: str = "baseline-orchestration-v1"

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


CURRENT_RECOVERY_EPOCHS = RecoveryEpochs()
_AUTHORITY_EPOCHS = frozenset({
    "resolver", "preparation", "project", "predicate", "constraint", "solver"
})


@dataclasses.dataclass(frozen=True)
class RecoveryPlan:
    found: bool
    resumable: bool
    previous_status: str = ""
    interrupted: bool = False
    state: Mapping[str, object] = dataclasses.field(default_factory=dict)
    changed_epochs: tuple[str, ...] = ()
    recheck_from: str = ""
    reason: str = ""


class BaselineRunRecoveryStore:
    """Atomic latest checkpoint + append-only audit journal.

    Checkpoints are hints.  They may restore exact learned clauses/exclusions
    only when the caller validates the same run identity and compatible producer
    epochs.  A corrupt/missing checkpoint always fails open to a fresh run.
    """

    def __init__(self, progress_path: Optional[Path]) -> None:
        self.path = (
            progress_path.with_name("baseline-run-recovery.json")
            if progress_path is not None
            else None
        )
        self.journal_path = (
            progress_path.with_name("baseline-run-journal.jsonl")
            if progress_path is not None
            else None
        )
        self._lock = threading.RLock()

    @staticmethod
    def _slot(project: str, mode: str) -> str:
        return hashlib.sha256(f"{project}\0{mode}".encode("utf-8")).hexdigest()[:24]

    def _empty(self) -> dict[str, object]:
        return {"schemaVersion": RECOVERY_SCHEMA_VERSION, "entries": {}}

    def _preserve_corrupt(self, exc: Exception) -> None:
        if self.path is None or not self.path.exists():
            return
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(
            f"{self.path.name}.corrupt-{stamp}-{os.getpid()}"
        )
        try:
            os.replace(self.path, target)
        except OSError:
            # Recovery storage must never make Baseline authority unavailable.
            pass

    def _read_locked(self) -> dict[str, object]:
        if self.path is None or not self.path.is_file():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            self._preserve_corrupt(exc)
            return self._empty()
        if (
            not isinstance(payload, dict)
            or int(payload.get("schemaVersion") or 0) != RECOVERY_SCHEMA_VERSION
        ):
            return self._empty()
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            payload["entries"] = {}
        return payload

    def _write_locked(self, payload: Mapping[str, object]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        data = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp, self.path)

    def append_event(
        self,
        project: str,
        mode: str,
        event: str,
        **details: object,
    ) -> None:
        if self.journal_path is None:
            return
        record = {
            "schemaVersion": 1,
            "authority": RECOVERY_AUTHORITY,
            "project": str(project),
            "mode": str(mode),
            "event": str(event),
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            **details,
        }
        line = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
        with self._lock:
            try:
                self.journal_path.parent.mkdir(parents=True, exist_ok=True)
                with self.journal_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
            except OSError:
                # Journal loss changes observability, never correctness.
                pass

    def inspect(
        self,
        project: str,
        mode: str,
        *,
        identity: str,
        epochs: RecoveryEpochs = CURRENT_RECOVERY_EPOCHS,
    ) -> RecoveryPlan:
        if self.path is None:
            return RecoveryPlan(False, False, reason="recovery-disabled")
        with self._lock:
            payload = self._read_locked()
            entries = payload.get("entries")
            entry = (
                entries.get(self._slot(project, mode))
                if isinstance(entries, dict)
                else None
            )
        if not isinstance(entry, dict):
            return RecoveryPlan(False, False, reason="checkpoint-missing")
        if str(entry.get("identity") or "") != str(identity):
            return RecoveryPlan(
                True,
                False,
                previous_status=str(entry.get("status") or ""),
                reason="identity-mismatch",
            )
        old_epochs = entry.get("epochs")
        if not isinstance(old_epochs, dict):
            old_epochs = {}
        current_epochs = epochs.as_dict()
        changed = tuple(
            key
            for key in sorted(current_epochs)
            if str(old_epochs.get(key) or "") != current_epochs[key]
        )
        authoritative_changed = tuple(
            key for key in changed if key in _AUTHORITY_EPOCHS
        )
        state = entry.get("state")
        safe_state = dict(state) if isinstance(state, dict) else {}
        previous_status = str(entry.get("status") or "")
        interrupted = previous_status in {"running", "process-interrupted", "application-crash"}
        if authoritative_changed:
            return RecoveryPlan(
                True,
                False,
                previous_status=previous_status,
                interrupted=interrupted,
                state=safe_state,
                changed_epochs=changed,
                recheck_from=min(authoritative_changed, key=_epoch_order),
                reason="authority-epoch-changed",
            )
        if previous_status in {"completed", "passed"}:
            return RecoveryPlan(
                True,
                False,
                previous_status=previous_status,
                state=safe_state,
                changed_epochs=changed,
                reason="already-complete",
            )
        return RecoveryPlan(
            True,
            True,
            previous_status=previous_status,
            interrupted=interrupted,
            state=safe_state,
            changed_epochs=changed,
            reason=("interrupted" if interrupted else "recoverable-checkpoint"),
        )

    def begin(
        self,
        project: str,
        mode: str,
        *,
        identity: str,
        policy: str = "auto",
        epochs: RecoveryEpochs = CURRENT_RECOVERY_EPOCHS,
    ) -> RecoveryPlan:
        normalized = normalize_resume_policy(policy)
        if normalized == "restart":
            self.clear(project, mode)
            self.append_event(project, mode, "restart-requested", identity=identity)
            return RecoveryPlan(False, False, reason="restart-requested")
        if normalized == "off":
            return RecoveryPlan(False, False, reason="resume-disabled")
        plan = self.inspect(project, mode, identity=identity, epochs=epochs)
        if plan.found:
            self.append_event(
                project,
                mode,
                "resume-inspected",
                identity=identity,
                resumable=plan.resumable,
                previousStatus=plan.previous_status,
                changedEpochs=list(plan.changed_epochs),
                recheckFrom=plan.recheck_from,
                reason=plan.reason,
            )
        return plan

    def checkpoint(
        self,
        project: str,
        mode: str,
        *,
        identity: str,
        state: Mapping[str, object],
        status: str = "running",
        phase: str = "",
        epochs: RecoveryEpochs = CURRENT_RECOVERY_EPOCHS,
    ) -> None:
        if self.path is None:
            return
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self._lock:
            payload = self._read_locked()
            entries = payload.setdefault("entries", {})
            assert isinstance(entries, dict)
            entries[self._slot(project, mode)] = {
                "identity": str(identity),
                "authority": RECOVERY_AUTHORITY,
                "epochs": epochs.as_dict(),
                "status": str(status),
                "phase": str(phase),
                "updatedAt": now,
                "state": _json_safe_state(state),
            }
            self._write_locked(payload)
        self.append_event(
            project,
            mode,
            "checkpoint",
            identity=identity,
            status=status,
            phase=phase,
            iteration=state.get("iteration"),
            learnedConstraints=len(state.get("learnedConstraints") or []),
            exactExclusions=len(state.get("globalExactExclusions") or []),
        )

    def mark_terminal(
        self,
        project: str,
        mode: str,
        *,
        identity: str,
        status: str,
        state: Mapping[str, object],
        phase: str = "",
        epochs: RecoveryEpochs = CURRENT_RECOVERY_EPOCHS,
    ) -> None:
        self.checkpoint(
            project,
            mode,
            identity=identity,
            state=state,
            status=status,
            phase=phase,
            epochs=epochs,
        )

    def clear(self, project: str, mode: str) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = self._read_locked()
            entries = payload.get("entries")
            if not isinstance(entries, dict):
                return
            if entries.pop(self._slot(project, mode), None) is not None:
                self._write_locked(payload)


def _epoch_order(name: str) -> int:
    order = {
        "resolver": 0,
        "preparation": 1,
        "project": 2,
        "predicate": 3,
        "constraint": 4,
        "solver": 5,
        "orchestration": 6,
    }
    return order.get(name, 999)


def normalize_resume_policy(value: object) -> str:
    text = str(value or "auto").strip().lower()
    if text in {"auto", "continue", "resume"}:
        return "auto"
    if text in {"restart", "fresh", "start-over", "start_over"}:
        return "restart"
    if text in {"off", "disabled", "none"}:
        return "off"
    return "auto"


def baseline_resume_policy(environment: Optional[Mapping[str, str]] = None) -> str:
    env = environment if environment is not None else os.environ
    return normalize_resume_policy(env.get("DEPLOOM_BASELINE_RESUME", "auto"))


def baseline_run_identity(
    *,
    project: str,
    mode: str,
    source_snapshot_key: str,
    resolver_context_key: str,
    config: Mapping[str, object],
) -> str:
    payload = {
        "schema": RECOVERY_IDENTITY_SCHEMA,
        "project": str(project),
        "mode": str(mode),
        "sourceSnapshotKey": str(source_snapshot_key),
        "resolverContextKey": str(resolver_context_key),
        "config": _json_safe_state(config),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_run_state(
    *,
    iteration: int,
    learned_constraints: Sequence[Mapping[str, str]],
    global_exact_exclusions: Sequence[Mapping[str, str]],
    confirmed_failed_assignments: Sequence[str],
    liveness: Optional[Mapping[str, object]] = None,
    last_assignment: str = "",
    last_predicate: str = "",
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "iteration": max(0, int(iteration)),
        "learnedConstraints": [
            dict(sorted((str(k), str(v)) for k, v in item.items()))
            for item in learned_constraints
            if item
        ],
        "globalExactExclusions": [
            dict(sorted((str(k), str(v)) for k, v in item.items()))
            for item in global_exact_exclusions
            if item
        ],
        "confirmedFailedAssignments": sorted({
            str(item) for item in confirmed_failed_assignments if str(item)
        }),
        "liveness": dict(liveness or {}),
        "lastAssignment": str(last_assignment or ""),
        "lastPredicate": str(last_predicate or ""),
    }


def restore_run_state(
    state: Mapping[str, object],
) -> tuple[list[dict[str, str]], list[dict[str, str]], set[str], int, dict[str, object]]:
    def clauses(name: str) -> list[dict[str, str]]:
        raw = state.get(name)
        if not isinstance(raw, list):
            return []
        result: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            clause = {
                str(k): str(v)
                for k, v in item.items()
                if str(k) and str(v)
            }
            if clause and clause not in result:
                result.append(dict(sorted(clause.items())))
        return result

    failed_raw = state.get("confirmedFailedAssignments")
    failed = {
        str(item)
        for item in failed_raw
        if str(item)
    } if isinstance(failed_raw, list) else set()
    try:
        iteration = max(0, int(state.get("iteration") or 0))
    except (TypeError, ValueError):
        iteration = 0
    liveness = state.get("liveness")
    return (
        clauses("learnedConstraints"),
        clauses("globalExactExclusions"),
        failed,
        iteration,
        dict(liveness) if isinstance(liveness, dict) else {},
    )


def restore_liveness_budget(target: object, snapshot: Mapping[str, object]) -> None:
    """Restore only bounded counters onto BaselineLivenessBudget-like object."""
    mapping = {
        "certifiedExtensions": "certified_extensions",
        "learnedExtensions": "learned_extensions",
        "exactExtensionCredits": "exact_extension_credits",
        "learnedConstraints": "learned_constraints",
        "exactExclusions": "exact_exclusions",
        "exactSinceLearning": "exact_since_learning",
        "generalizationAttempts": "generalization_attempts",
        "diagnostics": "diagnostics",
    }
    for source, attr in mapping.items():
        if not hasattr(target, attr):
            continue
        try:
            value = max(0, int(snapshot.get(source) or 0))
        except (TypeError, ValueError):
            continue
        # certified extension count must never exceed the configured bound.
        if attr in {"certified_extensions", "learned_extensions", "exact_extension_credits"}:
            limit = max(0, int(getattr(target, "max_learning_extensions", value)))
            value = min(value, limit)
        if attr == "learned_constraints":
            # Constraints already loaded from the durable constraint store before
            # recovery are part of the starting state, not newly earned liveness.
            value = max(value, int(getattr(target, "starting_learned_constraints", 0)))
        setattr(target, attr, value)


def _json_safe_state(value: object) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe_state(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe_state(item) for item in value]
    return str(value)
