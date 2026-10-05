"""Adaptive greedy cohort scheduling. Heuristics never grant verification proof."""
from __future__ import annotations
import re
import json
from pathlib import Path
from cohort_action_cost import estimate_action, STRATEGIES
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


def _budget_strategy(config):
    # B4: the split-then-recombine closure is strictly bounded so a fully
    # conflicting group collapses to a few leaves instead of a combinatorial
    # explosion; the explicit cohort cap remains authoritative. The bound is
    # generous enough to complete a realistic cap-8 cohort (a dense conflict
    # graph visits at most a few hundred closure nodes) but never unbounded.
    return max(8, min(4096, int(config.get("cohortSearchSteps") or 128)))


def plan_adaptive_cohort(*, incumbent, desired, config, ledger, checkpoints, blocked_fingerprints, learned_nogoods, fingerprint_fn, base_checkpoint_id):
    actionable = sorted(name for name in desired if incumbent.get(name) != desired[name])
    if not actionable: return None, {}
    # An explicit cap remains authoritative. Existing singleton runs adopt the
    # new default unless they explicitly configured cohortMaxPackages=1.
    cap = max(1, min(32, int(config.get("cohortMaxPackages") or 24)))
    strategy = config.get("cohortSchedulingStrategy", "whole-first")
    local_mode = strategy == "local-adaptive-region" or (strategy == "whole-first" and config.get("localConflictRegions", True))
    size = min(cap, max(1, int(config.get("cohortInitialPackages") or cap)))
    if strategy == "adaptive-size":
        size = min(cap, max(1, int((ledger.get("cohortSizeState") or {}).get("size") or size)))
    by_id = {c.get("checkpointId"): c for c in checkpoints if c.get("checkpointId")}
    chain, seen, cursor = [], set(), by_id.get(base_checkpoint_id)
    while cursor and cursor.get("checkpointId") not in seen:
        seen.add(cursor.get("checkpointId")); chain.append(cursor)
        cursor = by_id.get(cursor.get("parentCheckpointId"))
    sizes = [len((c.get("acceptedDelta") or {}).get("changed") or {}) for c in reversed(chain) if c.get("status") == "VERIFIED"]
    for accepted in (sizes[-3:] if strategy != "adaptive-size" and config.get("cohortSizePolicy") != "fixed" else []):
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
        for names in [*(config.get("cohortGroups") or []), *(config.get("cohortAtoms") or [])]:
            if isinstance(names, list): link(names)
        # Preserve actual proposed companions together with their original
        # package(s), never attach them to an arbitrary unrelated singleton.
        for entry in ledger.get("scopeExpansions") or []:
            if entry.get("baseCheckpointId") == base_checkpoint_id:
                link([*(entry.get("packages") or []), *(entry.get("companions") or [])])
    # Mandatory atoms are independent of soft family/locality hints.
    atom_parent = {n: n for n in actionable}
    def atom_find(n):
        while atom_parent[n] != n:
            atom_parent[n] = atom_parent[atom_parent[n]]; n = atom_parent[n]
        return n
    def atom_link(names):
        names = sorted(set(names) & atom_parent.keys())
        for n in names[1:]: atom_parent[atom_find(n)] = atom_find(names[0])
    for names in config.get("cohortAtoms") or []: atom_link(names)
    for entry in ledger.get("scopeExpansions") or []:
        if entry.get("baseCheckpointId") == base_checkpoint_id:
            atom_link([*(entry.get("packages") or []), *(entry.get("companions") or [])])
    atoms = {}
    for n in actionable: atoms.setdefault(atom_find(n), []).append(n)
    atom_for = {n: set(group) for group in atoms.values() for n in group}
    if any(len(group) > cap for group in atoms.values()):
        raise ValueError("COHORT_ATOM_EXCEEDS_CAP: increase cap or revise the exact companion proposal")
    def atomic_parts(names):
        parts = {}
        for n in names: parts.setdefault(atom_find(n), []).append(n)
        return list(parts.values())
    units = {}
    for name in actionable: units.setdefault(find(name), []).append(name)
    ordered = sorted(units.values(), key=lambda names: (not bool(priority.intersection(names)), max(_risk(incumbent.get(n), desired[n]) for n in names), tuple(names)))
    peer_edges = _peer_edges(set(actionable), config.get("projectDir"))
    roots, batch = [], []
    pending = list(ordered)
    while pending:
        unit = min(pending, key=lambda names: (not bool(priority.intersection(names)), -sum(1 for a,b in peer_edges if (a in batch and b in names) or (b in batch and a in names)), max(_risk(incumbent.get(n),desired[n]) for n in names), tuple(names)))
        pending.remove(unit)
        # Families are scheduling hints, not solver constraints. Oversized
        # families still obey the explicit cap, with deterministic chunks.
        if local_mode or strategy == "adaptive-size" or config.get("cohortSizePolicy") == "fixed":
            parts, chunk = [], []
            for atom in atomic_parts(unit):
                if chunk and len(chunk) + len(atom) > size: parts.append(chunk); chunk = []
                chunk.extend(atom)
                if len(chunk) >= size: parts.append(chunk); chunk = []
            if chunk: parts.append(chunk)
        else: parts = [unit[offset:offset+cap] for offset in range(0,len(unit),cap)]
        for part in parts:
            if batch and len(batch) + len(part) > size:
                roots.append(batch); batch = []
            batch.extend(part)
            if len(batch) >= size: roots.append(batch); batch = []
    if batch: roots.append(batch)
    regions = ledger.get("conflictRegions") or []
    if local_mode:
        from cohort_conflict_regions import region_roots
        roots = region_roots(roots, regions, atomic_parts, size)

    # B4: split-then-recombine with a strictly bounded closure.
    #
    # If a whole group is rejected by exact evidence, it is divided (existing
    # behavior). If however a STRICTLY SMALLER learned clause (typically a
    # mutually exclusive pair like a+h) is satisfied by the group, resolution
    # must not depend on the order the packages appear in; instead every member
    # of the minimal conflicting clause is dropped in turn and the conflict-free
    # subgroups are RE-COMBINED into a maximal compatible cover. This closes the
    # cross-group gap without skipping, repeating or duplicating packages. The
    # closure is bounded by the cohort cap and an explicit node budget.
    # Clauses spanning the cumulative checkpoint and a new cohort remain active.
    # Project fixed members only when the incumbent already satisfies them.
    mutable = set(actionable)
    learned_nogoods = [
        {n: v for n, v in clause.items() if n in mutable}
        for clause in learned_nogoods
        if all(str(incumbent.get(n)) == str(v) for n, v in clause.items() if n not in mutable)
    ]
    blocked = set(blocked_fingerprints)
    node_budget = [_budget_strategy(config)]
    options = []
    # Legacy strategies remain available to benchmarks; adaptive size is opt-in.
    if strategy not in STRATEGIES:
        raise ValueError("UNKNOWN_COHORT_SCHEDULING_STRATEGY: " + str(strategy))
    observations = ledger.get("schedulerObservations") or []
    intact = set()


    def clause_matches(clause, assignment):
        return all(str(assignment.get(n)) == str(v) for n, v in clause.items())

    def whole_blocked(names, assignment, fingerprint):
        if any(not atom_for[n].issubset(names) for n in names): return True
        # Splitting a failed cohort must not drop mandatory companions.
        for entry in ledger.get("scopeExpansions") or []:
            if entry.get("baseCheckpointId") == base_checkpoint_id and any(n in names for n in entry.get("packages", [])):
                if any(assignment.get(n) != v for n, v in (entry.get("requiredVersions") or {}).items()):
                    return True
        if not fingerprint or fingerprint in blocked:
            return True
        return any(
            clause and set(clause) == set(names) and clause_matches(clause, assignment)
            for clause in learned_nogoods
        )

    def min_subset_conflict(names, assignment):
        nameset = set(names)
        clauses = [
            clause
            for clause in learned_nogoods
            if clause and set(clause).issubset(nameset) and set(clause) != nameset
            and clause_matches(clause, assignment)
        ]
        return min(clauses, key=lambda c: len(c)) if clauses else None

    def compatible_cover(names, leaves):
        # Greedy, deterministic re-combine of mutually disjoint conflict-free
        # leaves. The cohort cap bounds the cover; the compatibility check keeps
        # the union free of whole blocks and of smaller conflicting clauses, so
        # closing a gap never re-creates a rejected combination.
        picked = set()
        for leaf in sorted(leaves, key=lambda leaf: (-len(leaf), leaf)):
            union = tuple(sorted(set(leaf) | picked))
            if len(union) > max(size, max((len(atom_for[n]) for n in union), default=0)):
                continue
            assignment = dict(incumbent); assignment.update({n: desired[n] for n in union})
            fingerprint = fingerprint_fn(assignment)
            if whole_blocked(union, assignment, fingerprint) or min_subset_conflict(union, assignment) is not None:
                continue
            picked = set(union)
        return tuple(sorted(picked))

    def closure_leaf(names):
        # Maximal compatible subset of `names` with all inner conflicts closed.
        # A member of the blocker (the minimal conflicting clause, or ANY member
        # for a whole-blocked sub-group) is dropped in turn and the conflict-free
        # results are re-combined, so even a dense conflict graph collapses to
        # compatible leaves instead of returning None. Strictly decreasing in
        # size per step; node_budget keeps the resolution bounded.
        node_budget[0] -= 1
        if node_budget[0] <= 0 or not names:
            return None
        names = tuple(sorted(set(names)))
        assignment = dict(incumbent); assignment.update({n: desired[n] for n in names})
        fingerprint = fingerprint_fn(assignment)
        is_whole = whole_blocked(names, assignment, fingerprint)
        conflict = None if is_whole else min_subset_conflict(names, assignment)
        if not is_whole and conflict is None:
            return names
        dropset = sorted(conflict) if conflict is not None else sorted(names)
        seen, leaves = set(), []
        for name in dropset:
            leaf = closure_leaf(tuple(n for n in names if n not in atom_for[name]))
            if leaf and leaf not in seen:
                seen.add(leaf)
                leaves.append(leaf)
        if not leaves:
            return None
        return compatible_cover(names, leaves) or None

    def consider(names, *, full_only=False):
        # A fallback half is eligible only when every ancestor was rejected by
        # exact evidence/deferral. Merely listing smaller groups must not recreate
        # the old singleton-first behavior.
        names = tuple(sorted(set(names)))
        if not names:
            return
        assignment = dict(incumbent); assignment.update({n: desired[n] for n in names})
        fingerprint = fingerprint_fn(assignment)
        if whole_blocked(names, assignment, fingerprint):
            if full_only: return
            if len(names) > 1:
                parts = atomic_parts(names)
                if len(parts) > 1:
                    midpoint = len(parts) // 2
                    consider([n for part in parts[:midpoint] for n in part]); consider([n for part in parts[midpoint:] for n in part])
            return
        conflict = min_subset_conflict(names, assignment)
        if conflict is not None:
            if full_only: return
            # Split-then-recombine: resolve the conflicting pair order-
            # independently, then close the gap over the conflict-free leaves.
            leaves = []
            for name in sorted(conflict):
                leaf = closure_leaf(tuple(n for n in names if n not in atom_for[name]))
                if leaf:
                    leaves.append(leaf)
            cover = compatible_cover(names, leaves) if leaves else None
            if cover:
                push_option(cover)
            return
        push_option(names)

    def push_option(names):
        assignment = dict(incumbent); assignment.update({n: desired[n] for n in names})
        fingerprint = fingerprint_fn(assignment)
        changed = tuple(sorted(names))
        operator = "whole" if changed in intact else "split"
        estimate = estimate_action(changed, incumbent, desired, observations, operator=operator) if strategy == "cost-aware" else None
        # Use observed costs only when ALL alternatives can be compared. A
        # missing prior must not turn an unmeasured action into a free action.
        rank = (not bool(priority.intersection(changed)), -len(changed), sum(_risk(incumbent.get(n),desired[n]) for n in changed), changed)

        options.append((rank, ProgressiveExtensionPlan(tuple(sorted(assignment.items())), changed, fingerprint, len(actionable)-len(changed)), operator, estimate))

    # Prefer another intact cohort before spending work localizing a failure.
    for names in roots:
        intact.add(tuple(sorted(names)))
        consider(names, full_only=True)
    selection_phase = "whole-cohort"
    if not options or strategy == "split-first":
        selection_phase = "split-recombine"
        for names in roots: consider(names)
    else:
        # A tiny intact cohort must not starve a much larger deferred remainder.
        # Exploration is purely structural here; each produced candidate still
        # needs fresh project verification. The closure budget bounds CPU work.
        whole_size = max(len(option[1].packages) for option in options)
        ratio = max(2, min(16, int(config.get("cohortExplorationRatio") or 2)))
        minimum = max(2, int(config.get("cohortExplorationMinPackages") or 8))
        worthwhile = [names for names in roots if strategy not in {"whole-first", "adaptive-size", "local-adaptive-region"}
                      and (strategy == "cost-aware" or len(names) >= max(minimum, whole_size * ratio))
                      and whole_blocked(names, {**incumbent, **{n: desired[n] for n in names}},
                                        fingerprint_fn({**incumbent, **{n: desired[n] for n in names}}))]
        if worthwhile:
            selection_phase = "size-aware-split"
            for names in worthwhile: consider(names)
    # Exact blocks are not subset nogoods: rejected singletons may work together.
    # Explore bounded cross-root combinations after splits fail, preserving the
    # normal greedy path and leaving compatibility proof to the verifier.
    if not options and cap > 1:
        import itertools
        for width in range(2, min(size, len(actionable)) + 1):
            for names in itertools.combinations(actionable, width):
                node_budget[0] -= 1
                if node_budget[0] <= 0:
                    break
                assignment = dict(incumbent)
                assignment.update({n: desired[n] for n in names})
                if not whole_blocked(names, assignment, fingerprint_fn(assignment)) and min_subset_conflict(names, assignment) is None:
                    push_option(names)
                    break
            if options or node_budget[0] <= 0:
                break
    if not options:
        exhausted = node_budget[0] <= 0
        limit = _budget_strategy(config)
        if exhausted and limit < 4096:
            # Cheap structural search may grow before spending another install
            # or project check. Budget exhaustion is not scope incompatibility.
            plan, details = plan_adaptive_cohort(incumbent=incumbent, desired=desired,
                config={**config, "cohortSearchSteps": min(4096, limit * 4)}, ledger=ledger,
                checkpoints=checkpoints, blocked_fingerprints=blocked_fingerprints,
                learned_nogoods=learned_nogoods, fingerprint_fn=fingerprint_fn,
                base_checkpoint_id=base_checkpoint_id)
            details["searchEscalations"] = details.get("searchEscalations", 0) + 1
            return plan, details
        return None, {"strategy": "adaptive-greedy", "batchLimit": size,
                      "searchBudgetExhausted": exhausted, "searchStepsLimit": limit}
    measured = strategy == "cost-aware" and all(option[3] is not None for option in options)
    if measured:
        chosen = min(options, key=lambda option: (option[0][0], -option[3]["expectedPackagesPerSecond"], option[0]))
    else:
        chosen = min(options, key=lambda option: option[0])
    _, plan, operator, estimate = chosen
    from cohort_conflict_regions import selection_region
    region_selection = selection_region(regions, plan.packages) if local_mode else {"operatorReason": selection_phase}
    if region_selection.get("regionIds") and region_selection["operatorReason"] == "opaque-region-local-split": operator = "local-split"
    return plan, {**region_selection, "localRegionScheduling": local_mode, "selectedAtoms": atomic_parts(plan.packages),"strategy": "adaptive-greedy", "schedulingStrategy": strategy, "operator": operator, "costEstimate": estimate if measured else None, "batchLimit": size, "maxPackages": cap, "selectedPackages": len(plan.packages), "selectionPhase": selection_phase, "remainingActionable": len(actionable), "familyGroups": [names for names in ordered if len(names)>1], "dependencyAtomCount": len(atomic_parts(plan.packages)), "companionPackages": sorted({n for e in ledger.get("scopeExpansions", []) if e.get("baseCheckpointId") == base_checkpoint_id for n in e.get("companions", []) if n in plan.packages}), "peerHintEdges": len(peer_edges)}
