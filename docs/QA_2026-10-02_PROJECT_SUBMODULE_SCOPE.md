# Project-scoped submodule readiness (2026-10-02)

The preflight previously inspected the selected package directory while capture
checked every submodule of the containing Git repository. An independent nested
frontend could pass readiness and then fail capture because an unrelated sibling
submodule was not initialized.

Both paths now resolve the Git root and use the same readiness scope: the selected
package, owning workspace manifests, and recursively declared file/link/portal
inputs. If topology cannot be bound, the repository-wide guard stays enabled.
Repository-root projects and Git conflicts retain their strict checks.

This is a readiness change, not a source exclusion. The whole existing source
and Git metadata are still sealed and hashed, including initialized submodule
bytes. Missing sibling contents remain absent. No submodule is fetched and no
user checkout is changed. Real install and project checks still decide whether
that captured state passes. The source policy identity was revised to prevent
reuse of evidence from the previous policy.

## Validation

- `python -m unittest discover -s tests/regression -p test_project_submodule_scope.py -v`:
  8 passed. Real Git repositories, real capture and real CLI/Node verification;
  independent sibling passes, a command reading the absent sibling fails;
  direct/transitive local dependencies, an internal submodule, root scope,
  workspace scope and conflicts remain blocked.
- `python -m unittest discover -s tests/regression -p test_iterative_preflight_readiness.py -v`:
  10 passed; existing early readiness and machine-readable recovery contracts.
- `python run_tool_tests.py --suite all`: 1587 tests, OK (4 skipped).
- `python run_tool_tests.py --suite production-fast`: 64 passed.
- `python -m unittest discover -s tests -p test_block_x_source_truth.py -v`:
  15 tests, OK (1 skipped: Windows symlink privilege).
- The nested-project recovery command was exercised on a real local Git fixture.
- Public sanitization covers the new test and this document. Desktop release
  gates run separately on the exact release commit; ignored local logs are not
  release artifacts.

A Git update command for a required submodule now explicitly uses the selected
project as its working directory and a top-level pathspec, so nested package
paths do not accidentally resolve the submodule relative to that package.
