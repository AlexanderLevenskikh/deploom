"""Small observed-cost heuristic. Measurements never create proof or nogoods."""
from __future__ import annotations
import hashlib
import json
import math

STRATEGIES = ('local-adaptive-region', 'adaptive-size', 'whole-first', 'split-first', 'size-aware', 'cost-aware')

def observation_scope(run, config):
    # Costs may transfer across accepted cohorts within this run, never across
    # another project/policy/runtime/validation contract. They are hints only.
    coordinates = {"runId": run.get("runId"), "projectDir": config.get("projectDir"),
                   "policyHash": config.get("policyHash"), "runtime": config.get("runtime"),
                   "validation": config.get("validationScope") or config.get("validationProfile"),
                   "verifyConfig": config.get("verifyConfig")}
    return hashlib.sha256(json.dumps(coordinates, sort_keys=True, default=str).encode()).hexdigest()

def estimate_action(names, incumbent, desired, observations, *, operator):
    """Smoothed success/cost from similar sized physical observations.

    Unknown actions keep conservative priors. Scope filtering belongs to the
    controller. Small samples are blended with priors; unknown/infra outcomes
    contribute observed cost but never count as incompatibility evidence.
    """
    size = len(names)
    related = [r for r in observations[-128:]
               if r.get('operator') == operator and isinstance(r.get('packages'), list)
               and size / 2 <= len(r['packages']) <= size * 2
               and isinstance(r.get('wallSeconds'), (int, float))
               and math.isfinite(r['wallSeconds']) and r['wallSeconds'] > 0]
    # At least three measured attempts before costs can change ordering.
    if len(related) < 3:
        return None
    duration = sum(r['wallSeconds'] for r in related) / len(related)
    definitive = [r for r in related if r.get('outcome') in ('accepted', 'rejected')]
    probability = (2 + sum(r['outcome'] == 'accepted' for r in definitive)) / (3 + len(definitive))
    return {'expectedSeconds': duration, 'successProbability': probability,
            'expectedPackagesPerSecond': size * probability / duration, 'samples': len(related)}

def append_observation(ledger, *, scope, candidate, seconds, outcome, source_key=''):
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        return
    rows = list(ledger.get('schedulerObservations') or [])
    rows.append({'scope': scope, 'candidateId': candidate.get('candidateId'),
                 'sourceSnapshotKey': source_key,
                 'operator': (candidate.get('cohortSelection') or {}).get('operator', 'whole'),
                 'packages': sorted((candidate.get('delta') or {}).get('changed') or {}),
                 'wallSeconds': seconds, 'outcome': outcome,
                 'costScope': 'materialize-precheck-verify; agent time unavailable'})
    ledger['schedulerObservations'] = rows[-128:]
