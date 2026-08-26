#!/usr/bin/env python3
"""Canonical inventory and reconstruction of links/reparse points."""
from __future__ import annotations

import dataclasses
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Optional


class ReparseMaterializationError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ReparseLink:
    link_relative: str
    target_relative: str
    link_kind: str
    authority: str
    package_name: str = ""

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


def is_windows_junction(path: Path) -> bool:
    if os.name != "nt":
        return False
    checker = getattr(os.path, "isjunction", None)
    if callable(checker):
        try:
            return bool(checker(path))
        except OSError:
            return False
    checker = getattr(path, "is_junction", None)
    if callable(checker):
        try:
            return bool(checker())
        except OSError:
            return False
    return False


def is_reparse(path: Path) -> bool:
    return path.is_symlink() or is_windows_junction(path)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _package_link_name(relative: Path) -> str:
    parts = relative.parts
    indexes = [index for index, part in enumerate(parts) if part == "node_modules"]
    if not indexes:
        return ""
    index = indexes[-1] + 1
    if index >= len(parts):
        return ""
    if parts[index].startswith("@"):
        if index + 2 != len(parts):
            return ""
        return f"{parts[index]}/{parts[index + 1]}"
    if index + 1 != len(parts):
        return ""
    return parts[index]


def inventory_reparse_plan(
    root: Path,
    *,
    excluded_dir_names: Iterable[str] = (),
    excluded_file_names: Iterable[str] = (),
    workspace_package_targets: Optional[Mapping[str, Path]] = None,
) -> tuple[ReparseLink, ...]:
    root = root.resolve()
    excluded_dirs = set(excluded_dir_names)
    excluded_files = set(excluded_file_names)
    workspace_targets = {
        str(name): Path(target).resolve()
        for name, target in (workspace_package_targets or {}).items()
    }
    links: list[ReparseLink] = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.lower())
        except OSError as exc:
            raise ReparseMaterializationError(
                f"REPARSE_INVENTORY_UNREADABLE: {directory}: {exc}"
            ) from exc
        for item in entries:
            path = Path(item.path)
            if item.name in excluded_files or item.name in excluded_dirs:
                continue
            try:
                relative = path.relative_to(root)
            except ValueError as exc:
                raise ReparseMaterializationError(
                    f"REPARSE_INVENTORY_ESCAPE: {path}"
                ) from exc
            if is_reparse(path):
                try:
                    target = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ReparseMaterializationError(
                        f"REPARSE_TARGET_UNAVAILABLE_OR_CYCLE: {relative.as_posix()}: {exc}"
                    ) from exc
                if not _within(target, root):
                    raise ReparseMaterializationError(
                        f"REPARSE_EXTERNAL_TARGET_UNSUPPORTED: {relative.as_posix()} -> {target}"
                    )
                if target == path or target in path.parents:
                    raise ReparseMaterializationError(
                        f"REPARSE_CYCLE_UNSUPPORTED: {relative.as_posix()} -> {target}"
                    )
                package_name = _package_link_name(relative)
                authority = "internal"
                if package_name:
                    expected = workspace_targets.get(package_name)
                    if workspace_package_targets is not None and expected is None:
                        raise ReparseMaterializationError(
                            "REPARSE_UNDECLARED_WORKSPACE_LINK: "
                            f"{relative.as_posix()}; package={package_name}"
                        )
                    if expected is not None:
                        try:
                            matches = target.samefile(expected)
                        except OSError:
                            matches = target == expected
                        if not matches:
                            raise ReparseMaterializationError(
                                "REPARSE_WORKSPACE_TARGET_MISMATCH: "
                                f"{relative.as_posix()} -> {target}; expected={expected}"
                            )
                        authority = "workspace-private-upper"
                try:
                    target_relative = target.relative_to(root).as_posix()
                except ValueError as exc:
                    raise ReparseMaterializationError(
                        f"REPARSE_TARGET_ESCAPE: {relative.as_posix()} -> {target}"
                    ) from exc
                links.append(ReparseLink(
                    relative.as_posix(),
                    target_relative,
                    "junction" if is_windows_junction(path) else (
                        "symlink-directory" if target.is_dir() else "symlink-file"
                    ),
                    authority,
                    package_name,
                ))
                continue
            try:
                mode = item.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise ReparseMaterializationError(
                    f"REPARSE_ENTRY_UNREADABLE: {path}: {exc}"
                ) from exc
            if stat.S_ISDIR(mode):
                walk(path)

    walk(root)
    return tuple(sorted(links, key=lambda item: item.link_relative.lower()))


def _remove_destination(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if is_windows_junction(path):
        os.rmdir(path)
    elif path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _create_junction(link: Path, target: Path) -> None:
    comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
    if not comspec:
        raise ReparseMaterializationError("REPARSE_JUNCTION_CREATE_UNAVAILABLE: cmd.exe")
    command = subprocess.list2cmdline(["mklink", "/J", str(link), str(target)])
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
    if result.returncode != 0 or not is_windows_junction(link):
        raise ReparseMaterializationError(
            f"REPARSE_JUNCTION_CREATE_FAILED: {link} -> {target}; exit={result.returncode}"
        )


def recreate_reparse_plan(
    source_root: Path,
    target_root: Path,
    plan: Iterable[ReparseLink],
) -> None:
    source_root.resolve()
    target_root = target_root.resolve()
    for item in sorted(
        plan,
        key=lambda value: (value.link_relative.count("/"), value.link_relative),
    ):
        link = target_root.joinpath(*Path(item.link_relative).parts)
        target = target_root.joinpath(*Path(item.target_relative).parts)
        try:
            link.relative_to(target_root)
            target.relative_to(target_root)
        except ValueError as exc:
            raise ReparseMaterializationError(
                f"REPARSE_RECONSTRUCTION_ESCAPE: {item.link_relative}"
            ) from exc
        if not target.exists():
            raise ReparseMaterializationError(
                "REPARSE_RECONSTRUCTION_TARGET_MISSING: "
                f"{item.link_relative} -> {item.target_relative}"
            )
        _remove_destination(link)
        link.parent.mkdir(parents=True, exist_ok=True)
        if item.link_kind == "junction":
            if os.name == "nt":
                _create_junction(link, target)
            else:
                os.symlink(str(target), str(link), target_is_directory=True)
        else:
            os.symlink(
                str(target),
                str(link),
                target_is_directory=item.link_kind == "symlink-directory",
            )


def plan_from_mapping(values: Iterable[Mapping[str, str]]) -> tuple[ReparseLink, ...]:
    result = []
    for value in values:
        result.append(ReparseLink(
            str(value.get("link_relative") or ""),
            str(value.get("target_relative") or ""),
            str(value.get("link_kind") or ""),
            str(value.get("authority") or ""),
            str(value.get("package_name") or ""),
        ))
    if any(not item.link_relative or not item.target_relative for item in result):
        raise ReparseMaterializationError("REPARSE_PLAN_RECORD_INVALID")
    return tuple(sorted(result, key=lambda item: item.link_relative.lower()))
