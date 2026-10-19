import assert from 'node:assert/strict';
import runtime from 'builder-util-runtime';
import github from 'electron-updater/out/providers/GitHubProvider.js';
import { checkUpdateFeed, INITIAL_UPDATE_CHECK_DELAY_MS, UPDATE_CHECK_INTERVAL_MS, scheduleUpdateChecks } from "../dist-electron/updater-schedule.js";

if (INITIAL_UPDATE_CHECK_DELAY_MS !== 10_000) throw new Error(`Initial update check must run after 10s, got ${INITIAL_UPDATE_CHECK_DELAY_MS}`);
if (UPDATE_CHECK_INTERVAL_MS !== 30 * 60 * 1_000) throw new Error(`Background update interval must be 30m, got ${UPDATE_CHECK_INTERVAL_MS}`);

const calls = [];
const cleared = [];
const callbacks = [];
const scheduler = {
  setTimeout(callback, delay) { calls.push(["timeout", delay]); callbacks.push(callback); return { id: "timeout" }; },
  setInterval(callback, delay) { calls.push(["interval", delay]); callbacks.push(callback); return { id: "interval" }; },
  clearTimeout(handle) { cleared.push(handle.id); },
  clearInterval(handle) { cleared.push(handle.id); },
};
let checks = 0;
const stop = scheduleUpdateChecks(() => { checks += 1; }, scheduler);
if (calls[0]?.[1] !== INITIAL_UPDATE_CHECK_DELAY_MS || calls[1]?.[1] !== UPDATE_CHECK_INTERVAL_MS) throw new Error(`Unexpected updater schedule: ${JSON.stringify(calls)}`);
callbacks.forEach((callback) => callback());
if (checks !== 2) throw new Error(`Scheduled callbacks did not check for updates: ${checks}`);
stop();
if (cleared.join(",") !== "timeout,interval") throw new Error(`Updater timers were not cleared: ${cleared}`);
// Exercise the production retry policy with a controlled feed and no delays.
let attempts = 0;
const delays = [];
const result = await checkUpdateFeed(async () => {
  attempts += 1;
  if (attempts < 3) throw Object.assign(new Error('connection lost'), { code: 'ECONNRESET' });
  return { version: '2.0.0' };
}, async delay => { delays.push(delay); });
assert.equal(result.version, '2.0.0');
assert.equal(attempts, 3);
assert.deepEqual(delays, [2000, 8000]);
for (const message of ['HTTP error: 503', 'status code: 429', 'HTTP error: 504']) {
  let calls = 0;
  const failure = new Error(message);
  await assert.rejects(checkUpdateFeed(async () => { calls += 1; throw failure; }, async () => {}), error => error === failure);
  assert.equal(calls, 3, 'transient failures exhaust a finite retry budget');
}
for (const message of ['HTTP error: 401', 'HTTP error: 403', 'HTTP error: 404', 'sha512 checksum mismatch', 'Cannot parse update info', 'disk permission denied']) {
  let calls = 0;
  const failure = new Error(message);
  await assert.rejects(checkUpdateFeed(async () => { calls += 1; throw failure; }, async () => { assert.fail('permanent failure must not sleep'); }), error => error === failure);
  assert.equal(calls, 1);
}
// Use the actual transport error emitted by electron-updater's HTTP layer.
for (const status of [401, 403, 404, 429, 500, 502, 503, 504]) {
  let calls = 0;
  const failure = new runtime.HttpError(status, `${status}: test response mentions ECONNRESET`);
  await assert.rejects(checkUpdateFeed(async () => { calls++; throw failure; }, async () => {}), error => error === failure);
  assert.equal(calls, [429, 500, 502, 503, 504].includes(status) ? 3 : 1);
}
// GitHubProvider wraps some HTTP failures and drops statusCode on the outer
// error. Exercise the real provider, replacing only its network transport.
for (const status of [401, 404, 429, 503]) {
  let latestRequests = 0;
  const provider = new github.GitHubProvider({provider:'github',owner:'fixture',repo:'fixture'},
    {allowPrerelease:false,currentVersion:'1.0.0',fullChangelog:false},
    {platform:'win32',executor:{request:async options=>{
      if(options.path.endsWith('.atom'))return '<feed><entry><title>Fixture</title><link href="https://github.com/fixture/fixture/releases/tag/v2.0.0"/></entry></feed>';
      latestRequests++;
      throw new runtime.HttpError(status, `${status} ${status===401?'ECONNRESET text in an auth response':'service unavailable'}`);
    }}});
  await assert.rejects(checkUpdateFeed(()=>provider.getLatestVersion(),async()=>{}));
  assert.equal(latestRequests,[429,503].includes(status)?3:1,`wrapped HTTP ${status} must preserve retry classification`);
}
let successfulChecks = 0;
assert.equal(await checkUpdateFeed(async () => { successfulChecks += 1; return null; }, async () => assert.fail('successful check must not sleep')), null);
assert.equal(successfulChecks, 1);
console.log("Background updater schedule and bounded feed recovery OK");
