# Migration performance boundaries

Performance state never establishes resolver, preparation or project proof. All
selected checks and installed-state verification remain required.

## Shared I/O admission

Copy passes, complete source/artifact hash passes and verifier/materialization
installs use shared per-user machine admission. Defaults are two copies, two tree
hash passes and one package-manager install. Per-file hashing retains its local
worker bound; it does not acquire a kernel lock per file. Windows uses named
mutex slots, including abandoned-owner recovery; POSIX uses crash-released file
locks. Nested admission of the same kind on one thread reuses its slot. The
DEPLOOM_IO_COPY_SLOTS, DEPLOOM_IO_HASH_SLOTS and DEPLOOM_IO_PM_SLOTS overrides
should be consistent for all processes sharing the machine. Queue time is
observable and does not change proof identity. Copy/hash waits count against
their operation budget. Installs retain their active subprocess limit after
waiting, while the remaining overall run/attempt deadline still caps execution. Unsupported or unavailable admission fails as infrastructure.

## Trial retirement

A retired private trial is re-enumerated after transient Windows sharing or
nonempty-directory failures, for at most six attempts with a short bounded
backoff. Root/parent ownership and reparse boundaries are checked again before
each attempt. Read-only regular files may be made writable only when private;
hardlinked source/snapshot files are never unprotected. Persistent locks or a
continuing writer retain the specific cleanup error and the verified checkpoint.
Nx daemon is disabled in DepLoom child environments and verifier identity, using
[the documented Nx setting](https://nx.dev/docs/reference/nx-daemon); this does
not stop any existing user development daemon or edit nx.json.

## Source cache policy

Repositories owning nx.json exclude only the canonical regenerable paths
.nx/cache and .nx/workspace-data from newly captured source snapshots. nx.json,
project.json, other .nx files and shared source remain authoritative. The policy
is serialized into snapshot identity and preserved by reopening. Old snapshots
retain their original policy. Generated caches may be recreated by an install
or check; they are never a substitute for missing source. Dependency payload
remains separate from source.

## Private verification trials

Every check observes the original validated prepared inputs. A quiescent,
completely observed private trial may be reused after removing new output paths
and restoring changed regular source files from the exact prepared source seal.
Only touched files are hashed. A clean guarded dependency lower additionally
permits cleanup of private .vite, .vite-temp, .vitest and .cache directories.
Package changes, link aliases, directory replacement, watcher loss/overflow or
an oversized/unavailable delta require fresh materialization. Existing file
permissions are preserved when restoring a private file. This reset is equivalent
to the previous fresh-input behavior; it does not turn source mutations into
verification evidence.

## Prepared state and restart

Intermediate candidates, diagnostics and failure confirmation keep preparation
private even when stage recovery is enabled. They do not pay a durable whole
dependency-tree seal merely to make a rejected candidate restartable. A crash
may replay installation/checks for that candidate. The previous verified source
checkpoint and selected validation profile remain intact. Baseline control,
incumbent promotion and legacy callers retain their durable recovery boundaries.
Command receipts require a physically published durable prepared artifact;
private preparation cannot grant a cross-process command receipt.

## Yarn Berry exact continuation

On Windows the exact same-process resolver seed bridge supports pinned Yarn 3/4 with the
node-modules linker, in addition to the existing Classic bridge. Source,
assignment, runtime/configuration and resolved lock identity must match. The
cache is watcher-backed and always cloned privately. Its private canonical
.yarn/install-state.gz is reset before the ordinary scripts-enabled immutable
install, and the installed assignment is verified again. Unknown state layouts,
missing markers, changed bytes, uncertain continuity and unsupported runtimes
fall back to fresh installation. Parent directory metadata notifications require
the same filesystem object identity; package events still reject reuse.

This exact bridge does not authorize skipping install for a changed assignment,
sharing a mutable node_modules between projects, or focused workspace installs.
Different candidates and a process restart still require authoritative install;
cross-process neighboring tree reuse needs a separate continuity owner. Existing
shared package archives continue to reduce download work.

Yarn documents the distinction between skip-build and immutable installation in
[the install reference](https://yarnpkg.com/cli/install).

## Residual repair and result actions

The residual-security prompt requests bounded cohorts of at most three install
or lock regeneration cycles, short targeted audit/dependency queries and a
handoff with exact blockers when a hypothesis repeats. It preserves existing
lock/installed state unless a diagnosed failure needs reconstruction. Native
command exit codes must be captured before PowerShell output pipelines. Native
audit output remains diagnostic; the independent canonical audit guards and
controller verification retain authority. These prompt instructions complement
the existing inactivity/session limits; they do not guarantee agent compliance.

A terminal saved checkpoint exposes commit preparation/retry beside the main
result action, including failures before delivery state is created. An existing
delivery offers continuation; completed delivery hides that preparation action.
The same backend verifies bytes, audit and original-worktree safety on retry.

## Measuring changes

Compare cold start, an exact repeated request, a neighboring candidate and crash
recovery separately. Repeat with one and three projects. Report total wall time,
queue time, copied/hashed files and bytes, backend, tool build and cache decisions.
Source pin/capture time is reported before the first proof lookup; summaries carry
the tool build ID. Nested stage work must not be added as if it were wall time.
A fixture speedup does not establish the same speedup for a real migration.
