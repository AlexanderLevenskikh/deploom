# Local adaptive conflict regions — measured decision

Production policy stays `whole-first`, with local fallback enabled after opaque project failures. Healthy cohorts retain aggressive prior/cap 24. Global adaptive-size is not the default; cost-aware remains benchmark-only. The explicit `local-adaptive-region` strategy uses the same fallback. The benchmark disables local fallback for its `whole-first` control (`localConflictRegions=false`); this distinguishes the previous baseline from the new production whole-first behavior.

## Region behavior

- Deterministic dependency/peer/companion/version evidence uses existing exact filtering, alternatives, companion expansion and bounded recombination. It does not vote for local shrink.
- Definitive opaque project failures create a region over the failed packages/atoms and halve only its soft size. A failed strict subset creates a child region; its untested sibling keeps the parent size. The most specific region owns each atom. Mandatory atoms may exceed the soft size but must fit the explicit hard cap.
- Regions persist in the ledger with packages, atoms, failureStage, attempts, attemptsWithoutProgress, localSoftSize, knownConstraints, companions, lastProgress, stable regionId and lineage. Run/project/policy/runtime/validation scope and a source/config hint identity derived from the sealed checkpoint bound reuse. The hint identity excludes dependency declaration sections and known lockfiles, and preserves every other manifest entry plus scripts/overrides/config. It is distinct from the full source snapshot proof key. Dependency-only checkpoints preserve regions; repaired source/config invalidates reuse. Missing or mismatched snapshot evidence disables hint reuse. Full source verification is unchanged.
- An accepted subset updates lastProgress only after the verifier and durable checkpoint switch. Acceptance retains the original failed-region lineage even when the accepted repair creates a new source snapshot. Remaining targets stay in the denominator.
- UNKNOWN, infrastructure, unavailable versions, preparation failures and timeout/cancel exit codes do not vote for shrink. No arbitrary compiler output is promoted to a learned dependency clause.
- Agent repair stays a distinct operator with the existing per-candidate bounded budget. Exhausted repair returns to independent cohort selection/local fallback. Provider prose supplies no validity or progress authority. This change does not introduce a new repair budget.

## Matched comparison

84 synthetic scheduler runs: seven profiles, four seeds, three policies, 96 targets, initial/cap 24, ratio 2 and structural budget 128. No parameter sweep. Checks cost 120 modeled seconds and each rejected action costs 60 modeled recovery seconds. Oracle reachable coverage is fixture knowledge used only to stop measurement, never planner evidence or production completion authority. All runs reached the same fixture maximum.

Mean modeled completion minutes:

| Profile | Whole-first baseline | Global adaptive-size | Local region |
| --- | ---: | ---: | ---: |
| clean | 8.00 | 8.00 | 8.00 |
| cross-conflicts | 35.75 | 66.50 | 35.75 |
| dense-conflicts | 48.50 | 79.75 | 48.50 |
| opaque-failure | 1524.75 | 1204.25 | 711.75 |
| large-remainder | 38.50 | 38.50 | 38.50 |
| mandatory-companion | 11.00 | 11.00 | 11.00 |
| multi-failed-regions | 256.00 | 796.00 | 136.00 |

Local-region preserves completion exactly for every matched clean, cross-conflict, dense-conflict, large-remainder and mandatory-companion run. Mean opaque completion improves by 53.3% against whole-first and 40.9% against global adaptive. Multi-region completion improves by 46.9% against whole-first. This is synthetic evidence, not a measured speedup on a customer project.

Anytime metrics for the two opaque profiles (mean modeled minutes):

| Profile / policy | First checkpoint | 25% | 50% | 75% | 90% | Failed wall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| opaque-failure / whole-first | 6.50 | 10.75 | 52.00 | 357.25 | 865.25 | 1484.25 |
| opaque-failure / adaptive-size | 5.00 | 11.75 | 66.50 | 335.75 | 784.50 | 1121.25 |
| opaque-failure / local-adaptive-region | 6.50 | 7.75 | 30.25 | 104.75 | 328.75 | 660.75 |
| multi-failed-regions / whole-first | 17.00 | 28.00 | 65.00 | 125.00 | 182.00 | 228.00 |
| multi-failed-regions / adaptive-size | 11.00 | 46.00 | 227.00 | 477.00 | 670.00 | 648.00 |
| multi-failed-regions / local-adaptive-region | 17.00 | 22.00 | 32.00 | 52.00 | 72.00 | 96.00 |

