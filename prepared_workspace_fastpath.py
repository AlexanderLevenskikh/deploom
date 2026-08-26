from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import ctypes
import dataclasses
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Optional
from io_governor import io_slot
from reparse_materialization import (
    ReparseLink,
    ReparseMaterializationError,
    inventory_reparse_plan,
)
from verification_observability import (
    emit_observability_event,
    new_observability_id,
)

# BLOCK_Y_FULL_OBSERVABILITY_V1

ProgressCallback = Callable[[str], None]


@dataclasses.dataclass(frozen=True)
class GuardResult:
    mutations: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    notification_only: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class WorkspaceChangeResult:
    # Observed writes in one fully-private verification trial.
    changes: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class WorkspaceChangeGuard:
    # Conservative whole-workspace mutation detector. A clean result only
    # permits reuse of the already-private trial for the next check. Any event,
    # watcher error, unsupported platform, or overflow forces fresh materialization.

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._watcher: Optional["_DirectoryWatcher"] = None
        self._started = False
        self._stopped: Optional[WorkspaceChangeResult] = None

    def start(self) -> bool:
        if os.name != "nt":
            return False
        watcher = _DirectoryWatcher(self.root)
        self._watcher = watcher
        self._started = watcher.start()
        return self._started

    def stop(self) -> WorkspaceChangeResult:
        if self._stopped is not None:
            return self._stopped
        watcher = self._watcher
        if watcher is None:
            self._stopped = WorkspaceChangeResult(errors=("workspace watcher unavailable",))
            return self._stopped
        if self._started:
            watcher.stop()
        changes = tuple(sorted({
            f"{action}:{name.replace(chr(92), '/')}"
            for action, name in watcher.events
            if name
        }))
        self._stopped = WorkspaceChangeResult(
            changes=changes,
            errors=tuple(watcher.errors),
        )
        return self._stopped




# The rendezvous file name is not authority. Ownership is a live Windows kernel
# FILE HANDLE opened with shareMode=0. A stale file is deliberately harmless.
_SNAPSHOT_LEASE_BUSY_ERRORS = frozenset({32, 33})  # sharing / lock violation


@dataclasses.dataclass
class _SnapshotLease:
    path: Path
    handle: int
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        handle = int(self.handle or 0)
        self.handle = 0
        if os.name == "nt" and handle:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        try:
            self.path.unlink()
        except OSError:
            pass


@dataclasses.dataclass(frozen=True)
class _SnapshotLeaseAttempt:
    lease: Optional[_SnapshotLease]
    reason: str = ""
    win_error: int = 0


def _snapshot_lease_path(prepared_workspace_root: Path) -> Path:
    identity = os.path.normcase(str(prepared_workspace_root.resolve()))
    digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
    root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir())
    return root / "deploom-prepared-snapshot-leases" / f"{digest}.lease"


