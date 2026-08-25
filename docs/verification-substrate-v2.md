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
copied from the sealed PreparedArtifact, including the exact captured `.git`
database. Git provenance is no longer borrowed from a mutable live checkout.

## Durable artifacts

PreparedArtifact records now persist a small manifest of relative
`node_modules` roots. It is performance metadata only. Missing/old records
simply fall back to private materialization.

A durable PreparedArtifact may use the guarded lower path because cross-process
serialization is already provided by the snapshot lease.

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
