import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { withStorageControlFile } from '../dist-electron/storage-control-file.js';
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const fixture = mkdtempSync(join(tmpdir(), 'deploom-storage-controls-'));
const wait = ms => new Promise(done => setTimeout(done, ms));
try {
  const verification = join(fixture, 'verification'); mkdirSync(verification);
  const keep = join(fixture, 'source.txt'); writeFileSync(keep, 'protected');
  let writeMode, observedPause, child;
  const paused = new Promise(done => { observedPause = done });
  const operationId = 'real-storage-handoff';
  const controls = { operationId, mode: () => 'pause', bind: write => { writeMode = write; write('pause'); return () => { writeMode = undefined } }, terminate: () => false };
  const running = withStorageControlFile(join(fixture, 'mailbox'), controls, payload => new Promise((done, fail) => {
    child = spawn(process.env.PYTHON || 'python', [join(repo, 'storage_cleanup_inventory.py'), 'inspect'], { cwd: repo, env: { ...process.env, DEPLOOM_VERIFICATION_ROOT: verification }, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; if (stdout.includes('STORAGE_CONTROL_V1 ') && stdout.includes('paused')) observedPause() });
    child.stderr.on('data', chunk => { stderr += chunk });
    child.on('error', fail);
    child.on('close', code => code === 0 ? done(stdout) : fail(new Error(stderr || 'Child exit ' + code)));
    child.stdin.end(JSON.stringify({ ...payload, workspaces: [] }));
  }));
  const deadline = setTimeout(() => child?.kill(), 20_000);
  try {
    await Promise.race([paused, running.then(() => { throw Error('Worker exited before pause acknowledgement') })]);
    assert.ok(existsSync(join(fixture, 'mailbox', operationId + '.json')));
    await wait(150);
    assert.ok(child && child.exitCode === null, 'paused worker remains owned');
    writeMode('cancel');
    const output = await running;
    const line = output.split(/\r?\n/).find(value => value.startsWith('STORAGE_RESULT_V1 '));
    assert.ok(line, 'real Python cancellation publishes a result');
    assert.equal(JSON.parse(line.slice('STORAGE_RESULT_V1 '.length)).canceled, true);
    assert.equal(existsSync(join(fixture, 'mailbox', operationId + '.json')), false);
    assert.equal(existsSync(keep), true);
  } finally { clearTimeout(deadline); if (child && child.exitCode === null) child.kill() }
  console.log('Storage controls: real Node mailbox / Python pause acknowledgement / cancellation / owned cleanup PASS');
} finally {
  assert.equal(dirname(fixture), resolve(tmpdir()));
  assert.ok(fixture.startsWith(join(resolve(tmpdir()), 'deploom-storage-controls-')));
  rmSync(fixture, { recursive: true, force: true });
}
