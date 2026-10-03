# Migration validation profiles

Before checking a project, FLOW shows **Migration checks**. Review the commands,
select a focused local test command when needed, and record deferred checks and
why their environment is unavailable. Known browser/E2E/visual script names or
commands are proposed as deferred rather than started automatically. This is a
suggestion, not proof that other scripts need no services. Explicit commands
always remain the user's choice.

The selection is saved as `verification-profile.json` and copied into the durable
`run-config.json`. The same command set is used for C0, candidate diagnostics and
authoritative verification. An active run cannot change its selection; start a
new run to change the verification contract. Editing the pre-run selection
invalidates the UI's previous ready verdict. Deferred checks are shown in FLOW,
checkpoint metadata, the final report and both languages of the agent task.

## Existing test failures

The default is strict: a nonzero exit blocks control. The optional **Compare
existing Vitest failures** mode supports a single explicitly selected Vitest
command. For example, an npm project can choose:

```json
{
  "commands": ["npm run typecheck", "npm run build", "npm run test -- --run src"],
  "unitCommand": "npm run test -- --run src",
  "compareExistingFailures": true,
  "deferredChecks": "Browser E2E need a separately prepared server and credentials"
}
```

`src` is an explicit user-selected filter, not an automatic claim that every
local test lives there. Add other directories or commands where appropriate.
The CLI accepts this JSON through `begin --validation-profile <file>`.

The initial isolated control records the JSON report; this observation alone
cannot accept C0. Only when every other selected command passes and Vitest has
complete individual-case evidence does DepLoom seal the baseline and run a fresh
authoritative comparison. The baseline content hash is part of the project-check
command identity and is checked before reuse. Resolver, runtime and source
identity guards remain active. A repair-only run still requires genuinely
passing checks; comparing old failures cannot claim that a repair fixed them.

Every later comparison rejects:

- a newly failing test or a different failure for an existing failing test;
- a missing/renamed original test or an executed test becoming skipped;
- collection/runtime errors, absent or inconsistent reports, unsupported exit
  codes, or a missing/modified baseline.

Vitest 0.34 can report a collection-error file as `status=passed` with an empty
assertion list. Such a suite is unavailable evidence, never an accepted baseline.
Infrastructure/evidence failures do not teach dependency incompatibilities.
Other commands remain strict; their exit code alone is not enough to compare
individual failures.

`validationScope.mode=test-nonregression` means **selected checks and absence of
new Vitest failures**, not an entirely green project. Initially failing and
skipped counts remain visible, and deferred E2E are not confirmed. Baselines and
per-comparison case evidence live in `validation-evidence/`; hashes, scope and
references accompany checkpoints and the task. The audit remains independent
and is not weakened by this mode.
