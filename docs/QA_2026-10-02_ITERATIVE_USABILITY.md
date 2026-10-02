# Iterative migration usability acceptance — 2026-10-02

Reviewed base: `a17085322f622a58eb7f756c6bf4f207d63382da` (0.2.164).
Changes remain in the working tree. No release, tag or push is part of this task.
Environment: native Windows, Python 3.14.3, Node 24.18.0.

## Faults reproduced and corrected

1. The busy state marked checking and planning green before either finished.
   Pipeline projection now uses actual attempt phase and durable runner context.
2. The primary migration card was below technical details and logs. It now
   precedes both. Connectors are separate flexible siblings with consistent gaps.
3. Windows iterative precheck invoked bare `npm` through CreateProcess while
   control verification used a shell. The same shell command builder now owns
   both paths, including quoted paths, compound commands and runtime environment.
4. A checked project without saved targets launched a no-op. Starting now uses
   existing configured targets or automatic discovery as the fallback.
5. A fresh render allowed a launch before persisted status loaded. The primary
   control stays disabled until task/status reads finish.
6. A source tree changing during hashing escaped as INTERNAL. Transient changes
   receive bounded retries; persistent uncertainty remains an infrastructure
   verdict with an actionable retry message. Generated files are still hashed:
   proof coverage and ordinary source guards were not weakened.
7. INCONCLUSIVE feedback freed a failed candidate but did not stop the planner
   from proposing it repeatedly. Scheduling deferrals now record exact assignment
   fingerprints and are respected on the same verified base, separately from
   incompatibility constraints. A changed verified base permits reconsideration.
8. First finish output rendered checkpoints read before its final audit. Reports
   now reload checkpoint evidence after auditing, so first output has real metrics.
9. Result export required a second click, exposed artifact IDs as notices and
   elapsed time increased after a restart. Export-and-view now opens the dialog,
   ordinary success notices omit internal IDs, and finished elapsed time is frozen.
10. Removed the duplicate Stop control. Renamed legacy import to “Create an
    assignment from an earlier plan”, with a tooltip explaining re-verification.

## Real execution

The complete React app was tested in the in-app Browser through the actual
Electron IPC handlers and production Python CLI. A local QA bridge restricted
operations to an isolated demo project; no user project was mutated. It is not a
mock renderer or a fake verifier. QA bridge startup/restart and unsupported theme
persistence produced harness-only console errors; migration did not produce an
unhandled product exception.

Fresh UI path: Check project → Start update → automatic discovery → C0 → real
npm materialization → npm project checks → C1 → independent audit → COMPLETE →
View result → Copy assignment. Check took about 7 seconds, the update about
32 seconds on this small fixture. During checking only the check stage was active;
planning/updating were neutral. Result survived Electron restart. The copy button
confirmed “Copied” through the real Electron clipboard round-trip.

The retained 12-dependency demo separately exercises C0 → C11, two repairable
configuration contracts, rejection of a wrong repair and continued independent
updates after an impossible target. Every step starts a fresh Python process.
See [reproduction instructions](ITERATIVE_MIGRATION_DEMO.md). The final retained
run completed 69 CLI calls: 11 accepted upgrades, one unmet goal, one rejected
wrong repair, scheduling deferrals at C9/C10/C11 without repeating on the same
base. Final audit: PASS, lag-ok 83.3% (10/12), unknown coverage 0, vulnerability
packages 0. Its source Git status remained clean. The FIRST report now contains
these fresh audit metrics.

A second real fixture continuously added generated files under `build/WebApi`.
The check returned SOURCE_CAPTURE_UNSTABLE with no traceback, no run.json and an
instruction to stop the writer. Repeating the same command after stopping it
passed without any state cleanup.

## Validation

- Full final Python suite: `python run_tool_tests.py --suite all`: 1570 tests OK, 4 skipped, 921.150 seconds on the final Python implementation. Final process exit code: 0.
- Separate production-fast gate: 64 tests OK.
- Iterative regression set: 127 tests OK.
- Source identity / real npm shell regressions: 5 tests OK.
- Desktop final build, lint and relevant checks: `npm run build`, `npm run lint` (existing warnings only), and
  `check:iterative-scenario`, `check:iterative-begin`, `check:iterative-runner`,
  `check:iterative-stream`, `check:iterative-attempt`, `check:iterative-migration`,
  `check:task-clipboard`, `check:electron-clipboard`, `check:i18n`,
  `check:ui-lifecycle`, `check:human-flow`, `check:ui-shell` all passed.
- Diff whitespace and public sanitization: `git diff --check` and `python scripts/check-public-sanitization.py`
  passed. A supplementary run of the SAME scanner rules included all four new
  untracked source/documentation files, without modifying the denylist or index.

This validates the described fixtures and runtime boundaries. It does not prove
that every real monorepo, private registry, Node version or external agent works.
A continuously changing source tree still requires stopping the relevant writer
and retrying; it is not silently excluded from verification identity. Python 3.12
and Node 22 CI were not executed locally in this task.
