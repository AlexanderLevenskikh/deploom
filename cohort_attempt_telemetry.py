"""Physical attempt measurements; only the verifier can record accepted progress."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import math
import re
from pathlib import Path
from cohort_action_cost import observation_scope
from cohort_size_policy import next_size

def command_stage(command):
    text=str(command).lower()
    for stage,pattern in (("tsc",r"\btsc\b|type.?check|check:types"),("lint",r"lint"),("build",r"build"),("tests",r"\btest(?:s|:|\b)|vitest|jest"),("audit",r"audit")):
        if re.search(pattern,text):return stage
    return "project-check"

def stage_measurement(stage, seconds, outcome, command=""):
    value=seconds if isinstance(seconds,(int,float)) and math.isfinite(seconds) and seconds>=0 else None
    return {"stage":stage,"durationSeconds":value,"outcome":outcome,"command":command}

def event_offset(path):
    try:return Path(path).stat().st_size
    except OSError:return 0

def verification_stages(path, offset, fingerprint):
    rows=[]
    try:
        with Path(path).open("rb") as source:
            if source.seek(0,2)<offset:return rows # Rotation: unavailable rather than mixed evidence.
            source.seek(offset)
            for line in source:
                try:r=json.loads(line)
                except (ValueError,UnicodeError):continue
                if not isinstance(r,dict):continue
                if r.get("assignment")!=fingerprint or "durationMs" not in r:continue
                event=str(r.get("event") or "")
                if event=="verify.project-check.finish":stage=command_stage(r.get("command"))
                elif event=="verify.resolver.finish":stage="install"
                elif event=="verify.preparation.finish":stage="preparation"
                else:continue
                duration=r.get("durationMs")
                if not isinstance(duration,(int,float)):continue
                rows.append(stage_measurement(stage,duration/1000,
                    "UNKNOWN" if isinstance(r.get("exitCode"),int) and (r["exitCode"]<0 or r["exitCode"] in {124,137}) else "PASS" if r.get("outcome")=="passed" or r.get("exitCode")==0 else "FAIL" if r.get("outcome")=="failed" or (isinstance(r.get("exitCode"),int) and r["exitCode"]>0) else "UNKNOWN",str(r.get("command") or "")))
    except OSError:pass
    return rows

def agent_measurement(run_dir, candidate):
    # Desktop owns this lease; agent prose supplies no timing or authority.
    try:
        lease=json.loads((Path(run_dir)/"trial"/"agent-lease.json").read_text(encoding="utf-8"))
        if not isinstance(lease,dict):return None
        if any(str(lease.get(k))!=str(candidate.get(k)) for k in ("runId","candidateId","attemptId")):return None
        started=datetime.fromisoformat(lease["startedAt"].replace("Z","+00:00"))
        seconds=(datetime.now(timezone.utc)-started).total_seconds()
        if not 0<=seconds<=86400:return None
        return seconds
    except (OSError,ValueError,TypeError,KeyError):return None

def append_attempt(ledger, *, scope, config, candidate, stage, outcome, seconds,
                   stages=(), accepted_delta=None, cumulative_verified=None, size_feedback="NONE", evidence_kind=None):
    names=sorted(set((candidate.get("delta") or {}).get("changed") or {})|set((candidate.get("delta") or {}).get("added") or {}))
    selection=candidate.get("cohortSelection") or {}
    # Idempotency key is assigned by the locked caller to this physical action.
    sequence=int(candidate.get("telemetrySequence") or 0)
    key=f"{candidate.get('candidateId')}:{stage}:{sequence}"
    rows=list(ledger.get("attemptTelemetry") or [])
    if any(r.get("key")==key and r.get("scope")==scope for r in rows):return
    if size_feedback=="PASS" and accepted_delta is None:size_feedback="NONE"
    if size_feedback=="FAIL" and outcome!="FAIL":size_feedback="NONE"
    state=ledger.get("cohortSizeState") or {}
    state=next_size(state,config,feedback=size_feedback if config.get("cohortSchedulingStrategy")=="adaptive-size" else "NONE",actual_size=len(names),scope=scope)
    ledger["cohortSizeState"]=state
    from cohort_conflict_regions import update_regions
    failure_stage=next((r["stage"] for r in stages if r.get("outcome")=="FAIL"),stage)
    evidence=evidence_kind or ("structural" if failure_stage=="install" else "opaque" if failure_stage in {"tsc","lint","build","tests","project-check"} else "unavailable")
    regions, region = update_regions(ledger.get("conflictRegions") or [],scope=scope,config=config,candidate=candidate,
        outcome=outcome if size_feedback=="FAIL" else "UNKNOWN",stage=failure_stage,evidence=evidence,accepted_delta=accepted_delta,
        constraints=[e["nogood"] for e in ledger.get("blocks",[]) if e.get("scope")=="deterministic" and isinstance(e.get("nogood"),dict)])
    ledger["conflictRegions"]=regions
    progress=dict(ledger.get("schedulerProgress") or {})
    if progress.get("scope")!=scope:progress={"scope":scope,"cumulativeVerifiedPackages":cumulative_verified or 0,"candidateIdsWithoutProgress":[]}
    ids=list(progress.get("candidateIdsWithoutProgress") or [])
    physical_attempts=int(progress.get("physicalAttemptsWithoutVerifiedProgress") or 0)
    if accepted_delta is not None:
        ids=[];physical_attempts=0
        if cumulative_verified is not None:progress["cumulativeVerifiedPackages"]=cumulative_verified
    elif candidate.get("candidateId") and candidate["candidateId"] not in ids:ids.append(candidate["candidateId"])
    if accepted_delta is None and candidate.get("candidateId") and stage in {"install","precheck","verify-exact"}:physical_attempts+=1
    progress["candidateIdsWithoutProgress"]=ids
    progress["physicalAttemptsWithoutVerifiedProgress"]=physical_attempts
    progress["attemptsWithoutVerifiedProgress"]=physical_attempts
    ledger["schedulerProgress"]=progress
    families={tuple(g) for g in selection.get("familyGroups",[]) if set(g)&set(names)}
    covered={n for g in families for n in g}
    companions=set(candidate.get("scopeExpansionAdditions") or {})|set(candidate.get("scopeExpansionVersions") or {})|set(selection.get("companionPackages") or [])
    row={"key":key,"scope":scope,"candidateId":candidate.get("candidateId"),"baseCheckpointId":candidate.get("baseCheckpointId"),
         "operator":"agent-repair" if stage=="agent" else selection.get("operator","whole"),"cohortSize":len(names),"packages":names,"packageCount":len(names),
         "familyCount":len(families)+len(set(names)-covered),"dependencyAtomCount":selection.get("dependencyAtomCount"),"companionCount":len(companions&set(names)),
         "regionId":region["regionId"] if region else None,"regionLineage":region.get("lineage",[]) if region else [],
         "evidenceKind":evidence,"regionSoftSize":region.get("localSoftSize") if region else None,"regionStateReason":region.get("selectionReason") if region else None,"operatorReason":"bounded-agent-repair" if stage=="agent" else selection.get("operatorReason",selection.get("selectionPhase","physical-verification")),
         "stage":stage,"outcome":outcome,"failureStage":next((r["stage"] for r in stages if r.get("outcome")=="FAIL"),stage if outcome=="FAIL" else None),
         "wallSeconds":stage_measurement(stage,seconds,outcome)["durationSeconds"],"stages":list(stages),
         "agentSeconds":seconds if stage=="agent" else None,"acceptedDelta":accepted_delta,
         "cumulativeVerifiedPackages":progress["cumulativeVerifiedPackages"],"attemptsWithoutVerifiedProgress":physical_attempts,"candidatesWithoutVerifiedProgress":len(ids),
         "sizeFeedback":size_feedback,"schedulingStrategy":selection.get("schedulingStrategy",config.get("cohortSchedulingStrategy","whole-first")),"suggestedCohortSize":state["size"],"nextCohortSize":state["size"] if config.get("cohortSchedulingStrategy")=="adaptive-size" else None,"createdAt":datetime.now(timezone.utc).isoformat()}
    ledger["attemptTelemetry"]=(rows+[row])[-2048:]
    return row

def record_attempt(run_dir, run, config, candidate, **kwargs):
    from iterative_migration import load_ledger, save_ledger, load_checkpoint
    from bounded_telemetry import append_bounded_telemetry
    ledger=load_ledger(run_dir)
    checkpoint=load_checkpoint(run_dir,str(run["activeCheckpointId"]))
    base_id=candidate.get("baseCheckpointId") or run["activeCheckpointId"]
    region_base=checkpoint if base_id==run["activeCheckpointId"] else load_checkpoint(run_dir,str(base_id))
    from cohort_conflict_regions import region_source_key
    candidate={**candidate,"regionSourceKey":region_source_key(region_base)}
    assignment=checkpoint.get("fullAssignment") or {}
    cumulative=sum(str(assignment.get(n))==str(v) for n,v in (config.get("targets") or {}).items())
    kwargs.setdefault("cumulative_verified",cumulative)
    # Repair a crash between the durable checkpoint switch and telemetry write.
    progress=ledger.get("schedulerProgress") or {}
    if progress.get("checkpointId") and progress["checkpointId"]!=run["activeCheckpointId"]:
        ledger["schedulerProgress"]={"scope":observation_scope(run,config),"cumulativeVerifiedPackages":cumulative,"candidateIdsWithoutProgress":[]}
    row=append_attempt(ledger,scope=observation_scope(run,config),config=config,candidate=candidate,**kwargs)
    ledger["schedulerProgress"]["checkpointId"]=run["activeCheckpointId"]
    save_ledger(run_dir,ledger)
    if row:
        try:append_bounded_telemetry(Path(run_dir)/"cohort-attempts.jsonl",json.dumps(row)+"\n")
        except OSError:pass
