# Large cumulative migration demo

Run from the repository root:

```powershell
python scripts/run-iterative-demo.py --packages 24
```

`--packages 12` retains the smaller profile. The default is 24. The script creates
an owned Git project under `.dependency-roadmap/iterative-demo/`, seeds exact old
and new public package versions through the configured npm registry, then runs
migration operations offline against that cache. The workspace is retained.
`--seed-cache <existing-directory>` can reuse an earlier demo cache inside this
repository; the exact old and new sets are still warmed before execution.

All 24 dependencies are loaded by real Node project checks. Four application
configuration contracts deliberately require adaptations when their selected
versions change. These are synthetic application contracts for testing the
migration protocol; they are not claims about breaking changes in those npm
releases. Two nonexistent target versions exercise deferral and bounded retries.

A named direct target rejected by npm `ETARGET` is deferred for the current run
and registry/environment context. New cumulative checkpoints do not retry that
unavailable version. The target remains in the requested policy and remaining
work; it never becomes an accepted upgrade or an incompatibility constraint.
A new run, target or registry context can retry. Network, authentication, cache
miss and timeout failures do not create availability deferrals.

The physical acceptance checks verify:

- Cumulative parent checkpoints and preservation of previously accepted versions.
- Rejection of a deliberately wrong source repair by `verify-exact`.
- Correct source repairs accepted only after real installation and project checks.
- A real running npm/Node check is killed as an owned process tree; the last
  checkpoint and candidate survive, and the same candidate resumes.
- Unreachable targets cannot silently become accepted updates.
- Final exact versions, migration reports and developer guide artifacts.

Repairs and feedback are scripted proposals. This demo exercises the real
Python CLI, Git, npm, Node, verification and durable recovery; it does not test
an autonomous external LLM session or the Desktop buttons.

Each retained workspace contains `TIMELINE.json`, `STOP_RESUME.json`, `cli.log`,
`ACCEPTANCE.json`, a source Git project, and `run/` with checkpoints, verification
telemetry and reports. Treat `DEMO_ACCEPTED` plus the verified assertions as the
result; creating the source snapshot or resolving versions alone is insufficient.

## Verified physical run (2026-10-02)

The 24-package Windows run completed with `DEMO_ACCEPTED`:

- 22 accepted updates through C0 -> C22; 2 unavailable targets remain deferred.
- Exactly 2 unavailable-target install attempts, one per target.
- All 4 application adaptations accepted after verification; the wrong repair rejected.
- Running check stopped at C1; the same candidate resumed and became C2.
- Original source Git status remained clean.
- Independent final audit: `PASS`; lag coverage 20/24 (83.3%), no unknown packages,
  4 lagging packages, 0 packages with vulnerabilities.
- Final result: `PARTIAL_VERIFIED`, since the requested targets are not all reached.
  Both migration report and developer guide were generated.

This proves the scripted cumulative CLI scenario, including interruption recovery.
It does not establish successful autonomous agent adaptation, Desktop button behavior,
or completion time for a large user repository. The two earlier runs exposed repeated
unavailable-target attempts and failed acceptance; the final run passed after fixing
both the scheduling filter and the solver's effective desired assignment.

## Initial-check timeout diagnostics

A separate product fix forwards actual verifier progress from `begin --check-only`
to the durable Desktop journal. A watchdog timeout produces
`PROJECT_CHECK_TIMEOUT` with the latest progress message, freezes elapsed time,
and cannot reuse an older `project-check.json` verdict. The 20-minute watchdog
remains unchanged. This improves evidence and recovery; it does not establish
that a previously timed-out large user repository now completes.
