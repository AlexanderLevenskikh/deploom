"""Local search hints. Failed regions never establish compatibility or proof."""
from __future__ import annotations
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from cohort_size_policy import initial_size


def update_regions(regions, *, scope, config, candidate, outcome, stage, evidence="opaque", accepted_delta=None, constraints=()):
    rows=[deepcopy(r) for r in regions if r.get("scope")==scope]
    names=set((candidate.get("delta") or {}).get("changed") or {})|set((candidate.get("delta") or {}).get("added") or {})
    if not names:return rows, None
    matching=[r for r in rows if names.intersection(r["packages"]) and (candidate.get("regionSourceKey") is None or r.get("sourceHintKey")==candidate["regionSourceKey"])]
    if accepted_delta is not None:
        for r in matching:
            r["lastProgress"]={"candidateId":candidate.get("candidateId"),"acceptedPackages":sorted(names.intersection(r["packages"]))}
            r["attemptsWithoutProgress"]=0
        return rows, matching[0] if matching else None
    if outcome!="FAIL":return rows, matching[0] if matching else None
    members=names
    parents=sorted(r["regionId"] for r in matching)
    exact=next((r for r in matching if set(r["packages"])==names),None)
    if exact:
        region=exact
    else:
        identity=json.dumps([scope,candidate.get("regionSourceKey"),sorted(members),parents],sort_keys=True)
        region={"regionId":"region-"+hashlib.sha256(identity.encode()).hexdigest()[:16],"lineage":parents,
                "scope":scope,"sourceHintKey":candidate.get("regionSourceKey"),"attempts":0,"attemptsWithoutProgress":0,
                "localSoftSize":min([initial_size(config),len(names)]+[r["localSoftSize"] for r in matching]),
                "knownConstraints":[],"companions":[],"lastProgress":None}
        rows.append(region)
    region["packages"]=sorted(members)
    region["failureStage"]=stage
    region["evidence"]=evidence
    region["attempts"]+=1
    region["attemptsWithoutProgress"]+=1
    region["lastCandidateId"]=candidate.get("candidateId")
    selection=candidate.get("cohortSelection") or {}
    region["companions"]=sorted(set(region["companions"])|set(selection.get("companionPackages") or [])|set(candidate.get("scopeExpansionAdditions") or {}))
    region["atoms"]=selection.get("selectedAtoms") or [[n] for n in sorted(names)]
    for clause in constraints:
        if clause and set(clause)&members and clause not in region["knownConstraints"]:region["knownConstraints"].append(clause)
    if evidence=="opaque":
        region["localSoftSize"]=max(1,min(region["localSoftSize"],len(names))//2)
    region["selectionReason"]="opaque-project-failure-local-shrink" if evidence=="opaque" else "structural-evidence-constraint-filtering"
    return rows,region


def region_roots(roots, regions, atomic_parts, size):
    """Partition only opaque failed regions; unrelated whole cohorts keep size."""
    opaque=[r for r in regions if r.get("evidence")=="opaque"]
    if not opaque:return roots
    buckets={};healthy=[]
    for root in roots:
        for atom in atomic_parts(root):
            owners=[(len(r["packages"]),-i,r) for i,r in enumerate(regions) if set(atom)&set(r["packages"])]
            owner=min(owners,key=lambda x:x[:2])[2] if owners else None
            if owner and owner.get("evidence")=="opaque":buckets.setdefault(owner["regionId"],[]).append(atom)
            else:healthy.append(atom)
    result=[]
    def pack(parts,limit):
        chunk=[]
        for atom in parts:
            if chunk and len(chunk)+len(atom)>limit:result.append(chunk);chunk=[]
            chunk.extend(atom)
            if len(chunk)>=limit:result.append(chunk);chunk=[]
        if chunk:result.append(chunk)
    pack(healthy,size)
    for r in opaque:pack(buckets.get(r["regionId"],[]),max(1,min(size,r["localSoftSize"])))
    return result


def selection_region(regions,names):
    matches=[r for r in regions if set(names)&set(r["packages"])]
    owners=[min([(len(r["packages"]),-i,r) for i,r in enumerate(regions) if n in r["packages"]],key=lambda x:x[:2])[2] for n in names if any(n in r["packages"] for r in regions)]
    return {"regionIds":[r["regionId"] for r in matches],"regionLineage":{r["regionId"]:r.get("lineage",[]) for r in matches},
            "operatorReason":("opaque-region-local-split" if any(r.get("evidence")=="opaque" for r in owners) else "structural-constraint-filtering" if owners else "independent-intact-cohort")}


def region_source_key(checkpoint):
    """Source/config hint identity only; full source snapshot stays proof authority.

    Read the already sealed checkpoint manifest. Dependency assignment fields and
    known lockfiles do not reset search hints. Every other byte/config field does.
    Unavailable evidence disables reuse instead of inventing a source identity.
    """
    try:
        if not checkpoint.get("sourceSnapshotContainer"):return None
        container=Path(checkpoint["sourceSnapshotContainer"])
        manifest=json.loads((container/"manifest.json").read_text(encoding="utf-8"))
        if manifest.get("sourceSnapshotKey")!=checkpoint.get("sourceSnapshotKey"):return None
        entries=manifest.get("entries")
        if not isinstance(entries,list):return None
        normalized=[];tree=(container/"tree").resolve()
        locks={"package-lock.json","npm-shrinkwrap.json","yarn.lock","pnpm-lock.yaml","bun.lock","bun.lockb"}
        for original in entries:
            entry=dict(original);relative=Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:return None
            if relative.name in locks:continue
            if relative.name=="package.json" and entry.get("kind")=="file":
                target=(tree/relative).resolve()
                if not target.is_relative_to(tree):return None
                raw=target.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=entry.get("sha256"):return None
                package=json.loads(raw)
                for section in ("dependencies","devDependencies","optionalDependencies","peerDependencies"):
                    package.pop(section,None)
                entry.pop("size",None)
                entry["sha256"]=hashlib.sha256(json.dumps(package,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()
            normalized.append(entry)
        payload={"policyKey":manifest.get("policyKey"),"projectRelative":checkpoint.get("projectRelative","."),"entries":normalized}
        return "source-hint-"+hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    except (OSError,ValueError,TypeError,KeyError,AttributeError):return None
