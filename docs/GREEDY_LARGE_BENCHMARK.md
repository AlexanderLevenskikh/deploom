# Larger greedy scheduling experiment

Production planner and manifest writer, synthetic dependency oracle, modeled check time. No registry install, vulnerability audit, actual repair agent, or production verification authority.

720 virtual runs: 96 and 192 targets, six compatibility/cost profiles, six seeds for the strategy comparison; additional ratio/search-budget sweeps and ten-seed fragmented-family cases. All reached the known fixture maximum. This maximum is only known because the oracle is synthetic.

Each check costs 120 modeled seconds, plus 30 seconds per installed slow package in the variable-cost profile. Each rejected check adds 60 modeled recovery seconds. Recovery is a fixed delay, not an agent simulation. CPU/planning time is recorded separately; projected time excludes it. Failure rules provide exact clauses in the conflict fixtures, whereas opaque blocker fixtures supply only the rejected assignment.

Baseline below is the CURRENT planner with whole-first and initial size 8. It isolates tuning effects; it does not reproduce every detail of an older release. New configuration uses size-aware, initial size 24, ratio 2 and initial search budget 128. Cohort cap is 24 in both. A larger budget may be retried automatically within the production hard bound.

| Targets | Scenario | Accepted / targets | Initial 8 whole-first, min | Initial 24 size-aware, min | Time reduction |
| ---: | --- | ---: | ---: | ---: | ---: |
| 96 | clean | 96/96 | 12.00 | 8.00 | 33.3% |
| 96 | large-remainder | 95/96 | 51.50 | 44.00 | 14.6% |
| 96 | cross-conflicts | 72/96 | 48.67 | 38.00 | 21.9% |
| 96 | dense-conflicts | 81/96 | 39.50 | 44.50 | -12.7% |
| 96 | variable-cost | 95/96 | 84.33 | 73.67 | 12.6% |
| 96 | mandatory-companion | 96/96 | 15.00 | 11.00 | 26.7% |
| 192 | clean | 192/192 | 20.00 | 16.00 | 20.0% |
| 192 | large-remainder | 191/192 | 66.17 | 54.00 | 18.4% |
| 192 | cross-conflicts | 144/192 | 79.17 | 77.83 | 1.7% |
| 192 | dense-conflicts | 177/192 | 54.17 | 57.67 | -6.5% |
| 192 | variable-cost | 191/192 | 110.33 | 91.08 | 17.4% |
| 192 | mandatory-companion | 192/192 | 23.00 | 19.00 | 17.4% |

Numbers are means across matched seeds, not measured project runtimes. Negative reduction means a regression. On 96 targets, whole-first and size-aware with the SAME initial size 24 tie across these six profiles. At 192 targets, they also tie here; cost-aware saves about 20 modeled seconds on average in the sparse conflict case, and does not consistently improve the other profiles.

## Threshold and search budget

On 96 targets, ratio 2/4/8/16 and initial budget 128/512/4096 produced the same check counts for the four tuning seeds in each of the sparse/dense/remainder fixtures. No demonstrated benefit justifies making the large budget the default. A separate six-seed budget 8/32 sweep also reached the same maximum, with small changes in check counts; more search is not monotonically faster.

Alternating 23-package families and 2-package groups exercise the large-remainder threshold. Across ten seeds, whole-first averaged 20.3 checks and 52.4 modeled minutes; size-aware ratio 2 averaged 20.1 checks and 52.1 minutes. Ratio 16 matched whole-first. Both budgets 8 and 128 matched here. Thus the observed threshold benefit is only 0.6%, not a strong performance claim.

In scaled sparse/dense conflict fixtures, the final search can exhaust its budget AFTER reaching the known maximum. The report retains search-budget-exhausted rather than claiming a proof that no more work is possible. Coverage gap is reported independently. A real project does not reveal the oracle maximum, so that exhaustion still requires an actionable partial-progress message.

## Physical check and parity

A physical clean 26-target run with two 120-second Node checks took 240.130 seconds; first acceptance was at 120.060 seconds. This confirms the actual delay path, not a strategy speedup. Six separate 26-target scenarios were run with both virtual oracle and real Node subprocesses: accepted versions and every cohort size/pass decision matched. Physical parity checks used short delays to avoid spending hours reproducing the same scheduling choices.

Validation for this benchmark change: six physical/virtual parity cases; 16 storage/adaptive-cohort tests and seven cost-model tests passed. The first sandboxed storage run failed because Windows Temp denied writes; the approved rerun passed. No production behavior was changed in this benchmark-only follow-up, and the full suite was not rerun.

## Reproduce

```powershell
python -X utf8 benchmarks/bench_greedy_cohorts.py --virtual-time --delay-seconds 120 --packages 96 --reps 6 --ratios 2 --steps 128 --strategies whole-first,size-aware,cost-aware --initial-packages 8,24 --profiles clean,large-remainder,cross-conflicts,dense-conflicts,variable-cost,mandatory-companion --recovery-seconds 60
# Repeat with --packages 192 for the larger comparison.
python -X utf8 benchmarks/bench_greedy_cohorts.py --virtual-time --delay-seconds 120 --packages 96 --reps 4 --ratios 2,4,8,16 --steps 128,512,4096 --profiles cross-conflicts,dense-conflicts,large-remainder --recovery-seconds 60
python -X utf8 benchmarks/bench_greedy_cohorts.py --virtual-time --delay-seconds 120 --packages 96 --reps 10 --ratios 2,16 --steps 8,128 --strategies whole-first,size-aware,cost-aware --profiles fragmented-families --recovery-seconds 60
# Actual two-minute checks: omit --virtual-time. This command takes about four minutes.
python -X utf8 benchmarks/bench_greedy_cohorts.py --delay-seconds 120 --reps 1 --ratios 2 --steps 128 --profiles clean
```

Generated projects contain package.json with a test command node check.cjs, fixture.json and check.cjs. delayMs: 120000 makes the generated command genuinely wait two minutes. Do not add that artificial delay to a real migration profile: it is for the owned demo only.

Detailed JSON and per-attempt histories remain in ignored .dependency-roadmap/qa/greedy-tuning/. Source baseline: 8d76803ed20004dc6ab860856e1aa9304af65472 plus the local scheduler changes documented in GREEDY_SCHEDULER_BENCHMARK.md.

Run directories:

- 96: 20261005-232813
- 192: 20261005-233036
- tuning: 20261005-232836
- families: 20261005-233046
- small-budget: 20261005-233208
- Physical 120-second check: 20261005-232649

Practical interpretation: keep large initial cohorts as a useful prior, report regressions, and shrink/switch after failures. These models do not establish real-project speedup or a universal best ratio/budget. Further cost adaptation needs actual installation and agent timing and richer, independent projects; simply increasing a fixed sleep changes absolute savings, not the percentage caused by the same number of checks.
