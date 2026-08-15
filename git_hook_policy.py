#!/usr/bin/env python3
"""Run Git commands with an explicit hook policy.

Intermediate dependency-roadmap operations use a temporary empty hooksPath so
project hooks do not run for audit, work, merge, cleanup, or internal push
steps.  The final release commit deliberately uses the repository's normal
hook configuration.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional

from cli_io import configure_utf8_stdio


class GitHookPolicyError(RuntimeError):
    pass


def run_git(
    project: Path,
    args: Iterable[str],
    *,
    skip_hooks: bool,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git in *project* with hooks explicitly skipped or enabled.

    `--no-verify` does not cover every hook-capable Git command.  A temporary
    empty `core.hooksPath` is command-local, works with normal Git and Husky,
    and never mutates repository/global configuration.
    """

    project = project.resolve()
    git_args: List[str] = [str(value) for value in args]

    def invoke(prefix: List[str]) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", "-C", str(project), *prefix, *git_args],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=capture_output,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "git command failed").strip()
            raise GitHookPolicyError(
                f"git {' '.join(git_args)} failed with exit={completed.returncode}: {detail[-1600:]}"
            )
        return completed

    if not skip_hooks:
        return invoke([])

    with tempfile.TemporaryDirectory(prefix="dependency-roadmap-empty-hooks-") as hooks_dir:
        return invoke(["-c", f"core.hooksPath={Path(hooks_dir).resolve()}"])


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Run one Git command with an explicit dependency-roadmap hook policy.")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--mode", choices=("skip", "run"), required=True)
    parser.add_argument("git_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    git_args = list(args.git_args)
    if git_args and git_args[0] == "--":
        git_args = git_args[1:]
    if not git_args:
        parser.error("a Git command is required after --")

    try:
        completed = run_git(
            Path(args.project_dir),
            git_args,
            skip_hooks=args.mode == "skip",
            check=False,
            capture_output=True,
        )
    except (OSError, GitHookPolicyError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2

    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=__import__("sys").stderr)
    if completed.returncode == 0:
        print(json.dumps({"hookPolicy": args.mode, "gitArgs": git_args}, ensure_ascii=False))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
