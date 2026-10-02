"""Run-scoped availability deferrals; these never grant compatibility authority."""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Mapping


def availability_scope(run: Mapping, config: Mapping) -> str:
    # Hash environment coordinates without persisting proxy/auth configuration.
    coordinates = {key.lower(): value for key, value in os.environ.items()
                   if key.lower() in {'npm_config_registry', 'npm_config_userconfig',
                                      'npm_config_offline', 'npm_config_cache', 'yarn_npm_registry_server',
                                      'http_proxy', 'https_proxy'}}
    value = {'runId': run.get('runId'), 'policyHash': config.get('policyHash'),
             'registry': (config.get('verifyConfig') or {}).get('registry', ''),
             'runtime': config.get('runtime') or {}, 'coordinates': coordinates}
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def record_unavailable_targets(ledger: dict, run: Mapping, config: Mapping,
                               candidate: Mapping, output: str) -> bool:
    if not re.search(r'\bnpm (?:error|ERR!) code ETARGET\b', output):
        return False
    changed = (candidate.get('delta') or {}).get('changed') or {}
    targets = config.get('targets') or {}
    entries = list(ledger.get('targetAvailabilityDeferrals') or [])
    scope = availability_scope(run, config)
    added = False
    for match in re.finditer(r'No matching version found for ((?:@[^\s/@]+/)?[^\s/@]+)@([^\s.]+(?:\.[^\s.]+)*?)\.(?:\s|$)', output):
        name, version = match.groups()
        if changed.get(name) != version or targets.get(name) != version:
            continue
        entry = {'package': name, 'target': version, 'scope': scope,
                 'code': 'TARGET_VERSION_UNAVAILABLE', 'candidateId': candidate.get('candidateId'),
                 'reason': f'npm ETARGET: {name}@{version} unavailable for this run/registry context'}
        if not any(item.get('package') == name and item.get('target') == version and
                   item.get('scope') == scope for item in entries if isinstance(item, dict)):
            entries.append(entry)
            added = True
    if added:
        ledger['targetAvailabilityDeferrals'] = entries
    return added


def unavailable_targets(ledger: Mapping, run: Mapping, config: Mapping) -> set[str]:
    scope = availability_scope(run, config)
    targets = config.get('targets') or {}
    return {str(entry['package']) for entry in ledger.get('targetAvailabilityDeferrals', [])
            if isinstance(entry, dict) and entry.get('code') == 'TARGET_VERSION_UNAVAILABLE'
            and entry.get('scope') == scope and entry.get('package') in targets
            and targets[entry['package']] == entry.get('target')}