def _open_snapshot_lease(prepared_workspace_root: Path) -> _SnapshotLeaseAttempt:
    """One non-blocking OS-level lease attempt."""
    if os.name != "nt":
        return _SnapshotLeaseAttempt(None, "unsupported-platform", 0)

    from ctypes import wintypes

    lease_path = _snapshot_lease_path(prepared_workspace_root)
    try:
        lease_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _SnapshotLeaseAttempt(None, "lease-directory-error", 0)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_ALWAYS = 4
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    handle = kernel32.CreateFileW(
        str(lease_path),
        GENERIC_READ | GENERIC_WRITE,
        0,  # shareMode=0: live handle == exclusive snapshot ownership
        None,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    handle_value = ctypes.cast(handle, ctypes.c_void_p).value
    if handle_value in (None, invalid):
        error = int(ctypes.get_last_error())
        reason = "lease-busy" if error in _SNAPSHOT_LEASE_BUSY_ERRORS else "lease-open-error"
        return _SnapshotLeaseAttempt(None, reason, error)
    return _SnapshotLeaseAttempt(_SnapshotLease(lease_path, int(handle_value)))


def _try_acquire_snapshot_lease(
    prepared_workspace_root: Path,
    *,
    progress: Optional[ProgressCallback] = None,
) -> Optional[_SnapshotLease]:
    """Try exclusive ownership for a junction-backed consumer.

    Busy means another live consumer owns the shared payload, so this fast path
    is not used. The caller's private-copy fallback acquires the SAME lease
    before reading snapshot bytes; it never races a transient shared mutation.
    """
    if os.name != "nt":
        return None
    attempt = _open_snapshot_lease(prepared_workspace_root)
    if attempt.lease is not None:
        return attempt.lease
    if progress:
        progress(
            f"NTFS fast clone unavailable: reason={attempt.reason}; "
            f"winError={attempt.win_error}; private copy will use the snapshot lease"
        )
    return None


def acquire_snapshot_copy_lease(
    prepared_workspace_root: Path,
    *,
    timeout_seconds: int,
    progress: Optional[ProgressCallback] = None,
) -> Optional[_SnapshotLease]:
    """Acquire exclusive ownership before ANY private read of shared snapshot.

    Waiting is correctness-critical: copying immediately after a fast-path lease
    collision could read bytes while the live junction consumer is transiently
    mutating them. Only sharing/lock violations are retried. Every other lease
    error fails closed; timeout is infrastructure failure, never proof evidence.
    """
    if os.name != "nt":
        return None
    started = time.monotonic()
    deadline = started + max(1, int(timeout_seconds))
    announced = False
    while True:
        attempt = _open_snapshot_lease(prepared_workspace_root)
        if attempt.lease is not None:
            if announced and progress:
                progress(
                    "prepared snapshot private-copy lease acquired after contention; "
                    f"waited={int(time.monotonic() - started)}s"
                )
            return attempt.lease
        if attempt.reason != "lease-busy":
            raise RuntimeError(
                "PREPARED_SNAPSHOT_LEASE_UNAVAILABLE: "
                f"reason={attempt.reason}; winError={attempt.win_error}"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "PREPARED_SNAPSHOT_LEASE_TIMEOUT: another live consumer retained "
                f"the prepared snapshot for >={max(1, int(timeout_seconds))}s"
            )
        if not announced and progress:
            progress(
                "prepared snapshot is owned by another live verification consumer; "
                "waiting before proof-safe private copy"
            )
            announced = True
        time.sleep(0.10)


def try_acquire_snapshot_cleanup_lease(
    prepared_workspace_root: Path,
) -> Optional[_SnapshotLease]:
    """Best-effort exclusive ownership for cache retirement.

    Eviction is never allowed to delete bytes under a live junction/copy
    reader. On contention or lock infrastructure failure we intentionally leak
    the unregistered temp snapshot until process cleanup rather than race proof
    execution.
    """
    if os.name != "nt":
        return None
    return _open_snapshot_lease(prepared_workspace_root).lease


@dataclasses.dataclass
class _GuardedClone:
    target_root: Path
    project: Path
    junctions: list[Path]
    guard: "_DependencyTreeGuard"
    lease: "_SnapshotLease"
    stopped: Optional[GuardResult] = None


_ACTIVE: dict[str, _GuardedClone] = {}
_ACTIVE_LOCK = threading.Lock()


def _key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _git_root(project_dir: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "--show-toplevel"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _dependency_roots(prepared_root: Path) -> list[Path]:
    result: list[Path] = []
    prepared_root = prepared_root.resolve()
    for current, dirs, _files in os.walk(prepared_root):
        dirs[:] = [name for name in dirs if name != ".git"]
        current_path = Path(current)
        if current_path.name == "node_modules":
            result.append(current_path.resolve())
            dirs[:] = []
            continue
        if "node_modules" in dirs:
            node_modules = (current_path / "node_modules").resolve()
            result.append(node_modules)
            dirs.remove("node_modules")
    unique = {os.path.normcase(str(path)): path for path in result}
    return [unique[key] for key in sorted(unique)]


def guarded_lower_enabled() -> bool:
    return str(
        os.environ.get("DEPLOOM_DISABLE_GUARDED_LOWER") or ""
    ).strip().lower() not in {"1", "true", "yes", "on"}


def dependency_root_manifest(
    prepared_root: Path,
    *,
    progress: Optional[ProgressCallback] = None,
) -> tuple[str, ...]:
    """Return only relative node_modules roots; never hash dependency payloads."""
    prepared_root = prepared_root.resolve()
    operation_id = new_observability_id("dependency-roots")
    started = time.monotonic()
    emit_observability_event(
        "filesystem.dependency-roots.start",
        operationId=operation_id,
        schema="verification-substrate-v2",
    )
    roots = _dependency_roots(prepared_root)
    relative: list[str] = []
    for root in roots:
        try:
            item = root.relative_to(prepared_root)
        except ValueError as exc:
            raise RuntimeError(
                f"DEPENDENCY_ROOT_ESCAPE: {root} outside {prepared_root}"
            ) from exc
        relative.append(item.as_posix())
    result = tuple(sorted(set(relative)))
    if progress:
        progress(
            "Ω dependency lower discovery ready: "
            f"roots={len(result)}, integrityHashPass=skipped"
        )
    emit_observability_event(
        "filesystem.dependency-roots.finish",
        operationId=operation_id,
        schema="verification-substrate-v2",
        outcome="passed",
        rootCount=len(result),
        integrityHashPass=False,
        durationMs=max(0, int((time.monotonic() - started) * 1000)),
    )
    return result


def _overlay_copy_workers() -> int:
    raw = str(os.environ.get("DEPLOOM_OMEGA_OVERLAY_WORKERS") or "").strip()
    if raw:
        try:
            return max(1, min(64, int(raw)))
        except ValueError:
            pass
    logical = max(1, int(os.cpu_count() or 4))
    # Overlay excludes node_modules; huge /MT values mostly amplify AV/metadata
    # pressure. Keep enough concurrency to hide latency without 128-thread I/O.
    return max(8, min(32, logical))


def _integrity_key(path: Path, prepared_root: Path) -> str:
    relative = path.relative_to(prepared_root).as_posix()
    return os.path.normcase(relative.replace("/", os.sep)).replace("\\", "/")


def _integrity_hash_workers() -> int:
    """Bound parallel hashing so NTFS/AV latency is hidden without I/O flooding."""
    raw = str(os.environ.get("DEPLOOM_INTEGRITY_HASH_WORKERS") or "").strip()
    if raw:
        try:
            return max(1, min(32, int(raw)))
        except ValueError:
            pass
    # node_modules sealing on Windows is normally metadata/filter-driver bound,
    # not SHA-256 CPU bound. A moderate pool overlaps that latency without an
    # unbounded queue against Defender/EDR/NTFS.
    return min(12, max(4, (os.cpu_count() or 4) // 2))


def _fingerprint_path_with_size(path: Path) -> tuple[str, int]:
    """Return the exact content fingerprint plus bytes consumed from the file."""
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        payload = os.readlink(path).encode("utf-8", errors="surrogatepass")
        return "symlink:" + hashlib.sha256(payload).hexdigest(), int(metadata.st_size)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"unsupported dependency payload type: {path}")

    digest = hashlib.sha256()
    total = 0
    with path.open("rb", buffering=1024 * 1024) as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    return "file:" + digest.hexdigest(), total


def _fingerprint_path(path: Path) -> str:
    # Watcher classification uses the same byte-level fingerprint as sealing.
    return _fingerprint_path_with_size(path)[0]


def _seal_dependency_candidate(
    path: Path,
    prepared_root: Path,
) -> Optional[tuple[str, str, int]]:
    """Hash one os.walk file candidate, preserving fail-closed semantics."""
    try:
        metadata = os.lstat(path)
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            return None
        fingerprint, size = _fingerprint_path_with_size(path)
        return _integrity_key(path, prepared_root), fingerprint, size
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"PREPARED_DEPENDENCY_INTEGRITY_CAPTURE_FAILED: {path}: {exc}"
        ) from exc


# BLOCK_OMEGA_VERIFICATION_SUBSTRATE_V2
# Legacy diagnostic helper only; production guarded lower does not call it.
def build_dependency_integrity_manifest(
    prepared_root: Path,
    *,
    progress: Optional[ProgressCallback] = None,
    progress_interval_seconds: int = 15,
) -> dict[str, str]:
    """Seal every dependency payload byte before any junction consumer exists.

    Proof semantics are unchanged: every regular dependency file/symlink gets
    the same SHA-256 fingerprint and unreadable payloads fail closed. Only the
    execution strategy is parallelized. Final mapping order is canonical.
    """
    prepared_root = prepared_root.resolve()
    started = time.monotonic()
    integrity_operation_id = new_observability_id("integrity")
    emit_observability_event(
        "filesystem.integrity.start",
        operationId=integrity_operation_id,
        label="sealed dependency integrity manifest",
        preparedRootName=prepared_root.name,
    )
    interval = max(1, int(progress_interval_seconds or 15))
    next_progress = started + interval
    candidates: list[Path] = []

    # Inventory discovery stays deterministic and single-threaded. The prepared
    # stage is private here; no junction-backed consumer exists until sealing is
    # complete.
    for dependency_root in _dependency_roots(prepared_root):
        for current, dirs, names in os.walk(dependency_root, followlinks=False):
            dirs.sort(key=str.lower)
            names.sort(key=str.lower)
            current_path = Path(current)
            candidates.extend(current_path / name for name in names)

    manifest: dict[str, str] = {}
    files = 0
    total_bytes = 0
    workers = _integrity_hash_workers()
    if progress:
        progress(
            "sealed dependency integrity manifest hashing: "
            f"candidates={len(candidates)}, workers={workers}"
        )

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="deploom-integrity",
    ) as executor:
        future_paths = {
            executor.submit(_seal_dependency_candidate, path, prepared_root): path
            for path in candidates
        }
        for future in as_completed(future_paths):
            result = future.result()
            if result is None:
                continue
            key, fingerprint, size = result
            manifest[key] = fingerprint
            files += 1
            total_bytes += size

            now = time.monotonic()
            if progress and now >= next_progress:
                progress(
                    "sealed dependency integrity manifest: "
                    f"files={files}/{len(candidates)}, bytes={total_bytes}, "
                    f"workers={workers}, elapsed={int(now - started)}s"
                )
                next_progress = now + interval

    canonical = dict(sorted(manifest.items()))
    if progress:
        progress(
            "sealed dependency integrity manifest ready: "
            f"files={files}, bytes={total_bytes}, workers={workers}, "
            f"elapsed={int(time.monotonic() - started)}s"
        )
    emit_observability_event(
        "filesystem.integrity.finish",
        operationId=integrity_operation_id,
        label="sealed dependency integrity manifest",
        outcome="passed",
        candidateFiles=len(candidates),
        fileCount=files,
        byteCount=total_bytes,
        workers=workers,
        durationMs=max(0, int((time.monotonic() - started) * 1000)),
    )
    return canonical


def _run_overlay_robocopy(
    source: Path,
    target: Path,
    excluded: list[Path],
    progress: Optional[ProgressCallback],
) -> bool:
    robocopy = shutil.which("robocopy")
    if not robocopy:
        return False
    argv = [
        robocopy,
        str(source),
        str(target),
        "/E",
        "/COPY:DAT",
        "/DCOPY:DAT",
        "/R:1",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP",
        "/SL",
        "/XJ",
        f"/MT:{_overlay_copy_workers()}",
    ]
    if excluded:
        argv.extend(["/XD", *[str(path) for path in excluded]])
    if progress:
        progress(
            "Ω guarded lower: copying sealed source/config/.git upper "
            "without dependency payload bytes"
        )
    try:
        result = subprocess.run(
            argv,
            cwd=str(source),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode < 8


def _create_junction(link: Path, target: Path) -> bool:
    comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
    if not comspec:
        return False
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        try:
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link)
            else:
                link.unlink()
        except OSError:
            return False
    command = subprocess.list2cmdline(["mklink", "/J", str(link), str(target)])
    try:
        result = subprocess.run(
            [comspec, "/d", "/s", "/c", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and link.is_dir()


def _remove_junction(path: Path) -> None:
    try:
        os.rmdir(path)
    except OSError:
        try:
            subprocess.run(
                [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", "rmdir", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def _batch_escape(value: Path) -> str:
    return str(value).replace("%", "%%").replace('"', '""')


def _create_junction_batch(pairs: list[tuple[Path, Path]]) -> bool:
    if not pairs:
        return True
    comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
    if not comspec:
        return False
    batch = pairs[0][0].parent / f".deploom-junctions-{os.getpid()}-{threading.get_ident()}.cmd"
    lines = ["@echo off"]
    for link, target in pairs:
        link.parent.mkdir(parents=True, exist_ok=True)
        lines.append(
            f'mklink /J "{_batch_escape(link)}" "{_batch_escape(target)}" >nul || exit /b 1'
        )
    lines.append("exit /b 0")
    try:
        batch.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        command = subprocess.list2cmdline([str(batch)])
        result = subprocess.run(
            [comspec, "/d", "/s", "/c", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(30, min(180, len(pairs))),
            check=False,
        )
        return result.returncode == 0 and all(link.is_dir() for link, _ in pairs)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            batch.unlink()
        except OSError:
            pass


def _copy_local_bin(source: Path, target: Path) -> bool:
    try:
        if target.exists():
            shutil.rmtree(target)
        # Follow symlinks: local CLI shims must resolve through this clone's
        # package junctions instead of pointing back into the prepared tree.
        shutil.copytree(source, target, symlinks=False)
        return True
    except OSError:
        return False


def _is_internal_windows_reparse(
    entry: Path,
    prepared_workspace_root: Path,
) -> tuple[bool, Optional[Path]]:
    if os.name != "nt":
        return False, None
    checker = getattr(os.path, "isjunction", None)
    is_junction = False
    if callable(checker):
        try:
            is_junction = bool(checker(entry))
        except OSError:
            is_junction = False
    if not (is_junction or entry.is_symlink()):
        return False, None
    try:
        target = entry.resolve(strict=True)
        relative = target.relative_to(prepared_workspace_root.resolve())
    except (OSError, ValueError):
        return True, None
    return True, relative


def _populate_node_modules_shell(
    prepared_root: Path,
    clone_root: Path,
    *,
    prepared_workspace_root: Path,
    clone_workspace_root: Path,
    reparse_plan: tuple[ReparseLink, ...],
) -> tuple[bool, list[Path]]:
    """Build a private node_modules shell from one canonical reparse plan.

    Scope directories are always private shells. Each package child is handled
    independently, so an owned workspace link can never carry a scope-level
    junction back into the PreparedArtifact.
    """
    clone_root.mkdir(parents=True, exist_ok=True)
    pairs: list[tuple[Path, Path]] = []
    local_cache_names = {".vite", ".vitest", ".cache"}
    plan_by_link = {item.link_relative: item for item in reparse_plan}

    def planned_target(entry: Path) -> Optional[Path]:
        try:
            relative = entry.relative_to(prepared_workspace_root).as_posix()
        except ValueError:
            return None
        item = plan_by_link.get(relative)
        if item is None:
            return None
        return clone_workspace_root.joinpath(*Path(item.target_relative).parts)

    def add_entry(entry: Path, destination: Path, *, scope_level: bool) -> bool:
        lowered = entry.name.lower()
        if lowered in local_cache_names:
            destination.mkdir(parents=True, exist_ok=True)
            return True
        if lowered == ".bin" and entry.is_dir():
            return _copy_local_bin(entry, destination)

        is_reparse, _legacy_relative = _is_internal_windows_reparse(
            entry, prepared_workspace_root
        )
        if is_reparse:
            target = planned_target(entry)
            if target is None:
                return False
            # A scope directory itself is authority-ambiguous. Only individual
            # package children may be workspace links.
            if entry.name.startswith("@") and not scope_level:
                return False
            pairs.append((destination, target))
            return True

        if entry.is_dir():
            if not scope_level and entry.name.startswith("@"):
                destination.mkdir(parents=True, exist_ok=True)
                try:
                    children = sorted(entry.iterdir(), key=lambda item: item.name.lower())
                except OSError:
                    return False
                return all(
                    add_entry(child, destination / child.name, scope_level=True)
                    for child in children
                )
            pairs.append((destination, entry.resolve()))
            return True
        if entry.is_file():
            shutil.copy2(entry, destination, follow_symlinks=True)
        return True

    try:
        entries = sorted(prepared_root.iterdir(), key=lambda item: item.name.lower())
        if not all(add_entry(entry, clone_root / entry.name, scope_level=False) for entry in entries):
            return False, []
        if not _create_junction_batch(pairs):
            for link, _ in reversed(pairs):
                _remove_junction(link)
            return False, []
        return True, [link for link, _ in pairs]
    except OSError:
        for link, _ in reversed(pairs):
            _remove_junction(link)
        return False, []


def _is_ephemeral_change(relative: str) -> bool:
    parts = [part.lower() for part in relative.replace("\\", "/").split("/") if part]
    return bool(parts) and parts[0] in {".vite", ".vitest", ".cache"}


class _DirectoryWatcher:
    FILE_ACTION_ADDED = 0x00000001
    FILE_ACTION_REMOVED = 0x00000002
    FILE_ACTION_MODIFIED = 0x00000003
    FILE_ACTION_RENAMED_OLD_NAME = 0x00000004
    FILE_ACTION_RENAMED_NEW_NAME = 0x00000005

    FILE_LIST_DIRECTORY = 0x0001
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILTER = (
        0x00000001  # FILE_NOTIFY_CHANGE_FILE_NAME
        | 0x00000002  # DIR_NAME
        | 0x00000004  # ATTRIBUTES
        | 0x00000008  # SIZE
        | 0x00000010  # LAST_WRITE
        | 0x00000040  # CREATION
    )
    ERROR_OPERATION_ABORTED = 995

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.events: list[tuple[int, str]] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._handle = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if os.name != "nt":
            return False
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(self.root),
            self.FILE_LIST_DIRECTORY,
            self.FILE_SHARE_READ | self.FILE_SHARE_WRITE | self.FILE_SHARE_DELETE,
            None,
            self.OPEN_EXISTING,
            self.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        handle_value = ctypes.cast(handle, ctypes.c_void_p).value if handle else None
        if handle_value in (None, 0, invalid):
            self.errors.append(f"CreateFileW failed for {self.root}: {ctypes.get_last_error()}")
            return False
        self._handle = handle
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID, wintypes.LPVOID,
        ]
        kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        buffer = ctypes.create_string_buffer(64 * 1024)
        returned = ctypes.c_ulong(0)
        try:
            while not self._stop.is_set():
                ok = kernel32.ReadDirectoryChangesW(
                    self._handle,
                    ctypes.byref(buffer),
                    len(buffer),
                    True,
                    self.FILTER,
                    ctypes.byref(returned),
                    None,
                    None,
                )
                if not ok:
                    error = ctypes.get_last_error()
                    if self._stop.is_set() and error == self.ERROR_OPERATION_ABORTED:
                        break
                    self.errors.append(
                        f"ReadDirectoryChangesW failed for {self.root}: {error}"
                    )
                    break
                size = int(returned.value)
                if size == 0:
                    self.errors.append(
                        f"ReadDirectoryChangesW buffer overflow for {self.root}"
                    )
                    break
                offset = 0
                while offset + 12 <= size:
                    raw = buffer.raw
                    next_offset = int.from_bytes(raw[offset:offset + 4], "little")
                    action = int.from_bytes(raw[offset + 4:offset + 8], "little")
                    name_len = int.from_bytes(raw[offset + 8:offset + 12], "little")
                    end = offset + 12 + name_len
                    if end > size:
                        self.errors.append(
                            f"ReadDirectoryChangesW malformed buffer for {self.root}"
                        )
                        break
                    name = raw[offset + 12:end].decode("utf-16-le", errors="replace")
                    if name:
                        self.events.append((action, name.replace("\\", "/")))
                    if next_offset == 0:
                        break
                    offset += next_offset
        finally:
            if self._handle not in (None, 0):
                try:
                    kernel32.CloseHandle(self._handle)
                except Exception:
                    pass
                self._handle = None

    def stop(self) -> None:
        if self._thread is None:
            return
        # Give the synchronous watcher one scheduling slice to consume the
        # final filesystem notification before cancelling an idle read.
        import time
        time.sleep(0.05)
        self._stop.set()
        if self._handle not in (None, 0):
            try:
                from ctypes import wintypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
                kernel32.CancelIoEx.restype = wintypes.BOOL
                kernel32.CancelIoEx(self._handle, None)
            except Exception as exc:
                self.errors.append(f"CancelIoEx failed for {self.root}: {exc}")
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            self.errors.append(f"dependency watcher did not stop for {self.root}")



def _classify_integrity_notification(
    prepared_root: Path,
    root: Path,
    action: int,
    relative: str,
    integrity_manifest: Mapping[str, str],
) -> str:
    """Return ignored | notification-only | mutation for one watcher event."""
    if action in {
        _DirectoryWatcher.FILE_ACTION_ADDED,
        _DirectoryWatcher.FILE_ACTION_REMOVED,
        _DirectoryWatcher.FILE_ACTION_RENAMED_OLD_NAME,
        _DirectoryWatcher.FILE_ACTION_RENAMED_NEW_NAME,
    }:
        return "mutation"
    if action != _DirectoryWatcher.FILE_ACTION_MODIFIED:
        return "mutation"

    target = root.joinpath(*[part for part in relative.replace("\\", "/").split("/") if part])
    try:
        if target.is_dir():
            return "ignored"
        if not target.is_file() and not target.is_symlink():
            return "mutation"
        expected = integrity_manifest.get(_integrity_key(target, prepared_root))
        if not expected:
            return "mutation"
        return "notification-only" if _fingerprint_path(target) == expected else "mutation"
    except (OSError, ValueError):
        return "mutation"


class _DependencyTreeGuard:
    """Guard a shared dependency lower tree without an O(files) pre-hash.

    The proof rule is stricter and cheaper than the legacy integrity-manifest
    path: while the exclusive snapshot lease is held, any non-ephemeral
    filesystem notification in a shared dependency root rejects the fast path.
    Watcher errors/overflow also reject it. Therefore a byte-for-byte baseline
    hash of every node_modules file is not needed to accept a *quiet* command.
    """

    def __init__(
        self,
        roots: list[Path],
    ) -> None:
        self.watchers = [_DirectoryWatcher(root) for root in roots]

    def start(self) -> bool:
        started: list[_DirectoryWatcher] = []
        for watcher in self.watchers:
            if not watcher.start():
                for item in started:
                    item.stop()
                return False
            started.append(watcher)
        return True

    def stop(self) -> GuardResult:
        mutations: list[str] = []
        errors: list[str] = []
        for watcher in self.watchers:
            watcher.stop()
            errors.extend(watcher.errors)
            for _action, event in watcher.events:
                if _is_ephemeral_change(event):
                    continue
                mutations.append(f"{watcher.root}:{event}")
        return GuardResult(
            mutations=tuple(sorted(set(mutations))),
            errors=tuple(errors),
            notification_only=(),
        )


def try_materialize_guarded_clone(
    *,
    source_project: Path,
    prepared_workspace_root: Path,
    project_relative: Path,
    target: Path,
    dependency_roots: Optional[tuple[str, ...]] = None,
    dependency_integrity: Optional[Mapping[str, str]] = None,
    progress: Optional[ProgressCallback] = None,
) -> Optional[Path]:
    """Materialize a fresh Windows upper over an exclusively guarded lower.

    `dependency_integrity` is accepted only for compatibility with pre-Ω
    callers. It is deliberately not consulted: Ω accepts a shared lower only
    when its watcher observed no meaningful event at all.
    """
    del dependency_integrity

    if os.name != "nt" or not guarded_lower_enabled():
        return None

    prepared_workspace_root = prepared_workspace_root.resolve()
    target = target.resolve()

    if dependency_roots:
        resolved_roots: list[Path] = []
        for raw in dependency_roots:
            relative = Path(str(raw))
            if relative.is_absolute() or ".." in relative.parts:
                return None
            candidate = (prepared_workspace_root / relative).resolve()
            try:
                candidate.relative_to(prepared_workspace_root)
            except ValueError:
                return None
            if not candidate.is_dir() or candidate.name != "node_modules":
                return None
            resolved_roots.append(candidate)
        dependency_root_paths = sorted(
            {os.path.normcase(str(item)): item for item in resolved_roots}.values(),
            key=lambda item: str(item).lower(),
        )
    else:
        dependency_root_paths = _dependency_roots(prepared_workspace_root)

    if not dependency_root_paths:
        return None

    try:
        canonical_reparse_plan = inventory_reparse_plan(prepared_workspace_root)
    except ReparseMaterializationError:
        return None

    operation_id = new_observability_id("guarded-lower")
    started = time.monotonic()
    emit_observability_event(
        "filesystem.guarded-lower.start",
        operationId=operation_id,
        schema="verification-substrate-v2",
        dependencyRoots=len(dependency_root_paths),
        sourceProject=str(source_project.resolve()),
    )

    lease = _try_acquire_snapshot_lease(
        prepared_workspace_root,
        progress=progress,
    )
    if lease is None:
        emit_observability_event(
            "filesystem.guarded-lower.finish",
            operationId=operation_id,
            schema="verification-substrate-v2",
            outcome="fallback",
            reason="snapshot-lease-unavailable",
            durationMs=max(0, int((time.monotonic() - started) * 1000)),
        )
        return None
    lease_transferred = False

    try:
        # Block X Source Truth already captured the exact .git database and
        # source/config bytes into the prepared tree. Copy that sealed upper
        # directly. Never create a --shared Git clone whose alternates point at
        # a live mutable checkout.
        excluded = list(dependency_root_paths)
        if not _run_overlay_robocopy(
            prepared_workspace_root,
            target,
            excluded,
            progress,
        ):
            shutil.rmtree(target, ignore_errors=True)
            return None

        junctions: list[Path] = []
        for dependency_root in dependency_root_paths:
            relative = dependency_root.relative_to(prepared_workspace_root)
            clone_root = target / relative
            ok, created = _populate_node_modules_shell(
                dependency_root,
                clone_root,
                prepared_workspace_root=prepared_workspace_root,
                clone_workspace_root=target,
                reparse_plan=canonical_reparse_plan,
            )
            if not ok:
                for existing in reversed(junctions):
                    _remove_junction(existing)
                shutil.rmtree(target, ignore_errors=True)
                return None
            junctions.extend(created)

        guard = _DependencyTreeGuard(dependency_root_paths)
        if not guard.start():
            for existing in reversed(junctions):
                _remove_junction(existing)
            shutil.rmtree(target, ignore_errors=True)
            return None

        project = target / project_relative
        if not project.is_dir():
            guard.stop()
            for existing in reversed(junctions):
                _remove_junction(existing)
            shutil.rmtree(target, ignore_errors=True)
            return None

        state = _GuardedClone(
            target_root=target,
            project=project,
            junctions=junctions,
            guard=guard,
            lease=lease,
        )
        with _ACTIVE_LOCK:
            _ACTIVE[_key(target)] = state
        lease_transferred = True

        if progress:
            progress(
                "Ω guarded lower ready: sealed source/config copied privately; "
                f"dependencyRoots={len(dependency_root_paths)}, "
                f"packageJunctions={len(junctions)}, integrityHashPass=skipped"
            )
        emit_observability_event(
            "filesystem.guarded-lower.finish",
            operationId=operation_id,
            schema="verification-substrate-v2",
            outcome="passed",
            dependencyRoots=len(dependency_root_paths),
            packageJunctions=len(junctions),
            integrityHashPass=False,
            durationMs=max(0, int((time.monotonic() - started) * 1000)),
        )
        return project
    except Exception as exc:
        if lease_transferred:
            cleanup_guarded_clone(target)
        else:
            shutil.rmtree(target, ignore_errors=True)
        emit_observability_event(
            "filesystem.guarded-lower.finish",
            operationId=operation_id,
            schema="verification-substrate-v2",
            outcome="fallback",
            reason=type(exc).__name__,
            durationMs=max(0, int((time.monotonic() - started) * 1000)),
        )
        return None
    finally:
        if not lease_transferred:
            lease.close()


def guarded_clone_is_active(target: Path) -> bool:
    with _ACTIVE_LOCK:
        return _key(target) in _ACTIVE


def stop_guarded_clone(target: Path) -> GuardResult:
    with _ACTIVE_LOCK:
        state = _ACTIVE.get(_key(target))
    if state is None:
        return GuardResult()
    if state.stopped is None:
        state.stopped = state.guard.stop()
    return state.stopped


def cleanup_guarded_clone(target: Path) -> GuardResult:
    with _ACTIVE_LOCK:
        state = _ACTIVE.pop(_key(target), None)
    if state is None:
        return GuardResult()
    result = state.stopped if state.stopped is not None else state.guard.stop()
    try:
        for junction in sorted(state.junctions, key=lambda item: len(item.parts), reverse=True):
            _remove_junction(junction)
    finally:
        # Lease survives watcher stop and the complete project command. It is
        # released only after every junction from this consumer is gone.
        state.lease.close()
    return result
