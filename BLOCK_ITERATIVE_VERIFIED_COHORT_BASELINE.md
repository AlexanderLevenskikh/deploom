# Block — Iterative Verified Cohort Baseline

## Goal

Make the normal DepLoom Baseline an anytime, Fast-first workflow:

1. try the broad desired scope;
2. physically confirm compatibility failures;
3. infer an actionable **direct-package compatibility group** as `DIAGNOSTIC_HINT`;
4. ask the user to temporarily defer that group;
5. solve and globally verify the remaining scope;
6. publish an honest `VERIFIED_PARTIAL_SCOPE` incumbent;
7. keep deferred groups as an explicit queue for later passes;
8. optionally run deep/exhaustive search for users willing to wait.

This block does **not** change proof authority. Cohort inference may change navigation/search/user policy only.

## Correctness invariants

- Exact PM/project verification remains authority.
- `UNKNOWN` / infrastructure failure is never cohort incompatibility.
- Cohort suggestion is always `DIAGNOSTIC_HINT`.
- `keep-current` is created only by explicit user action.
- `required` and Critical-security packages are not auto-proposed for deferral.
- High-security packages remain visible as warnings.
- Every new incumbent is globally physically verified.
- Deferred scope is explicit and `VERIFIED_PARTIAL_SCOPE` is never reported as full target completion.
- Cohort metadata is not included in proof/cache/recovery authority identity beyond the actual policy literals (`keep-current` / `required`).

## Hybrid cohort inference

Evidence is combined in this order:

- structural failure predicate subject (`rollup`, `vite`, etc.);
- reverse direct-package consumers of that subject from the exact desired/current metadata;
- direct peer/dependency interaction graph;
- known ecosystem prior (Vite, Storybook, ESLint, Stylelint, Webpack, TypeScript, React);
- repeated-predicate boundary expansion;
- existing user-deferred group metadata.

Static package-name rules are priors/fallbacks, never proof.

## Bridge handling

If the same predicate returns after deferring the same group, the next suggestion expands one bounded interaction-graph shell. Boundary packages are shown separately. This supports overlapping/dynamic groups without pretending they are hard components.

## Product flow

Normal UI exposes one primary path: **Fast Baseline**.

Long-running global/exhaustive search is under **Advanced**.

When a group is suggested, the main action is:

> Temporarily defer this group and continue

After a partial verified result, the next Baseline dialog shows the deferred-group queue and lets the user reactivate one group for the next pass.

## Empirical evidence

Observability emits non-authoritative bounded events:

- `baseline.cohort.suggested`
- `baseline.cohort.expanded`
- `baseline.cohort.user-action` (`DEFER` / `REACTIVATE`)
- `baseline.cohort.active-scope`
- `baseline.cohort.incumbent`

Use:

```bash
python cohort_telemetry_report.py <verification-observability.jsonl> --json
```

Primary metrics:

- median/p90 **Time To First Verified Usable Result (TTFVUR)**;
- suggestion acceptance rate;
- defer → verified-incumbent conversion rate;
- median/p90 defer → incumbent time;
- cohort expansion count;
- cohort-specific suggestion/defer/reactivation counts.

These metrics are evidence for/against the architectural hypothesis; they never feed proof authority automatically.

## Validation

Focused:

```bash
python -m unittest \
  tests.test_baseline_cohort_inference \
  tests.test_iterative_cohort_intent \
  tests.test_cohort_telemetry_report \
  tests.test_block_phi_execution_modes \
  tests.test_baseline_progress_envelope \
  tests.test_block_psi_anytime
python run_tool_tests.py --suite production-fast
```

Desktop:

```bash
npm run build
npm run check:baseline-intent
npm run check:interaction-contracts
npm run check:flow-recovery
```

Full gate:

```bash
python run_tool_tests.py --suite unit
python run_tool_tests.py --suite regression
python run_tool_tests.py --suite acceptance
```

`production-stress` remains an explicit opt-in gate because it is intentionally expensive.
