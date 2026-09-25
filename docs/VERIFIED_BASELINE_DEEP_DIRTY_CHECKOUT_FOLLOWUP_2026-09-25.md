# Verified Baseline Deep: deterministic checkout preflight is masked by worker retries

## Production reproduction (anonymized)

The latest user's Verified Baseline run selected `productMode=deep`, explicit `budgetMinutes=80`, `searchMode=AUTO`. Its run-scoped `activity.log` starts at 22:55:38 UTC and stops one second later. The first worker invocation reports:

`[error] SOURCE_CHECKOUT_DIRTY: <project>: commit/stash/remove changes before generation: M package.json; M yarn.lock`

The generator returns exit 2 before registry analysis, solving or physical verification. Desktop labels this a transient failure and retries it (attempts 2/3, 3/3). The worker retires after exit 2 (`reusable = code in {0,3}`); immediate retries hit `BASELINE_WORKER_RETIRING`. The final UI/recovery state contains only `FLOW_COMMAND_FAILED` and `BASELINE_WORKER_RETIRING`. The persisted `baseline-verification-progress.json` still shows a 903-second, one-candidate budget failure from an earlier Fast run; its timestamp predates the Deep run. Thus the Deep run did **not** exhaust its search budget or find a candidate; it never began searching.

The source guard itself is appropriate for Verified provenance. Do not auto-discard, stash or commit the user's project changes, and do not weaken the guard to mark a dirty checkout verified.

## Required fix

1. Classify `SOURCE_CHECKOUT_DIRTY` and other deterministic `SOURCE_CHECKOUT_*` preflight outcomes as non-retryable. Preserve the first exit code, source reason and relevant file list through the worker and Desktop UI. Do not retry a known preflight rejection or replace it with worker lifecycle noise. Retrying a genuinely transient worker failure must await retirement and start a fresh worker; `BASELINE_WORKER_RETIRING` should not be emitted as a misleading project diagnosis.
2. Show a clear action: "Baseline did not start because the project's tracked source is changed: package.json, yarn.lock. Commit or intentionally stash the changes, or run on a clean checkout/worktree; then retry." Do not suggest that the search failed or that increasing the Deep budget helps. If a user wants to analyze uncommitted changes, that requires a separate explicit product contract and source snapshot proof; it is not a workaround for this bug.
3. Scope baseline progress to the current run (or mark prior progress clearly by run ID/time). On a preflight stop, the current run must not display the previous run's `budget-exhausted` phase as its own outcome. Retain old diagnostics in history.

## Acceptance

- End-to-end Desktop/worker test: stub generator returns exit 2 with `SOURCE_CHECKOUT_DIRTY` and a relevant file list. Exactly one invocation occurs; final result shows the dirty-checkout cause and next action; no `BASELINE_WORKER_RETIRING` or stale budget message becomes primary.
- Worker reuse test: after the deterministic failure, a subsequent user-initiated run, once the source is clean, can start a new worker normally.
- Transient retry test: a genuinely retryable worker exit still retries on a ready fresh worker and preserves the original failure if retirement itself fails.
- Run identity test: old Fast progress remains accessible but is not presented as current Deep progress.
- Validate on the same real project without modifying its files automatically; redact private names, paths and registry URLs from any public test/report.

## Implementation (v0.2.132)

**Generator (`dependency_live_roadmap_generator.py`)**
- `BaselineProgressReporter` is now run-scoped: constructor accepts `run_id`/`started_at`; `begin_run(project, mode, run_id, started_at=...)` rotates stale state from a different `runId` into a history sibling `baseline-verification-progress.json.previous-<runId>-<stamp>` via `_rotate_locked()`, resets the terminal flag and emits a fresh `run-started` marker. Every payload now carries `runId`/`startedAt`; `run-started` re-opens a terminal state; `preflight-failed` is a new terminal phase.
- `run_cli`: derives one run identity from `args.run_id`/`DEPLOOM_RUN_ID` or a fresh `baseline-<hex>`; before the source-checkout guard it creates a run-scoped reporter and calls `begin_run(...)` (stale prior progress is rotated before the preflight can stop). `except SourceCheckoutGuardError` emits `preflight-failed` with `code`+message and still exits 2.
- `resolve_peer_compatibility_with_verification` accepts `run_id`/`run_started_at` and stamps the deep reporter so verification telemetry belongs to the same run.

