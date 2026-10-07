"""Evidence-based scheduling recovery; never grants verification authority."""
from __future__ import annotations

import re


def transient_audit_reason(report):
    """Retry transport/resource failures, never findings or invalid evidence."""
    if report.get("auditComplete") is not False:
        return ""
    audit = report.get("audit") or {}
    text = "\n".join(str(x) for x in audit.get("notes") or [])
    # Invalid inventory/registry/configuration cannot improve by repetition.
    if re.search(r"mismatch|drift|inventory.incomplete|E401|E403|E404|ETARGET|ENOENT|EACCES", text, re.I):
        return ""
    match = re.search(r"\b(?:EAI_AGAIN|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EPIPE|EBUSY|EPERM|E429|E502|E503|E504)\b|timed out|timeout|rate limit", text, re.I)
    return match.group(0) if match else ""


def adaptive_atom_cap(*, cap, atoms, incumbent, desired, config, has_progress,
                      blocked_fingerprints, learned_nogoods, fingerprint_fn):
    """Grow a soft default cap only for a new, unsplit required assignment."""
    if config.get("cohortSizePolicy") == "fixed" or cap != 24 or not has_progress:
        return cap
    sizes = []
    for names in atoms.values():
        if not cap < len(names) <= 32:
            continue
        assignment = {**incumbent, **{n: desired[n] for n in names}}
        if fingerprint_fn(assignment) in blocked_fingerprints:
            continue
        if any(clause and all(str(assignment.get(n)) == str(v) for n, v in clause.items())
               for clause in learned_nogoods):
            continue
        sizes.append(len(names))
    return min(sizes) if sizes else cap


def recover_checkpoint_audit(*, checkpoint_id, config, read_run, write_run,
                             read_checkpoint, operation, budget_ok, emit, pause, audit_identity=""):
    """Reserve attempts durably before work; restart cannot reset retry budget."""
    run = read_run()
    checkpoint = read_checkpoint()
    scope = ":".join([checkpoint_id, str(checkpoint.get("sourceSnapshotKey") or ""),
                      str(run.get("targetPolicyHash") or config.get("policyHash") or "")])
    if audit_identity:
        scope += ":" + audit_identity
    limit = 1 + max(0, min(5, int((config.get("budget") or {}).get("maxInfraRetries", 2))))
    while True:
        run = read_run()
        recovery = dict(run.get("auditRecovery") or {})
        state = recovery.get(scope) or {}
        attempts = int(state.get("attempts") or 0)
        in_flight = state.get("inFlight") is True
        if (attempts >= limit and not in_flight) or not budget_ok(run):
            return 0
        if attempts and not in_flight:
            audit = read_checkpoint().get("audit") or {}
            if audit.get("status") in {"PASS", "FAIL"} or not audit.get("retryableReason"):
                return 0
            emit({"event": "audit.retry", "checkpointId": checkpoint_id,
                  "attempt": attempts + 1, "limit": limit, "reason": audit["retryableReason"]})
            pause(min(4, attempts))
            if not budget_ok(read_run()):
                return 0
        reserved = attempts if in_flight else attempts + 1
        recovery[scope] = {"attempts": reserved, "limit": limit, "inFlight": True}
        write_run({**run, "auditRecovery": recovery})
        operation()
        latest = read_run()
        recovery = dict(latest.get("auditRecovery") or {})
        recovery[scope] = {"attempts": reserved, "limit": limit, "inFlight": False}
        write_run({**latest, "auditRecovery": recovery})
        audit = read_checkpoint().get("audit") or {}
        if audit.get("status") in {"PASS", "FAIL"} or not audit.get("retryableReason"):
            return 0


def discover_in_waves(*, discover, current, initial_packages, wall_budget_s,
                      progress, emit, clock):
    """Spend another wave only after useful, mostly reliable registry evidence."""
    names = sorted(current)
    batch_size = max(1, min(200, initial_packages or 60))
    ceiling = min(200, len(names))
    deadline = clock() + wall_budget_s if wall_budget_s > 0 else None
    targets, evidence, offset = {}, [], 0
    while offset < ceiling:
        remaining_time = deadline - clock() if deadline is not None else None
        if remaining_time is not None and remaining_time < 1:
            break
        batch = names[offset:min(ceiling, offset + batch_size)]
        wave_targets, rows = discover(
            {n: current[n] for n in batch},
            0 if remaining_time is None else max(1, int(remaining_time)),
            (lambda processed, total: progress(offset + processed, ceiling)) if progress else None)
        targets.update(wave_targets)
        evidence.extend(rows)
        offset += len(batch)
        reliable = sum(r.get("status") in {"discovered", "discovered-compatible", "up-to-date", "no-newer-compatible"} for r in rows)
        if not wave_targets or reliable * 5 < len(batch) * 4:
            break
        if offset < ceiling:
            emit({"event": "begin.discovery-budget-expanded", "processed": offset,
                  "nextPackages": min(batch_size, ceiling - offset), "maxPackages": ceiling,
                  "reason": "useful registry evidence; next wave within wall deadline"})
    evidence.extend({"package": n, "declared": current[n], "status": "discovery-budget-skipped",
                     "reason": "adaptive discovery stopped: no useful progress, unreliable registry or hard budget; no lag is claimed"}
                    for n in names[offset:])
    return dict(sorted(targets.items())), sorted(evidence, key=lambda r: r["package"])
