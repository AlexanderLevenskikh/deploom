# Block Y — Verification Observability & Performance Intelligence

Block Y is **non-authoritative**. Telemetry can explain a decision but can never
change a solver domain, proof identity, cache authority, learned constraint, or
Verifier outcome.

## Correlation hierarchy

Every verification event keeps the existing JSONL `schemaVersion: 1` envelope
and adds `observabilitySchema=verification-observability-v2-performance`.

The correlation hierarchy is:

```
runId
  requestId
    attemptId
      stageId
        eventId
```

Lower-level operations that can execute outside an assignment attempt use an
`operationId` (for example Z3 and filesystem materialization).

## Performance categories

The runtime summary deliberately counts **low-level work** rather than every
composite stage, so source capture is not double-counted on top of its manifest
and materialization work.

Current categories:

- `solver-z3`
- `source-manifest`
- `filesystem-materialize`
- `dependency-integrity`
- `resolver-install`
- `lifecycle-preparation`
- `snapshot-publish`
- `project-clone`
- `project-check`
- `localization-check`
- `localization-confirmation`

`knownOperationWorkMs` is work-time and can exceed wall-clock because operations
may overlap. `coveredWallMs` is the union of instrumented intervals.
`unattributedWallMs` is wall-clock not covered by those intervals.

This is intentionally more honest than summing durations and calling the result
"Baseline time".

## Z3 telemetry

Every authoritative Z3 call emits model dimensions without exposing source or
credentials:

- package count
- candidate count
- hard constraint count
- requirement count
- objective width
- deterministic tie-break objective count
- state upper bound
- timeout
- status
- solver-reported elapsed time
- wrapper wall/CPU/RSS metrics

Generator-level component summaries additionally expose `changed`,
`refinements`, and total component solve time.

## Filesystem telemetry

`materialize_private_tree` reports:

- backend/method
- duration
- platform/filesystem
- configured copy-worker count
- exclusion policy cardinality
- process CPU/RSS

It does **not** walk the tree again just for telemetry.

Source manifests already know their exact file/byte counts and report them
without an extra scan. Dependency-integrity sealing reports its actual hashed
file/byte counts from the existing hashing pass.

## Localization telemetry

`parallel_ddmin` emits machine-readable events even when no human progress
callback is configured. Parallel screening is still non-authoritative until the
existing serial confirmation succeeds; telemetry does not change that rule.

## Analyze a run

```
python analyze_verification_telemetry.py \
  .dependency-roadmap/baseline-verification-telemetry.jsonl
```

Write a reusable JSON summary:

```
python analyze_verification_telemetry.py \
  .dependency-roadmap/baseline-verification-telemetry.jsonl \
  --json-out .dependency-roadmap/baseline-performance-summary.json
```

Use `--strict` when validating correlation contracts in automation.

## Optimization rule after Block Y

Performance work should target the measured top categories. A faster path is
acceptable only if the existing correctness, determinism, SourceSnapshot and
proof-authority contracts remain unchanged.
