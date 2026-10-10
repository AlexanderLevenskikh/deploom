# Modern Yarn

DepLoom supports Yarn Classic 1.x and pinned Yarn 2/3/4 with `nodeLinker: node-modules`. Keep the owning root `yarn.lock`, `.yarnrc.yml` and exact `packageManager: yarn@X.Y.Z`. The selected workspace can be nested; install runs at its declared manager root and project checks run in the selected directory. Local `yarnPath` executables are preserved in isolated source copies, even when ignored alongside Yarn caches.

Classic still uses `--ignore-scripts` for resolution and `--frozen-lockfile` for lifecycle installation. Modern Yarn resolves with `--skip-builds` (2.x) or `--mode=skip-build` (3/4), then runs scripts-enabled `install --immutable` over the captured exact lockfile. These phases retain the normal source, assignment, lockfile and installed-content proof guards. Classic resolver seed acceleration is not reused for Berry. Yarn 3/4 lock sync uses native dedupe when enabled. Yarn 2 auto skips dedupe; required dedupe fails before updating because Yarn 2 cannot skip builds during that command.

The actual modern Yarn version must match the declared exact version. A mismatch is infrastructure, not dependency incompatibility. Use a local `yarnPath` or a working Corepack installation to select the project's version. DepLoom never silently changes the package manager or creates a package-lock in the project.

Modern lockfiles are parsed as YAML with duplicate descriptor rejection. Audit follows selected-package dependencies through the canonical root lockfile. It sends an exact version inventory to the configured npm security endpoint via the existing isolated inventory adapter; it does not ask npm to resolve Yarn ranges. Missing graph edges remain incomplete evidence. Root manifest/lock/config changes invalidate workspace audit identity in both Python and Desktop.

PnP and the Yarn `pnpm` linker are not supported by the installed node_modules proof model. Unversioned modern runtimes and unvalidated future major versions remain explicitly unsupported. npm and Classic behavior is retained; support for the pnpm package manager is a separate feature.

CI installs isolated pinned Yarn 2.4.3, 3.8.7 and 4.0.0 runtimes without replacing Classic. Physical acceptance uses a local registry, a real nested Git workspace, a package lifecycle script and exact installed-version checks. To run locally, prepare runtimes with `node scripts/prepare-yarn-test-runtimes.mjs`, set `DEPLOOM_TEST_YARN_ROOT` to the printed path, then run `python -m unittest tests.acceptance.test_modern_yarn_physical -v`.
