# Verification source closure and reuse

Selected project, execution scope, source closure and proof authority are distinct.
The selected package chooses automatic scripts and cwd. The real commands keep their
semantics, including task dependencies. Workspace membership alone cannot prove the
complete filesystem inputs of opaque Node.js commands.

## Source boundary

Until an optimized closure can be proven, capture the authoritative repository root,
or the standalone package root when no repository exists. Preserve relative layout,
sibling packages, root configuration, local tooling and ignored/generated inputs.
Dependency payload under `node_modules` remains separate resolved-state authority.
The existing explicit source policy excludes tool state and IDE/desktop metadata;
completeness is stated as `complete-under-source-policy`, not unrestricted access to
arbitrary external files. External junctions and unrelocated absolute local package
paths fail closed. Existing package-manager support policy still applies: npm,
Yarn Classic and pinned Yarn 2/3/4 with node-modules; pnpm and PnP remain unsupported.

`project-gitignore` remains a legacy CLI spelling, but no longer authorizes removing
unproven source inputs. Old partial captures cannot certify project verification;
recapture is required. New default CLI scope is `repository`.

For an explicitly frontend-only workload, opt into `--source-scope frontend`, or
set `"sourceScope": "frontend"` on its project entry in Desktop workspace settings.
This profile excludes `bin` and `obj` only when their direct owner contains a
`.csproj`, `.fsproj` or `.vbproj` and has no Node `package.json`. It preserves the
backend source/project files, all Node bins, sibling resources and root task tooling.
It is an explicit input policy, not a claim that arbitrary custom commands cannot
read backend outputs. Workloads needing prebuilt backend binaries must use
`repository`. A declared local package dependency targeting an excluded path returns
`SOURCE_REQUIRED_INPUT_EXCLUDED` before copy. Exclusions are persisted, shown in
scope diagnostics and bound into source/proof identity; unknown policy paths fail closed.
The profile also excludes agent state in .claude / .codex and root .nx state
when nx.json identifies Nx. Future backend bin / obj paths are covered even
before the first build creates them. Git, .env, .npmrc, .yarnrc.yml,
.storybook and other configuration remain captured. A failed command referring
to a demonstrably existing original file in an excluded path is infrastructure
inconclusive with a recommendation to use repository scope; absent original imports
remain project failures. Capture races report bounded changed paths without contents.
Git URL/committed-only acquisition and filtered Git-object copying are outside this change.

Capture preflight sums bytes during the existing reparse inventory before copying
file data. The default source-copy budget is 8 GiB. Exceeding it returns infrastructure
inconclusive, never a project failure or PASS. `DEPLOOM_SOURCE_COPY_MAX_BYTES` specifies
a nonnegative byte limit; `0` explicitly removes the limit. Raising it is appropriate
only with adequate storage and an accepted copy cost. It does not prove a smaller
closure. Selecting a package excluded by the source policy is rejected before copy.

The execution diagnostic includes selected project, actual cwd, source root,
package-manager root, commands, materialization strategy, completeness level and
snapshot key. Build/test output remains project evidence only in a complete verified
environment. Missing imports already absent in the original source remain real
project failures; missing or modified bytes in a clone return inconclusive even when
a command would exit zero.

## Proof and isolation

Resolver identity includes the source closure policy. PreparedArtifact's existing
strong seal additionally derives a source-only content receipt in the same pass.
Every fresh project clone validates that receipt before executing commands, without
rehashing dependency payload. This intentionally adds a source-byte validation pass
per fresh clone: copy success alone cannot establish completeness. An unchanged
reused private trial does not perform another materialization pass.

Local fixed package identity is projected from the validated sealed source manifest.
Captured relative paths replace temporary absolute locations in its key. `dist`,
`build` and `.cache` are retained when they are local package authority. Before/after
source validation catches concurrent tampering. Identity diagnostics contain safe
component labels, hashes and bounded changed-path lists, not source contents or URL
credentials.

Completed source subjects may be reused within one verifier process after BOTH
sealed-source validation and live-source continuity validation. Idle reuse holds at
most two snapshots. Context leases remove a subject from the idle pool while used;
concurrent requests acquire separate copies. A changed live source, changed sealed
source, interrupted request or uncertain watcher retires the candidate. On platforms
without a proven notification stream, live continuity uses a full content comparison.
Watcher admission/retention stays inside the source binding so idle reuse does not
force another full hash solely because its context changed. Separate CLI processes
still capture source independently; no durable source locator
has been promoted to authority.

Git worktree is a useful checkout optimization, but is not an equivalent working-tree
snapshot: physical measurements confirmed that dirty bytes, ignored inputs and
installed dependencies were absent. Sharing dependency payload or mutable source via
ordinary hardlinks cannot establish write isolation. The supported speedups reuse
exact ResolverProof/PreparationProof and immutable prepared artifacts, with guarded
Windows dependency lowers or existing private-copy fallback.

## Confirmed defects and fixes

* CI and Release validation at the base release failed two README contract tests.
  A prior lint optimization commit had replaced the product README with a short patch
  note. Restore the original product documentation; retain the lint implementation.
* Narrow workspace/Git-visible filtering dropped sibling resources and root tools.
  A live trial reproduction detected missing files before cleanup. Conservative
  source policy and clone receipt validation prevent that false project failure.
