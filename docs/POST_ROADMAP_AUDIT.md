# DepLoom Post-Roadmap Audit — Final Engineering Report

Scope: production code in `baseline_constraint_verifier.py`, `verification_proof.py`,
`constraint_cache.py`, `constraint_verify.py`, `peer_solver_model.py`, `peer_solver_z3.py`,
`peer_solver_transition.py`, `dependency_interaction.py`, `dependency_compatibility_evidence.py`,
`dependency_live_roadmap_generator.py` (main orchestrator), plus `desktop/` Run Monitor.
README/tests/comments were used only as navigation; every claim below comes from reading
actual production code. Line numbers refer to the audited tree.

---

## Executive verdict

- **Correctness state:** High. The proof model is deliberately **fail-closed** and honours the
  "package-manager evidence is authoritative, everything else is diagnostic" discipline almost
  everywhere. Solver assignment, exact confirmation, conflict localization, minimization and
  cross-iteration learning all converge on `verify_assignment` (a fresh clone + real PM install)
  as the single authority gate. I did **not** find a P0 that can turn a wrong version decision
  into a "proven" one.
- **Proof model soundness:** Yes, under its own identity model. Resolver/project proofs are content-
  and environment-addressed and chained (`project -> preparation -> resolver`). The two known gaps
  (below) are *identity-completeness* gaps for fixed inputs, not a broken chain.
- **Most dangerous remaining bugs:** (1) fixed `workspace:`/`link:`/`file:`/`git+` inputs whose
  real resolved content lives **outside** the project's hashed identity can yield a stale
  resolver PASS / project PASS being reused for a genuinely different resolver state;
  (2) the per-command serial control re-materialization is the dominant wall-clock cost and also
  the most fragile part of the UI pipeline.
- **Biggest performance bottleneck:** fully **sequential** dependency discovery (registry + OSV +
  tarball probes), with **no persistent on-disk HTTP cache**. Second: repeated full workspace
  materialization (`git clone --shared` of the same repo) + repeated lifecycle install for every
  control/probe/confirmation/minimization on the same assignment.
- **Realistic potential speedup:** with 4–6 Pareto changes, a complex 75-package project can go
  from a ~tens-of-minutes-to-hours fat tail to roughly **8–20 minutes in steady state** (and
  near-idle for the scan on a warm cache). The scan drops ~5–10×; verification economics drops
  2–4× via control batching + immutable snapshots + proof-cache hits. The physical lower bound is
  bounded below by **one fresh real-PM install per distinct verified assignment** and **one fresh
  confirmation per localized culprit**, so it cannot reach single-digit minutes on a pathological
  many-conflict run.

---

## P0 — Correctness findings

**No P0 found in the verification/proof core.** The strongest candidates were examined and each
fails closed rather than producing a bad assignment:

- Exact confirmation of a `dependency` failure cannot wrongly "pass" from a stale resolver proof:
  a failing assignment never has a PASS cached for the same key, and if a mismatched resolver PASS
  were somehow present, `matching_dependency_failure_signature` returns `""` → `confirmed_exact_failure=False`
  → `BASELINE_VERIFY_INCONCLUSIVE_CONFIRMATION` (roadmap:8854–8887). Never learned.
- `UNKNOWN != FAIL != PASS` is enforced at every gate (`infrastructure`/`unknown` raises instead
  of being treated as evidence) — e.g. `_classify_install_failure` returns `"unknown"` for
  unclassified non-zero exits (baseline_constraint_verifier.py:692–700) and callers raise on it.
- Solver assignment cannot be silently changed after verification: the post-loop `apply_results=True`
  re-solve is a **replay** and any drift raises `PROVEN_ASSIGNMENT_REOPENED`
  (roadmap:9965–9972); a second conformance assertion runs after every consumer at 15039.
- Global exact-exclusion coordination rejects a component tuple only when the *complete* tuple
  matches a confirmed exclusion, never a single local component (constraint_verify.py:75–256).

---

## P1 — Correctness / determinism findings

### P1-1. Fixed-input identity does not capture content that lives outside the hashed project
**Severity:** P1 (correctness/identity). **Files:** `verification_proof.py:231–277` (`fixed_resolver_input_fingerprint`),
`verification_proof.py:280–348` (`source_snapshot_fingerprint`).

