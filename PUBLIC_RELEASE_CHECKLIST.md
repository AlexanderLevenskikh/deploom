# Public release checklist

- [ ] Run `python scripts/check-public-sanitization.py`.
- [ ] Run `python run_tool_tests.py --suite all`.
- [ ] Run Desktop install/lint/build/contract checks.
- [ ] Manually review every new or changed screenshot/binary and update the binary allowlist only after review.
- [ ] Confirm `desktop/package-lock.json` resolves only through intended public registry hosts.
- [ ] Run `npm run check:public-update-target` from `desktop/`; updater/release coordinates must be `AlexanderLevenskikh/deploom`.
- [ ] **Create `AlexanderLevenskikh/deploom` from this sanitized working tree with fresh Git history. Do not mirror/push private Git history, old branches, tags, stashes, CI artifacts or releases. First public release tag: `v0.2.0`.**
- [ ] Enable GitHub secret scanning/push protection and private vulnerability reporting where available.

- [ ] Choose and add an explicit open-source license before inviting external reuse/contributions; a public repository without a license does not grant general reuse rights.
