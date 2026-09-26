#!/usr/bin/env node
// Run every "check:*" npm script declared in desktop/package.json.
//
// The desktop contract checks are the release gate: the local release helper
// (push-branch-and-tag.ps1) discovers the same scripts, and CI/release runs
// this runner so a newly-added check automatically becomes a gate everywhere
// instead of being left out of a hardcoded workflow list. Exits nonzero when
// any check fails; fails when package.json declares no checks at all.
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));
const names = Object.keys(pkg.scripts ?? {})
  .filter((name) => name.startsWith('check:'))
  .sort();

if (names.length === 0) {
  console.error('run-all-contract-checks: no check:* scripts found in package.json');
  process.exit(1);
}

const win = process.platform === 'win32';
let failed = 0;

for (const name of names) {
  const result = spawnSync('npm', ['run', name], {
    cwd: ROOT,
    stdio: 'inherit',
    shell: win,
  });
  if (result.error) {
    console.error(`run-all-contract-checks: cannot run ${name}: ${result.error.message}`);
    failed += 1;
  } else if (result.status !== 0) {
    console.error(`run-all-contract-checks: FAILED ${name} (exit ${result.status})`);
    failed += 1;
  }
}

if (failed > 0) {
  console.error(`run-all-contract-checks: ${failed}/${names.length} check(s) failed`);
  process.exit(1);
}

console.log(`run-all-contract-checks: all ${names.length} desktop contract checks passed`);
