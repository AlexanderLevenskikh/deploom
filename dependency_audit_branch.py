#!/usr/bin/env python3
"""Legacy audit-branch compatibility helpers.

Ordinary roadmap generation never calls the capture CLI and never creates an
npm lockfile for a Yarn project. The supported user-facing vulnerability check
is ``manual_dependency_audit.py`` via ``scripts/audit-project.*``, which stores
its isolated bridge under roadmap artifacts. This module remains only for safe
recovery/cleanup of workspaces created by older kit versions and for backwards
compatibility with existing runs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from cli_io import configure_utf8_stdio
from manual_dependency_audit import build_report, markdown
from git_hook_policy import run_git
from workspace_noise import relevant_porcelain

MANAGED_BY = "dependency-roadmap-tool"
DEFAULT_WORKSPACE = ".dependency-roadmap-audit"
DEFAULT_CAPTURE_MESSAGE = "chore(deps): capture dependency audit baseline"
DEFAULT_FINAL_MESSAGE = "chore(deps): capture final dependency audit"
DEFAULT_CLEANUP_MESSAGE = "chore(deps): remove dependency audit workspace"


class AuditBranchError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _git(
    project: Path,
    args: List[str],
    check: bool = True,
    *,
    skip_hooks: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = run_git(
            project,
            args,
            skip_hooks=skip_hooks,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise AuditBranchError("AUDIT_GIT_COMMAND_FAILED", str(exc)) from exc
    if check and completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "git command failed").strip()
        raise AuditBranchError("AUDIT_GIT_COMMAND_FAILED", f"git {' '.join(args)}: {message[-1200:]}")
    return completed


def _git_value(project: Path, args: List[str]) -> str:
    return _git(project, args).stdout.strip()


def _clean(project: Path) -> bool:
    status = _git_value(project, ["status", "--porcelain=v1", "--untracked-files=all"])
    return not relevant_porcelain(status)


def _require_clean(project: Path, code: str = "AUDIT_CHECKOUT_DIRTY") -> None:
    status = relevant_porcelain(_git_value(project, ["status", "--porcelain=v1", "--untracked-files=all"]))
    if status:
        preview = "; ".join(status.splitlines()[:12])
        raise AuditBranchError(code, preview)


def _workspace_rel(value: str) -> str:
    normalized = str(PurePosixPath(str(value or DEFAULT_WORKSPACE).replace("\\", "/")))
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or normalized in {"", "."}:
        raise AuditBranchError("AUDIT_WORKSPACE_INVALID", f"workspace must be a safe project-relative path: {value!r}")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _branch_exists(project: Path, branch: str) -> bool:
    return _git(project, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False).returncode == 0


def _current_branch(project: Path) -> str:
    return _git_value(project, ["symbolic-ref", "--quiet", "--short", "HEAD"])


def _branch_manifest(project: Path, branch: str, workspace: str) -> Optional[Dict[str, Any]]:
    result = _git(project, ["show", f"{branch}:{workspace}/branch-manifest.json"], check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AuditBranchError("AUDIT_BRANCH_MANIFEST_INVALID", str(exc)) from exc
    return value if isinstance(value, dict) else None


def _only_workspace_diff(project: Path, source_commit: str, workspace: str, ref: str = "HEAD") -> None:
    output = _git_value(project, ["diff", "--name-only", f"{source_commit}..{ref}"])
    outside = [
        line for line in output.splitlines()
        if line and line != workspace and not line.startswith(workspace.rstrip("/") + "/")
    ]
    if outside:
        raise AuditBranchError(
            "AUDIT_BRANCH_HAS_NON_AUDIT_CHANGES",
            "audit/base branch contains project changes outside the managed workspace: " + ", ".join(outside[:20]),
        )


def _normalized_registry(value: Any) -> str:
    return str(value or "").strip().rstrip("/").lower()


def _cached_prepare_result(
    project: Path,
    branch: str,
    previous: Optional[Dict[str, Any]],
    source_commit: str,
    registry: str,
    workspace: str,
) -> Optional[Dict[str, Any]]:
    if not previous or previous.get("managedBy") != MANAGED_BY:
        return None
    package_json = project / "package.json"
    yarn_lock = project / "yarn.lock"
    if str(previous.get("sourceCommit") or "") != source_commit:
        return None
    if str(previous.get("packageJsonSha256") or "") != _sha256(package_json):
        return None
    if str(previous.get("sourceLockfileSha256") or "") != (_sha256(yarn_lock) if yarn_lock.exists() else ""):
        return None
    if _normalized_registry(previous.get("requestedRegistry")) != _normalized_registry(registry):
        return None
    if not bool(previous.get("vulnerabilityAuditComplete")):
        return None
    _only_workspace_diff(project, source_commit, workspace, branch)
    commit = _git_value(project, ["rev-parse", branch])
    return {
        "prepared": True,
        "reused": True,
        "branch": branch,
        "commit": commit,
        "workspace": workspace,
        "sourceCommit": source_commit,
        "auditEngine": previous.get("auditEngine"),
        "effectiveAuditRegistry": previous.get("effectiveAuditRegistry"),
        "vulnerabilityAuditComplete": previous.get("vulnerabilityAuditComplete"),
        "manualAuditComplete": previous.get("manualAuditComplete"),
        "vulnerabilityTotals": previous.get("vulnerabilityTotals") or {},
        "cleanupRequired": True,
    }


def _write_capture(
    project: Path,
    project_name: str,
    registry: str,
    workspace_rel: str,
    source_remote: str,
    source_branch: str,
    source_commit: str,
    dashboard_state: Optional[Path],
    phase: str,
) -> Dict[str, Any]:
    workspace = project / workspace_rel
    report = build_report(
        project=project,
        project_name=project_name,
        registry=registry,
        lag_months=12,
        dashboard_state=dashboard_state,
        audit_workspace=workspace,
        project_dir_display=".",
    )
    md = markdown(report)
    (workspace / "manual-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (workspace / "manual-audit.md").write_text(md, encoding="utf-8")
    package_json = project / "package.json"
    lockfile = project / "yarn.lock"
    manifest = {
        "schemaVersion": 1,
        "managedBy": MANAGED_BY,
        "phase": phase,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "project": project_name,
        "sourceRemote": source_remote,
        "sourceBranch": source_branch,
        "sourceCommit": source_commit,
        "captureBranch": _current_branch(project),
        "workspace": workspace_rel,
        "packageJsonSha256": _sha256(package_json),
        "sourceLockfile": "yarn.lock" if lockfile.exists() else "",
        "sourceLockfileSha256": _sha256(lockfile) if lockfile.exists() else "",
        "auditEngine": report.get("audit", {}).get("engine"),
        "requestedRegistry": registry,
        "effectiveAuditRegistry": report.get("audit", {}).get("effectiveRegistry"),
        "vulnerabilityAuditComplete": bool(report.get("audit", {}).get("complete")),
        "manualAuditComplete": bool(report.get("complete")),
        "vulnerabilityTotals": report.get("audit", {}).get("totals", {}),
        "lagging": sum(1 for item in report.get("lag", []) if item.get("status") == "lagging"),
        "lagOk": sum(1 for item in report.get("lag", []) if item.get("status") == "ok"),
        "lagUnknown": sum(1 for item in report.get("lag", []) if item.get("status") == "unknown"),
        "cleanupRequiredBeforeSourceMerge": True,
        "intermediateHooksSkipped": True,
    }
    (workspace / "branch-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"report": report, "manifest": manifest}


def _commit_workspace(project: Path, workspace: str, message: str) -> str:
    _git(project, ["add", "-A", "-f", "--", workspace])
    staged = _git(project, ["diff", "--cached", "--quiet", "--", workspace], check=False)
    if staged.returncode == 0:
        return _git_value(project, ["rev-parse", "HEAD"])
    body = (
        message
        + "\n\nDependency-Roadmap-Managed: audit-bootstrap"
        + "\nDependency-Roadmap-Hooks-Bypassed: true"
    )
    commit = _git(project, ["commit", "-m", body], check=False, skip_hooks=True)
    if commit.returncode != 0:
        message_text = (commit.stderr or commit.stdout or "git commit failed").strip()
        raise AuditBranchError("AUDIT_BOOTSTRAP_COMMIT_FAILED", message_text[-1200:])
    return _git_value(project, ["rev-parse", "HEAD"])


def _discard_managed_workspace_changes(project: Path, workspace: str) -> None:
    """Discard only tool-managed workspace changes after an interrupted capture.

    This never resets unrelated project files.  The implementation handles the
    awkward case where the audit directory is ignored by the project but was
    staged before an internal commit failed: ``git clean -fd`` does not remove
    ignored files, so we explicitly unstage the path and remove only the
    verified tool-managed directory when it is absent from ``HEAD``.
    """

    _git(project, ["reset", "-q", "HEAD", "--", workspace], check=False, skip_hooks=True)
    head_has_workspace = _git(
        project,
        ["cat-file", "-e", f"HEAD:{workspace}"],
        check=False,
        skip_hooks=True,
    ).returncode == 0

    workspace_path = project / workspace
    if head_has_workspace:
        _git(
            project,
            ["restore", "--source=HEAD", "--staged", "--worktree", "--", workspace],
            check=False,
            skip_hooks=True,
        )
        _git(project, ["checkout", "HEAD", "--", workspace], check=False, skip_hooks=True)
    else:
        # Remove any residual index entries first, then delete only the managed
        # workspace. ``-x`` is intentionally scoped to this path because audit
        # workspaces are commonly ignored by the project.
        _git(
            project,
            ["rm", "-r", "--cached", "--ignore-unmatch", "--", workspace],
            check=False,
            skip_hooks=True,
        )
        if workspace_path.is_dir():
            shutil.rmtree(workspace_path)
        elif workspace_path.exists():
            workspace_path.unlink()
        _git(project, ["clean", "-fdx", "--", workspace], check=False, skip_hooks=True)


def _changed_paths(project: Path) -> List[str]:
    """Return all staged, unstaged, untracked and conflicted paths."""

    paths: set[str] = set()
    commands = (
        ["diff", "--name-only", "-z"],
        ["diff", "--cached", "--name-only", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
        ["ls-files", "--unmerged", "-z"],
    )
    for args in commands:
        output = _git(project, list(args), check=False, skip_hooks=True).stdout
        for value in output.split("\0"):
            value = value.strip()
            if not value:
                continue
            # ``ls-files --unmerged`` prefixes metadata and a tab.
            if "\t" in value:
                value = value.split("\t", 1)[1]
            paths.add(value.replace("\\", "/"))
    return sorted(paths)


def _managed_marker(project: Path, workspace: str) -> Optional[Dict[str, Any]]:
    marker_rel = f"{workspace.rstrip('/')}/.dependency-roadmap-audit-workspace.json"
    marker_path = project / marker_rel
    payload = ""
    if marker_path.exists():
        try:
            payload = marker_path.read_text(encoding="utf-8")
        except OSError:
            payload = ""
    if not payload:
        staged = _git(project, ["show", f":{marker_rel}"], check=False, skip_hooks=True)
        if staged.returncode == 0:
            payload = staged.stdout
    if not payload:
        return None
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def recover_orphaned_managed_workspace(project: Path, workspace: str = DEFAULT_WORKSPACE) -> bool:
    """Recover a source checkout polluted only by a failed audit capture.

    Recovery is deliberately conservative: every changed path must be inside
    the configured workspace, the workspace marker must identify this tool,
    and the source ``HEAD`` must not already track the workspace.  User changes
    elsewhere are never discarded.
    """

    project = project.resolve()
    workspace = _workspace_rel(workspace)
    changed = _changed_paths(project)
    if not changed:
        return False
    prefix = workspace.rstrip("/") + "/"
    outside = [path for path in changed if path != workspace and not path.startswith(prefix)]
    if outside:
        return False

    marker = _managed_marker(project, workspace)
    if not marker or marker.get("managedBy") != MANAGED_BY:
        raise AuditBranchError(
            "AUDIT_WORKSPACE_RECOVERY_REFUSED",
            f"only {workspace} is dirty, but its tool-managed marker is missing or invalid",
        )
    if _git(project, ["cat-file", "-e", f"HEAD:{workspace}"], check=False, skip_hooks=True).returncode == 0:
        raise AuditBranchError(
            "AUDIT_WORKSPACE_TRACKED_ON_SOURCE",
            f"{workspace} is already tracked by source HEAD; automatic deletion was refused",
        )

    _discard_managed_workspace_changes(project, workspace)
    remaining = _changed_paths(project)
    if remaining:
        raise AuditBranchError(
            "AUDIT_WORKSPACE_RECOVERY_FAILED",
            "; ".join(remaining[:20]),
        )
    return True


def _restore_source_checkout(project: Path, source_branch: str, source_commit: str, workspace: str) -> None:
    current = _current_branch(project)
    if current != source_branch:
        checkout = _git(project, ["checkout", source_branch], check=False, skip_hooks=True)
        if checkout.returncode != 0:
            # A failed internal commit may leave only the managed audit path
            # staged/untracked. Clean that path and retry without touching user
            # files elsewhere in the repository.
            _discard_managed_workspace_changes(project, workspace)
            checkout = _git(project, ["checkout", source_branch], check=False, skip_hooks=True)
        if checkout.returncode != 0:
            detail = (checkout.stderr or checkout.stdout or "checkout failed").strip()
            raise AuditBranchError("AUDIT_SOURCE_RESTORE_FAILED", detail[-1200:])

    # Source branch must not inherit staged/untracked evidence even when an
    # earlier internal operation failed.
    _discard_managed_workspace_changes(project, workspace)
    restored = _git_value(project, ["rev-parse", "HEAD"])
    if restored != source_commit:
        raise AuditBranchError("AUDIT_SOURCE_RESTORE_FAILED", f"restored HEAD {restored} != {source_commit}")
    _require_clean(project, "AUDIT_SOURCE_DIRTY_AFTER_RESTORE")


def prepare_audit_branch(
    project: Path,
    project_name: str,
    registry: str,
    source_remote: str,
    source_branch: str,
    source_commit: str,
    audit_branch: str,
    workspace: str = DEFAULT_WORKSPACE,
    dashboard_state: Optional[Path] = None,
    push: bool = False,
    commit_message: str = DEFAULT_CAPTURE_MESSAGE,
) -> Dict[str, Any]:
    project = project.resolve()
    workspace = _workspace_rel(workspace)
    _require_clean(project)
    current_branch = _current_branch(project)
    current_head = _git_value(project, ["rev-parse", "HEAD"])
    if current_branch != source_branch or current_head != source_commit:
        raise AuditBranchError(
            "AUDIT_SOURCE_CHECKOUT_MISMATCH",
            f"expected {source_branch}@{source_commit}, got {current_branch}@{current_head}",
        )

    result: Optional[Dict[str, Any]] = None
    primary_error: Optional[BaseException] = None
    try:
        local_exists = _branch_exists(project, audit_branch)
        remote_available = _git(project, ["remote", "get-url", source_remote], check=False).returncode == 0
        remote_ref = f"refs/remotes/{source_remote}/{audit_branch}"
        if not local_exists and remote_available:
            fetch = _git(
                project,
                ["fetch", source_remote, f"+refs/heads/{audit_branch}:{remote_ref}"],
                check=False,
                skip_hooks=True,
            )
            if fetch.returncode == 0 and _git(project, ["show-ref", "--verify", "--quiet", remote_ref], check=False).returncode == 0:
                _git(project, ["checkout", "-b", audit_branch, "--track", f"{source_remote}/{audit_branch}"], skip_hooks=True)
                _git(project, ["checkout", source_branch], skip_hooks=True)
                local_exists = True
        if local_exists:
            previous = _branch_manifest(project, audit_branch, workspace)
            if previous and previous.get("managedBy") != MANAGED_BY:
                raise AuditBranchError(
                    "AUDIT_BRANCH_NOT_TOOL_MANAGED",
                    f"existing branch {audit_branch!r} has an incompatible {workspace}/branch-manifest.json",
                )
            if previous:
                previous_source = str(previous.get("sourceCommit") or "")
                if previous_source:
                    ancestor = _git(project, ["merge-base", "--is-ancestor", previous_source, source_commit], check=False)
                    if ancestor.returncode != 0:
                        raise AuditBranchError(
                            "AUDIT_SOURCE_HISTORY_DIVERGED",
                            f"previous source {previous_source} is not an ancestor of {source_commit}",
                        )
                cached = _cached_prepare_result(
                    project,
                    audit_branch,
                    previous,
                    source_commit,
                    registry,
                    workspace,
                )
                if cached is not None:
                    cached["intermediateHooksSkipped"] = True
                    result = cached
            else:
                ancestor = _git(project, ["merge-base", "--is-ancestor", audit_branch, source_commit], check=False)
                if ancestor.returncode != 0:
                    raise AuditBranchError(
                        "AUDIT_BRANCH_NOT_TOOL_MANAGED",
                        f"existing branch {audit_branch!r} has project commits and no {workspace}/branch-manifest.json",
                    )

            if result is None:
                _git(project, ["checkout", audit_branch], skip_hooks=True)
                merge = _git(project, ["merge", "--no-edit", source_commit], check=False, skip_hooks=True)
                if merge.returncode != 0:
                    detail = (merge.stderr or merge.stdout or "merge failed").strip()
                    raise AuditBranchError("AUDIT_BRANCH_SOURCE_MERGE_FAILED", detail[-1200:])
        else:
            _git(project, ["checkout", "-b", audit_branch, source_commit], skip_hooks=True)

        if result is None:
            _only_workspace_diff(project, source_commit, workspace)
            capture = _write_capture(
                project, project_name, registry, workspace,
                source_remote, source_branch, source_commit,
                dashboard_state, "baseline",
            )
            if not capture["report"].get("audit", {}).get("complete"):
                raise AuditBranchError(
                    "AUDIT_VULNERABILITY_SCAN_INCOMPLETE",
                    "; ".join(capture["report"].get("audit", {}).get("notes", []))[-1200:],
                )
            commit = _commit_workspace(project, workspace, commit_message)
            if push:
                push_result = _git(
                    project,
                    ["push", "-u", source_remote, audit_branch],
                    check=False,
                    skip_hooks=True,
                )
                if push_result.returncode != 0:
                    detail = (push_result.stderr or push_result.stdout or "push failed").strip()
                    raise AuditBranchError("AUDIT_BRANCH_PUSH_FAILED", detail[-1200:])
            result = {
                "prepared": True,
                "reused": False,
                "branch": audit_branch,
                "commit": commit,
                "workspace": workspace,
                "sourceCommit": source_commit,
                "auditEngine": capture["manifest"].get("auditEngine"),
                "effectiveAuditRegistry": capture["manifest"].get("effectiveAuditRegistry"),
                "vulnerabilityAuditComplete": capture["manifest"].get("vulnerabilityAuditComplete"),
                "manualAuditComplete": capture["manifest"].get("manualAuditComplete"),
                "vulnerabilityTotals": capture["manifest"].get("vulnerabilityTotals") or {},
                "cleanupRequired": True,
                "intermediateHooksSkipped": True,
            }
    except BaseException as exc:  # preserve the primary failure across restore
        primary_error = exc

    try:
        _restore_source_checkout(project, source_branch, source_commit, workspace)
    except BaseException as restore_exc:
        if primary_error is None:
            raise
        if isinstance(primary_error, AuditBranchError):
            primary_error.detail = f"{primary_error.detail}; restore failure: {restore_exc}"
            primary_error.args = (f"{primary_error.code}: {primary_error.detail}",)

    if primary_error is not None:
        raise primary_error
    assert result is not None
    return result

def capture_current_branch(
    project: Path,
    project_name: str,
    registry: str,
    workspace: str = DEFAULT_WORKSPACE,
    dashboard_state: Optional[Path] = None,
    commit_message: str = DEFAULT_FINAL_MESSAGE,
) -> Dict[str, Any]:
    project = project.resolve()
    workspace = _workspace_rel(workspace)
    _require_clean(project)
    branch = _current_branch(project)
    head = _git_value(project, ["rev-parse", "HEAD"])
    capture = _write_capture(
        project, project_name, registry, workspace,
        "", branch, head, dashboard_state, "final",
    )
    if not capture["report"].get("audit", {}).get("complete"):
        raise AuditBranchError(
            "AUDIT_VULNERABILITY_SCAN_INCOMPLETE",
            "; ".join(capture["report"].get("audit", {}).get("notes", []))[-1200:],
        )
    commit = _commit_workspace(project, workspace, commit_message)
    return {"captured": True, "branch": branch, "commit": commit, "workspace": workspace, **capture["manifest"]}


def cleanup_current_branch(
    project: Path,
    workspace: str = DEFAULT_WORKSPACE,
    commit_message: str = DEFAULT_CLEANUP_MESSAGE,
) -> Dict[str, Any]:
    project = project.resolve()
    workspace = _workspace_rel(workspace)
    _require_clean(project)
    path = project / workspace
    if not path.exists():
        return {"cleaned": True, "alreadyAbsent": True, "branch": _current_branch(project), "commit": _git_value(project, ["rev-parse", "HEAD"])}
    marker = path / ".dependency-roadmap-audit-workspace.json"
    if not marker.exists():
        raise AuditBranchError("AUDIT_WORKSPACE_NOT_TOOL_MANAGED", str(path))
    shutil.rmtree(path)
    _git(project, ["add", "-A", "--", workspace])
    staged = _git(project, ["diff", "--cached", "--quiet", "--", workspace], check=False)
    if staged.returncode == 0:
        return {"cleaned": True, "alreadyAbsent": False, "branch": _current_branch(project), "commit": _git_value(project, ["rev-parse", "HEAD"])}
    body = (
        commit_message
        + "\n\nDependency-Roadmap-Managed: audit-cleanup"
        + "\nDependency-Roadmap-Hooks-Bypassed: true"
    )
    commit = _git(project, ["commit", "-m", body], check=False, skip_hooks=True)
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout or "git commit failed").strip()
        raise AuditBranchError("AUDIT_CLEANUP_COMMIT_FAILED", detail[-1200:])
    return {"cleaned": True, "alreadyAbsent": False, "branch": _current_branch(project), "commit": _git_value(project, ["rev-parse", "HEAD"])}


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Manage isolated dependency audit evidence on Git branches.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--project-dir", required=True)
        target.add_argument("--project-name", default="")
        target.add_argument("--registry", default="")
        target.add_argument("--workspace", default=DEFAULT_WORKSPACE)
        target.add_argument("--dashboard-state")

    prepare = sub.add_parser("prepare")
    common(prepare)
    prepare.add_argument("--source-remote", default="origin")
    prepare.add_argument("--source-branch", required=True)
    prepare.add_argument("--source-commit", required=True)
    prepare.add_argument("--audit-branch", required=True)
    prepare.add_argument("--push", action="store_true")
    prepare.add_argument("--commit-message", default=DEFAULT_CAPTURE_MESSAGE)

    capture = sub.add_parser("capture")
    common(capture)
    capture.add_argument("--commit-message", default=DEFAULT_FINAL_MESSAGE)

    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--project-dir", required=True)
    cleanup.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    cleanup.add_argument("--commit-message", default=DEFAULT_CLEANUP_MESSAGE)

    args = parser.parse_args()
    project = Path(args.project_dir).expanduser().resolve()
    dashboard = Path(args.dashboard_state).expanduser().resolve() if getattr(args, "dashboard_state", None) else None
    try:
        if args.command == "prepare":
            result = prepare_audit_branch(
                project, args.project_name or project.name, args.registry,
                args.source_remote, args.source_branch, args.source_commit,
                args.audit_branch, args.workspace, dashboard, args.push, args.commit_message,
            )
        elif args.command == "capture":
            result = capture_current_branch(
                project, args.project_name or project.name, args.registry,
                args.workspace, dashboard, args.commit_message,
            )
        else:
            result = cleanup_current_branch(project, args.workspace, args.commit_message)
    except AuditBranchError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
