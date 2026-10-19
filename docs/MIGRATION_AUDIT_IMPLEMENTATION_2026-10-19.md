# Migration boundaries, CI and test cleanup — 2026-10-19

Historical implementation report. Investigated baseline: `v1.0.0-preview.14`,
`d08dc00f27f175d247f6271800257bbb5164354b`. This report does not certify a
migration of a user's project. All new reproductions use controlled fixtures.
Private run evidence is deliberately excluded from the public repository.

## Changes and boundaries

| Finding | Owning layer | Implementation and regression boundary |
| --- | --- | --- |
| B1: bytes change after verification | verifier / checkpoint acceptance | Successful verification returns the captured source key and assignment fingerprint. Acceptance compares them with the newly sealed trial. C0 repair checks the expected key before swapping containers. A real verifier followed by an injected mutation must preserve the incumbent. |
| B2: clean Git status conceals different working bytes | delivery | Compare the pinned commit's raw blobs with selected physical inputs, independently of index stat flags and with Git replace objects disabled for tree/blob reads. Permit the established EOL equivalence and verify LFS physical bytes against the committed pointer's SHA-256/size; reverify the delivered subject. Real Git fixtures cover assume-unchanged, skip-worktree, pinned commits, real LFS and changed content. |
| B3: authoritative state read before writer admission | iterative CLI wrappers | Move run/config/feedback reads under the writer lock. Recheck run existence before begin writes durable configuration. A six-entrypoint fixture advances run generation and targets immediately before real OS writer admission and checks that the admitted operation sees the new values. The same fixture detects all six stale reads against archived preview.14. The wider asynchronous generation/CAS matrix remains separate work. |
| B4: stale lock reclaim/release races | run writer lock | A stable sidecar OS lock owns exclusion; JSON metadata is diagnostic. Token-checked release cannot remove another owner's metadata. OS handles release after owner death. Three real reclaim processes, a killed owner and foreign metadata are covered. |
| B5: unrelated root selectors | security workspace scope | Restrict changed root overrides/resolutions to exact dependency names with direct consumers contained in the pinned reachable workspace closure. Reject broad/nested selectors and known shared consumers. This is a conservative partial repair: full transitive/shared-lock blast radius still needs proof. |
| B6: ancestor registry files absent from audit identity | manual audit / Desktop acceptance | Hash local controls and ancestor `.npmrc`, `.yarnrc`, `.yarnrc.yml` and `.pnpmfile.cjs` where the existing no-local-lock ancestor walk applies, in both runtimes. A real nested fixture changes its root registry and checks Python/Node hash parity. Global user configuration and effective auth/environment context are not fully covered by this change. |
| B7: UNKNOWN precheck opens a paid source-repair gate | iterative precheck | Timeout/kill and the supervisor's full-stream infrastructure marker remain inconclusive. Continue to authoritative verification without creating a source-repair request or spending a repair attempt. A real supervised timeout supplies the negative fixture. |
| B8: actions divided by current goals | task projection / UI | Separate historical accepted actions, unique accepted packages and goals satisfied now. The percentage uses satisfied current goals over the same current denominator. A two-actions/one-package fixture checks both satisfied and changed targets. |
| B9: UTF-8 characters split across pipe chunks | process launcher / streaming consumers | Keep a decoder per process stream and buffer incomplete UTF-8 prefixes. Test all byte cuts for Cyrillic/emoji and a real child writing separate stdout/stderr byte streams. OEM fallback remains available for complete Windows diagnostics. |

### Checkpoint version upgrades

Materialization preserves the recorded capture policy. Fresh verification is
explicitly bound to that snapshot; a bare `ok` or a receipt for another source
or assignment cannot publish an upgrade record. The original checkpoint and
proof identities remain unchanged. The new pass record binds its own subject.
A real fixture requires a file retained by a historical `.nx/cache` policy;
it cannot pass by verifying a copy which silently drops that file.