JSON retains progress curves, attempt history, chosen operator/reason, region identity/lineage and final region state. The multi-region fixture places four opaque conflicting pairs in independent initial cohorts. Sparse/dense expose exact clauses; opaque profiles expose only physical action PASS/FAIL. Both evidence regimes are reported separately. The large-remainder case intentionally feeds only an exact rejected tuple, not a member nogood, preserving the earlier stress case.

An initial prototype shrank the entire parent when a strict subset failed and regressed on the multi-region case (910 vs 256 modeled minutes, first seed). Fixed that locality defect by creating child regions and preserving untested siblings. The final four-seed comparison uses this corrected implementation; no ratio, step or shrink-factor tuning was performed.

## Telemetry and validation

Attempt rows now include `regionId`, `regionLineage`, `regionSoftSize`, `regionStateReason`, `evidenceKind`, and `operatorReason`. Selection retains all involved region IDs/lineage and selected atoms. Agent feedback records operator `agent-repair` and reason `bounded-agent-repair`. Existing command timings, accepted delta, cumulative verified targets and attempts without progress remain physical measurements.

21 focused local-region tests cover independent large work, child/sibling locality, multiple/overlapping regions, restart, source isolation, actual sealed-snapshot dependency-only continuity, atom preservation, timeout UNKNOWN, verified progress and production whole-first fallback. Existing adaptive/telemetry tests continue to cover the optional global controller and authority boundaries. Physical acceptance exercises both whole-first and explicit local-region via fresh CLI processes, real Git/npm installation, rejected repair, repaired-source verification and cumulative checkpoints. It uses scripted repair, not an actual provider agent.

Targeted checks on the final implementation: local-region 21 passed; existing adaptive/telemetry 18 passed; final physical acceptance 2 passed in 173.346s; separate final production-fast 64 passed in 13.664s. Six Node/virtual parity runs matched the full sequence of cohort sizes, PASS/FAIL, accepted targets and region IDs (opaque: whole-first 92 checks, global 41, local 31; multi-region: 31/19/13 checks). These 26-target parity fixtures use 10ms delays and measure oracle equivalence, not realistic stage costs.

Additional repair boundary gates: source checkout repair capture 4 passed; settings/workspace-base 2 passed; Desktop check:repair-handoff and check:repair-dispatch passed.

An urgent release was requested while the final full Python gate was still running. The full gate is not yet reported green; the saved log is `.dependency-roadmap/qa/local-region-full-python-final.log`. No additional optional test runs are being started. The release helper retains its mandatory validation. No customer migration was performed. Release requested after the measurements. Source HEAD before release: `8d76803ed20004dc6ab860856e1aa9304af65472`; release publication status is reported separately after the helper finishes.

## Reproduce

```powershell
python -X utf8 benchmarks/bench_greedy_cohorts.py --virtual-time --delay-seconds 120 --packages 96 --reps 4 --ratios 2 --steps 128 --strategies whole-first,adaptive-size,local-adaptive-region --profiles clean,cross-conflicts,dense-conflicts,opaque-failure,large-remainder,mandatory-companion,multi-failed-regions --recovery-seconds 60
python -X utf8 -m unittest discover -s tests/regression -p test_local_conflict_regions.py -v
python -X utf8 -m unittest discover -s tests/acceptance -p test_iterative_migration_physical.py -v
python -X utf8 run_tool_tests.py --suite all
```

Raw final synthetic results: ignored `.dependency-roadmap/qa/greedy-tuning/20261006-011913`. Next calibration should use 2–3 production-like/real projects and their physical telemetry; do not extend synthetic tuning to claim universally optimal behavior.
