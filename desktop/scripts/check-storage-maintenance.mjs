import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, utimesSync, existsSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const fixture = mkdtempSync(join(tmpdir(), 'deploom-storage-check-'));
const root = join(fixture, 'verification');
const trials = join(root, 'trials');
mkdirSync(trials, { recursive: true });
const old = new Date(Date.now() - 48 * 3600 * 1000);
const stamp = BigInt(old.getTime()) * 1000000n;
function trash(id) {
  const path = join(trials, `.deploom-trial-trash-dependency-flow-baseline-verify-${id}-999-${stamp}`);
  mkdirSync(path); writeFileSync(join(path, 'data.txt'), 'private retired data');
  return path;
}
const garbage = trash('old'); utimesSync(garbage, old, old);
const protectedTree = trash('linked');
const outside = join(fixture, 'protected-source'); mkdirSync(outside);
writeFileSync(join(outside, 'keep.txt'), 'must remain');
symlinkSync(outside, join(protectedTree, 'link'), process.platform === 'win32' ? 'junction' : 'dir');
utimesSync(protectedTree, old, old);
const active = join(trials, 'dependency-flow-baseline-verify-active'); mkdirSync(active);
const checkpoint = join(root, 'checkpoints'); mkdirSync(checkpoint);
const invoke = action => JSON.parse(execFileSync(process.env.PYTHON || 'python', [join(repo, 'verification_storage_maintenance.py'), action], {
  cwd: repo, env: { ...process.env, DEPLOOM_VERIFICATION_ROOT: root }, encoding: 'utf8', timeout: 60000,
}));
try {
  const inspected = invoke('inspect');
  assert.equal(resolve(inspected.root), resolve(root)); assert.equal(inspected.eligible, 2);
  const cleaned = invoke('clean'); assert.equal(cleaned.removed, 1); assert.equal(cleaned.protected, 1);
  assert.equal(existsSync(garbage), false);
  for (const path of [active, checkpoint, protectedTree, join(outside, 'keep.txt')]) assert.ok(existsSync(path), path);
  console.log('Storage maintenance: real Python IPC payload, retired cleanup and junction protection PASS');
} finally {
  assert.equal(dirname(fixture), resolve(tmpdir()));
  assert.ok(fixture.startsWith(join(resolve(tmpdir()), 'deploom-storage-check-')));
  rmSync(fixture, { recursive: true, force: true });
}
