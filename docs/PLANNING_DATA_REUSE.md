# Planning data reuse and public migration research

Planning caches never establish install, resolver, preparation, audit or project
proof. Existing installed-state observation, configured checks, independent audit
and exact proof identities remain authoritative.

## Shared query cache

The per-user default is `~/.cache/deploom/planning-v1`. Set
`DEPLOOM_PLANNING_CACHE_DIR` to a private directory to relocate it, or `0` to
disable this additional cache. Existing configured workspace packument caches
retain their own TTL and enable/disable semantics. Removing the planning cache
is safe while DepLoom is stopped; it contains no verified checkpoint state.

Anonymous HTTP packument clients with durable caching enabled share successful
responses across workspace cache directories, keyed by registry, request context,
package and tool identity. Authenticated, hooked or otherwise unkeyable transport
continues to bypass durable HTTP reuse. Stale workspace entries may supply
ETag/Last-Modified validators; only a successful conditional response renews them.
Failures, 404s and stale data are never returned as fresh shared metadata.

Iterative npm/Yarn discovery also caches successful normalized CLI query results
for ten minutes, conservatively scoped to the original project, environment and
runtime. Custom npm/Yarn configuration, authentication/proxy environment and
unrecognized configuration bypass this additional cache; the package manager
continues to own credentials and its native cache. This is not cross-project
sharing of private metadata.

Per-key machine mutexes coalesce producers. Windows abandoned mutex ownership
and POSIX file locks release on process death; budget checks continue during
bounded waiting. Failed admission/persistence degrades to live work. Entries are
published atomically and reads are bounded to 16 MiB. Metrics include
`planning.cache.hit`, `planning.cache.miss`, `planning.cache.lease` and
`planning.metadata.revalidated`. No raw URLs, credentials or package graphs are
included in these events. A cache hit counts saved work, never a successful check.

## Small Yarn lockfile probe

Pinned Yarn 3/4 registry/workspace projects with explicit `enableGlobalCache: true` may first run
`yarn install --mode=update-lockfile` in a private directory containing only the
selected/workspace manifests, canonical lockfile, supported Yarn configuration
and bundled Yarn executable. The directory contains no Git history, project
source/assets or node_modules. It is retired after the probe. The native mode
skips linking: see [Yarn install](https://yarnpkg.com/cli/install).

Only exact unavailable-target evidence for a changed selected target can stop
materialization here. Generic failures, authorization/network errors and missing
inputs cannot become dependency incompatibilities. Unsupported/path-dependent
inputs (including local files, patches, plugins and environment files) use the
normal complete workspace. A successful probe still requires ordinary full
materialization, observation and verification; no lockfile/proof is adopted from
the small directory. Successful candidates incur an extra resolution pass;
`planning.lockfile.probe` records its time and copied input size. The optimization
saves heavy trial copies for rejected unavailable targets, not all installs.
Local-cache and implicit-cache configurations retain the normal path so an
exploratory fetch cannot be discarded and downloaded again during materialization.

## On-demand agent facts

`migration_research.py --package NAME --from-version OLD --to-version NEW
--repository OFFICIAL_GITHUB_URL` obtains bounded release excerpts for the exact
transition. It scans at most the latest thirty releases and up to three exact
target/major-boundary/monorepo tags, then returns at most three
excerpts of 2,500 characters. Package-specific monorepo tags are matched by package
name. Coverage is explicitly partial. Results are cached for one hour. Arbitrary
sites, private repository credentials and automatic codemod execution are not
part of this helper.

Optional `--include-osv` submits only the explicitly selected package/version to
[OSV QueryBatch](https://google.github.io/osv.dev/post-v1-querybatch/), with a
ten-minute cache. This is for confirmed public dependencies. It returns advisory
IDs/modification revisions, not complete advisory details; pagination and missing
evidence remain visible. It never replaces the configured independent audit or
claims a vulnerability-free project. Public network requests use bounded response
sizes/timeouts and no inherited credentials/proxies.

Candidate and security repair prompts reference the helper only for diagnosed
transitions, treat external notes as untrusted data, and require consulting a
short hypothesis/action/outcome history in the run migration report before
repeating an edit/install. This history is guidance, not a learned incompatibility
or a mechanism for skipping validation. Official codemods still require a pinned
version, permitted edit scope and verification.

## Verification and performance interpretation

Tests exercise three independent processes with separate workspace caches against
one HTTP server, conditional 304 reuse, producer death, failed-query handling,
context isolation, bounded research and incomplete OSV evidence. Physical Yarn
acceptance exercises the small probe without node_modules and the subsequent
real immutable lifecycle and nested project check. These establish boundaries,
not an end-to-end speed multiplier. Compare identical cold/warm/restart runs and
one versus three concurrent projects before reporting whole-product improvement.
