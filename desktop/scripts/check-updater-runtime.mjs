import { readFileSync } from "node:fs";
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
if (!control.includes('disabled={blocked}') || !control.includes('onInstall') || !control.includes('onCheck')) throw new Error('explicit restart and retry controls are required');
if (!control.includes("typeof status.percent === 'number'")) throw new Error('unknown download progress must stay indeterminate');
const guards = compiledMain.slice(compiledMain.indexOf('async function installLatestUpdate()'), compiledMain.indexOf('function findWorkspace('));
if ((guards.match(/if \(updateRestartBlocked\(\)\)/g) || []).length !== 2) throw new Error('restart must check activity both before and after the async download');
for (const owner of ['iterativeStepInFlight.size', 'iterativeAutopilot.hasActiveSessions()', 'projectAudits.active()', 'storageCleanup.status().phase']) {
  if (!compiledMain.includes(owner)) throw new Error('restart activity owner missing: ' + owner);
}

console.log("electron-updater runtime and explicit-install policy OK");