- `workspace:` deps hash only `source_snapshot_fingerprint(project_dir)` (verification_proof.py:263–266),
  which is the **project's own** git HEAD + diff + untracked (280–348). A `workspace:`/`link:`/`file:`/`portal:`
  sibling that lives in a **different git repo** (or is a symlinked package) is not covered. Its
  content can change (version bump or peer change) without changing the resolver/project key.
- `git+`/`github:` deps: the spec (`#ref`) is captured, but the *resolved commit* is only captured
  because the lockfile is hashed (`_resolver_ancestor_files`, 351–370). If the dependency is not
  pinned by a committed lockfile and the remote ref/commit moves, a previously cached resolver
  PASS / **project PASS** (cache hit short-circuits both installs, baseline_constraint_verifier.py:1216–1240)
  is reused for a different underlying tree.
- Impact: a false proof *reuse* (over-strong reuse), not a false acceptance of a broken assignment
  — bounded but real. On the reused-project-proof path no fresh materialization runs at all.

**Reproducer:** A `package.json` with `"internal-ui": "workspace:"` whose sibling package.json lives
in a second repo; bump the sibling's dependency and re-run without touching the project repo.
Observe `proof.cache.hit` (project) while the sibling's tree is different.

**Fix:** include the resolved content identity of every fixed target: for `workspace:`/`link:` hash the
resolved sibling dir (`_external_fixed_target_identity` when resolvable, else the workspace entry);
for `git+`/`github:` hash the locked resolved commit from the lockfile (or run `git ls-remote` when
unpinned). Roll `PROOF_SCHEMA_VERSION` and `SOLVER_SCHEMA_VERSION`.
**Proof implications:** only makes reuse stricter.
**Complexity:** M. **Wall-clock:** none (identity already computed per run).

### P1-2. `observed_versions = []` rebinding in resolver-reuse path
**Severity:** P1 (latent, currently masked) → degrade to P3 for now, but flag. **File:**
`baseline_constraint_verifier.py:907`.

Inside the manifest-drift loop, `observed_versions = []` (907) shadows the dict built at 839–847.
The loop's local list is then discarded, and `observed_versions` is recomputed on every path that
uses it (lifecycle, 1026/1119), so today it is masked. However the `run_project_checks=False` +
`resolver_reused=True` final return at 1130–1134 would return `observed_resolved_versions=[]`
(an empty list) — reachable only if `reuse_resolver_proof_key` is set with `run_project_checks=False`,
which the cache-aware wrapper does not currently do (it only sets reuse when `wants_project_proof`,
1200–1215). Worth fixing to remove the trap: rename the loop variable.

**Fix:** use `declared_versions` for the loop list; never rebind `observed_versions`.
**Complexity:** S.

### P1-3. Fixed-only authoritative conflict leaves no caller-visible diagnostic channel
**Severity:** P1 (hard failure, but fail-closed). **File:** roadmap:3103–3112 (`_project_constraints_over_fixed_inputs`).
When a learned/confirmed clause matches only immutable fixed inputs, it raises
`FIXED_INPUT_CONSTRAINT_CONFLICT` — correct (cannot make an empty clause) but terminates the whole
project/mode with no path to surface "your fixed dependency graph itself is broken." Today the only
authoritative signal is the resolver install failing, which is then re-localized. Acceptable; keep
but document. Not a correctness bug.

### P1-4. `environment_key` hashes the *entire* inherited process environment (values sha256'd)
**Severity:** P1 for cache effectiveness, not correctness. **File:** verification_proof.py:78–84.
Any env change (even irrelevant `TMPDIR`, `CI`, random session vars) invalidates **all** proof keys,
causing re-materialization on essentially every run. This is conservative (never unsound) but is a
major contributor to repeated installs. See Optimization 3.

### P1-5. Determinism is sound, with one caveat
Solver, domains, tie-breaks, heap ordering and graph traversal are fully deterministic on fixed
inputs (peer_solver_z3.py:97–108 deterministic lex tie-break; `_solve_large_peer_component`
counter-secondary-key heap, roadmap:6179–6194; deterministic DFS `_graph_components` 5425–5440).
Localization identity includes `sourceHead + env + assignment + commands + units`
(roadmap:9479–9496), so resume only matches an identical experiment. Caveat: `_candidate_domain`
and `_potential_peer_graph` make registry calls and depend on **candidate availability**; flaky
registry availability changes unit membership and thus the *localization experiment* — but this is
diagnostic until fresh-certified and never changes the verified final assignment. OK.

