"""Explicit continuation from a verified terminal checkpoint, preserving budgets."""
from datetime import datetime, timezone


def reopen_terminal_run(run, checkpoint, candidate):
    if run.get("phase") == "READY" and not run.get("terminal"):
        return dict(run)
    verdict = (run.get("terminalOutcome") or {}).get("outcome") or run.get("terminal")
    if run.get("phase") != "TERMINAL" or verdict not in {
        "BLOCKED_BASELINE", "PARTIAL_VERIFIED", "SCOPE_EXHAUSTED", "NO_ACTIONABLE",
        "NO_VERIFIED_UPGRADE", "INFRA_BLOCKED",
    }:
        raise ValueError("RUN_NOT_CONTINUABLE")
    if checkpoint.get("status") != "VERIFIED" or checkpoint.get("checkpointId") != run.get("activeCheckpointId"):
        raise ValueError("RESUME_CHECKPOINT_NOT_VERIFIED")
    if candidate and candidate.get("stage") in {"PLANNED", "MATERIALIZED", "PRECHECKED", "REPAIRING", "VERIFYING"}:
        raise ValueError("RESUME_CANDIDATE_IN_FLIGHT")
    now = datetime.now(timezone.utc).isoformat()
    history = list(run.get("continuations") or [])
    history.append({"resumedAt": now, "checkpointId": checkpoint["checkpointId"],
                    "sourceSnapshotKey": checkpoint.get("sourceSnapshotKey"),
                    "terminal": run.get("terminal"), "terminalOutcome": run.get("terminalOutcome")})
    return {**run, "phase": "READY", "terminal": None, "terminalOutcome": None,
            "activeCandidateId": None, "continuations": history, "updatedAt": now}


def terminal_repair_resumable(run_dir, run, checkpoint, candidate, ledger, config):
    """Expose only a one-time actionable repair; no budget or proof is reset."""
    if run.get("phase") != "TERMINAL":
        return False
    try:
        reopen_terminal_run(run, checkpoint, candidate)
        return recover_timed_out_candidate(run_dir, run, checkpoint, candidate, ledger, config) is not None
    except (ValueError, OSError):
        return False


def recover_timed_out_candidate(run_dir, run, checkpoint, candidate, ledger, config):
    """Explicit, one-time recovery of a legacy timed-out trial; no proof granted."""
    from pathlib import Path
    if not candidate or candidate.get("stage") != "REJECTED" or candidate.get("timeoutRecoveryCount", 0):
        return None
    if candidate.get("runId") != run.get("runId") or any(b.get("candidateId") == candidate.get("candidateId") for b in ledger.get("blocks", [])):
        return None
    if candidate.get("baseCheckpointId") != checkpoint.get("checkpointId"):
        return None
    matches = [d for d in ledger.get("deferrals", [])
               if d.get("candidateId") == candidate.get("candidateId")
               and d.get("reason") == "Active agent repair deadline exceeded; try another cohort from the verified checkpoint."]
    if not matches or int(candidate.get("attemptId", 0)) >= int(config.get("budget", {}).get("maxRepairAttemptsPerRevision", 2)):
        return None
    # Do not reinterpret incompatibility or other scheduling conclusions.
    feedback = [d for d in ledger.get("feedback", []) if d.get("candidateId") == candidate.get("candidateId")]
    if not feedback or feedback[-1].get("kind") != "INCONCLUSIVE":
        return None
    refs = candidate.get("materializationRefs") or {}
    root = Path(str(refs.get("workspaceRoot", ""))).resolve()
    trial = (Path(run_dir) / "trial").resolve()
    if not root.is_relative_to(trial) or not root.is_dir():
        raise ValueError("RESUME_REPAIR_TRIAL_MISSING")
    project = (root / str(refs.get("projectRelative", "."))).resolve()
    if not project.is_relative_to(root) or not project.is_dir():
        raise ValueError("RESUME_REPAIR_TRIAL_INVALID")
    return {**candidate, "stage": "REPAIRING", "timeoutRecoveryCount": 1}