**Desktop**
- `baseline-retry.ts`: new `isDeterministicSourcePreflightFailure` (`SOURCE_CHECKOUT_*|SOURCE_BRANCH_*|SOURCE_REMOTE_NOT_FOUND|SOURCE_NOT_GIT_REPOSITORY|SOURCE_PROJECT_NOT_FOUND|SOURCE_FETCH_FAILED|SOURCE_BRANCH_DIVERGED|GIT_NOT_FOUND`) and `formatSourceCheckoutDirtyFailure` (parses `M package.json; M yarn.lock` rows into a file list, stripping git short-status prefixes; `undefined` when absent).
- `main.ts`: `nonRetryableDeterministicFailure` now returns true for source-preflight outcomes, so a `SOURCE_CHECKOUT_DIRTY` exit-2 is NOT retried (exactly one invocation). New `isWorkerLifecycleNoise` distinguishes worker lifecycle bookkeeping from a project diagnosis; the retry loop keeps the first real failure and restores it when the final attempt only yields lifecycle noise. New `sourcePreflightCommandFailureMessage` composes the explicit action ("Baseline не запустился: в отслеживаемых файлах проекта есть изменения: <files>. Закоммитьте или сознательно спрячьте (stash) изменения либо запустите на чистом checkout/worktree; затем повторите. Анализ незакоммиченных изменений возможен только через отдельный явный контракт продукта со снимком source snapshot, а не как обход этого отказа.") without any budget/search wording; it is used as the thrown error for baseline preflight failures.
- `baseline-worker.ts`: a request arriving while its worker is retiring now AWAITS that instance's close and then runs on a READY FRESH worker (`retireAwaitTimeoutMs`, default 15 s; on timeout rejects `BASELINE_WORKER_RETIRE_AWAIT_TIMEOUT`). `BASELINE_WORKER_RETIRING` is no longer emitted as a primary rejection; genuinely transient retries reuse a fresh worker, and if the real cause is a preflight rejection the caller keeps the preflight diagnosis.

**Tests / checks**
- `tests/test_verified_baseline_deep_dirty_checkout.py` (6 tests): reporter run-scoping (begin_run rotation+identity, carries runId/startedAt, preflight-failed terminal, same-run begin does not rotate) and hermetic CLI end-to-end on a real dirty git repo (exit 2, `SOURCE_CHECKOUT_DIRTY` + file list in stderr, progress file `phase=preflight-failed` with a new runId, previous Fast `budget-exhausted` preserved in `.previous-*`, second run gets its own identity and keeps history).
- `check-baseline-retry.mjs` extended: preflight classification, `formatSourceCheckoutDirtyFailure` rows, and source contracts (non-retryable wiring, `isWorkerLifecycleNoise`, `BASELINE_WORKER_RETIRE_AWAIT_TIMEOUT`, no budget wording in the message).
- `check-baseline-worker-hardening.mjs` extended: retirement-await reuse runs on a distinct fresh worker process (PID-proven via a startup instance file) and `BASELINE_WORKER_RETIRE_AWAIT_TIMEOUT` rejection when retirement never completes (hang-on-EOF worker); `check-flow-recovery.mjs` import contract updated.

## Verification (v0.2.132)

- New suite: `pytest tests/test_verified_baseline_deep_dirty_checkout.py -q` → 6 passed.
- Targeted regression: `test_baseline_progress.py`, `test_source_checkout_guard.py`, `test_verified_baseline_fast_budget.py` → 33 passed.
- Full Python regression: 1293 passed, 4 skipped, 125 subtests (26:53).
- Desktop: `tsc -p tsconfig.electron.json` + `vite build` OK; `npm run lint` (oxlint) 0 errors; all 46 `check:*` scripts pass (including the extended baseline-retry, baseline-worker-hardening and flow-recovery contracts).

