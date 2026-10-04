# Physical iterative migration demo

The demo drives the real iterative migration control loop over real Git/npm
fixtures. Since the coordinator loop lives in the Desktop control plane, the
demo runs it through the REAL compiled production modules by driving
`scripts/iterative-node-drive.mjs` (the same `decide → status → step` loop that
`desktop/electron/main.ts` wires to `flow:iterative:drive`), with the REAL Python
CLI, npm, Git, materializer, project checks, proof validation, feedback
identities and audit.

No fake verifier, no external LLM agent session, and no user checkout is
touched. Scripted repairs are proposals: only `verify-exact` may accept a
checkpoint. The demo driver never sends feedback on the application's behalf
and never sends `INCONCLUSIVE`.

Run from the repository root:

```powershell
# build/refresh the compiled production coordinator modules first
cd desktop; npx tsc -p tsconfig.electron.json; cd ..

python scripts/run-iterative-demo.py                       # default: happy-path-24
python scripts/run-iterative-demo.py --profile failure-matrix
python scripts/run-iterative-demo.py --profile cross-group-closure
python scripts/run-iterative-demo.py --profile cross-group-companions
python scripts/run-iterative-demo.py --keep-output         # retain the stage dir for inspection
```

The Desktop `dist-electron` build is required by `scripts/iterative-node-drive.mjs`
(the helper fails fast with a build hint otherwise). The script requires Git,
Node, npm and one-time registry access to warm cache fixtures; every subsequent
dependency materialization runs with `npm_config_offline=true`.

## Profiles

| Profile | What it proves |
| --- | --- |
| `happy-path-24` (default) | 24 real packages migrate in greedy cohorts with a deliberately wrong repair (rejected), version-sensitive config adaptation, stop/resume of a real project command with the checkpoint preserved, ETARGET on impossible targets (`is-string@99.99.99`, `is-weakset@99.99.99`) that defers exactly and never repeats on an unchanged base. Terminal `PARTIAL_VERIFIED`. |
| `failure-matrix` | With a degraded cache (none of the new versions present), exact `maxInfraRetries` install attempts occur, the run stops at `INFRA_BLOCKED` with durable evidence (CACHE_MISS, fingerprint, verified checkpoint preserved), a healthy cache alone does NOT silently continue, and the operator heals via `plan-next --retry-infra`. A drive kill right after the healed materialize resumes the SAME candidate and the SAME checkpoint (restart-safe, no attempt re-spent); a wrong repair is rejected before the correct one lands. Terminal `COMPLETE`. |
| `cross-group-closure` | A mutually exclusive pair (`is-string` + `is-symbol`, "a+h") inside a noisy 8-package batch. The combined assignment is inherently RED, gets REJECTED, and the planner's split-then-recombine finds a workable separation. The pair is never accepted together, no package is skipped/repeated/duplicated across accepted checkpoints, and the search is never declared exhausted early. Because the pair is an exclusive OR, exactly one member is upgraded and the other is honestly blocked — terminal `PARTIAL_VERIFIED`, never a fabricated `COMPLETE`. |

Every profile asserts the driver only ever writes `READY_FOR_VERIFY` at agent
gates, never `INCONCLUSIVE`, and never edits durable state for the application.

The additional `cross-group-companions` profile uses four real packages. The
`is-finite` / `is-symbol` pair lies across the positional split and must upgrade
TOGETHER; both singletons fail, and two noisy targets are deliberately blocked.
The production planner must reach and accept the pair after rejecting the large
batch and split candidates. This tests positive companion closure separately
from the mutually exclusive pair in `cross-group-closure`. The expected result
is `PARTIAL_VERIFIED` with both companions upgraded and the two noisy targets
remaining at their verified old versions.

## Fixtures

The script creates a NEW isolated directory (a temp stage by default, or
`.dependency-roadmap/iterative-demo/` with `--output-root`) with a fresh Git
project pinned to the OLD versions. Version-sensitive `src/config.js` contracts
are fixture behavior — intentional rules that the scripted agent adapts to —
not claims about real breaking changes in those public packages. Repair
proposals are scripted to keep the scenario reproducible; this does not test an
external agent's quality.

The script prints `DEMO_WORKSPACE` (the stage) and finally
`DEMO_ACCEPTED <path-to-summary>`. Inspect the retained stage:

- `DEMO_SUMMARY.json` / `ACCEPTANCE.json`: assertions and exact final state.
- `TIMELINE.json`, `cli.log`, `run/attempt.log`: every invocation and result.
- `run/checkpoints/`: accepted cumulative states and proof identities.
- `run/ledger.json`: feedback, infra and scheduling deferrals.
- `run/audit/…/audit-report.json|.md`: independent audit evidence.
- `run/reports/MIGRATION_REPORT.md` and `DEVELOPER_UPGRADE_GUIDE.md`.

These reports are generated summaries. Agent-authored upgrade explanations are
not fabricated by this scripted demo; the guide explicitly reports their
absence. Runtime artifacts are ignored local data. Do not add caches, sealed
snapshots or machine-specific paths to a public commit.
