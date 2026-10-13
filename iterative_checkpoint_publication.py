"""Recover a verified incumbent publication interrupted between atomic writes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

TRANSACTION = "checkpoint-publication.json"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def prepare_publication(run_dir, run, config, checkpoint, next_run, candidate_id, write_json):
    if (run_dir / TRANSACTION).exists():
        raise ValueError("CHECKPOINT_PUBLICATION_PENDING")
    record = {"schema": 1, "runId": run["runId"], "candidateId": candidate_id,
              "baseCheckpointId": run["activeCheckpointId"], "generation": run.get("generation", 1),
              "configHash": digest(config), "checkpoint": checkpoint, "nextRun": next_run}
    record["digest"] = digest(record)
    write_json(run_dir / TRANSACTION, record)


def recover_publication(run_dir, run, config, *, read_checkpoint, save_checkpoint,
                        validate_source, save_run, clear_candidate, clear_repairs):
    """Caller holds the run lock; no proof is invented and no budget is reset."""
    path = run_dir / TRANSACTION
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(record, dict):
        raise ValueError("CHECKPOINT_PUBLICATION_INVALID")
    checksum = record.pop("digest", None)
    cp, next_run = record.get("checkpoint"), record.get("nextRun")
    if checksum != digest(record) or record.get("schema") != 1 or record.get("configHash") != digest(config):
        raise ValueError("CHECKPOINT_PUBLICATION_IDENTITY_MISMATCH")
    if not isinstance(cp, dict) or not isinstance(next_run, dict):
        raise ValueError("CHECKPOINT_PUBLICATION_INVALID")
    base, target = record.get("baseCheckpointId"), cp.get("checkpointId")
    generation = record.get("generation")
    if not isinstance(generation, int) or generation < 1:
        raise ValueError("CHECKPOINT_PUBLICATION_INVALID")
    if (not isinstance(target, str) or Path(target).name != target or target in {"", ".", ".."}
        or cp.get("status") != "VERIFIED" or cp.get("parentCheckpointId") != base
        or (cp.get("verification") or {}).get("status") != "passed"
        or not cp.get("sourceSnapshotKey") or not cp.get("sourceSnapshotContainer")
        or run.get("runId") != record.get("runId") or next_run.get("runId") != record.get("runId")
        or next_run.get("activeCheckpointId") != target or next_run.get("activeCandidateId") is not None
        or next_run.get("phase") != "READY" or next_run.get("generation") != generation + 1):
        raise ValueError("CHECKPOINT_PUBLICATION_INVALID")
    before = run.get("activeCheckpointId") == base and run.get("generation", 1) == generation
    after = run.get("activeCheckpointId") == target and run.get("generation", 1) == generation + 1
    if not (before or after):
        raise ValueError("CHECKPOINT_PUBLICATION_STALE")
    # All non-pointer state, notably spent budgets and constraints, is retained.
    transitions = {"activeCheckpointId", "activeCandidateId", "generation", "phase", "updatedAt"}
    if {k: v for k, v in run.items() if k not in transitions} != {k: v for k, v in next_run.items() if k not in transitions}:
        raise ValueError("CHECKPOINT_PUBLICATION_RUN_DRIFT")
    existing = read_checkpoint(target)
    if existing is not None and existing != cp:
        raise ValueError("CHECKPOINT_PUBLICATION_ARTIFACT_DRIFT")
    # Validate the physical sealed bytes BEFORE switching the incumbent pointer.
    validate_source(cp)
    if existing is None:
        save_checkpoint(cp)
    if before:
        save_run(next_run)
    clear_candidate()
    clear_repairs()
    path.unlink()
    return next_run if before else dict(run)