* Artifact seal and cold replay assigned different contextual authority labels to the
  same workspace junction. Canonical physical identity now stays equal. Declared
  workspace target mapping, internal bounds and changed-target rejection remain checked.
* Cache normalization used selected-package cwd with workspace-relative dependency
  roots. Normalize the canonical workspace roots and clean allowed dependency caches
  before sealing. Cache writes no longer demand another lifecycle install.
* A shared-lower watcher event is not a confirmed byte comparison. Such events still
  reject the fast path, with action/path evidence and measured fallback duration.
  Per-process command quarantine already forces private clones after an observed write.
* Identical lint bodies no longer discard distinct `prelint`/`postlint` hooks. Exact
  composite discovery preserves shell order and additional stages.
* A real probe also reproduced native exit `0xc0000005` after a successful round trip.
  Successful shutdown now leaves the stdout I/O callback and uses graceful `app.quit`;
  eight consecutive Windows probes passed with no retry after this change. Native
  failure is still a failing gate, even when stdout reports matching bytes. The gate
  checks exit code, signal, launch errors and the exact protocol. Only a typed
  app-readiness timeout
  permits a bounded retry; clipboard mismatch, malformed output and launch failures fail.

## Measurements and limits

Pinned physical fixture: Nx 21.2.1, Sass 1.89.2 and Vitest 3.2.4, npm workspaces.
The build executes a dependent sibling task with a shared SCSS import; the test loads
a root-level custom reporter. Both commands execute at the selected package cwd.
On the same source subject, cold verification took 64.017 s and warm verification
16.493 s, with install-stage counts 2 and 0. This is one measured cold/warm pair, not
a universal speed guarantee. Before the artifact canonicalization repair, repeated
verification of the same fixture template unnecessarily replayed lifecycle installation.

A scale fixture materialized 33,949 covered files / 3,867,993,515 bytes on NTFS.
Cold verification took 374.340 s; warm verification took 148.973 s with no recapture
or install. Source capture took 207.119 s, including a 112.666 s sealed hash and
32.102 s live-final comparison. Private trial creation took 17.695 / 21.332 s;
the real command took 0.722 / 0.712 s. Parent-process CPU was 402,312 / 168,516 ms;
observed parent peak RSS was 191.5 / 231.9 MiB, excluding child-process memory.
Cold hashing overlapped a small targeted regression run; these are physical timings,
not an isolated hardware benchmark. Warm admission no longer performs the extra
full-manifest passes reproduced before the binding fix. Source copying and fresh
clone validation still cost time; this is not zero-copy execution. The later opt-in
MSBuild profile does not exclude anything in this standalone scale fixture.
Windows native memory measurement now uses correctly sized HANDLE signatures;
previous zero values were unavailable telemetry, not zero memory use.

An earlier 512 MiB/8,192-file checkout comparison measured worktree creation at
16.265 s on NTFS and 4.904 s on ReFS. Those volumes are on different physical disks;
these measurements do not isolate filesystem causality or establish project proof.

A real large repository inventory found approximately 62.1 GB of policy-covered
inputs, dominated by generated binary outputs. Its limited recheck returned typed
inconclusive before copying source data or launching install. That real project is
NOT certified PASS by the smaller fixtures. Generic task commands can consume ignored
outputs, so blindly excluding a directory by name or Git visibility would be unsound.
Automatic provably minimal source closures and zero-copy source virtualization remain
open work; the bounded fallback prevents an unexpected giant copy meanwhile.
After user cleanup, a fresh metadata-only preflight fit within the 8 GiB limit;
no existing MSBuild output directories remained to measure this rule against that
repository. A later real check result must be reported separately from preflight.
Final frontend metadata inventory covered 86,786 files / 4,279,157,942 bytes;
classification took 5.015 s and total inventory 17.229 s. It registered 1,868
current-or-future backend output paths. Agent/Nx state exclusions reduced the
previous inventory of about 209,000 files; Git still accounts for about 3.8 GB.
This is a source inventory measurement, not a physical project PASS.
A live real-project snapshot was inspected before cleanup: both sibling SCSS
inputs and the root reporter were present. Final comparison detected 67 changed
inputs, including Git index/reflogs and newly added Git objects. The expensive
retry was stopped because the authoritative live repository was still changing.
The real-project outcome remains inconclusive; no build/test PASS is claimed.

Yarn phase telemetry reports resolve/fetch/link/build observations, package-manager
reported durations, explicit cache messages and typed network errors/hosts. It is
`PERFORMANCE_ONLY`; lack of cache messages is unknown, not zero cache hits. Link may
include lifecycle builds, so Berry build messages are not an independently measured
build phase. Corporate registry latency has not been attributed without a measured
network run. Filesystem/resource timing remains in existing observability events.

Existing baseline lint errors are source failures at zero updates, not candidate
incompatibilities. This change neither repairs external projects nor suppresses their
ESLint diagnostics. Physical failed-check regression retains the original missing
path and project outcome. Real external project source failures and generated-input
requirements still require project-level attention.

Tests cover source omission with zero exit, same-size clone corruption, original
missing inputs, sibling resources/root reporter, task graph changes, excluded project,
external junctions, sealed/live mutation, concurrent leases, bounded cleanup, local
package payload changes, lint hooks, metadata canonicalization and clipboard readiness.
The CI Yarn bootstrap enables the actual Nx/Sass/Vitest acceptance fixture. Release
status and exact SHA verification must be reported separately from these local results.
