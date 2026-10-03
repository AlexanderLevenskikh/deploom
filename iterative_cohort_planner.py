"""Adaptive greedy cohort scheduling. Heuristics never grant verification proof."""
from __future__ import annotations
import re
import json
from pathlib import Path
from block_psi_progressive_baseline import ProgressiveExtensionPlan


def _family(name):
    for prefix, family in [("@babel/", "babel"), ("@storybook/", "storybook"), ("@typescript-eslint/", "ts-eslint"), ("@commitlint/", "commitlint")]:
        if name.startswith(prefix): return family
    return {"storybook": "storybook", "typescript-eslint": "ts-eslint", "react": "react-core", "react-dom": "react-core", "@types/react": "react-core", "@types/react-dom": "react-core"}.get(name, name)


def _peer_edges(names, project_dir):
    # Installed metadata is only a locality hint. Missing/stale metadata never
    # establishes compatibility or rejects a candidate; the resolver verifies.
    edges = set()
    if not project_dir: return edges
    for name in names:
        if not re.fullmatch(r"(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_.-]+", name): continue
        try:
            raw = json.loads((Path(project_dir) / "node_modules" / name / "package.json").read_text(encoding="utf-8"))
            for other in {**(raw.get("peerDependencies") or {}), **(raw.get("dependencies") or {})}:
                if other in names and other != name: edges.add(tuple(sorted((name, other))))
        except (OSError, ValueError, TypeError): pass
    return edges


def _risk(old, new):
    parse = lambda value: re.match(r"^(\d+)\.(\d+)\.(\d+)(?:$|[-+])", str(value))
    a, b = parse(old), parse(new)
    if not a or not b: return 3
    av, bv = tuple(map(int, a.groups())), tuple(map(int, b.groups()))
    return 3 if av[0] != bv[0] or (av[0] == 0 and av[1] != bv[1]) else (1 if av[1] != bv[1] else 0)


def plan_adaptive_cohort(*, incumbent, desired, config, ledger, checkpoints, blocked_fingerprints, learned_nogoods, fingerprint_fn, base_checkpoint_id):
    actionable = sorted(name for name in incumbent if name in desired and incumbent[name] != desired[name])
    if not actionable: return None, {}
    # An explicit cap remains authoritative. Existing singleton runs adopt the
    # new default unless they explicitly configured cohortMaxPackages=1.
    cap = max(1, min(32, int(config.get("cohortMaxPackages") or 24)))
    size = min(cap, 8)
    by_id = {c.get("checkpointId"): c for c in checkpoints if c.get("checkpointId")}
    chain, seen, cursor = [], set(), by_id.get(base_checkpoint_id)
    while cursor and cursor.get("checkpointId") not in seen:
        seen.add(cursor.get("checkpointId")); chain.append(cursor)
        cursor = by_id.get(cursor.get("parentCheckpointId"))
    sizes = [len((c.get("acceptedDelta") or {}).get("changed") or {}) for c in reversed(chain) if c.get("status") == "VERIFIED"]
    for accepted in sizes[-3:]:
        if accepted >= size: size = min(cap, max(size + 4, (max(size, accepted) * 3 + 1) // 2))
    priority = set(config.get("priorityPackages") or [])
    parent = {name: name for name in actionable}
    def find(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]; name = parent[name]
        return name
    def link(names):
        names = sorted(set(names) & parent.keys())
        for name in names[1:]: parent[find(name)] = find(names[0])
    families = {}
    for name in actionable: families.setdefault(_family(name), []).append(name)
    if cap > 1:
        for names in families.values(): link(names)
        for names in config.get("cohortGroups") or []:
            if isinstance(names, list): link(names)
        # Preserve actual proposed companions together with their original
        # package(s), never attach them to an arbitrary unrelated singleton.
        for entry in ledger.get("scopeExpansions") or []:
            if entry.get("baseCheckpointId") == base_checkpoint_id:
                link([*(entry.get("packages") or []), *(entry.get("companions") or [])])
    units = {}
    for name in actionable: units.setdefault(find(name), []).append(name)
    ordered = sorted(units.values(), key=lambda names: (not bool(priority.intersection(names)), max(_risk(incumbent[n], desired[n]) for n in names), tuple(names)))
    peer_edges = _peer_edges(set(actionable), config.get("projectDir"))
    roots, batch = [], []
    pending = list(ordered)
    while pending:
        unit = min(pending, key=lambda names: (not bool(priority.intersection(names)), -sum(1 for a,b in peer_edges if (a in batch and b in names) or (b in batch and a in names)), max(_risk(incumbent[n],desired[n]) for n in names), tuple(names)))
        pending.remove(unit)
        # Families are scheduling hints, not solver constraints. Oversized
        # families still obey the explicit cap, with deterministic chunks.
        for offset in range(0, len(unit), cap):
            part = unit[offset:offset+cap]
            if batch and len(batch) + len(part) > size:
                roots.append(batch); batch = []
            batch.extend(part)
            if len(batch) >= size: roots.append(batch); batch = []
    if batch: roots.append(batch)
    # A fallback half is eligible only when every ancestor was rejected by
    # exact evidence/deferral. Merely listing smaller groups must not recreate
    # the old singleton-first behavior.
    blocked = set(blocked_fingerprints)
    options = []
    def consider(names):
        assignment = dict(incumbent)
        assignment.update({n: desired[n] for n in names})
        fingerprint = fingerprint_fn(assignment)
        forbidden = not fingerprint or fingerprint in blocked or any(all(str(assignment.get(n)) == str(v) for n,v in clause.items()) for clause in learned_nogoods if clause)
        if forbidden:
            if len(names) > 1:
                midpoint = len(names) // 2
                consider(names[:midpoint]); consider(names[midpoint:])
            return
        changed = tuple(sorted(names))
        rank = (not bool(priority.intersection(changed)), -len(changed), sum(_risk(incumbent[n],desired[n]) for n in changed), changed)
        options.append((rank, ProgressiveExtensionPlan(tuple(sorted(assignment.items())), changed, fingerprint, len(actionable)-len(changed))))
    for names in roots: consider(names)
    if not options: return None, {"strategy": "adaptive-greedy", "batchLimit": size}
    _, plan = min(options, key=lambda option: option[0])
    return plan, {"strategy": "adaptive-greedy", "batchLimit": size, "maxPackages": cap, "selectedPackages": len(plan.packages), "remainingActionable": len(actionable), "familyGroups": [names for names in ordered if len(names)>1], "peerHintEdges": len(peer_edges)}
