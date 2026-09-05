# Fast Exact-Failure Confirmation — Durable Publication Deferral

## Production evidence

A real `partner-form` Fast Baseline showed:

- adaptive `yarn lint:types` rejected the candidate;
- control passed;
- exact confirmation reproduced the same structural failure;
- exact confirmation then spent about 906 seconds hashing about 170k files for
  `durable-record:integrity-seal`;
- the complete candidate cost about 1504 seconds, which is longer than the
  15-minute Fast admission budget.

## Root cause

The intended architecture was already present:

```text
pre-incumbent Fast confirmation
→ publish_durable_prepared_artifact = false
→ authoritative request-private PreparedSnapshot
→ project check
→ discard on FAIL
```

But adaptive command narrowing rebuilt the confirmation config from the original
`config` after the publication policy had already been derived:

```python
confirmation_config = dataclasses.replace(config, commands=targeted_commands)
```

That accidentally restored `publish_durable_prepared_artifact=True`.

## Fix

Targeted narrowing now derives from the existing confirmation config:

```python
confirmation_config = dataclasses.replace(
    confirmation_config,
    commands=targeted_commands,
)
```

## Preserved proof invariants

The optimization does not skip:

- sealed SourceSnapshot materialization;
- ResolverProof / exact lockfile restoration;
- frozen lifecycle install;
- observed resolved-assignment checks;
- fresh private project-check materialization;
- process-tree supervision;
- candidate-vs-control structural comparison;
- independent exact confirmation;
- exact failing-assignment exclusion.

It skips only cross-process durable PreparedArtifact publication for the
pre-incumbent failing confirmation. The verifier already uses a request-private
prepared snapshot when durable publication is disabled.

Candidates that may become reusable verified incumbents continue to use the
normal durable publication policy.

## Expected production signal

For the same failure class, exact confirmation should still run the responsible
project check, but before the exact FAIL there must be no:

```text
snapshot-publish: durable-record:integrity-seal started
```
