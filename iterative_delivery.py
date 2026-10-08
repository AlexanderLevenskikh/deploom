"""Audit-led residual repair and reviewed Git delivery of an iterative checkpoint.

Agent output never grants authority: fresh checks, source fingerprints and an
independent audit gate adoption. The original checkout is never switched.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=120,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if result.returncode:
        raise RuntimeError(f"GIT_DELIVERY_FAILED: {result.stderr.strip()[:1500]}")
    return result.stdout.strip()


def contained(root, path):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def inventory(root):
    # Match source truth: only VCS metadata, dependency installations and
    # DepLoom's own state are excluded. No build/source directory exclusion.
    result = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [n for n in dirs if n not in {".git", "node_modules", ".dependency-roadmap"}]
        for name in files:
            if name == ".git":
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise RuntimeError(f"DELIVERY_SYMLINK_UNSUPPORTED: {path.relative_to(root)}")
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def summary(report, evidence, checkpoint_id=None):
    from iterative_audit_policy import audit_policy_status
    audit = report.get("audit") or {}
    return {"status": audit_policy_status(report), "evidenceRef": str(evidence),
            "checkpointId": checkpoint_id, "generatedAt": report.get("generatedAt"),
            "auditComplete": report.get("auditComplete") is True and report.get("lagComplete") is True,
            "lagOkPct": audit.get("lagOkPct"), "lagOk": audit.get("lagOk"),
            "lagTotal": audit.get("lagTotal"), "lagUnknown": audit.get("lagUnknown"),
            "packageTotals": audit.get("packageTotals") or {}, "policy": report.get("policy") or {},
            "vulnerablePackages": [{"package": name, "severity": value.get("severity"),
                                    "direct": value.get("isDirect"), "nodes": value.get("nodes") or []}
                                   for name, value in (audit.get("packages") or {}).items()]}


def collect_audit(project, config, evidence):
    import iterative_migration as core
    from manual_dependency_audit import build_report, markdown
    evidence.mkdir(parents=True, exist_ok=True)
    policy = config.get("auditPolicy") or {}
    dashboard = config.get("dashboardState")
    report = build_report(project, config.get("projectName", ""), registry="",
                          lag_months=policy.get("lagMonths", 12),
                          dashboard_state=Path(dashboard) if dashboard and Path(dashboard).exists() else None,
                          audit_workspace=evidence, target_level=config.get("targetLevel", "yellow"),
                          min_lag_ok_pct=policy.get("minLagOkPct", 80),
                          max_known_high=policy.get("maxKnownHigh", 1),
                          max_known_moderate=policy.get("maxKnownModerate"),
                          max_known_low=policy.get("maxKnownLow"),
                          yarn_audit_engine="yarn-inventory" if (project / "yarn.lock").exists() else "auto",
                          runtime_env=core._runtime_env(config))
    core._write_json_atomic(evidence / "audit-report.json", report)
    (evidence / "audit-report.md").write_text(markdown(report), encoding="utf-8")
    return report


def current_audit(run_dir, inputs):
    import iterative_migration as core
    from manual_dependency_audit import dependency_input_identity
    from project_runtime import resolve_requested_node
    project = Path(inputs["projectDir"]).resolve()
    intent = inputs.get("intent") or {}
    policy = intent.get("acceptancePolicy") or {}
    config = {"projectName": inputs["projectName"], "targetLevel": intent.get("targetLevel", policy.get("targetLevel", "yellow")),
              "auditPolicy": {"lagMonths": intent.get("lagPolicyMonths", policy.get("lagPolicyMonths", 12)),
                              "minLagOkPct": intent.get("minLagOkPct", policy.get("minLagOkPct", 80)),
                              **{k: v for k, v in policy.items() if k.startswith("maxKnown")}},
              "dashboardState": inputs.get("dashboardState")}
    if "lagOverrides" in inputs:
        pinned = run_dir / "initial-audit-policy-state.json"
        core._write_json_atomic(pinned, {"packageOverrides": {inputs["projectName"]: inputs["lagOverrides"]}})
        config["dashboardState"] = str(pinned)
    requested = inputs.get("requestedNode")
    if requested:
        runtime = resolve_requested_node(requested)
        if not runtime.found:
            raise RuntimeError("ENVIRONMENT_UNAVAILABLE: selected Node is not installed")
        config["runtime"] = {"nodePath": runtime.node_path, "effectiveVersion": runtime.effective_version}
    identity = hashlib.sha256(json.dumps([dependency_input_identity(project)[0], inputs, config.get("runtime") or {}], sort_keys=True).encode()).hexdigest()
    cache = run_dir / "current-audit.json"
    if cache.exists():
        try:
            previous = read(cache)
            if not isinstance(previous, dict):
                previous = {}
        except (ValueError, OSError):
            previous = {}
        try:
            generated = datetime.fromisoformat(previous.get("generatedAt", "").replace("Z", "+00:00"))
            fresh = 0 <= (datetime.now(timezone.utc) - generated).total_seconds() < 86400
        except (ValueError, TypeError):
            fresh = False
        if previous.get("inputIdentity") == identity and fresh and previous.get("status") in {"PASS", "FAIL"}:
            return previous
    before = dependency_input_identity(project)[0]
    evidence = run_dir / "audit" / "initial"
    report = collect_audit(project, config, evidence)
    if dependency_input_identity(project)[0] != before:
        raise RuntimeError("AUDIT_INPUT_CHANGED: dependency files changed during the audit")
    result = {**summary(report, evidence), "inputIdentity": identity}
    core._write_json_atomic(cache, result)
    return result


def audit_score(value):
    totals = value.get("packageTotals") or {}
    return (totals.get("critical", float("inf")), totals.get("high", float("inf")),
            -(value.get("lagOkPct") or 0), totals.get("moderate", float("inf")), totals.get("low", float("inf")))


def security_prepare(run_dir, inputs):
    import iterative_migration as core
    run, config = core.load_run(run_dir), core.load_config(run_dir)
    core._assert_runtime_unchanged(config)
    checkpoint = core.load_checkpoint(run_dir, run["activeCheckpointId"])
    if checkpoint.get("status") != "VERIFIED" or run.get("activeCandidateId"):
        raise RuntimeError("SECURITY_CHECKPOINT_NOT_READY")
    state_file = run_dir / "security-state.json"
    state = read(state_file) if state_file.exists() else {}
    if state.get("status") == "accepting":
        return security_verify(run_dir, inputs)
    if state.get("status") in {"ready", "running"} and state.get("baseCheckpointId") == checkpoint["checkpointId"]:
        return state
    attempts = int(state.get("attempt", 0))
    if attempts >= int((config.get("budget") or {}).get("maxRepairAttemptsPerRevision", 2)):
        return {**state, "status": "exhausted"}
    audit = checkpoint.get("audit") or {}
    if audit.get("status") not in {"PASS", "FAIL"}:
        core._audit_locked(run_dir, run, config, argparse.Namespace(registry="", lag_months=None,
                           min_lag_ok_pct=None, max_known_high=None, yarn_audit_engine="yarn-inventory"))
        checkpoint = core.load_checkpoint(run_dir, run["activeCheckpointId"])
        audit = checkpoint.get("audit") or {}
    evidence_path = Path(audit.get("evidenceRef") or "") / "audit-report.json"
    if evidence_path.is_file() and contained(run_dir, evidence_path):
        audit = {**audit, **summary(read(evidence_path), evidence_path.parent, checkpoint["checkpointId"])}
    if audit.get("status") not in {"PASS", "FAIL"}:
        raise RuntimeError("SECURITY_AUDIT_UNKNOWN: no authoritative evidence to repair")
    if core._policy_satisfied(config, checkpoint):
        return {"status": "not-needed"}
    root = run_dir / "security" / f"attempt-{attempts + 1}" / "workspace"
    if root.exists():
        raise RuntimeError("SECURITY_UNREGISTERED_WORKSPACE: preserved files need inspection")
    snapshot = core._open_checkpoint_source(run_dir, checkpoint, config, run_id=run["runId"])
    core._materialize_trial_tree(snapshot, root, 1200)
    rel = checkpoint.get("projectRelative") or "."
    manifest = read(root / rel / "package.json")
    state = {"schemaVersion": 1, "runId": run["runId"], "baseCheckpointId": checkpoint["checkpointId"],
             "sourceSnapshotKey": checkpoint["sourceSnapshotKey"], "attempt": attempts + 1,
             "auditTool": str(Path(__file__).with_name("manual_dependency_audit.py")),
             "status": "ready", "workspaceRoot": str(root), "projectRelative": rel,
             "before": inventory(root), "sourceHead": git(root, "rev-parse", "HEAD"),
             "scripts": manifest.get("scripts") or {}, "beforeAudit": audit,
             "originalInput": core.manifest_and_lock_hashes(Path(config["projectDir"]))[:2]}
    core._write_json_atomic(state_file, state)
    return state


def security_verify(run_dir, inputs):
    import iterative_migration as core
    state_file = run_dir / "security-state.json"
    state = read(state_file)
    run, config = core.load_run(run_dir), core.load_config(run_dir)
    if state.get("runId") != run["runId"]:
        raise RuntimeError("SECURITY_STALE_RUN")
    if state.get("status") == "accepted":
        core._finish_locked(run_dir, run, config)
        return state
    if state.get("status") == "rejected":
        return state
    if state.get("status") == "accepting":
        accepted_id = state["pendingCheckpointId"]
        path = run_dir / core.CHECKPOINT_DIR / f"{accepted_id}.json"
        if path.exists():
            accepted = core.load_checkpoint(run_dir, accepted_id)
            if (accepted.get("parentCheckpointId") != state["baseCheckpointId"] or
                    accepted.get("cohortId") != "audit-residual" or accepted.get("status") != "VERIFIED" or
                    accepted.get("fullAssignment") != state["pendingAssignment"] or
                    run["activeCheckpointId"] not in {state["baseCheckpointId"], accepted_id}):
                raise RuntimeError("SECURITY_ACCEPTANCE_IDENTITY_CHANGED")
            core._open_checkpoint_source(run_dir, accepted, config, run_id=run["runId"])
            accepted["audit"] = {**state["pendingAudit"], "checkpointId": accepted_id}
            core.save_checkpoint(run_dir, accepted)
            run = dict(run, activeCheckpointId=accepted_id, activeCandidateId=None, phase="READY")
            run.pop("terminal", None); run.pop("terminalOutcome", None)
            core.save_run(run_dir, run)
            state.update(status="accepted", checkpointId=accepted_id, audit=accepted["audit"],
                         goalSatisfied=core._policy_satisfied(config, accepted))
            core._write_json_atomic(state_file, state)
            core._finish_locked(run_dir, run, config)
            return state
        source = run_dir / core.SOURCE_DIR / accepted_id
        if source.exists():
            # A crash between sealing and saving Cn left an orphan snapshot.
            # Preserve it; repeat verification and capture rather than claiming
            # authority from an incomplete checkpoint transaction.
            if not contained(run_dir, source) or source.is_symlink():
                raise RuntimeError("SECURITY_ORPHAN_SOURCE_PATH_UNSAFE")
            from uuid import uuid4
            archive = source.with_name(f"{accepted_id}.security-recovery-{uuid4().hex}")
            state.setdefault("sourceOrphans", []).append(str(archive))
            core._write_json_atomic(state_file, state)
            source.rename(archive)
    checkpoint = core.load_checkpoint(run_dir, run["activeCheckpointId"])
    if run["runId"] != state["runId"] or run["activeCheckpointId"] != state["baseCheckpointId"]:
        raise RuntimeError("SECURITY_STALE_CHECKPOINT")
    core._assert_runtime_unchanged(config)
    root = Path(state["workspaceRoot"]).resolve()
    if not contained(run_dir / "security", root):
        raise RuntimeError("SECURITY_PATH_OUTSIDE_RUN")
    rel = Path(state["projectRelative"])
    project = (root / rel).resolve()
    if not contained(root, project):
        raise RuntimeError("SECURITY_PROJECT_OUTSIDE_WORKSPACE")
    if list(core.manifest_and_lock_hashes(Path(config["projectDir"]))[:2]) != state["originalInput"]:
        raise RuntimeError("SECURITY_ORIGINAL_INPUT_CHANGED")
    after = inventory(root)
    changed = {p for p in set(after) | set(state["before"]) if after.get(p) != state["before"].get(p)}
    allowed_docs = f"docs/dependency-migration/{run['runId']}/"
    prefix = rel.as_posix().rstrip("/") + "/" if rel.as_posix() != "." else ""
    if any(not (p.startswith(prefix) or p.startswith(allowed_docs)) for p in changed):
        raise RuntimeError("SECURITY_OUTSIDE_PROJECT_MUTATION")
    if any(Path(p).name in {".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs"} for p in changed):
        raise RuntimeError("SECURITY_REGISTRY_CONFIG_CHANGED")
    if git(root, "rev-parse", "HEAD") != state["sourceHead"]:
        raise RuntimeError("SECURITY_GIT_MUTATION")
    manifest = read(project / "package.json")
    if (manifest.get("scripts") or {}) != state["scripts"]:
        raise RuntimeError("SECURITY_CHECK_COMMANDS_CHANGED")
    old_assignment = checkpoint["fullAssignment"]
    declared = core.direct_dependency_assignment(project)
    if set(declared) != set(old_assignment):
        raise RuntimeError("SECURITY_DIRECT_SCOPE_CHANGED")
    assignment = core.observed_resolved_assignment(project, declared, allow_declared_selectors=True)
    for name, policy in (config.get("packagePolicies") or {}).items():
        if policy == "keep-current" and assignment.get(name) != old_assignment.get(name):
            raise RuntimeError(f"SECURITY_KEEP_CURRENT_CHANGED: {name}")
    verify_config = dataclasses.replace(core.verify_config_from(config["verifyConfig"], run_dir),
                                       verification_purpose="intermediate-candidate")
    result = core.verify_assignment(project, assignment, config=verify_config, run_project_checks=True,
                                    runtime_env=core._runtime_env(config))
    if not result.ok:
        state.update(status="rejected", reason=f"Verification {result.kind}: {result.summary}")
        core._write_json_atomic(state_file, state)
        return state
    report = collect_audit(project, config, run_dir / "audit" / f"security-{state['attempt']}")
    audited = summary(report, run_dir / "audit" / f"security-{state['attempt']}")
    prior = state["beforeAudit"]
    # Missing evidence, a worsening severity count or less lag coverage never
    # replaces the last verified cumulative checkpoint.
    old_totals, new_totals = prior.get("packageTotals") or {}, audited.get("packageTotals") or {}
    targets = config.get("targets") or {}
    required = {n for n, policy in (config.get("packagePolicies") or {}).items() if policy == "required"}
    required_progress = sum(assignment.get(n) == targets.get(n) and n in targets for n in required) > sum(old_assignment.get(n) == targets.get(n) and n in targets for n in required)
    improving = audited["status"] in {"PASS", "FAIL"} and (audited["status"] == "PASS" and prior.get("status") != "PASS" or
                  audit_score(audited) < audit_score(prior) or required_progress)
    nonregression = all(new_totals.get(k, 0) <= old_totals.get(k, 0) for k in ("critical", "high")) and (audited.get("lagOkPct") or 0) >= (prior.get("lagOkPct") or 0)
    if not improving or not nonregression:
        state.update(status="rejected", reason="AUDIT_NO_VERIFIED_IMPROVEMENT", audit=audited)
        core._write_json_atomic(state_file, state)
        return state
    candidate = {"candidateId": f"security-{state['attempt']}", "cohortId": "audit-residual",
                 "runId": run["runId"], "baseCheckpointId": checkpoint["checkpointId"],
                 "fullAssignment": assignment, "materializationRefs": {"workspaceRoot": str(root), "projectRelative": str(rel)}}
    run = dict(run)
    run.pop("terminal", None); run.pop("terminalOutcome", None)
    state.update(status="accepting", pendingCheckpointId=f"C{int(checkpoint.get('seq', 0)) + 1}",
                 pendingAssignment=assignment, pendingAudit=audited)
    core._write_json_atomic(state_file, state)
    core._accept_checkpoint(run_dir, run, config, candidate, checkpoint, result)
    updated = core.load_run(run_dir)
    accepted = core.load_checkpoint(run_dir, updated["activeCheckpointId"])
    accepted["audit"] = {**audited, "checkpointId": accepted["checkpointId"]}
    core.save_checkpoint(run_dir, accepted)
    state.update(status="accepted", checkpointId=accepted["checkpointId"], audit=audited,
                 goalSatisfied=core._policy_satisfied(config, accepted))
    core._write_json_atomic(state_file, state)
    core._finish_locked(run_dir, updated, config)
    return state


def deliverable_source_file(name):
    parts = Path(name).parts
    return not any(p in {"node_modules", ".artifacts", ".playwright-mcp", "dist", "build"} for p in parts) and not name.endswith((".log", ".tmp"))


def assert_delivery_inputs_visible(snapshot_root):
    # Never silently remove hidden source/config from a verification subject or
    # copy ignored potentially sensitive payloads into another Git checkout.
    ignored = set(git(snapshot_root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z").split("\0")) - {""}
    hidden = sorted(n for n in ignored if deliverable_source_file(n))
    if hidden:
        raise RuntimeError("DELIVERY_IGNORED_SOURCE_INPUTS: В проверенном состоянии есть исключённые из Git входы. "
                           "Checkpoint сохранён; без явно выбранных входов доставка не подтверждается: " + ", ".join(hidden[:8]))


def delivery_files(snapshot_root):
    tracked = set(git(snapshot_root, "ls-files", "-z").split("\0")) - {""}
    untracked = set(git(snapshot_root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}
    # Agent scratch files never become migration deliverables. Tracked files
    # are preserved even when their names also match this scratch policy.
    return tracked | {n for n in untracked if deliverable_source_file(n)}


def delivery_root(run_dir, run_id):
    # A registered worktree must not be moved by archive-run. Keep it beside
    # active runs, under a deterministic namespace that cannot traverse paths.
    identity = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:12]
    return run_dir.parent / "deliveries" / f"{run_dir.name}-{identity}" / "workspace"


def select_delivery_branch(repo, requested, source_head, run_id):
    git(repo, "check-ref-format", "--branch", requested)
    refs = {line.removeprefix("refs/heads/") for line in
            git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/").splitlines()}
    occupied = {line.removeprefix("branch refs/heads/") for line in
                git(repo, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("branch refs/heads/")}
    if requested in refs and requested not in occupied and git(repo, "rev-parse", f"refs/heads/{requested}") == source_head:
        return requested, True, None
    def available(name):
        return not any(name == ref or name.startswith(ref + "/") or ref.startswith(name + "/") for ref in refs)
    if available(requested):
        return requested, False, None
    identity = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:12]
    # A suffix avoids the Git ref-directory collision with an existing branch.
    for base in (f"{requested}--deploom-{identity}", f"codex/deploom-{identity}", f"deploom-{identity}"):
        for index in range(100):
            candidate = base if index == 0 else f"{base}-{index + 1}"
            if available(candidate):
                return candidate, False, "configured-branch-preserved"
    raise RuntimeError("DELIVERY_BRANCH_NAMESPACE_UNAVAILABLE")


def delivery_prepare(run_dir, inputs):
    import iterative_migration as core
    run, config = core.load_run(run_dir), core.load_config(run_dir)
    if run.get("phase") != "TERMINAL" or run.get("activeCandidateId"):
        raise RuntimeError("DELIVERY_RUN_NOT_FINISHED")
    checkpoint = core.load_checkpoint(run_dir, run["activeCheckpointId"])
    if checkpoint.get("status") != "VERIFIED":
        raise RuntimeError("DELIVERY_NO_VERIFIED_CHECKPOINT")
    branch = inputs.get("branch", "").strip()
    if not branch:
        raise RuntimeError("DELIVERY_BRANCH_REQUIRED")
    state_file = run_dir / "delivery-state.json"
    if state_file.exists():
        state = read(state_file)
        if (state.get("runId") != run["runId"] or state.get("sourceSnapshotKey") != checkpoint["sourceSnapshotKey"] or
                state.get("checkpointId") != checkpoint["checkpointId"] or state.get("requestedBranch", state.get("branch")) != branch or
                Path(state.get("workspaceRoot") or "").resolve() != delivery_root(run_dir, run["runId"]).resolve()):
            raise RuntimeError("DELIVERY_SCOPE_CHANGED: preserve the prepared branch; do not overwrite it")
        if state.get("status") == "preparing":
            return resume_delivery_preparation(run_dir, run, config, checkpoint, state)
        return state
    snapshot = core._open_checkpoint_source(run_dir, checkpoint, config, run_id=run["runId"])
    source = Path(snapshot.root)
    original = Path(config["projectDir"])
    repo = Path(git(original, "rev-parse", "--show-toplevel"))
    if git(repo, "status", "--porcelain"):
        raise RuntimeError("DELIVERY_ORIGINAL_DIRTY: commit or save your unrelated work first")
    requested_branch = branch
    branch, reuse_branch, branch_resolution = select_delivery_branch(repo, branch, checkpoint["sourceHead"], run["runId"])
    root = delivery_root(run_dir, run["runId"])
    if root.exists():
        raise RuntimeError("DELIVERY_UNREGISTERED_WORKTREE: preserve and inspect the existing directory")
    root.parent.mkdir(parents=True, exist_ok=True)
    head = checkpoint["sourceHead"]
    assert_delivery_inputs_visible(source)
    files = delivery_files(source)
    # Publish the intent before Git so a crash leaves a visible recoverable
    # worktree rather than silently creating another branch on restart.
    state = {"schemaVersion": 1, "runId": run["runId"], "checkpointId": checkpoint["checkpointId"],
             "sourceSnapshotKey": checkpoint["sourceSnapshotKey"], "branch": branch,
             "requestedBranch": requested_branch, "reuseBranch": reuse_branch, "branchResolution": branch_resolution,
             "workspaceRoot": str(root), "projectRelative": checkpoint.get("projectRelative") or ".",
             "sourceHead": head, "status": "preparing", "files": sorted(files),
             "auditTool": str(Path(__file__).with_name("manual_dependency_audit.py"))}
    core._write_json_atomic(state_file, state)
    return resume_delivery_preparation(run_dir, run, config, checkpoint, state)


def resume_delivery_preparation(run_dir, run, config, checkpoint, state):
    import iterative_migration as core
    root = Path(state["workspaceRoot"]).resolve()
    if root != delivery_root(run_dir, run["runId"]).resolve():
        raise RuntimeError("DELIVERY_PATH_OUTSIDE_RUN")
    repo = Path(git(Path(config["projectDir"]), "rev-parse", "--show-toplevel"))
    if not root.exists():
        refs = git(repo, "for-each-ref", "--format=%(refname)", f"refs/heads/{state['branch']}").splitlines()
        if state.get("reuseBranch"):
            if (f"refs/heads/{state['branch']}" not in refs or
                    git(repo, "rev-parse", f"refs/heads/{state['branch']}") != state["sourceHead"]):
                raise RuntimeError("DELIVERY_BRANCH_CHANGED: preserve the existing branch")
            git(repo, "worktree", "add", str(root), state["branch"])
        else:
            if f"refs/heads/{state['branch']}" in refs:
                raise RuntimeError("DELIVERY_BRANCH_CREATED_WITHOUT_WORKTREE: preserve the branch for inspection")
            git(repo, "worktree", "add", "-b", state["branch"], str(root), state["sourceHead"])
    if (git(root, "branch", "--show-current") != state["branch"] or
            git(root, "rev-parse", "HEAD") != state["sourceHead"] or
            not any(line == f"worktree {root.as_posix()}" for line in git(repo, "worktree", "list", "--porcelain").splitlines())):
        raise RuntimeError("DELIVERY_WORKTREE_IDENTITY_CHANGED")
    snapshot = core._open_checkpoint_source(run_dir, checkpoint, config, run_id=run["runId"])
    source = Path(snapshot.root)
    files = set(state["files"])
    if "baseFiles" not in state:
        if git(root, "status", "--porcelain"):
            raise RuntimeError("DELIVERY_UNREGISTERED_EDITS: preparation did not record the base")
        state["baseFiles"] = inventory(root)
        core._write_json_atomic(run_dir / "delivery-state.json", state)
    expected = {n: hashlib.sha256((source/n).read_bytes()).hexdigest() if (source/n).is_file() else None for n in files}
    allowed = f"docs/dependency-migration/{run['runId']}/"
    for name in ("MIGRATION_REPORT.md", "DEVELOPER_UPGRADE_GUIDE.md"):
        path = allowed + name
        if path not in expected or expected[path] is None:
            expected[path] = hashlib.sha256((run_dir / "reports" / name).read_bytes()).hexdigest()
    current = inventory(root)
    for name in set(current) | set(state["baseFiles"]):
        if current.get(name) not in {state["baseFiles"].get(name), expected.get(name)}:
            raise RuntimeError(f"DELIVERY_PREPARATION_FILES_CHANGED: preserved user edits ({name})")
    for name in files:
        src, dst = source / name, root / name
        if not contained(root, dst) or src.is_symlink():
            raise RuntimeError(f"DELIVERY_INVALID_PATH: {name}")
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst)
            dst.chmod(dst.stat().st_mode | 0o200)
        elif not src.exists() and dst.is_file():
            dst.unlink()
        elif src.is_dir():
            raise RuntimeError(f"DELIVERY_SUBMODULE_REQUIRES_MATERIALIZATION: {name}")
    docs = root / "docs" / "dependency-migration" / run["runId"]
    docs.mkdir(parents=True, exist_ok=True)
    for name in ("MIGRATION_REPORT.md", "DEVELOPER_UPGRADE_GUIDE.md"):
        target = docs / name
        if not target.exists():
            shutil.copy2(run_dir / "reports" / name, target)
        files.add(target.relative_to(root).as_posix())
    state.update(status="ready", files=sorted(files),
                 expected={n: hashlib.sha256((root/n).read_bytes()).hexdigest() if (root/n).is_file() else None for n in files})
    core._write_json_atomic(run_dir / "delivery-state.json", state)
    return state


def delivery_verify(run_dir, inputs):
    import iterative_migration as core
    state_file = run_dir / "delivery-state.json"
    state = read(state_file)
    run, config = core.load_run(run_dir), core.load_config(run_dir)
    checkpoint = core.load_checkpoint(run_dir, run["activeCheckpointId"])
    if state.get("status") == "preparing":
        raise RuntimeError("DELIVERY_PREPARATION_INTERRUPTED: do not dispatch an agent on an incomplete copy")
    if state["runId"] != run["runId"] or state["sourceSnapshotKey"] != checkpoint["sourceSnapshotKey"]:
        raise RuntimeError("DELIVERY_STALE_CHECKPOINT")
    root = Path(state["workspaceRoot"]).resolve()
    if root != delivery_root(run_dir, run["runId"]).resolve():
        raise RuntimeError("DELIVERY_PATH_OUTSIDE_RUN")
    if git(root, "branch", "--show-current") != state["branch"]:
        raise RuntimeError("DELIVERY_BRANCH_CHANGED")
    git(root, "merge-base", "--is-ancestor", state["sourceHead"], "HEAD")
    if git(root, "status", "--porcelain"):
        raise RuntimeError("DELIVERY_UNCOMMITTED_CHANGES: finish semantic commits before verifying")
    documentation = {f"docs/dependency-migration/{state['runId']}/{name}" for name in ("MIGRATION_REPORT.md", "DEVELOPER_UPGRADE_GUIDE.md")}
    expected = dict(state["expected"])
    for name, digest in state["expected"].items():
        path = root / name
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if name in documentation:
            if not path.is_file() or path.is_symlink() or not path.read_text(encoding="utf-8-sig").strip():
                raise RuntimeError(f"DELIVERY_DOCUMENTATION_MISSING: {name}")
            expected[name] = current
        elif current != digest:
            raise RuntimeError(f"DELIVERY_SOURCE_CHANGED: commit grouping must preserve verified bytes ({name})")
    actual = set(git(root, "ls-files", "-z").split("\0")) - {""}
    if not actual.issubset(set(state["files"])):
        raise RuntimeError("DELIVERY_UNEXPECTED_TRACKED_FILES")
    core._assert_runtime_unchanged(config)
    delivered_head = git(root, "rev-parse", "HEAD")
    # The source guard intentionally rejects absolute .git indirection in a
    # linked worktree. Verify a private clone of the exact delivered commit,
    # without relaxing that guard or mutating the result worktree.
    verification = run_dir / "delivery" / "verification" / delivered_head
    if not verification.exists():
        verification.parent.mkdir(parents=True, exist_ok=True)
        git(root, "clone", "--no-hardlinks", "--no-checkout", str(root), str(verification))
        git(verification, "checkout", "--detach", delivered_head)
    if git(verification, "rev-parse", "HEAD") != delivered_head or git(verification, "status", "--porcelain"):
        raise RuntimeError("DELIVERY_VERIFICATION_COPY_CHANGED")
    # Git's autocrlf / checkout filters can change working bytes without a
    # semantic diff. Copy the exact already validated delivered bytes before
    # source capture; leave .git and the delivered commit identity untouched.
    for name, digest in expected.items():
        path = verification / name
        if digest is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, path)
        elif path.is_file():
            path.unlink()
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if actual_digest != digest:
            raise RuntimeError(f"DELIVERY_COMMITTED_TREE_MISMATCH: {name}")
    project = verification / state["projectRelative"]
    # Agent prose and clean Git status grant no project-check authority.
    result = core.verify_assignment(project, checkpoint["fullAssignment"],
               config=core.verify_config_from(config["verifyConfig"], run_dir),
               run_project_checks=True, runtime_env=core._runtime_env(config))
    if not result.ok:
        raise RuntimeError(f"DELIVERY_VERIFICATION_FAILED: {result.kind}: {result.summary}")
    report = collect_audit(project, config, run_dir / "audit" / "delivery")
    audit = summary(report, run_dir / "audit" / "delivery", checkpoint["checkpointId"])
    # Repeat audit can change the final verdict (e.g. a new advisory). Persist
    # that fact in the run as well as in the delivery UI; an old COMPLETE is
    # never reused after a failed or unknown delivered audit.
    if git(root, "rev-parse", "HEAD") != delivered_head or git(root, "status", "--porcelain"):
        raise RuntimeError("DELIVERY_CHANGED_DURING_VERIFICATION")
    for name, digest in expected.items():
        path = root / name
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if current != digest:
            raise RuntimeError(f"DELIVERY_BYTES_CHANGED_DURING_VERIFICATION: {name}")
    checkpoint["audit"] = audit
    core.save_checkpoint(run_dir, checkpoint)
    refreshed_run = dict(run)
    refreshed_run.pop("terminalOutcome", None)
    core._finish_locked(run_dir, refreshed_run, config)
    state.update(status="done", head=delivered_head, verificationWorkspace=str(verification),
                 documentationHashes={name: expected[name] for name in sorted(documentation)},
                 proofRefs={"resolvedStateKey": result.resolved_state_key, "preparationProofKey": result.preparation_proof_key},
                 commits=git(root, "log", "--format=%h %s", f"{state['sourceHead']}..HEAD").splitlines(),
                 audit=audit, finishedAt=datetime.now(timezone.utc).isoformat())
    core._write_json_atomic(state_file, state)
    return state


def main(argv=None):
    import iterative_migration as core
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--input-file", required=True)
    parser.add_argument("command", choices=("current-audit", "security-prepare", "security-verify", "delivery-prepare", "delivery-verify"))
    args = parser.parse_args(argv)
    run_dir = Path(args.run_dir).resolve(); run_dir.mkdir(parents=True, exist_ok=True)
    inputs = read(args.input_file)
    lock = core._RunLock(run_dir, f"delivery-{os.getpid()}", stale_seconds=600)
    lock.acquire()
    try:
        operation = {"current-audit": current_audit, "security-prepare": security_prepare,
                     "security-verify": security_verify, "delivery-prepare": delivery_prepare,
                     "delivery-verify": delivery_verify}[args.command]
        value = operation(run_dir, inputs)
        # Never print the potentially large source inventory.
        print("ITERATIVE_DELIVERY_V1 " + json.dumps({k:v for k,v in value.items() if k not in {"before", "expected", "baseFiles", "files"}}, ensure_ascii=False))
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    import iterative_migration as core
    core.configure_utf8_stdio()
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=__import__("sys").stderr)
        raise SystemExit(1)
