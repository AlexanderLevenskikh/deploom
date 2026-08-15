#!/usr/bin/env python3
"""Create a clean squash release branch and run hooks exactly once.

All intermediate dependency-roadmap commits/merges are expected to use the
`skip` hook policy.  This command starts from the verified source commit,
squashes the integration branch, removes tool-only audit evidence from the
resulting tree, optionally runs configured final gate commands, and performs a
normal Git commit without `--no-verify` or an overridden hooksPath.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional

from cli_io import configure_utf8_stdio
from git_hook_policy import GitHookPolicyError, run_git
from workspace_noise import git_exclude_pathspecs, relevant_porcelain

DEFAULT_WORKSPACE = ".dependency-roadmap-audit"
DEFAULT_COMMIT_MESSAGE = "chore(deps): update dependencies"

# Every sibling work branch used to write the same flat doc file (created
# fresh from the same base commit on every branch), so merging any two
# branches produced an add/add conflict on all three every time. The compact
# prompt now has each branch write to its own shard under one of these
# directories instead; this assembles the shards into the flat files the
# team actually reads, deterministically, once, right before the release
# commit -- after every branch is already merged and no more shards can
# appear.
SHARDED_DOC_NAMES = (
    "dependency-upgrades",
    "dependency-update-summary",
    "dependency-update-review-notes",
)


def _natural_sort_key(name: str) -> tuple:
    return tuple(int(chunk) if chunk.isdigit() else chunk for chunk in re.split(r"(\d+)", name))


def _assemble_sharded_docs(project: Path) -> List[str]:
    assembled: List[str] = []
    for doc_name in SHARDED_DOC_NAMES:
        shard_dir = project / "docs" / doc_name
        if not shard_dir.is_dir():
            continue
        shard_files = sorted(shard_dir.glob("*.md"), key=lambda path: _natural_sort_key(path.stem))
        if shard_files:
            flat_path = project / "docs" / f"{doc_name}.md"
            existing = flat_path.read_text(encoding="utf-8") if flat_path.exists() else ""
            shards = "\n\n".join(path.read_text(encoding="utf-8").strip() for path in shard_files)
            flat_path.parent.mkdir(parents=True, exist_ok=True)
            flat_path.write_text(f"{existing.rstrip()}\n\n{shards}\n".lstrip(), encoding="utf-8")
            assembled.append(flat_path.relative_to(project).as_posix())
        shutil.rmtree(shard_dir)
    return assembled


class ReleaseBranchError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _git(
    project: Path,
    args: Iterable[str],
    *,
    skip_hooks: bool,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = run_git(project, args, skip_hooks=skip_hooks, check=False, capture_output=True)
    except (OSError, GitHookPolicyError) as exc:
        raise ReleaseBranchError("RELEASE_GIT_COMMAND_FAILED", str(exc)) from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise ReleaseBranchError(
            "RELEASE_GIT_COMMAND_FAILED",
            f"git {' '.join(str(value) for value in args)}: {detail[-1600:]}",
        )
    return completed


def _git_value(project: Path, args: Iterable[str]) -> str:
    return _git(project, args, skip_hooks=False).stdout.strip()


def _require_clean(project: Path) -> None:
    status = relevant_porcelain(_git_value(project, ["status", "--porcelain=v1", "--untracked-files=all"]))
    if status:
        raise ReleaseBranchError("RELEASE_CHECKOUT_DIRTY", "; ".join(status.splitlines()[:16]))


def _stash_dirty_worktree(project: Path) -> Optional[Dict[str, str]]:
    status = relevant_porcelain(_git_value(project, ["status", "--porcelain=v1", "--untracked-files=all"]))
    if not status:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    message = f"dependency-flow-before-release-{stamp}"
    # Safety stash real project changes only; editor/OS state remains untouched.
    _git(
        project,
        ["stash", "push", "--include-untracked", "--message", message, "--", ".", *git_exclude_pathspecs()],
        skip_hooks=True,
    )
    stash_commit = _git_value(project, ["rev-parse", "refs/stash"])
    print(
        f"[release] Незакоммиченные изменения сохранены в safety stash {stash_commit} ({message}). "
        f"Восстановление: git stash apply {stash_commit}",
        flush=True,
    )
    return {"commit": stash_commit, "message": message, "restoreCommand": f"git stash apply {stash_commit}"}


def _branch_exists(project: Path, branch: str) -> bool:
    return _git(
        project,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        skip_hooks=False,
        check=False,
    ).returncode == 0


def _safe_workspace(value: str) -> str:
    normalized = str(PurePosixPath(str(value or DEFAULT_WORKSPACE).replace("\\", "/")))
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or normalized in {"", "."}:
        raise ReleaseBranchError("RELEASE_WORKSPACE_INVALID", repr(value))
    return normalized


def _run_gate(project: Path, command: str) -> Dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=str(project),
        shell=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=os.environ.copy(),
    )
    result = {
        "command": command,
        "exitCode": completed.returncode,
        "stdoutTail": completed.stdout[-2000:],
        "stderrTail": completed.stderr[-2000:],
    }
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "final gate failed").strip()
        raise ReleaseBranchError("RELEASE_FINAL_GATE_FAILED", f"{command}: {detail[-1600:]}")
    return result


def _staged_tree(project: Path) -> str:
    return _git_value(project, ["write-tree"])


def _assert_staged_tree_matches_merged(
    project: Path,
    staged_tree: str,
    merged_branch: str,
    workspace: str,
    extra_allowed_prefixes: Iterable[str] = (),
) -> None:
    changed = _git_value(project, ["diff", "--name-only", staged_tree, merged_branch])
    allowed = (workspace, *extra_allowed_prefixes)

    def is_allowed(line: str) -> bool:
        return any(line == prefix or line.startswith(prefix.rstrip("/") + "/") for prefix in allowed)

    outside = [line for line in changed.splitlines() if line and not is_allowed(line)]
    if outside:
        raise ReleaseBranchError(
            "RELEASE_TREE_MISMATCH",
            "staged release differs from merged branch outside the managed audit workspace: "
            + ", ".join(outside[:30]),
        )


def _assert_workspace_absent_from_commit(project: Path, commit: str, workspace: str) -> None:
    listed = _git_value(project, ["ls-tree", "-r", "--name-only", commit, "--", workspace])
    if listed:
        raise ReleaseBranchError("RELEASE_AUDIT_WORKSPACE_COMMITTED", listed.replace("\n", ", "))


def _current_branch(project: Path) -> str:
    return _git_value(project, ["symbolic-ref", "--quiet", "--short", "HEAD"])


def _has_staged_changes(project: Path) -> bool:
    return _git(project, ["diff", "--cached", "--quiet"], skip_hooks=False, check=False).returncode != 0


def _unstaged_or_untracked(project: Path) -> List[str]:
    unstaged = _git_value(project, ["diff", "--name-only"]).splitlines()
    untracked = _git_value(project, ["ls-files", "--others", "--exclude-standard"]).splitlines()
    return sorted({line for line in [*unstaged, *untracked] if line})


def create_release_branch(
    project: Path,
    source_branch: str,
    source_commit: str,
    merged_branch: str,
    release_branch: str,
    *,
    workspace: str = DEFAULT_WORKSPACE,
    commit_message: str = DEFAULT_COMMIT_MESSAGE,
    gate_commands: Optional[List[str]] = None,
    remote: str = "origin",
    push: bool = False,
) -> Dict[str, Any]:
    project = project.resolve()
    workspace = _safe_workspace(workspace)
    gate_commands = list(gate_commands or [])

    if not _branch_exists(project, merged_branch):
        raise ReleaseBranchError("RELEASE_MERGED_BRANCH_NOT_FOUND", merged_branch)

    source_actual = _git_value(project, ["rev-parse", source_branch])
    if source_actual != source_commit:
        raise ReleaseBranchError(
            "RELEASE_SOURCE_COMMIT_MISMATCH",
            f"{source_branch}={source_actual}, expected={source_commit}",
        )
    merged_commit = _git_value(project, ["rev-parse", merged_branch])

    release_exists = _branch_exists(project, release_branch)
    resumed = False
    safety_stash: Optional[Dict[str, str]] = None
    assembled_docs: List[str] = []
    if release_exists:
        # A failed final gate or hook intentionally leaves the release branch at
        # sourceCommit with the full squash staged.  Allow the same command to
        # resume that exact state; reject every other pre-existing branch.
        current = _current_branch(project)
        release_head = _git_value(project, ["rev-parse", release_branch])
        if current == release_branch and release_head == source_commit and _has_staged_changes(project):
            resumed = True
        else:
            raise ReleaseBranchError(
                "RELEASE_BRANCH_ALREADY_EXISTS",
                f"{release_branch}; only an active staged retry at {source_commit} can be resumed",
            )
    else:
        safety_stash = _stash_dirty_worktree(project)
        # Checkout/create/squash are internal lifecycle operations and
        # deliberately use an empty command-local hooksPath.
        _git(project, ["checkout", source_branch], skip_hooks=True)
        if _git_value(project, ["rev-parse", "HEAD"]) != source_commit:
            raise ReleaseBranchError("RELEASE_SOURCE_CHECKOUT_MISMATCH", source_branch)
        _git(project, ["checkout", "-b", release_branch, source_commit], skip_hooks=True)

        # Explicit --ff overrides a repository/global merge.ff=false setting.
        # Without it Git treats the configured value as --no-ff, which is
        # incompatible with --squash. A squash merge never commits by itself,
        # so --no-commit is unnecessary.
        squash = _git(project, ["merge", "--squash", "--ff", merged_branch], skip_hooks=True, check=False)
        if squash.returncode != 0:
            detail = (squash.stderr or squash.stdout or "squash merge failed").strip()
            raise ReleaseBranchError("RELEASE_SQUASH_FAILED", detail[-1600:])

        workspace_path = project / workspace
        if workspace_path.exists():
            marker = workspace_path / ".dependency-roadmap-audit-workspace.json"
            if not marker.exists():
                raise ReleaseBranchError("RELEASE_WORKSPACE_NOT_TOOL_MANAGED", str(workspace_path))
            shutil.rmtree(workspace_path)
            _git(project, ["add", "-A", "--", workspace], skip_hooks=True)

        # Every work branch wrote its migration docs to a per-branch shard
        # (see the compact prompt), so sibling branches never conflicted
        # merging into `merged`. Assemble those shards into the flat docs the
        # team actually reads now, once, deterministically -- after every
        # branch is already merged so no further shards can appear.
        assembled_docs = _assemble_sharded_docs(project)
        if assembled_docs:
            _git(project, ["add", "-A", "--", "docs"], skip_hooks=True)

    if not _has_staged_changes(project):
        raise ReleaseBranchError("RELEASE_EMPTY_SQUASH", f"{merged_branch} has no releasable changes over {source_branch}")

    audit_paths = _git_value(project, ["diff", "--cached", "--name-only", "--", workspace])
    if audit_paths:
        raise ReleaseBranchError("RELEASE_AUDIT_WORKSPACE_STAGED", audit_paths.replace("\n", ", "))

    doc_allowed_prefixes = tuple(
        path
        for name in SHARDED_DOC_NAMES
        for path in (f"docs/{name}", f"docs/{name}.md")
    )
    pre_hook_staged_tree = _staged_tree(project)
    _assert_staged_tree_matches_merged(project, pre_hook_staged_tree, merged_branch, workspace, doc_allowed_prefixes)
    gate_results = [_run_gate(project, command) for command in gate_commands]
    dirty_after_gates = _unstaged_or_untracked(project)
    if dirty_after_gates:
        raise ReleaseBranchError(
            "RELEASE_FINAL_GATE_DIRTY",
            "final gate commands changed tracked or untracked files without staging them: "
            + ", ".join(dirty_after_gates[:30]),
        )

    # A gate may intentionally update the staged tree. Revalidate the exact
    # release content after all gates and before repository hooks run.
    pre_hook_staged_tree = _staged_tree(project)
    _assert_staged_tree_matches_merged(project, pre_hook_staged_tree, merged_branch, workspace, doc_allowed_prefixes)

    body = (
        commit_message
        + "\n\n"
        + "Dependency-Roadmap-Managed: release\n"
        + f"Dependency-Roadmap-Source: {source_commit}\n"
        + f"Dependency-Roadmap-Merged: {merged_commit}\n"
        + "Dependency-Roadmap-Hooks-Bypassed: false"
    )

    # This is the one commit in the lifecycle that intentionally uses the
    # repository's configured hooks. No --no-verify and no hooksPath override.
    commit = _git(project, ["commit", "-m", body], skip_hooks=False, check=False)
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout or "release commit or hook failed").strip()
        raise ReleaseBranchError("RELEASE_COMMIT_OR_HOOK_FAILED", detail[-2000:])

    release_commit = _git_value(project, ["rev-parse", "HEAD"])
    _assert_workspace_absent_from_commit(project, release_commit, workspace)
    _require_clean(project)

    hook_changed = _git_value(project, ["diff", "--name-only", pre_hook_staged_tree, release_commit])
    hook_modified_files = [line for line in hook_changed.splitlines() if line]

    if push:
        # Normal push keeps pre-push hooks enabled for the release branch.
        pushed = _git(project, ["push", "-u", remote, release_branch], skip_hooks=False, check=False)
        if pushed.returncode != 0:
            detail = (pushed.stderr or pushed.stdout or "release push or pre-push hook failed").strip()
            raise ReleaseBranchError("RELEASE_PUSH_OR_HOOK_FAILED", detail[-2000:])

    return {
        "released": True,
        "resumed": resumed,
        "strategy": "squash",
        "sourceBranch": source_branch,
        "sourceCommit": source_commit,
        "mergedBranch": merged_branch,
        "mergedCommit": merged_commit,
        "releaseBranch": release_branch,
        "releaseCommit": release_commit,
        "preHookStagedTree": pre_hook_staged_tree,
        "hookModifiedFiles": hook_modified_files,
        "auditWorkspaceRemoved": True,
        "shardedDocsAssembled": assembled_docs,
        "safetyStash": safety_stash,
        "intermediateHooksPolicy": "skip",
        "releaseCommitHooksBypassed": False,
        "releasePushHooksBypassed": False if push else None,
        "finalGateCommands": gate_results,
        "pushed": push,
    }

def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Create a clean squash release branch and run repository hooks on its final commit.")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--source-branch", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--merged-branch", required=True)
    parser.add_argument("--release-branch", required=True)
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    parser.add_argument("--commit-message", default=DEFAULT_COMMIT_MESSAGE)
    parser.add_argument("--gate-command", action="append", default=[])
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    try:
        result = create_release_branch(
            Path(args.project_dir),
            args.source_branch,
            args.source_commit,
            args.merged_branch,
            args.release_branch,
            workspace=args.workspace,
            commit_message=args.commit_message,
            gate_commands=args.gate_command,
            remote=args.remote,
            push=args.push,
        )
    except ReleaseBranchError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
