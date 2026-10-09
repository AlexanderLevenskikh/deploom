"""Durable human scheduling decisions; never compatibility or verification evidence."""
import json
import os
from pathlib import Path


TRANSACTION = "cohort-review-transaction.json"


def _recover_locked(run_dir):
    import iterative_migration as core
    path = run_dir / TRANSACTION
    if not path.exists():
        return
    transaction = json.loads(path.read_text(encoding="utf-8-sig"))
    run = core.load_run(run_dir)
    candidate = core.load_candidate(run_dir)
    expected = transaction["run"]
    if (run["runId"] != expected["runId"] or run["activeCheckpointId"] != expected["activeCheckpointId"]
            or not candidate or candidate["candidateId"] != transaction["candidate"]["candidateId"]
            or candidate.get("stage") not in {"PLANNED", "REJECTED"}):
        raise core.InvalidInputError("COHORT_REVIEW_RECOVERY_IDENTITY_CHANGED")
    core.save_ledger(run_dir, transaction["ledger"])
    core.save_candidate(run_dir, transaction["candidate"])
    core.save_run(run_dir, expected)
    path.unlink()


def recover(run_dir):
    import iterative_migration as core
    run_dir = Path(run_dir)
    if not (run_dir / TRANSACTION).exists():
        return
    lock = core._RunLock(run_dir, f"cohort-review-recovery-{os.getpid()}", stale_seconds=60)
    lock.acquire()
    try:
        _recover_locked(run_dir)
    finally:
        lock.release()


def review(run_dir, candidate_id, selected):
    import iterative_migration as core
    run_dir = Path(run_dir)
    lock = core._RunLock(run_dir, f"cohort-review-{os.getpid()}", stale_seconds=60)
    lock.acquire()
    try:
        run = core.load_run(run_dir)
        candidate = core.load_candidate(run_dir)
        if (not candidate or candidate.get("candidateId") != candidate_id
                or candidate.get("stage") != "PLANNED"
                or candidate.get("baseCheckpointId") != run.get("activeCheckpointId")):
            raise core.InvalidInputError("COHORT_REVIEW_STALE")
        checkpoint = core.load_checkpoint(run_dir, candidate["baseCheckpointId"])
        if checkpoint.get("status") != "VERIFIED":
            raise core.InvalidInputError("COHORT_REVIEW_BASE_NOT_VERIFIED")
        changed = {name: version for name, version in candidate["fullAssignment"].items()
                   if version != checkpoint.get("fullAssignment", {}).get(name)}
        if not isinstance(selected, list) or not all(isinstance(n, str) for n in selected) or not set(selected) <= set(changed):
            raise core.InvalidInputError("COHORT_REVIEW_SELECTION_INVALID")
        if set(selected) == set(changed):
            core._write_json_atomic(run_dir / "cohort-approval.json", {
                "candidateId": candidate_id, "baseCheckpointId": candidate["baseCheckpointId"],
                "sourceSnapshotKey": checkpoint.get("sourceSnapshotKey"),
                "assignmentFingerprint": core.assignment_fingerprint(candidate["fullAssignment"]),
                "policyHash": candidate["policyHash"], "fullAssignment": candidate["fullAssignment"], "reviewedAt": core._now_iso()})
            return {"approved": True}
        excluded = set(changed) - set(selected)
        # Atomic peers cannot be split through a checkbox. Re-plan the untouched
        # remainder; defer the whole connected atom if one of its members is out.
        previous = None
        while previous != excluded:
            previous = set(excluded)
            for group in candidate.get("atomicGroups", []):
                if excluded.intersection(group):
                    excluded.update(group)
        ledger = core.load_ledger(run_dir)
        ledger["userDeferredPackages"] = sorted(set(ledger.get("userDeferredPackages", [])) | excluded)
        core._append_deferral(ledger, candidate, "Human review: cohort composition declined; re-plan from verified checkpoint")
        candidate["stage"] = "REJECTED"
        candidate["conclusion"] = {"kind": "human-deferred", "packages": sorted(excluded)}
        candidate["updatedAt"] = core._now_iso()
        run["activeCandidateId"] = None
        run["phase"] = "READY"
        run["updatedAt"] = core._now_iso()
        # One durable intent precedes the three state writes. Every production
        # CLI entry recovers it under the same lock before choosing a next step.
        core._write_json_atomic(run_dir / TRANSACTION, {"run": run, "candidate": candidate, "ledger": ledger})
        _recover_locked(run_dir)
        return {"approved": False, "deferred": sorted(excluded)}
    finally:
        lock.release()


def command(args):
    payload = json.loads(Path(args.input_file).read_text(encoding="utf-8-sig"))
    print(json.dumps(review(args.run_dir, payload.get("candidateId"), payload.get("selected")), ensure_ascii=False))
    return 0
