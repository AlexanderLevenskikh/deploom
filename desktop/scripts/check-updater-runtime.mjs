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

const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const appStyles = readFileSync(new URL("../src/App.css", import.meta.url), "utf8");
if (!appSource.includes("<Download size={17} />") || !appSource.includes("update-download-button") || !appSource.includes("disabled={!updateReady}")) {
  throw new Error("update action must be a compact download icon enabled only when ready");
}
if (!appStyles.includes(".update-download-button.ready")) throw new Error("ready update icon must have an active visual state");

console.log("electron-updater runtime and explicit-install policy OK");
