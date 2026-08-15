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

console.log("electron-updater CommonJS/ESM interop OK");
