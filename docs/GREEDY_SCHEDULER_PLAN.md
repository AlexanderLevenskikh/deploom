# Practical migration scheduler: revised implementation plan

Goal: maximize progress accepted by the selected validation profile within the user's time budget and target policy. Preserve the last verified cumulative checkpoint. Do not confuse solver acceptance or benchmark state with project verification.

## Immediate repair

Break identical-candidate loops. Parse exact companion proposals, materialize missing dependencies in the isolated trial, and preserve mandatory companions during splitting. Try independent cohorts and exact alternatives after failure. Report unresolved proposals with a concrete next action. Timeout, infrastructure failure and solver UNKNOWN are not incompatibility proofs.

## Initial scheduling policy

Use whole-first as the production default with a large initial prior. Prefer intact independent cohorts before exploring a blocked remainder. The new adaptive-size controller is opt-in until real measurements justify promotion. Split and recombine dependency atoms, preserving mandatory companions. Families and installed metadata provide hints; authoritative resolver and selected checks still decide acceptance. Full-assignment failure does not establish that every subset fails.

Initial priors: cohort cap 24, exploration ratio 2, initial structural search budget 128. These are tunable starting values, not universal optima. Exhausted cheap search may expand within a hard bound; exhaustion must not masquerade as completed work.

## Experiment plan

Compare whole-first, split-first, size-aware greedy and observed-cost greedy under equal check/time budgets. First use repeatable synthetic fixtures to isolate scheduling effects; then exercise real installation and validation separately. Include clean updates, large blocked remainders, sparse and dense conflicts, mandatory companion/non-monotonic cases, variable-duration checks, alternative versions and infrastructure failures.

Record time to first accepted checkpoint, accepted target coverage, accepted packages per minute, failed-check time, verification count, planning time and agent time when an agent actually runs. Unmeasured agent time is unavailable, not zero. Compare coverage alongside latency so premature stopping cannot look fast. Use multiple seeds and hold-out fixtures; report variability and scope rather than mathematical optimality.

## Adaptation after measurement

Keep lightweight observations per operator and relevant context: cohort size, family, upgrade risk, companion count, failure category, verification/agent duration and accepted delta. Rank actions by expected policy-valued accepted progress divided by expected wall-clock cost, with bounded exploration. Policy value includes priority/security targets, not only package count. Start with simple smoothed observations and conservative priors; add complexity only if it improves hold-out outcomes.

Candidate operators: intact cohort, exact version alternative, companion expansion, split/recombine and source/config repair. Evidence reuse and learned conflicts remain bound to source, dependency assignment, validation profile and relevant environment. Never transfer a failure classification across an incompatible evidence scope.

Cost-aware remains benchmark-only: it ranks whole/split candidates from smoothed observations, but physical migration rejects that mode. Production telemetry now records physical install/precheck/verification/audit stages and scoped agent spans; missing provider time remains unavailable. Whole-first remains the default. The global adaptive-size prototype improves first progress in some fixtures but regresses total time with exact conflict clauses, so it is available only by explicit selection.

## User-facing stopping and recovery

Save accepted checkpoints immediately and expose current checkout, progress, blocked scope, attempted alternatives and next action. Finish as partial only on explicit budget, cancellation or genuinely exhausted admissible options; low estimated gain alone is not permission to silently abandon the user's goal. Publication and transfer to the main checkout are distinct explicit operations.

## Current evidence boundary

The first demo harness uses the production planner and manifest writer with real Node subprocess checks delayed by five seconds. Dependency conflicts are synthetic. It does not measure package installation, registry resolution, vulnerability audits, agent repair, or production proof authority. The harness now compares all four scheduling strategies and records first acceptance, coverage and measured throughput. Constant-cost synthetic fixtures do not establish benefits from cost adaptation or source/config repair.

## Adaptive-size measurement

See [ADAPTIVE_COHORT_SIZE.md](ADAPTIVE_COHORT_SIZE.md) for the 120-run comparison, anytime coverage, controller/restart guards and three owned projects with real npm/TypeScript/build/runtime checks. Stop synthetic parameter sweeps here. Collect real project failure stages and timing before considering a region-specific controller; physical verification remains the authority.

## Larger timing experiments

The harness supports 26..512 packages, actual Node delays up to 600 seconds and a virtual clock using the same compatibility oracle. Model fixed and cumulative variable check cost, plus a separately disclosed fixed recovery delay after rejection. Validate oracle parity with physical subprocess checks before interpreting projections. Fixed recovery delay does not model an agent's decisions or repair success.

Separate the initial cohort size effect (8 versus 24 within the current planner) from strategy effects at the same initial size. Scale the number of conflicts with project size and include alternating large families and small groups so the exploration threshold is actually exercised. Record known fixture maximum and coverage gap, plus search exhaustion; never rank an incomplete run as faster completion.

The larger fixtures show gains from initial size 24 in clean and sparse conflict cases, but regressions in dense cases. Whole-first versus size-aware and cost-aware is often a tie. The ratio and search budget must remain conservative priors: these measurements do not justify claiming a universally best value. Prioritize detecting repeated failures and switching to compatible work; collect real install/agent durations before promoting cost-aware scheduling.


## Local conflict region follow-up

Current follow-up: [local conflict regions](LOCAL_CONFLICT_REGIONS.md) preserve the whole-first policy and add local opaque-failure fallback. The global adaptive measurements below remain historical evidence; global adaptive-size is not the production default.

Matched local-region evidence supports a local fallback inside production whole-first. Healthy cohorts keep cap/prior 24; opaque child failures affect only child regions. Keep exact constraints and physical verification authoritative. Stop synthetic parameter tuning and calibrate using physical attempt telemetry.