Committed blob comparison groups at most 256 objects / 8 MiB of payload per
Git request. A real Git replace-ref fixture fails before the raw-object guard
and rejects the substitution after it. One oversized object gets a separate batch; memory for an individual
large file remains a limitation. A real 303-file Git fixture detects removal of
either bound without bypassing content comparison.

Content mismatch diagnostics compare saved manifest indexes, record added,
removed and changed counts/samples plus schema/build/policy identities, and
state the recovery requirement. They do not declare compatibility from path
names or rewrite historical manifests. Missing or invalid evidence fails closed.

### Updater tooltip

A transient feed failure previously remained visible until a manual check or
the next 30-minute background interval. Also, a downloaded package retained an
old feed error after successful discovery. Feed checks now retry transport,
429 and selected 5xx failures twice with 2s/8s backoff. The final failure remains
visible; 401/403/404, malformed feeds and integrity failures are not hidden.
Actual `builder-util-runtime.HttpError` instances exercise classification.
A successful feed result clears the stale message while preserving download
progress and the installable package. No update is installed implicitly.

## CI/CD

In the preview.14 release run, `validate` took 11m36s and `validate-windows`
took 9m43s; the latter started only after the former completed. The total run
was 24m48s. Source: the public
[preview.14 workflow](https://github.com/AlexanderLevenskikh/deploom/actions/runs/39182049755).

`validate-windows` now starts independently. Windows packaging still depends
on both validation jobs, and publication depends on that packaging result.
At the recorded durations, this removes about 9m43s from the sequential path;
this is a DAG estimate, not a measured duration of a newly released installer.

CI and release use the same automatically discovered Desktop check runner.
The runner coalesces 14 identical Electron compiler hooks into one compilation
and launches the same checks in separate Node processes. Custom lifecycle
hooks retain npm semantics. Compiler failure prevents running stale outputs;
a failing check keeps the gate red and does not skip subsequent checks.
Redundant release-only pip installation and compilation steps were removed.

## Evidence-based removals

Repository-wide tracked references were inspected across CLI/Python imports,
Desktop imports, workflows, package scripts, prompts, documentation and tests.
No runtime import refers to the removed components/module. Tests which only
kept the orphan code alive were adjusted or removed; no live caller was moved.

| Deleted file | Lines | Evidence |
| --- | ---: | --- |
| `desktop/src/components/MonitoringPanel.tsx` | 170 | No production import/mount. References were obsolete source contracts, an explicit non-mount guard and historical documentation. Non-mount guards remain. |
| `desktop/src/components/GoalDetailsModal.tsx` | 112 | No production import. Only its own source contract read the component. |
| `desktop/electron/executor-context.ts` | 62 | Its exports were called only by the orphan check below. |
| `desktop/scripts/check-executor-context.mjs` | 56 | Tested only the unused module; its npm/CI entries were removed together. |
| `fix-block-p-theme-i18n.py` | 117 | Unreferenced one-off patcher; its localization targets are already in source. |
| `fix-resumable-baseline-localization-v2.ps1` | 839 | Unreferenced historical source patch installer. No canonical CLI, build or test entrypoint invokes it. |
| `install_block_u.py` | 784 | Unreferenced historical installer; its existing markers make the code patch a no-op. |
| `desktop/electron/main.ts.before-packageRelativePath-hotfix` | 5779 | Tracked backup, outside TypeScript's source extension/build graph. No file reference from runtime/CLI/CI. |
| Two production-fast aggregation modules | 14 | Re-exported 55 tests already collected from their canonical unit modules by `all`. Those tests remain in `all`. |

Whole-file removals total **7,933 lines**. Other local edits remove orphan
assertions and unused imports. These numbers exclude new regression fixtures
and do not pretend all net deletions are production logic.

The Desktop resource filter excludes source-repository CI/release/test helpers.
Those scripts remain available in the repository. Real electron-builder
`FileMatcher` tests prove runtime Python modules and retained CLI/demo scripts
remain included, while the specified release-only helpers are excluded.

## Test quality

- Two graph string-presence tests now call the compiled production Node module
  against actual installed-manifest fixtures, including removal of a manifest.
- Three publication-policy tests evaluate real configuration callsites with
  actual configs and check role/recovery combinations. They exercise policy,
  not a complete real installation; the physical verifier tests cover that boundary.
- The Draft path test no longer passes because an unused import exists. It
  evaluates the real Desktop CLI argument expression and compares Python writer
  and compiled Node reader paths.
- Release/CI wiring checks parse the actual workflow DAG. Negative mutations
  demonstrate that serialization or loss of either validation gate is detected.
- Runner tests execute the real runner in controlled package/compiler/process
  fixtures, including custom hooks and nonzero compiler/check exits.
- A Draft fixture now registers heartbeat cleanup. Its daemon previously kept
  emitting into unrelated tests after the test which created it had ended.
- Two controller budget fixtures mocked an obsolete snapshot function. They now
  mock the actual recovery boundary and supply explicit subject/assignment receipts;
  separate real-verifier tests reject missing or mismatched authority.
- Two existing Windows junction fixtures captured OEM `mklink` output as implicit
  UTF-8 text. In the full run this raised UnicodeDecodeError in the pipe reader,
  then TypeError while composing an assertion message. They now retain raw
  subprocess bytes; return-code and real junction/security assertions remain.
  Both targeted cases passed after the change. The earlier 2,255-test run with
  these two harness errors is not counted as a passing gate.
- Six duplicated CI command-string assertions were removed from two Desktop
  checks. Parsed workflow/package inventory and executable runner fixtures retain
  the inclusion and failure-propagation invariants.
- The redundant graph-authority source marker test was removed; the two real
  graph boundary tests retain that invariant. The two UI structure/CSS contracts
  remain: SSR only renders the initial loading state and does not establish
  asynchronous browser interaction. No React state was faked to claim coverage.

The negative fixtures exercise concrete violations:

| Invariant | Negative input / observed failure |
| --- | --- |
| Accepted bytes match the verified subject | Mutate a file after the real verifier returns; acceptance rejects and C0 survives. |
| Delivered commit contains the checked bytes | Assume-unchanged, skip-worktree and a real Git replace ref cannot redefine the raw committed content. |
| Both CI gates remain required | Serializing validation or removing either gate from the actual DAG is detected. |
| Runner propagates failures | A real fixture compiler/check exits nonzero; stale checks are blocked or the final gate fails while later checks still run. |
| Graph edges require observed manifests | Removing a manifest reduces installed coverage and removes its observed edges. |
| HTTP classification preserves permanent failures | Actual GitHubProvider wraps 401 containing ECONNRESET text; it remains one attempt, while 429/503 exhaust three attempts. |

`production-fast` changes from 64 executions to 8: 55 duplicate executions are
removed, plus one duplicate source-marker assertion. The 55 canonical tests are
retained. A file-reading heuristic found 160 test files reading source **or
fixtures**; that number is not a count of defective tests. Structural contracts
which remain must not be mistaken for proof of the runtime paths they mention.
A separate collection inventory also reports eight same-source cases loaded
through an imported acceptance base class in `test_phi_repeated_artifact_hit.py`.
That module additionally has a Windows-only subclass inheriting the physical
fixture cases. This canonical acceptance collection was retained for explicit
fixture/inheritance review; no global test-runner deduplication was introduced.

## Validation record

`python run_tool_tests.py --suite all`: PASS, 2256 tests in 1705.125s, 14 skipped. This final full run includes the writer-admission and Windows OEM fixture changes.
Earlier interrupted, sandbox-denied or mixed-build runs are not green evidence.

Completed checks on the frozen production implementation:

- From `desktop/`: `npm run lint`, `npm run build`, and
  `node scripts/run-all-contract-checks.mjs`: PASS, all 77 checks. Existing lint
  and build chunk-size warnings remain; no browser interaction is claimed.
- `python run_tool_tests.py --suite storage-delivery`: PASS, one composite
  small/medium/large physical scenario, 385.100s. The large fixture contains
  2,000 files / 64 MiB and reaches verified delivery and reclamation.
- `python run_tool_tests.py --suite production-fast`: 8 tests, PASS on all
  three final runs, with timings below.
- `python -m unittest discover -s tests/regression -p test_source_checkout_repair_capture.py -v`:
  4 tests, PASS, 4.005s.
- `python -m unittest discover -s tests/regression -p test_repair_settings_workspace_base.py -v`:
  2 tests, PASS, 0.026s.
- `python -m unittest discover -s tests/regression -p test_migration_authority_boundaries.py -k mutating_wrappers -v`:
  1 test / 6 subcases, PASS, 0.370s. The extracted test/setup running against
  archived preview.14 reports six assertion failures and zero errors; the
  observed stale generation/target is the intended before-change violation.

The archived preview.14 collection contains 2,230 `all` tests and 64
`production-fast` executions. The final collection contains 2,256 `all` tests. One writer-admission regression
adds six subcases; its initial separate run passed in 0.370s.
The revised fast gate contains 8 tests. Its three consecutive final runs
passed in 4.477s, 3.672s and 3.961s (median 3.961s);
the earlier 64-execution baseline passed in 22.604s. These are local samples,
with different cache warmth and competing system load, not a controlled CI
speedup estimate or a statistical flakiness guarantee.

Local real migrations occupied shared I/O slots. For fixture runs only, an
ignored `sitecustomize` harness prepended a unique namespace to the existing
cross-process slot key. It retained the actual OS locks, limits and recovery;
production limiter code and user processes were unchanged. CI's isolated VM
does not need this harness, and no committed test imports it.
The 14 skips include Linux-only process/reflink cases, unavailable Windows
symlink/hardlink/directory-swap capabilities, and the opt-in pinned Nx/Sass/Vitest
physical acceptance scenario. These paths are not certified by this local run.
The local runtime is Python 3.14 and Node 24; CI additionally checks its declared
Python 3.12 / Node 22 environment. Real user projects were kept read-only.

## Remaining systemic work

1. Complete B5 with transitive selector/shared-lock ownership and affected
   environment validation; direct workspace edges alone cannot prove blast radius.
2. Complete B6 with effective manager root, global/user configuration and
   registry/auth/environment context, hashing secrets without exposing values.
   Include nested projects with their own lockfile: the existing conditional
   ancestor walk does not yet bind their inherited registry controls.
3. Finish the async generation/CAS, aliases and interrupted archive matrix;
   handle exclusion and fresh state reads are necessary but not the entire matrix.
4. Design a durable cumulative useful-progress budget for terminal agents:
   persisted hypotheses/results and install/audit identities, with controller
   handback. Activity/watchdog output is not useful progress.
5. Add a browser fixture for graph/modal interaction and replace remaining
   behavioral claims based only on source strings. Retain useful structural
   checks until equivalent behavior is actually exercised.
6. Align the per-command precheck recovery journal with the controller's UNKNOWN
   classification. A direct fixture proves that exits 124/137 or a positive exit
   with the supervisor infrastructure marker are still stored as `FAIL` by
   `DiagnosticCommandSequence.complete`, while the controller routes them to fresh
   verification. These records are not reusable PASS evidence; the remaining bug
   is diagnostic inconsistency. The real TimeoutExpired exception is normalized to
   unavailable exit status (-1) and already records UNKNOWN. Use one shared
   classifier and test controller, telemetry and journal together.

These are explicit limitations, not claims that all monorepository, restart or
version-upgrade combinations have been certified by passing unit tests.
