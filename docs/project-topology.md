<!-- BLOCK_Z_PROJECT_TOPOLOGY_V1 -->

# Block Z — Project Topology & Package Manager Profiles

Block Z removes the assumption that one path is simultaneously the Git root,
package root, package-manager root and lockfile root.

## Authoritative topology

For every project DepLoom resolves these identities independently:

```
SourceRoot
  GitRoot (optional)
  PackageManagerRoot / WorkspaceRoot
    CanonicalLockfile
    package.json
    workspace member package.json files
    ...
    PackageRoot / ManifestRoot
      package.json
      ProjectCheckRoot
```

A nested package is therefore allowed to have:

```
PackageRoot != PackageManagerRoot != SourceRoot
```

Package-manager resolution/lifecycle commands run from `PackageManagerRoot`.
Project compatibility commands run from the selected `PackageRoot`.

The complete topology identity, including all workspace member manifests,
canonical lockfile bytes and package-manager control files, is included in the
resolver context. Changing a sibling workspace manifest therefore invalidates
resolver/proof reuse even when the selected package itself did not change.

## Capability matrix

| Package manager | Root package | Workspace package | Authoritative Baseline |
| --- | --- | --- | --- |
| npm | supported | supported | yes |
| Yarn Classic 1.x | supported | supported | yes |
| Yarn Berry 2/3/4, PnP | detected | detected | **fail closed** |
| Yarn Berry, node-modules linker | detected | detected | **fail closed** |
| pnpm | detected | detected | **fail closed** |

Berry is not treated as Yarn Classic merely because the executable is named
`yarn`. `--frozen-lockfile` is a Yarn 1 contract and is never used as proof for
Berry.

pnpm is intentionally disabled for authoritative Baseline until importer-level
lockfile identity, workspace resolution, fixed-source closure and store
semantics are represented in the proof model. Partial YAML parsing is not
promoted to authority.

## Lockfile ownership

An ancestor lockfile belongs to the selected package only when that ancestor's
workspace declaration owns the selected package. An unrelated ancestor
`package-lock.json`/`yarn.lock` is never guessed as canonical.

Mixed package-manager lockfiles at the owning root are an explicit error.

For npm workspaces, package-lock validation reads the target workspace record
(`packages["packages/app"]`) and its effective dependency installation paths,
rather than pretending the lockfile root record describes the target package.

## Source Truth interaction

SourceSnapshot still captures the full Git/source tree. Block Z additionally
preflights local `file:`, `link:` and `portal:` dependencies from every
topology-relevant workspace manifest. A sibling package cannot silently escape
the captured source root.

Current explicit fail-closed edges remain:

- linked Git worktrees whose `.git` metadata points outside the captured tree;
- uninitialized/conflicted submodules;
- external local dependencies outside SourceRoot;
- hardlink/reparse/symlink escapes not representable by the current snapshot.

These are infrastructure/unsupported results, never dependency incompatibility
and never learned nogoods.

## Desktop vs Python authority

Desktop nested-package discovery remains a UX convenience. The Python
`project_topology.py` model is recomputed before lockfile/proof/verifier
authority and is the canonical decision layer.

Inspect a project manually:

```bash
python project_topology.py path/to/package --json --require-supported
```

## Proof rule

Block Z does not require a global proof-schema version bump. The existing proof
schema remains `baseline-proof-v6-source-snapshot`, while the resolver-context
payload now contains `projectTopology`. This changes authority keys whenever
topology changes, causing older cache entries to miss naturally without
pretending the old topology and new topology are equivalent.
