# Iterative repair recovery

The repair agent runs in the private trial, not the user's checkout. Its file
changes are measured against a durable hash baseline; source repairs still
require authoritative verification before a cumulative checkpoint is accepted.

## Provider runtime and project dependencies

OpenCode may install/update its plugin under the selected trial project's
`.opencode/`. The repair guard recognizes only its plugin-only `package.json`
and matching npm v3 `package-lock.json`: explicit OpenCode provider, exact
project path, ordinary physical files, matching `@opencode-ai/plugin` version.
Missing files, links, additional manifest keys/dependencies or inconsistent
locks retain the ordinary guard. This is not an exemption for `.opencode/`
as a whole or for arbitrary nested manifests.

Those two runtime files cannot count as source repair evidence or appear in
agent feedback's changed-file list. They remain part of the source snapshot
used for fresh verification. Project manifests and lockfiles stay immutable.
The original durable baseline works after restart, including a trial whose
plugin installation was already updated by an earlier provider invocation.
No manual deletion or baseline rewrite is required for a recognized runtime.

Unavailable OpenCode models are rejected before dispatch. JSONL provider
errors, including errors from a process that exits zero, are reported as
provider failures; they cannot become successful repair feedback.

## Control and diagnostics

Autopilot can adopt a running manual step without dispatching that step again.
Disabling it leaves the current operation running and prevents subsequent
automatic steps. Stop cancels the active child and preserves checkpoints.
Concurrent status requests share one CLI read. Windows atomic state writes
retry short sharing violations with unique temporary files and retain the old
state on failure.

The upper activity panel shows recent readable messages. Run logs below show
a cumulative history; the UI reads only the latest 256 KB, while `run.log`
retains the complete history from this version onward. Existing trimmed log
history cannot be reconstructed. Full raw messages remain accessible in the
artifact. An agent gate is shown as a pause for repairs, not migration success.

## Validation

- `python run_tool_tests.py --suite all`: 1634 tests, 4 platform skips.
- `python run_tool_tests.py --suite production-fast`: 64 tests.
- `tests/regression/test_iterative_atomic_write.py` includes a real Windows
  reader that temporarily denies replacement of the durable state file.
- `check:iterative-agent` covers runtime updates against an existing baseline,
  unchanged project-manifest guards, inconsistent locks, provider failures and
  the actual Python feedback handoff.
- `check:iterative-scenario` covers adoption, cancellation, disabling and an
  identical repeated repair gate. Log checks cover persistence across attempts.
- Browser QA uses the rendered monitor harness with synthetic IPC and the real
  Autopilot coordinator: manual-to-Autopilot completion, unavailable-model
  refusal and cumulative-log presentation. It does not claim a completed
  migration of an external project or a paid provider run.