---

## P2 — Performance findings (evidence + numbers)

### P2-1. Dependency discovery is fully sequential with no durable cache  ← dominant scan cost
**Files:** roadmap:14640–14652 (project loop), 3153+ (per-dependency loop), LiveDataClient 1457–2450.
- Zero concurrency in all network paths (no `ThreadPoolExecutor`/asyncio in this file).
- One shared `requests.Session` (1464) is reused → keep-alive/pooling is already present; not the issue.
- **No on-disk cache** for registry metadata / OSV / tarball availability / candidate generation.
  All caches are in-process dicts (1477–1487). Every baseline re-fetches everything.
- Per package *core* scan ≈ metadata GET + 1–3 tarball ranged probes (Range: bytes=0-0, 1572) +
  1 OSV querybatch POST + V vuln-detail GETs + T full-tarball GETs ≈ **6–15 requests**; plus a global
  `RATE_SLEEP_SEC = 0.05s` per request. `enrich_release_intelligence` (14729, GitHub) adds even more.
- Observed ~213 s ≈ matches ~75 pkg × ~8–13 req × (0.05 + RTT ~0.2–0.5 s). **5–15 min with
  release-intel enabled.**

Safely compressible: results aggregate into `DependencyRow`s sorted downstream, so completion order
doesn't affect correctness. Parallelize per-package (and per-project) on the shared Session with a
small lock around the in-memory caches (or per-worker caches merged deterministically), plus a
persistent HTTP cache with **conditional requests (ETag/If-None-Match) + TTL** so repeat runs are
near-instant and never serve stale cross-version data. Deterministic regardless of completion order.
**Estimate: scan 213 s → ~25–45 s cold (8–16 workers), ~5–15 s warm.**

### P2-2. Control comparison re-materializes the lifecycle per command, serially  ← dominant verify cost
**File:** roadmap:8490–8537 (`adaptive_structural_evidence`), esp. 8518–8539 and 8575–8596.
For **each** project check that produced a candidate structural signature, `verify_assignment(spec.path, {},
commands=(command,))` runs a **fresh clone + fresh lifecycle install** serially, gated only by the
in-memory `adaptive_control_cache`. A baseline that is already red for N commands → N separate
full clone+install+run. This is exactly the observed "one control command ≈ 4 min".

