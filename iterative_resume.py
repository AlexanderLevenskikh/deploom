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
