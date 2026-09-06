# Verified Partial Completion + Graph Focus + Git Metadata Churn Closure

This block closes three production findings without changing proof authority.

## Verified partial completion

When the existing target-closure policy permits a safe best-effort release,
`VERIFIED_PARTIAL_SCOPE` is presented as a successful bounded result.

- **Finish with current verified result** uses the existing release gates/hooks.
- **Continue improving** reopens the persisted deferred-cohort queue without
  resetting its `keep-current` user policy.
- The existing publish stage pushes the integrated release branch and team
  state; temporary work/probe branches are not added to that publish command.

A later improvement pass is a new Baseline/verification epoch.

## Graph focus

The Graph toolbar can hide the project rail, hide Monitoring, or enter true
application-window focus mode. Escape exits focus mode.

## SourceSnapshot Git administrative churn

The production failure was conservative and published no proof, but `.git`
directory mtime changed repeatedly while hashing. Git uses transient `*.lock`
files for atomic updates, and read-only status/index refreshes may create them.

The closure does not exclude `.git`. Instead:

- `.git/**/.lock` / `*.lock` synchronization artifacts are excluded;
- copied lock files are removed from the private tree before sealing;
- Git directory stability uses persistent child names (excluding `*.lock`)
  rather than raw directory mtime;
- persistent Git entries and all non-lock Git file bytes remain hashed;
- sealed/live-final manifest equality remains mandatory;
- DepLoom's own SourceSnapshot Git reads set `GIT_OPTIONAL_LOCKS=0`.

Actual Git metadata changes still fail closed.