**Proof-safe fix (user's own proposal):** one fresh control workspace + **one** lifecycle install,
then either (a) run all control commands in that one workspace and read per-command structural
signatures from `project_failures` (structural_project_failure_signatures_at 227–252 are per-command
records, so the comparison is preserved), or (b) **immutable lifecycle-ready control snapshot →
cheap per-command clone** to keep cross-command isolation. The empty-assignment control is source+env
fixed for the whole run, so a snapshot is valid across all modes/commands.
**Estimate:** N×4 min → ~4 min + (N-1)×cheap clone. For a 3-command red baseline that's ~8 min/project saved.
**Must keep:** a failing control never caches a PASS; UNKNOWN control still raises (8516–8521).

### P2-3. Repeated full materialization of the same repo inside one Baseline
**File:** baseline_constraint_verifier.py:502–520 (`_materialize_workspace`) = `git clone --shared` of
the same `git_root` for **every** `verify_assignment` call. A conflict-heavy run triggers 1 candidate
verify + 1 exact confirmation + up to `max_delta_checks` localization subsets (parallel) + 2
reproductions + ≤12 minimization trials → roughly as many clones of the identical repo.

**Proof-safe fix:** after the first materialization+resolver-install under a `resolver_input_key`,
persist a content-addressed immutable snapshot (reflink tree on Linux; hardlink-copy on Windows).
Each proof = fresh COW clone of the snapshot, then apply the **same** assignment delta + **same**
lifecycle — authoritative proof unchanged (still a fresh clone + fresh install). Invalidation = the
resolver key itself. Contamination impossible with private clones. This is the "Persistent immutable
verified resolver snapshots" idea and is safe; it also makes the combined resolver→lifecycle path
skip re-cloning between phases (already same `verify_assignment`, so this only helps the many separate
calls).
**Estimate:** 2–4× on verification-heavy projects.

### P2-4. Project checks sequence runs everything before finding the first regression
**File:** roadmap:9116+ (in `verify_assignment`) and the caller ordering.
`discover_baseline_project_checks` (baseline_constraint_verifier.py:260–302) already orders
`lint:types/typecheck → … → build → test`. But `verify_assignment` runs **all** configured commands
before returning, and early-stop is not implemented. Because only *structural* signatures become
authority and exact confirmation re-runs the responsible commands, an **initial cheap-ordered
parallel screening → collect candidate signatures → fresh authoritative confirmation of only the
responsible commands** is proof-safe and matches the adaptive model (narrowing already exists:
`_targeted_adaptive_confirmation_commands`, roadmap:7105–7129). Only the *confirmation* phase must be
authoritative; screening can stop at the first evidence-bearing regression. Cannot skip the second
independent regression family — so keep screening all cheap checks, then confirm each responsible one.
**Estimate:** trims redundant full-select builds when the first regression is early.

### P2-5. Resolver-only no-op fixed-input proof per mode
**File:** roadmap:8595–8636. When no managed targets change but fixed inputs exist, each mode runs a
`run_project_checks=False` verify. The cache-aware wrapper makes modes 2–3 cheap (resolver PASS hit),
so cost is ~1 install. Minor; leave.

### P2-6. No subsumption/dominated-nogood pruning in the solver
**File:** `_build_peer_optimization_model`, roadmap:5722–5738 (dedupe exact duplicates only). Since the
solver is not the bottleneck (5–10 s), pruning is low-priority. Do **not** make the solver less exact.

---

## P3 — maintainability / observability

### P3-1. Run Monitor phase parsing: 4 phases are prose-regex-only
**File:** `desktop/src/data/processMonitor.ts` — structured `DEPLOOM_PROGRESS_V2` JSONL already drives
solver/verification/localization/minimization/re-solve/reproduce (lines ~274–395). **Only these are
still fragile prose regexes and break if log text changes:** dependency scan (`\[dependency (\d+)\/(\d+)\]` ~411),
planning (`target planning started` ~422), retrying (`transient retry` ~403), and project-check
start/PASS/RED (~504/514). Also `desktop/src/data/logPresentation.ts:151–156` hides transitions by
prose regex. The guard scripts (`check-run-monitor.mjs`) make these load-bearing.
**Recommendation:** emit structured events for `dependency-scan`, `planning`, `project-check` phases
and move all detail fields into the JSONL; keep prose only for humans (already planned for the rest).

### P3-2. `observed_versions = []` trap (see P1-2) — rename to prevent future misuse.

### P3-3. Sanitization gate is strong
`scripts/check-public-sanitization.py` covers secret patterns, private IPs, ticket IDs, credential
files, an opaque-hash denylist, binary SHA allowlist, and non-public lockfile hosts. It gates, it
does not delete tests — satisfies the "synthetic replacement" requirement. No private paths were found
in the audit of tests/scripts/desktop source (no `C:\Users`, home paths, internal registry hosts).

### P3-4. Failure handling is sound
Timeouts/kills/partial output/crashes never become authority: `_run` owns the process tree with a hard
timeout (416–465), infra/unknown raise at every consumer, ddmin parallel FAIL is only a screen until
serial confirmation (constraint_verify.py:498–580), and `persist_verified_nogood` failures are
warnings only (constraint_cache.py:504–509). Project proof is published only after a fresh lifecycle
install re-observes the same direct tree (baseline_constraint_verifier.py:1099–1126).

---

## Cache map (section 10)

| Cache | Key | Lifetime | Persistence | Invalidation | Authority risk |
|---|---|---|---|---|---|
| Proof store (resolver/prep/project PASS) | `resolver_input_key` / `preparation_key` / `project_proof_key` (env+source+assign+cmd) | on-disk | yes | identity change | low (keyed) — gap = fixed inputs outside identity (P1-1) |
| Persistent nogood cache | projectPath+envFP+literals; schema v3 | on-disk | yes | env/manifest/lock/fixed-input fp | low (over-scoped, conservative) |
| `resolver_cache`/`project_preflight_cache` | path+assign+removals(+cmds) | in-process generation | no | process | low (env fixed in-process) |
| `adaptive_control_cache` | path+command | in-process generation | no | none in-process | low (control is baseline state) |
| HTTP caches (meta/OSV/tarball) | in-memory pkg/ver/url | in-process | **no** | — | none (deterministic input) |
| Localization checkpoint | identity = sourceHead+env+assign+cmd+units | on-disk | yes | identity | diagnostic only until verified |

No under/over-defined keys found that break determinism; the only material risk is P1-1.

---

## Performance roadmap (ordered, Pareto)

### Optimization 1 — Parallelize dependency discovery + durable conditional HTTP cache
- **What:** thread-pool package (and project) discovery on the shared `requests.Session`; on-disk
  metadata/OSV/artifact cache via ETag/If-None-Match + TTL; safe locking around shared in-memory caches.
- **Proof:** no authority in discovery; outputs aggregate+sort deterministically. Cache serves fresh
  version sets via conditional requests.
- **Saves:** scan 3–15 min → ~0.5–1 min cold, ~seconds warm (dominant wall-clock win).
- **Tests:** synthetic registry reorder → identical rows; warm vs cold cache identical; OSV batch
  partial outage → deterministic handling.

### Optimization 2 — Batch control commands behind ONE lifecycle install
- **What:** empty-assignment control with `commands=(c1..cN)` in one workspace (or immutable snapshot
  + per-command clone) instead of N serial full installs.
- **Proof:** per-command structural signatures preserved; a failing control never caches PASS;
  UNKNOWN still raises.
- **Saves:** N×4 min → ~4 min per red-baseline project.
- **Tests:** two commands, one red for each → both signatures recovered; cross-command contamination
  adversarial test; flaky command → UNKNOWN not authoritative.

### Optimization 3 — Content-addressed immutable workspace snapshots (reflink/hardlink)
- **What:** after first resolver install for a key, store immutable snapshot; each proof = fresh COW
  clone + same assignment delta + same lifecycle; invalidate by resolver key.
- **Proof:** authoritative proof still = fresh clone + fresh real install; snapshot is read-only.
- **Saves:** 2–4× on verification-heavy/conflict-heavy runs.
- **Tests:** snapshot corruption → fail closed; Windows hardlink clone correctness; contamination test.

### Optimization 4 — Cheap-first parallel project screening then authoritative confirmation
- **What:** screen in cheap-first order (already the discovery order), stop at first evidence family,
  then fresh-confirm only responsible commands (`_targeted_adaptive_confirmation_commands`).
- **Proof:** confirmation remains authoritative; never skips a second independent regression family
  (kept in screening).
- **Saves:** redundant full-select builds when first regression is early.

### Optimization 5 — Fix fixed-input identity (P1-1) to enable safe tight-proof caching at scale
- **What:** capture the real resolved identity for `workspace:`/`link:`/`git+` fixed targets.
- **Proof:** makes reuse *stricter*, unblocks aggressive proof reuse without risk.
- **Tests:** moving sibling git commit / moving remote ref → cache miss; unchanged → hit.

### Acceptance criteria / gate for each
All must pass: fail-closed on infra/unknown; identical verified final assignment for identical
source+registry+env regardless of completion order; every accepted assertion backed by a fresh
package-manager proof; the `check-*.mjs` guards and public-sanitization gate green.

---

## Answer to the main research question

> With correctness/proof model unchanged — what minimum wall-clock is architecturally achievable,
> which 3–5 changes give the bulk of the speedup, and what residual bugs could currently make the
> proof unsound?

- **Minimum achievable:** steady-state ~8–20 min on a complex 75-package project, bounded below by
  ≥1 fresh real-PM install per *distinct* verified assignment + ≥1 fresh confirmation per localized
  culprit. Can’t reach single-digit minutes on pathological many-conflict runs because each certified
  constraint needs its own fresh serial install(s).
- **Bulk of the speedup:** Optimization 1 (scan), 2 (control), 3 (snapshots), 4 (screening);
  the first three give ~the majority.
- **Residual proof-unsoundness risks:** only P1-1 (fixed-input identity outside the hashed project
  → over-strong proof reuse). Everything else fails closed or is over-conservative. No bug found that
  accepts a wrong assignment as proven.
