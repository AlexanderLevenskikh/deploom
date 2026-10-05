"""Planner-owned dependency proposals; suggestions never grant verification."""
from __future__ import annotations
import json
import re
from pathlib import Path

NAME = r"(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_.-]+"
EXACT = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"

def parse_companion(value):
    value = str(value).strip()
    match = re.fullmatch(rf"({NAME})\s*=\s*(.+)", value)
    if not match:
        match = re.fullmatch(rf"({NAME})@({EXACT})", value)
    if match:
        return match.group(1), match.group(2)
    if re.fullmatch(NAME, value):
        return value, None
    return None


def prepare_expansions(incumbent, desired, ledger, checkpoint_id, checkpoints, unavailable=()):
    """Replay accepted-ancestor feedback, preserving the original target policy.

    Exact candidates are proposals, subsequently checked by install/engines and
    the authoritative verifier. Unknown/ranged inputs need an exact proposal,
    never an invented version or repeated unchanged repair.
    """
    ancestors = {checkpoint_id}
    by_id = {c.get('checkpointId'): c for c in checkpoints}
    cursor = by_id.get(checkpoint_id)
    while cursor and cursor.get('parentCheckpointId') not in ancestors:
        parent = cursor.get('parentCheckpointId')
        if not parent: break
        ancestors.add(parent)
        cursor = by_id.get(parent)
    entries = [e for e in ledger.get('scopeExpansions', [])
               if e.get('baseCheckpointId') in ancestors]
    versions = {}
    for entry in entries:
        for raw in entry.get('companions', []):
            parsed = parse_companion(raw)
            if parsed and parsed[1] and re.fullmatch(EXACT, parsed[1]) and parsed not in unavailable:
                versions[parsed[0]] = parsed[1]
    result = dict(desired)
    normalized, issues = [], []
    for entry in entries:
        companions, unresolved = {}, []
        for raw in entry.get('companions', []):
            parsed = parse_companion(raw)
            if not parsed:
                unresolved.append(str(raw)); continue
            name, version = parsed
            version = versions.get(name) or version or desired.get(name) or incumbent.get(name)
            if not version or not re.fullmatch(EXACT, str(version)) or (name, version) in unavailable:
                unresolved.append(str(raw)); continue
            companions[name] = version
        origins = [n for n in entry.get('packages', []) if n in incumbent]
        if unresolved or not companions:
            issues.append({'packages': origins, 'proposals': unresolved,
                           'reason': 'SCOPE_EXPANSION_NEEDS_EXACT_VERSION',
                           'nextAction': 'Provide package = exact-semver with registry evidence; other cohorts continue.'})
            for name in origins: result[name] = incumbent[name]
            continue
        result.update(companions)
        normalized.append({**entry, 'baseCheckpointId': checkpoint_id,
                           'companions': sorted(companions), 'requiredVersions': companions})
    # Unresolved requirements are not bypassed by a later unrelated proposal.
    for issue in issues:
        for name in issue['packages']: result[name] = incumbent[name]
    return result, {**ledger, 'scopeExpansions': normalized}, issues


def apply_expansion_assignment(project_dir, assignment, additions, apply_assignment, validate_assignment):
    """Declare planner-approved additions only in the isolated trial manifest.

    All original declarations are validated before writing, so fixed Git/file
    inputs retain the ordinary materialization guard. New peers are runtime
    dependencies: production installs must retain them too.
    """
    additions = dict(additions)
    if not additions:
        return apply_assignment(project_dir, assignment)
    if any(n not in assignment or assignment[n] != v or
           not re.fullmatch(NAME, n) or not re.fullmatch(EXACT, v)
           for n, v in additions.items()):
        raise ValueError('SCOPE_EXPANSION_ADDITIONS_INVALID')
    manifest = validate_assignment(project_dir, {n:v for n,v in assignment.items() if n not in additions})
    sections = ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies')
    for name in additions:
        if any(name in (manifest.get(s) or {}) for s in sections):
            raise ValueError('SCOPE_EXPANSION_ADDITION_ALREADY_DECLARED: ' + name)
    if additions:
        dependencies = manifest.setdefault('dependencies', {})
        if not isinstance(dependencies, dict): raise ValueError('SCOPE_EXPANSION_DEPENDENCIES_INVALID')
        dependencies.update(additions)
        path = Path(project_dir) / 'package.json'
        temp = path.with_name(path.name + '.expansion.tmp')
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temp.replace(path)
    return sorted(set(apply_assignment(project_dir, assignment)) | set(additions))
