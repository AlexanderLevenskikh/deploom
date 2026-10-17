# Checkpoint recovery boundaries

Preparation and migration preserve completed expensive work across process
termination. Checkpoints are evidence references, never independent permission
to accept an unverified assignment. Interrupted work remains RUNNING/UNKNOWN.

## Preparation

| Boundary | Durable state and validation on reuse |
| --- | --- |
| SourceSnapshot sealed | Sealed bytes and source identity. Reuse checks current source, physical snapshot integrity and ordinary live continuity. Incomplete capture is repeated. |
| Resolver install PASS | ResolverProof plus physical ResolvedState, exact assignment and lockfile. Missing or invalid state requires fresh resolution. |
| Lifecycle install PASS | PreparationProof plus intact PreparedArtifact. A proof without the physical tree cannot skip lifecycle scripts. |
| PreparedSnapshot published | Immutable prepared tree and its content key, published only after authoritative guards pass. Recovery enables durable reuse for baseline controls, incumbent promotion and legacy callers; explicitly intermediate preparation remains private. |
| Selected checks PASS | Separate receipts for each selected build, test, lint and lint:* command. Receipts bind source, dependencies, prepared artifact, command order, tool closure and effective environment. |
| Registry discovery | Independent per-package results in preparation-discovery.json. Settled planning results live for 24 hours under matching inputs; errors and UNKNOWN are retained as diagnostics and retried. |

The verifier checks physical prepared artifacts before reusing any command.
Only supervised completion with source/dependency guards can publish PASS.
Windows Job Objects currently provide the required complete process-tree
ownership. Existing POSIX process groups/token scanning are best-effort; on
those platforms commands are repeated and durable resolver/lifecycle/command
PASS reuse remains disabled. Source and controller publication recovery still
apply. A successful check never upgrades incomplete process supervision.
Unknown external command closures execute again. The same rules cover lint
scripts, type checks and any supported command in the selected validation profile.
A command receipt does not change which checks the user selected.

Commands retain the existing verifier isolation semantics: each check starts
from the validated prepared tree. A receipt therefore references that immutable
input; later checks cannot accidentally consume a half-mutated previous check.
Nonregression baseline/observation sidecars are hash-checked and the initial
control is never reset after an accepted cohort.

Intermediate candidate/diagnostic preparation remains private even with stage
recovery enabled. If that process stops, installation and checks may replay;
no cross-process command receipt is issued without a durable prepared artifact.
The previous verified checkpoint is retained. Baseline control, incumbent and
legacy durable boundaries below are unchanged. See
[MIGRATION_PERFORMANCE.md](MIGRATION_PERFORMANCE.md).

## Migration

| Boundary | Saved progress |
| --- | --- |
| Assignment selected | Candidate assignment, cohort, constraints and active attempt. |
| Resolver and lifecycle PASS | Exact proofs and resolved state; durable roles retain physical preparation. Intermediate private preparation repeats after process loss. |
| Diagnostic materialization PASS | Complete mutable trial integrity, observed assignment and next candidate state. Resume verifies the entire trial and base source before skipping installation. |
| Diagnostic checks PASS | Complete contiguous command prefix and its exact post-command trial integrity. A partial or changed trial cannot reuse the prefix. |
| Authoritative checks PASS | Durable preparation permits individual receipts for build, tests, lint and lint:*. Private intermediate checks replay after process loss; incompatible inputs or missing evidence cause fresh execution. |
| Verification concluded | Saved result independently of controller publication. Reuse requires unchanged trial/runtime and physical proof/artifact/command receipts for PASS. Infrastructure and budget failures are not compatibility facts. |
| Verified incumbent | Durable sealed source, assignment and proof references. A write-ahead publication journal protects the active checkpoint switch. |
| Iteration complete | A write-ahead transaction publishes candidate, ledger, repair handoff and run state together after a negative conclusion. Attempts and spent budgets remain intact. |
| Cohort complete | Accepted checkpoint ancestry, delta and cohort/iteration metadata. Remaining work derives from the verified cumulative assignment and visible deferrals. |

Checkpoint publication does not add a physical copy at every stage. Records
reference already sealed source trees, resolved states and prepared artifacts.
Historical verified incumbents remain available when a later iteration fails.

## Restart and retention

Desktop routes pending publication to resume under the single-writer lock.
The journal is written before metadata changes and is removed only after the
complete publication. Resume validates ownership, integrity, configuration,
generation and physical evidence before updating pointers. Corrupt or changed
inputs fail closed and preserve the previous incumbent and pending evidence.

A completed incumbent publication can finish after the work deadline; this
never extends the deadline or authorizes further solver work. Durable proof and
artifact reuse depends on validity and availability rather than an arbitrary
one-hour expiry. Registry planning data alone uses a 24-hour freshness limit.
Upgrading DepLoom may require ordinary fresh checkpoint verification while
preserving the saved assignment and source evidence.

## Interrupted registry planning and security preparation

Registry diagnostics are not engine constraints. Engine probes use the canonical
package-manager registry and bind parsed JSON metadata to the requested version.
Explicit continuation archives legacy engine deferrals containing npm warning
text, preserves real incompatibilities, and replans from the sealed checkpoint
without resetting budgets or acceptance evidence.

The primary Continue the update action selects checkpoint recovery when it is
actionable, otherwise it resumes an interrupted security session when its run,
checkpoint, private workspace and saved session match. The controller resumes
the same session and attempt, then independently verifies its result. Existing
terminal outcomes do not hide that unfinished work. A previous agent session
remains an explicitly explained alternative when checkpoint recovery is selected;
neither action repeats begin or grants an old PASS
to changed source, runtime or configuration.

## Hoisted dependencies in materialized trials

Fresh materialization and resumed materialization receipts observe dependencies
from the selected package through its owning package-manager root inside the
private trial. Yarn hoisting does not make an installed package missing. Exact
versions, nearest-package shadowing and external-path rejection remain enforced.

## Validation

Cross-process tests kill a real physical verifier after completed lint,
lint:types and build, then resume tests without repeating these commands or
resolver/lifecycle installation. Changed environment invalidates receipts.
Real CLI tests inject failures during incumbent and negative-result metadata
publication, verify recovery exactly once and preserve the previous incumbent,
attempt counters and spent budgets. Corrupt journals and changed inputs are
rejected; Desktop contract checks cover pending-publication routing.
