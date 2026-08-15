# Public release checklist

- [ ] Run `python scripts/check-public-sanitization.py`.
- [ ] Run `python run_tool_tests.py --suite all`.
- [ ] Run Desktop install/lint/build/contract checks.
- [ ] Manually review every new or changed screenshot/binary and update the binary allowlist only after review.
- [ ] Confirm `desktop/package-lock.json` resolves only through intended public registry hosts.
- [ ] Run `npm run check:public-update-target` from `desktop/`; updater/release coordinates must be `AlexanderLevenskikh/deploom`.
- [ ] **Publish `AlexanderLevenskikh/deploom` from this sanitized working tree with fresh Git history only. Use `./push-github-branch-and-tag.ps1 -Commit "Initial public release: DepLoom 0.2.0" -Tag v0.2.0`; it enforces branch `master` and SSH remote `git@github.com:AlexanderLevenskikh/deploom.git`. Do not mirror/push private Git history, old branches, tags, stashes, CI artifacts or releases.**
- [ ] Enable GitHub secret scanning/push protection and private vulnerability reporting where available.

- [ ] Choose and add an explicit open-source license before inviting external reuse/contributions; a public repository without a license does not grant general reuse rights.
