import { readFileSync } from "node:fs";
import { EventEmitter } from 'node:events';
import { runInNewContext } from 'node:vm';
import assert from 'node:assert/strict';
import { nextUpdaterStatus } from '../dist-electron/updater-state.js';
import updaterPackage from "electron-updater";

const descriptor = Object.getOwnPropertyDescriptor(updaterPackage, "autoUpdater");

if (!descriptor || (typeof descriptor.get !== "function" && !descriptor.value)) {
  throw new Error("electron-updater default export does not expose autoUpdater");
}

const compiledMain = readFileSync(new URL("../dist-electron/main.js", import.meta.url), "utf8");
if (/import\s*\{[^}]*\bautoUpdater\b[^}]*\}\s*from\s*["']electron-updater["']/.test(compiledMain)) {
  throw new Error("compiled Electron main uses an invalid named autoUpdater import");
}
if (!compiledMain.includes("import updaterPackage from 'electron-updater'")) {
  throw new Error("compiled Electron main is missing the CommonJS-compatible updater import");
}

if (!compiledMain.includes("autoUpdater.autoInstallOnAppQuit = false")) {
  throw new Error("normal app quit must not install a cached update");
}
if (!compiledMain.includes("const visibleStatus = downloadedUpdateVersion")) {
  throw new Error("a transient feed error must keep an already downloaded update installable");
}
const refreshIndex = compiledMain.indexOf("const result = await checkForLatestUpdate()");
const waitIndex = compiledMain.indexOf("await (result.downloadPromise ?? autoUpdater.downloadUpdate())", refreshIndex);
const installIndex = compiledMain.indexOf("autoUpdater.quitAndInstall(false, true)", waitIndex);
if (refreshIndex < 0 || waitIndex < 0 || installIndex < 0 || !(refreshIndex < waitIndex && waitIndex < installIndex)) {
  throw new Error("explicit update install must refresh the feed and await the latest package");
}

const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const control = readFileSync(new URL('../src/components/AppUpdateControl.tsx', import.meta.url), 'utf8');
if (!appSource.includes('<AppUpdateControl')) throw new Error('updater status must remain visible in the shell');
for (const state of ['idle', 'checking', 'available', 'downloading', 'ready', 'error']) {
  if (!control.includes("status.state === '" + state + "'")) throw new Error('missing updater state: ' + state);
}
if (!control.includes('disabled={restartBlocked || stopping}') || !control.includes('onInstall') || !control.includes('onCheck')) throw new Error('explicit restart and retry controls are required');
if (!control.includes("typeof status.percent === 'number'")) throw new Error('unknown download progress must stay indeterminate');
const guards = compiledMain.slice(compiledMain.indexOf('async function installLatestUpdate()'), compiledMain.indexOf('function findWorkspace('));
if ((guards.match(/if \(updateRestartBlocked\(\)\)/g) || []).length !== 2) throw new Error('restart must check activity both before and after the async download');
for (const owner of ['iterativeStepInFlight.has(', 'iterativeAutopilot.hasSession(', 'initialAuditJobs', 'storageCleanup.status()']) {
  if (!compiledMain.includes(owner)) throw new Error('restart activity owner missing: ' + owner);
}

// Exercise the compiled production listeners and publisher together: discovery
// events may repeat during a download, after caching, or during install refresh.
const publisherStart = compiledMain.indexOf('function publishUpdateStatus(');
const publisherEnd = compiledMain.indexOf('function checkForLatestUpdate(', publisherStart);
const listenersStart = compiledMain.indexOf('function setupAutoUpdater(');
const listenersEnd = compiledMain.indexOf('async function createWindow(', listenersStart);
assert.ok(publisherStart >= 0 && publisherEnd > publisherStart && listenersStart >= 0 && listenersEnd > listenersStart);
const updater = new EventEmitter();
const statuses = [];
const context = {
  app: { isPackaged: true }, autoUpdater: updater, nextUpdaterStatus,
  currentUpdateStatus: { state: 'idle' }, downloadedUpdateVersion: undefined, updateCheckInFlight: undefined,
  send: (channel, status) => { assert.equal(channel, 'flow:update-status'); statuses.push(status); },
  updateErrorStatus: (error) => ({ state: 'error', message: error.message }),
  stopUpdateChecks: undefined, scheduleUpdateChecks: () => () => {},
};
runInNewContext([compiledMain.slice(publisherStart, publisherEnd), compiledMain.slice(listenersStart, listenersEnd), 'setupAutoUpdater();'].join(String.fromCharCode(10)), context);
updater.emit('checking-for-update');
assert.equal(context.currentUpdateStatus.state, 'checking');
updater.emit('update-available', { version: '1.0.0-preview.4' });
assert.equal(context.currentUpdateStatus.state, 'downloading');
assert.equal(context.currentUpdateStatus.percent, undefined, 'initial download progress stays indeterminate');
updater.emit('download-progress', { percent: 32 });
const downloading = context.currentUpdateStatus;
const beforeRepeatedCheck = statuses.length;
updater.emit('checking-for-update');
updater.emit('update-available', { version: '1.0.0-preview.4' });
assert.equal(context.currentUpdateStatus, downloading, 'rechecking the same download preserves progress');
assert.equal(statuses.length, beforeRepeatedCheck, 'repeated discovery must not flash or emit duplicate status');
updater.emit('update-downloaded', { version: '1.0.0-preview.4' });
const ready = context.currentUpdateStatus;
updater.emit('checking-for-update');
updater.emit('update-available', { version: '1.0.0-preview.4' });
updater.emit('download-progress', { percent: 100 });
updater.emit('update-not-available', { version: '1.0.0-preview.3' });
assert.equal(context.currentUpdateStatus, ready, 'late discovery/progress cannot hide an installable package');
assert.equal(context.downloadedUpdateVersion, '1.0.0-preview.4', 'feed result cannot erase the cached package');
updater.emit('error', new Error('temporary feed error'));
assert.equal(context.currentUpdateStatus.state, 'ready');
assert.equal(context.currentUpdateStatus.message, 'temporary feed error');
updater.emit('checking-for-update');
assert.equal(context.currentUpdateStatus.message, 'temporary feed error', 'starting a check does not prove recovery');
updater.emit('update-not-available', { version: '1.0.0-preview.4' });
assert.equal(context.currentUpdateStatus.state, 'ready');
assert.equal(context.currentUpdateStatus.message, undefined, 'successful feed check clears stale error');
context.updateCheckInFlight = Promise.resolve();
updater.emit('error', new Error('transient request failure'));
assert.equal(context.currentUpdateStatus.message, undefined, 'in-flight request owns its retry result');
context.updateCheckInFlight = undefined;
updater.emit('checking-for-update');
updater.emit('update-available', { version: '1.0.0-preview.5' });
assert.equal(context.currentUpdateStatus.state, 'downloading', 'a newer version starts a fresh download');
assert.equal(context.currentUpdateStatus.version, '1.0.0-preview.5');
assert.equal(context.currentUpdateStatus.percent, undefined);
assert.equal(context.currentUpdateStatus.message, undefined);
updater.emit('update-downloaded', { version: '1.0.0-preview.5' });
assert.equal(context.currentUpdateStatus.state, 'ready');
assert.equal(context.downloadedUpdateVersion, '1.0.0-preview.5');
assert.ok(!statuses.some((status) => status.state === 'available'), 'auto-download never flashes Update available');
assert.equal(nextUpdaterStatus({ state: 'checking' }, { state: 'available', version: '2.0.0' }, false).state, 'available', 'manual-download discovery remains available');
console.log("electron-updater runtime, monotonic package state and explicit-install policy OK");
