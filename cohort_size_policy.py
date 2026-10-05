"""Persisted AIMD cohort size; hints never establish compatibility or proof."""
from __future__ import annotations

def initial_size(config):
    cap=max(1,min(32,int(config.get("cohortMaxPackages") or 24)))
    return min(cap,max(1,int(config.get("cohortInitialPackages") or cap)))

def next_size(state, config, *, feedback, actual_size, scope):
    cap=max(1,min(32,int(config.get("cohortMaxPackages") or 24)))
    current=dict(state or {}) if (state or {}).get("scope")==scope else {}
    size=min(cap,max(1,int(current.get("size") or initial_size(config))))
    failures=int(current.get("failureStreak") or 0)
    successes=int(current.get("successStreak") or 0)
    if feedback=="FAIL":
        failures+=1;successes=0
        # One failure allows an independent intact experiment at the same size.
        if failures>=2:size=max(1,size//2);failures=0
    elif feedback=="PASS":
        failures=0
        successes=successes+1 if actual_size>=size else 0
        if successes>=2:size=min(cap,size+max(1,size//4));successes=0
    return {"scope":scope,"size":size,"failureStreak":failures,"successStreak":successes}
