<!-- BLOCK_OMEGA_VERIFICATION_SUBSTRATE_V2 -->

# Block Ω — Verification Substrate 2.0

Ω changes the cost model of Windows project verification.

## Before Ω

For a guarded NTFS trial DepLoom sealed every dependency payload byte:

```
prepared node_modules
  -> enumerate ~100k–1M files
  -> SHA-256 every regular file
  -> create source/config clone
  -> mount package payload junctions
  -> watch dependency tree
  -> reject the fast path on any meaningful notification
```

The byte manifest was expensive but was not sufficient to detect a mutation
that produced no watcher event. Conversely, the verifier already rejected a
trial even when a watcher event ended with the original byte hash. Therefore
the O(files) pre-hash did not strengthen the acceptance condition for a quiet
trial.

## Ω proof rule

```
exclusive snapshot lease
        +
sealed PreparedArtifact
        +
private source/config/.git upper
        +
shared dependency lower
        +
ReadDirectoryChangesW subtree guard
        =
guarded trial

no meaningful dependency event
        -> trial may be authoritative

any dependency event
watcher error
buffer overflow
lease uncertainty
        -> fast path is infrastructure-only
        -> quarantine / retry private
        -> never learn a dependency nogood
```

The watcher starts before the project command. The exclusive kernel lease
prevents another DepLoom consumer from using or copying the same shared lower
while the guarded command is active.

Allowed ephemeral cache paths retain the pre-Ω policy and are normalized before
the lease is released.

## Source Truth improvement

The old fast path used `git clone --shared` and therefore left Git alternates
pointing to the live source checkout. Ω removes that path. The private upper is
copied from the sealed PreparedArtifact. Git provenance is never borrowed from
a mutable live checkout. On Windows, the ordinary root `.git/objects` database
can stay in that sealed artifact under the same exclusive lease and a strict
subtree watcher. The upper has a junction to those exact objects; index, refs,
config and the remaining Git metadata are copied privately. Git indirections,
alternates and reparse layouts outside this supported case retain private copy.

The private source-closure seal combines freshly hashed upper entries with the
already sealed object entries only after checking the live mapping and lower
continuity. The final guard checks object-directory identity and confirmed
watcher delivery. Any object write, watcher failure or mapping change rejects
the optimization and uses the existing full-copy verification retry; it cannot
become dependency incompatibility evidence. This supports read operations such
as `git describe` without copying or hashing the history for each check.
Commands that write new Git objects may require the private-copy fallback.

This optimization applies to temporary project checks, not to mutable installs
or to arbitrary agent checkouts. Initial capture and cold artifact validation
still seal the complete source and Git bytes. It does not claim one Git copy
for an entire migration or eliminate package-manager work.

## Durable artifacts

PreparedArtifact records now persist a small manifest of relative
`node_modules` roots. It is performance metadata only. Missing/old records
simply fall back to private materialization.

A durable PreparedArtifact may use the guarded lower path because cross-process
serialization is already provided by the snapshot lease.

On Windows, publication arms a directory watcher before the complete integrity
seal and transfers that live continuity to artifact reuse. The first load can
therefore reuse the publication seal instead of hashing the entire dependency
tree a second time. It still checks directory identity, notification delivery,
record identity and dependency topology. A mutation, unavailable watcher or
process restart retains the full cold validation. No durable receipt alone
grants permission to skip hashing.

## Durable source promotion

A durable capture prefers temporary storage on its destination volume, outside
the live source root. The call-owned sealed container is then published by
rename rather than a second whole-tree copy. Content is revalidated at the
final destination; existing destinations are never overwritten. Shared active
snapshots keep independent-copy semantics. Cross-volume promotion uses the
original private-copy path. This removes a full source/Git copy at checkpoint
publication without changing source identity or weakening verification.

## Complexity

Old Windows guarded preparation:

```
O(number of dependency files + dependency bytes)
```

Ω guarded preparation:

```
O(directory topology outside dependency payloads)
```

No dependency file contents are read merely to decide whether the guarded
lower optimization is available.

Each project command still gets a fresh private source/config upper. Commands
that touch dependency payloads automatically lose the optimization and retry
through the proof-safe private-copy backend.

## Platform matrix

Windows:
- guarded immutable lower v2 is preferred when dependency roots exist;
- private Robocopy remains the fallback;
- ReFS same-volume copy remains eligible for native block-clone behavior on
  supported Windows versions.

Linux:
- existing `cp --reflink=always` private CoW path remains authoritative.

macOS:
- existing `cp -cR` clonefile path remains authoritative.

Portable fallback:
- private deep copy.

No storage/materialization choice participates in ResolverProof,
PreparationProof, ProjectProof, solver constraints, or learned nogoods.
