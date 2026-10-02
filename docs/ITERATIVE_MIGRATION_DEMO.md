# Physical iterative migration demo

Run from the repository root:

```powershell
python scripts/run-iterative-demo.py
```

The script requires Git, Node, npm and registry access. It creates a NEW isolated
Git repository below `.dependency-roadmap/iterative-demo/`, installs real public
packages and retains all evidence. It does not migrate a developer's checkout or
publish anything. Initial and target versions are warmed in a private npm cache;
subsequent dependency materialization uses that cache offline. Final independent
audit still uses the production audit implementation and records its evidence.

The demo has 12 direct dependencies and one package per initial cohort:

| Cohort type | Expected behavior |
| --- | --- |
| Ordinary upgrades | Install exact versions, run `npm run test`, accept a checkpoint. |
| Version-sensitive configuration | The old config fails after upgrading `is-finite` / `is-number`; propose source/config repair, then run `verify-exact`. |
| Deliberately wrong repair | Real project checks remain RED; the verified base is unchanged. |
| Correct repair | Real checks pass, repaired source bytes are captured in the next checkpoint. |
| Impossible `is-string@99.99.99` | Real npm returns ETARGET. INCONCLUSIVE defers the exact attempt on the current base; later independent cohorts still run. |
| Process restart | Every CLI step is a fresh Python process; subsequent cohorts read the same durable cumulative chain. |

The artificial configuration contracts are fixture behavior, not claims about
breaking changes in those npm packages. Repair proposals are scripted to make the
scenario reproducible: this does not test the quality or availability of an
external LLM agent. The production CLI, npm, Git, materializer, project checks,
proof validation, feedback identities and audit are real.

Expected outcome: 11 accepted upgrades, one unmet target, C0 → C11,
`PARTIAL_VERIFIED` when the final independent audit passes. The impossible target
is never counted as satisfied. It may be tried again after a NEW verified base
changes dependency context; it must not repeat on an unchanged base.

The script prints `DEMO_WORKSPACE` and finally `DEMO_ACCEPTED`. Inspect:

- `ACCEPTANCE.json`: assertions, exact final state and original source Git status.
- `TIMELINE.json` and `cli.log`: all process invocations and results.
- `run/checkpoints/`: accepted cumulative states and proof identities.
- `run/ledger.json`: feedback and scheduling deferrals, distinct from learned incompatibilities.
- `run/audit/C11/audit-report.json` and `audit-report.md`: independent audit evidence.
- `run/reports/MIGRATION_REPORT.md` and `DEVELOPER_UPGRADE_GUIDE.md`.

These reports are generated summaries. Agent-authored upgrade explanations are
not fabricated by this scripted demo; the guide explicitly reports their absence.
Runtime artifacts are ignored local data. Do not add caches, sealed snapshots or
machine-specific paths to a public commit.
