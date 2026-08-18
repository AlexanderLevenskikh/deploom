#!/usr/bin/env python3
"""Block V-D2 production-grade durable Baseline recovery.

Recovery state is orchestration metadata, never proof authority.  Exact proof
stores and certified constraints remain the only authority sources.  V-D2 adds:

* automatic AST-based semantic component fingerprints;
* dependency-aware recovery invalidation;
* cross-process active-run ownership protection;
* atomic checkpoint publication with a process-shared storage mutex;
* stale-owner/crash detection and safe-point replay;
* authority-preserving solver/orchestration upgrades.

A recovered cursor never claims an in-flight subprocess completed.  The stored
iteration is always the last committed safe point and the next experiment is
rerun fresh when necessary.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

RECOVERY_SCHEMA_VERSION = 1
RECOVERY_AUTHORITY = "ORCHESTRATION_HINT"
RECOVERY_IDENTITY_SCHEMA = "baseline-run-recovery-v1"
SEMANTIC_FINGERPRINT_SCHEMA = "baseline-recovery-semantic-v2"


@dataclasses.dataclass(frozen=True)
class RecoveryEpochs:
    # These values remain as a backwards-compatible/test fallback. Production
    # orchestration uses derive_recovery_epochs(repo_root), which replaces them
    # with AST-derived fingerprints.
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

# Earliest producer whose semantics can affect downstream recovered authority.
_EPOCH_ORDER = {
    "resolver": 0,
    "preparation": 1,
    "project": 2,
    "predicate": 3,
    "constraint": 4,
    "solver": 5,
    "orchestration": 6,
}

# A change in one producer invalidates every downstream interpretation.  Solver
# and orchestration changes do NOT invalidate already-certified constraints;
# they only reset the orchestration cursor and rerun solving.
_INVALIDATION_DAG: dict[str, frozenset[str]] = {
    "resolver": frozenset({"resolver", "preparation", "project", "predicate", "constraint", "solver", "orchestration"}),
    "preparation": frozenset({"preparation", "project", "predicate", "constraint", "solver", "orchestration"}),
    "project": frozenset({"project", "predicate", "constraint", "solver", "orchestration"}),
    "predicate": frozenset({"predicate", "constraint", "solver", "orchestration"}),
    "constraint": frozenset({"constraint", "solver", "orchestration"}),
    "solver": frozenset({"solver", "orchestration"}),
    "orchestration": frozenset({"orchestration"}),
}

_AUTHORITY_INVALIDATING_EPOCHS = frozenset({
    "resolver", "preparation", "project", "predicate", "constraint"
})


@dataclasses.dataclass(frozen=True)
class RecoveryPlan:
    found: bool
    resumable: bool
    previous_status: str = ""
    interrupted: bool = False
    state: Mapping[str, object] = dataclasses.field(default_factory=dict)
    changed_epochs: tuple[str, ...] = ()
    invalidated_components: tuple[str, ...] = ()
    recheck_from: str = ""
    reason: str = ""
    preserved_authority: bool = False
    active_owner_pid: int = 0


class RecoveryConcurrentRunError(RuntimeError):
    """A second Baseline tried to mutate the same project/mode recovery slot."""


# ---------------------------------------------------------------------------
# Semantic component fingerprints
# ---------------------------------------------------------------------------

# AST names are deliberately explicit.  Comments/formatting do not invalidate a
# component, but executable semantic edits do.  Missing names are hashed as a
# sentinel so refactors cannot silently retain an old fingerprint.
_COMPONENT_AST_SPECS: dict[str, dict[str, tuple[str, ...]]] = {
    "resolver": {
        "verification_proof.py": (
            "build_resolver_context_key", "build_resolver_trial_key",
            "build_verification_proof_identity", "bind_resolved_state_identity",
        ),
        "baseline_constraint_verifier.py": ("verify_assignment",),
        "resolved_dependency_state.py": ("load_resolved_dependency_state",),
    },
    "preparation": {
        "baseline_constraint_verifier.py": (
            "PreparedWorkspaceSnapshot", "_lookup_prepared_workspace_snapshot",
            "_publish_prepared_workspace_snapshot", "_materialize_prepared_workspace_snapshot",
        ),
        "block_v_prepared_artifact.py": (
            "configure_prepared_artifact_store", "load_prepared_artifact_record",
            "publish_prepared_artifact_record", "invalidate_prepared_artifact_record",
        ),
        "verification_workspace_backend.py": ("materialize_private_tree",),
    },
    "project": {
        "baseline_constraint_verifier.py": (
            "verify_assignment", "structural_project_failure_signatures",
        ),
        "verification_proof.py": ("build_project_trial_key",),
    },
    "predicate": {
        "block_v_predicate_search.py": (
            "PredicateObservation", "rank_version_probes", "predicate_package",
            "predicate_repeat_count", "prioritize_probe_preference",
        ),
        "block_vf_active_search.py": ("run_active_predicate_search",),
        "dependency_live_roadmap_generator.py": (
            "_graph_generalization_repeat_predicate", "_adaptive_predicate_families",
        ),
    },
    "constraint": {
        "dependency_live_roadmap_generator.py": (
            "_proof_preserving_minimize_nogood",
            "_adaptive_graph_guided_generalization_proposal",
            "_cross_iteration_consensus_proposal",
        ),
        "constraint_verify.py": ("parallel_ddmin",),
        "constraint_cache.py": ("persist_verified_nogood", "load_verified_nogoods"),
    },
    "solver": {
        "dependency_live_roadmap_generator.py": (
            "_build_peer_optimization_model", "_run_z3_peer_component",
            "resolve_peer_compatibility",
        ),
        "peer_solver_z3.py": ("solve_z3_exact",),
        "peer_solver_model.py": ("PeerOptimizationModel",),
    },
    "orchestration": {
        "dependency_live_roadmap_generator.py": (
            "resolve_peer_compatibility_with_verification", "BaselineLivenessBudget",
            "BaselineProgressReporter",
        ),
        "block_v_recovery.py": (
            "BaselineRunRecoveryStore", "build_run_state", "restore_run_state",
        ),
        "block_v_predicate_state.py": ("PredicateSearchStateStore",),
    },
}


def _ast_named_nodes(path: Path) -> dict[str, ast.AST]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return {}
    result: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result[node.name] = node
    return result


def semantic_component_fingerprint(
    repo_root: Path,
    component: str,
    specs: Optional[Mapping[str, Sequence[str]]] = None,
) -> str:
    """Hash executable AST for one recovery producer component.

    The digest ignores source locations/comments/formatting.  It intentionally
    includes a MISSING sentinel for absent files/symbols, making refactors fail
    safe toward re-check rather than accidentally reusing stale recovery state.
    """
    selected = dict(specs or _COMPONENT_AST_SPECS.get(component, {}))
    digest = hashlib.sha256()
    digest.update(SEMANTIC_FINGERPRINT_SCHEMA.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(component).encode("utf-8"))
    for relative in sorted(selected):
        path = Path(repo_root) / relative
        nodes = _ast_named_nodes(path)
        digest.update(b"\0FILE\0" + relative.encode("utf-8"))
        for name in sorted({str(item) for item in selected[relative]}):
            digest.update(b"\0SYMBOL\0" + name.encode("utf-8"))
            node = nodes.get(name)
            if node is None:
                digest.update(b"MISSING")
                continue
            digest.update(ast.dump(node, annotate_fields=True, include_attributes=False).encode("utf-8"))
    return digest.hexdigest()


def derive_recovery_epochs(repo_root: Path) -> RecoveryEpochs:
    """Derive component versions from current executable producer semantics."""
    root = Path(repo_root).resolve()
    values = {
        component: f"semantic-v2-{semantic_component_fingerprint(root, component)[:24]}"
        for component in _EPOCH_ORDER
    }
    return RecoveryEpochs(**values)


def _changed_epoch_names(old: Mapping[str, object], new: RecoveryEpochs) -> tuple[str, ...]:
    current = new.as_dict()
    return tuple(
        key for key in sorted(current, key=lambda item: _EPOCH_ORDER.get(item, 999))
        if str(old.get(key) or "") != current[key]
    )


def _expanded_invalidations(changed: Sequence[str]) -> tuple[str, ...]:
    invalidated: set[str] = set()
    for name in changed:
        invalidated.update(_INVALIDATION_DAG.get(name, {name}))
    return tuple(sorted(invalidated, key=lambda item: _EPOCH_ORDER.get(item, 999)))


def _sanitize_state_for_epoch_change(
    state: Mapping[str, object],
    changed: Sequence[str],
) -> tuple[dict[str, object], bool]:
    """Return recoverable state and whether certified authority was preserved."""
    safe = dict(_json_safe_state(state)) if isinstance(state, Mapping) else {}
    if not changed:
        return safe, bool(safe.get("learnedConstraints") or safe.get("globalExactExclusions"))

    if any(name in _AUTHORITY_INVALIDATING_EPOCHS for name in changed):
        # Old checkpoint clauses may have depended on changed resolver/project/
        # predicate/certification semantics. Durable proof stores independently
        # validate their own identities; recovery must not resurrect these.
        return build_run_state(
            iteration=0,
            learned_constraints=[],
            global_exact_exclusions=[],
            confirmed_failed_assignments=[],
            liveness={},
        ), False

    # Solver/orchestration-only upgrades may keep independently certified clauses
    # and exact failed assignments, but restart solving from a safe cursor with a
    # fresh liveness budget.
    safe["iteration"] = 0
    safe["liveness"] = {}
    safe["lastAssignment"] = ""
    safe["lastPredicate"] = ""
    return safe, bool(safe.get("learnedConstraints") or safe.get("globalExactExclusions"))


# ---------------------------------------------------------------------------
# Cross-process ownership and atomic storage
# ---------------------------------------------------------------------------


def _process_birth_token(pid: int) -> str:
    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return ""
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation), ctypes.byref(exit_time),
                    ctypes.byref(kernel), ctypes.byref(user),
                ):
                    return ""
                value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
                return f"win:{value}"
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return ""

    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        text = proc_stat.read_text(encoding="utf-8")
        right = text.rfind(")")
        fields = text[right + 2:].split()
        # /proc stat field 22 (starttime); fields begins at field 3.
        if len(fields) > 19:
            return f"proc:{fields[19]}"
    except OSError:
        pass
    return ""


def _pid_alive(pid: int, birth: str = "") -> bool:
    if pid <= 0:
        return False
    current_birth = _process_birth_token(pid)
    if birth and current_birth:
        return current_birth == birth
    if current_birth:
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


@contextlib.contextmanager
def _process_file_mutex(path: Optional[Path]) -> Iterator[None]:
    """Serialize short recovery-file transactions across local processes."""
    if path is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.tell() == 0 and path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


class BaselineRunRecoveryStore:
    """Atomic latest checkpoint + append-only journal + active-run ownership."""

    def __init__(self, progress_path: Optional[Path]) -> None:
        self.path = (
            progress_path.with_name("baseline-run-recovery.json")
            if progress_path is not None else None
        )
        self.journal_path = (
            progress_path.with_name("baseline-run-journal.jsonl")
            if progress_path is not None else None
        )
        self.active_path = (
            progress_path.with_name("baseline-run-active.json")
            if progress_path is not None else None
        )
        self.lock_path = (
            progress_path.with_name(".baseline-run-storage.lock")
            if progress_path is not None else None
        )
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._owner_birth = _process_birth_token(self._owner_pid)
        self._owner_nonce = uuid.uuid4().hex

    @staticmethod
    def _slot(project: str, mode: str) -> str:
        return hashlib.sha256(f"{project}\0{mode}".encode("utf-8")).hexdigest()[:24]

    def _empty(self) -> dict[str, object]:
        return {"schemaVersion": RECOVERY_SCHEMA_VERSION, "entries": {}}

    @staticmethod
    def _empty_active() -> dict[str, object]:
        return {"schemaVersion": 1, "entries": {}}

    @contextlib.contextmanager
    def _storage_guard(self) -> Iterator[None]:
        with self._lock:
            with _process_file_mutex(self.lock_path):
                yield

    def _preserve_corrupt(self, path: Optional[Path]) -> None:
        if path is None or not path.exists():
            return
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{os.getpid()}")
        try:
            os.replace(path, target)
        except OSError:
            pass

    def _read_json(self, path: Optional[Path], empty: Mapping[str, object]) -> dict[str, object]:
        if path is None or not path.is_file():
            return dict(empty)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            self._preserve_corrupt(path)
            return dict(empty)
        if not isinstance(payload, dict):
            return dict(empty)
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            payload["entries"] = {}
        return payload

    def _read_recovery(self) -> dict[str, object]:
        payload = self._read_json(self.path, self._empty())
        if int(payload.get("schemaVersion") or 0) != RECOVERY_SCHEMA_VERSION:
            return self._empty()
        return payload

    def _read_active(self) -> dict[str, object]:
        payload = self._read_json(self.active_path, self._empty_active())
        if int(payload.get("schemaVersion") or 0) != 1:
            return self._empty_active()
        return payload

    def _atomic_write(self, path: Optional[Path], payload: Mapping[str, object]) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp, path)
        _fsync_parent(path)

    def _owner_record(self, identity: str) -> dict[str, object]:
        return {
            "pid": self._owner_pid,
            "birth": self._owner_birth,
            "nonce": self._owner_nonce,
            "identity": str(identity),
            "claimedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def _active_other(self, raw: object) -> tuple[bool, int]:
        if not isinstance(raw, dict):
            return False, 0
        try:
            pid = int(raw.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        birth = str(raw.get("birth") or "")
        nonce = str(raw.get("nonce") or "")
        same_owner = (
            pid == self._owner_pid
            and nonce == self._owner_nonce
            and (not birth or not self._owner_birth or birth == self._owner_birth)
        )
        if same_owner:
            return False, pid
        return _pid_alive(pid, birth), pid

    def _claim_locked(self, project: str, mode: str, identity: str) -> tuple[bool, int, bool]:
        active = self._read_active()
        entries = active.setdefault("entries", {})
        assert isinstance(entries, dict)
        slot = self._slot(project, mode)
        existing = entries.get(slot)
        active_other, pid = self._active_other(existing)
        if active_other:
            return False, pid, False
        stale = isinstance(existing, dict) and pid > 0
        entries[slot] = self._owner_record(identity)
        self._atomic_write(self.active_path, active)
        return True, 0, stale

    def _ensure_claim_locked(self, project: str, mode: str, identity: str) -> None:
        claimed, pid, _stale = self._claim_locked(project, mode, identity)
        if not claimed:
            raise RecoveryConcurrentRunError(
                f"BASELINE_RECOVERY_CONCURRENT_RUN: {project}/{mode}: activePid={pid}"
            )

    def _release_locked(self, project: str, mode: str) -> None:
        active = self._read_active()
        entries = active.get("entries")
        if not isinstance(entries, dict):
            return
        slot = self._slot(project, mode)
        raw = entries.get(slot)
        if isinstance(raw, dict) and str(raw.get("nonce") or "") == self._owner_nonce:
            entries.pop(slot, None)
            self._atomic_write(self.active_path, active)

    def append_event(self, project: str, mode: str, event: str, **details: object) -> None:
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
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            with self._storage_guard():
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

    def _plan_from_entry(
        self,
        entry: object,
        *,
        identity: str,
        epochs: RecoveryEpochs,
    ) -> RecoveryPlan:
        if not isinstance(entry, dict):
            return RecoveryPlan(False, False, reason="checkpoint-missing")
        if str(entry.get("identity") or "") != str(identity):
            return RecoveryPlan(
                True, False,
                previous_status=str(entry.get("status") or ""),
                reason="identity-mismatch",
            )
        old_epochs = entry.get("epochs")
        if not isinstance(old_epochs, dict):
            old_epochs = {}
        changed = _changed_epoch_names(old_epochs, epochs)
        invalidated = _expanded_invalidations(changed)
        raw_state = entry.get("state")
        safe_state = dict(raw_state) if isinstance(raw_state, dict) else {}
        previous_status = str(entry.get("status") or "")
        interrupted = previous_status in {"running", "process-interrupted", "application-crash", "tool-error"}

        if changed:
            state, preserved = _sanitize_state_for_epoch_change(safe_state, changed)
            earliest = min(changed, key=lambda item: _EPOCH_ORDER.get(item, 999))
            authority_invalidated = any(name in _AUTHORITY_INVALIDATING_EPOCHS for name in changed)
            return RecoveryPlan(
                True,
                not authority_invalidated,
                previous_status=previous_status,
                interrupted=interrupted,
                state=state,
                changed_epochs=changed,
                invalidated_components=invalidated,
                recheck_from=earliest,
                reason=("authority-epoch-changed" if authority_invalidated else "semantic-replay-required"),
                preserved_authority=preserved,
            )

        if previous_status in {"completed", "passed"}:
            return RecoveryPlan(
                True, False, previous_status=previous_status,
                state=safe_state, reason="already-complete",
                preserved_authority=bool(safe_state.get("learnedConstraints") or safe_state.get("globalExactExclusions")),
            )
        return RecoveryPlan(
            True, True,
            previous_status=previous_status,
            interrupted=interrupted,
            state=safe_state,
            reason=("interrupted" if interrupted else "recoverable-checkpoint"),
            preserved_authority=bool(safe_state.get("learnedConstraints") or safe_state.get("globalExactExclusions")),
        )

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
        with self._storage_guard():
            active = self._read_active()
            active_entries = active.get("entries")
            active_raw = active_entries.get(self._slot(project, mode)) if isinstance(active_entries, dict) else None
            active_other, active_pid = self._active_other(active_raw)
            if active_other:
                return RecoveryPlan(
                    True, False, reason="active-run", active_owner_pid=active_pid,
                )
            payload = self._read_recovery()
            entries = payload.get("entries")
            entry = entries.get(self._slot(project, mode)) if isinstance(entries, dict) else None
            return self._plan_from_entry(entry, identity=identity, epochs=epochs)

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
        if normalized == "off":
            return RecoveryPlan(False, False, reason="resume-disabled")

        stale_reclaimed = False
        with self._storage_guard():
            claimed, active_pid, stale_reclaimed = self._claim_locked(project, mode, identity)
            if not claimed:
                plan = RecoveryPlan(True, False, reason="active-run", active_owner_pid=active_pid)
            elif normalized == "restart":
                payload = self._read_recovery()
                entries = payload.get("entries")
                if isinstance(entries, dict):
                    entries.pop(self._slot(project, mode), None)
                    self._atomic_write(self.path, payload)
                plan = RecoveryPlan(False, False, reason="restart-requested")
            else:
                payload = self._read_recovery()
                entries = payload.get("entries")
                entry = entries.get(self._slot(project, mode)) if isinstance(entries, dict) else None
                plan = self._plan_from_entry(entry, identity=identity, epochs=epochs)

        if plan.reason == "active-run":
            self.append_event(
                project, mode, "concurrent-run-rejected",
                identity=identity, activePid=plan.active_owner_pid,
            )
            return plan
        if stale_reclaimed:
            self.append_event(project, mode, "stale-owner-reclaimed", identity=identity)
        if normalized == "restart":
            self.append_event(project, mode, "restart-requested", identity=identity)
            return plan
        if plan.found:
            self.append_event(
                project, mode, "resume-inspected",
                identity=identity,
                resumable=plan.resumable,
                previousStatus=plan.previous_status,
                changedEpochs=list(plan.changed_epochs),
                invalidatedComponents=list(plan.invalidated_components),
                recheckFrom=plan.recheck_from,
                preservedAuthority=plan.preserved_authority,
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
        with self._storage_guard():
            self._ensure_claim_locked(project, mode, identity)
            payload = self._read_recovery()
            entries = payload.setdefault("entries", {})
            assert isinstance(entries, dict)
            slot = self._slot(project, mode)
            old = entries.get(slot)
            generation = int(old.get("generation") or 0) + 1 if isinstance(old, dict) else 1
            entries[slot] = {
                "identity": str(identity),
                "authority": RECOVERY_AUTHORITY,
                "epochs": epochs.as_dict(),
                "status": str(status),
                "phase": str(phase),
                "updatedAt": now,
                "generation": generation,
                "state": _json_safe_state(state),
            }
            self._atomic_write(self.path, payload)
        self.append_event(
            project, mode, "checkpoint",
            identity=identity, status=status, phase=phase,
            iteration=state.get("iteration"),
            learnedConstraints=len(state.get("learnedConstraints") or []),
            exactExclusions=len(state.get("globalExactExclusions") or []),
            generation=generation,
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
            project, mode, identity=identity, state=state,
            status=status, phase=phase, epochs=epochs,
        )
        with self._storage_guard():
            self._release_locked(project, mode)

    def clear(self, project: str, mode: str) -> None:
        if self.path is None:
            return
        with self._storage_guard():
            payload = self._read_recovery()
            entries = payload.get("entries")
            if isinstance(entries, dict) and entries.pop(self._slot(project, mode), None) is not None:
                self._atomic_write(self.path, payload)
            self._release_locked(project, mode)


def _epoch_order(name: str) -> int:
    return _EPOCH_ORDER.get(name, 999)


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
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
            for item in learned_constraints if item
        ],
        "globalExactExclusions": [
            dict(sorted((str(k), str(v)) for k, v in item.items()))
            for item in global_exact_exclusions if item
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
            clause = {str(k): str(v) for k, v in item.items() if str(k) and str(v)}
            if clause and clause not in result:
                result.append(dict(sorted(clause.items())))
        return result

    failed_raw = state.get("confirmedFailedAssignments")
    failed = {str(item) for item in failed_raw if str(item)} if isinstance(failed_raw, list) else set()
    try:
        iteration = max(0, int(state.get("iteration") or 0))
    except (TypeError, ValueError):
        iteration = 0
    liveness = state.get("liveness")
    return (
        clauses("learnedConstraints"), clauses("globalExactExclusions"),
        failed, iteration, dict(liveness) if isinstance(liveness, dict) else {},
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
        if attr in {"certified_extensions", "learned_extensions", "exact_extension_credits"}:
            limit = max(0, int(getattr(target, "max_learning_extensions", value)))
            value = min(value, limit)
        if attr == "learned_constraints":
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
