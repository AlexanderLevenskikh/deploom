from __future__ import annotations

import ctypes
import dataclasses
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

ProgressCallback = Callable[[str], None]


@dataclasses.dataclass(frozen=True)
class GuardResult:
    mutations: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclasses.dataclass
class _GuardedClone:
    target_root: Path
    project: Path
    junctions: list[Path]
    guard: "_DependencyTreeGuard"
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
        robocopy, str(source), str(target),
        "/E", "/COPY:DAT", "/DCOPY:DAT", "/R:1", "/W:1",
        "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/SL", "/MT:32",
    ]
    if excluded:
        argv.extend(["/XD", *[str(path) for path in excluded]])
    if progress:
        progress("NTFS fast clone: overlaying prepared source/config without dependency bytes")
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


def _populate_node_modules_shell(
    prepared_root: Path,
    clone_root: Path,
) -> tuple[bool, list[Path]]:
    clone_root.mkdir(parents=True, exist_ok=True)
    pairs: list[tuple[Path, Path]] = []
    local_cache_names = {".vite", ".vitest", ".cache"}
    try:
        for entry in sorted(prepared_root.iterdir(), key=lambda item: item.name.lower()):
            name = entry.name
            destination = clone_root / name
            lowered = name.lower()
            if lowered in local_cache_names:
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if lowered == ".bin" and entry.is_dir():
                if not _copy_local_bin(entry, destination):
                    return False, []
                continue
            if entry.is_dir():
                pairs.append((destination, entry.resolve()))
                continue
            if entry.is_file():
                shutil.copy2(entry, destination, follow_symlinks=True)
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
    return ".vite" in parts or ".vitest" in parts


class _DirectoryWatcher:
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
        self.events: list[str] = []
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
                    name_len = int.from_bytes(raw[offset + 8:offset + 12], "little")
                    end = offset + 12 + name_len
                    if end > size:
                        self.errors.append(
                            f"ReadDirectoryChangesW malformed buffer for {self.root}"
                        )
                        break
                    name = raw[offset + 12:end].decode("utf-16-le", errors="replace")
                    if name:
                        self.events.append(name.replace("\\", "/"))
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


class _DependencyTreeGuard:
    def __init__(self, roots: list[Path]) -> None:
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
            for event in watcher.events:
                if not _is_ephemeral_change(event):
                    mutations.append(f"{watcher.root}:{event}")
        return GuardResult(
            mutations=tuple(sorted(set(mutations))),
            errors=tuple(errors),
        )


def try_materialize_guarded_clone(
    *,
    source_project: Path,
    prepared_workspace_root: Path,
    project_relative: Path,
    target: Path,
    progress: Optional[ProgressCallback] = None,
) -> Optional[Path]:
    if os.name != "nt":
        return None
    source_project = source_project.resolve()
    source_root = _git_root(source_project)
    if source_root is None:
        return None
    prepared_workspace_root = prepared_workspace_root.resolve()
    target = target.resolve()
    dependency_roots = _dependency_roots(prepared_workspace_root)
    if not dependency_roots:
        return None

    try:
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--shared", "--no-hardlinks", str(source_root), str(target)],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        if clone.returncode != 0:
            return None

        excluded = [prepared_workspace_root / ".git", *dependency_roots]
        if not _run_overlay_robocopy(
            prepared_workspace_root, target, excluded, progress
        ):
            shutil.rmtree(target, ignore_errors=True)
            return None

        junctions: list[Path] = []
        clone_dependency_roots: list[Path] = []
        for dependency_root in dependency_roots:
            relative = dependency_root.relative_to(prepared_workspace_root)
            clone_root = target / relative
            ok, created = _populate_node_modules_shell(dependency_root, clone_root)
            if not ok:
                for existing in reversed(junctions):
                    _remove_junction(existing)
                shutil.rmtree(target, ignore_errors=True)
                return None
            junctions.extend(created)
            clone_dependency_roots.append(clone_root.resolve())

        guard_roots = [
            *dependency_roots,
            *clone_dependency_roots,
        ]
        guard = _DependencyTreeGuard(guard_roots)
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
        )
        with _ACTIVE_LOCK:
            _ACTIVE[_key(target)] = state
        if progress:
            progress(
                f"NTFS fast clone ready: source/config copied, "
                f"{len(junctions)} package payload junction(s) mounted under mutation guard"
            )
        return project
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        return None


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
    for junction in sorted(state.junctions, key=lambda item: len(item.parts), reverse=True):
        _remove_junction(junction)
    return result
