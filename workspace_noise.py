#!/usr/bin/env python3
"""Shared policy for editor/OS workspace noise that must never block automation."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable, List

IDE_OS_DIRECTORIES = frozenset({'.idea', '.vs', '.vscode', '.fleet', '.history'})
IDE_OS_BASENAMES = frozenset({'.DS_Store', 'Thumbs.db', 'desktop.ini'})
IDE_OS_SUFFIXES = ('.swp', '.swo', '.suo', '.user', '.userosscache', '.sln.docstates')


def normalize_repo_path(value: str) -> str:
    value = str(value or '').strip().strip('"').replace('\\', '/').removeprefix('./')
    # porcelain rename/copy line in non-z mode: "old -> new". Either endpoint
    # being editor noise makes the entry irrelevant to dependency automation.
    if ' -> ' in value:
        value = value.split(' -> ', 1)[-1]
    return str(PurePosixPath(value)) if value else ''


def is_ignorable_workspace_path(value: str) -> bool:
    normalized = normalize_repo_path(value)
    if not normalized:
        return False
    parts = [part for part in normalized.split('/') if part]
    basename = parts[-1] if parts else ''
    return (
        any(part in IDE_OS_DIRECTORIES for part in parts)
        or basename in IDE_OS_BASENAMES
        or any(basename.endswith(suffix) for suffix in IDE_OS_SUFFIXES)
        or basename.endswith('~')
    )


def porcelain_path(entry: str) -> str:
    """Extract path from one git status --porcelain=v1 entry."""
    text = entry.rstrip('\r\n\0')
    if len(text) >= 4 and text[2] == ' ':
        return text[3:]
    return text.strip()


def relevant_porcelain_entries(output: str, *, nul: bool = False) -> List[str]:
    """Return git status entries excluding editor/OS noise for *all* statuses.

    `-z` rename/copy output includes a second path token. The first token carries
    the status and destination path; the following source-path token is skipped.
    """
    raw = output.split('\0') if nul else output.splitlines()
    result: List[str] = []
    skip_next_path = False
    for item in raw:
        if not item:
            continue
        if nul and skip_next_path and not (len(item) >= 4 and item[2] == ' '):
            skip_next_path = False
            continue
        path = porcelain_path(item)
        status = item[:2] if len(item) >= 2 else ''
        if not is_ignorable_workspace_path(path):
            result.append(item)
        if nul and ('R' in status or 'C' in status):
            skip_next_path = True
    return result


def relevant_porcelain(output: str, *, nul: bool = False) -> str:
    separator = '\0' if nul else '\n'
    return separator.join(relevant_porcelain_entries(output, nul=nul))


def git_exclude_pathspecs() -> List[str]:
    """Git pathspec exclusions used when safety-stashing real project changes."""
    specs: List[str] = []
    for directory in sorted(IDE_OS_DIRECTORIES):
        specs.append(f':(glob,exclude)**/{directory}/**')
    for basename in sorted(IDE_OS_BASENAMES):
        specs.append(f':(glob,exclude)**/{basename}')
    for suffix in IDE_OS_SUFFIXES:
        specs.append(f':(glob,exclude)**/*{suffix}')
    specs.append(':(glob,exclude)**/*~')
    return specs
