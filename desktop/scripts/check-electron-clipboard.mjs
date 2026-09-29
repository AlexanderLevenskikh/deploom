import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

// Acceptance item (follow-up part 3/4): the REAL Electron clipboard, written
// and read back under an Electron main process, on a synthetic marker. The
// DI-verified copy path (task-clipboard.ts) is already covered by
// check-task-clipboard.mjs with fake writers; this check proves the real
// device honours write then read-back byte equality on a multi-line Cyrillic
// payload. Nothing from any user's clipboard is inspected: the marker is
// generated here.
//
// In a display-less CI (Linux without DISPLAY/Wayland) the Electron app cannot
// come up; the check prints a SKIP with the concrete spawn error instead of
// faking green or failing an environment limitation.

const DESKTOP = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PROBE = join(DESKTOP, "scripts", "electron-clipboard-probe.cjs");

const marker = [
  "# Задание на адаптацию зависимостей",
  "",
  "Обновите `is-number` до 7.0.0 и `is-finite` до 1.1.0.",
  "Отложено: `@scope/deferred` 1.2.0 -> 2.0.0 (PEER_RESOLUTION_DEFERRED).",
  "",
  "Проверка кириллицы, служебных символов и многострочности.",
].join("\n");

let electronBin;
try {
  const electronImport = await import("electron");
  electronBin = electronImport.default ?? electronImport;
} catch (error) {
  console.log(`check-electron-clipboard: SKIP (electron package not resolvable: ${error.message})`);
  process.exit(0);
}
if (typeof electronBin !== "string" || electronBin.length === 0) {
  console.log("check-electron-clipboard: SKIP (electron binary path not resolvable)");
  process.exit(0);
}

let raw;
try {
  raw = execFileSync(electronBin, [PROBE, marker], {
    cwd: DESKTOP,
    encoding: "utf8",
    timeout: 90_000,
    windowsHide: true,
  });
} catch (error) {
  const stderr = String(error?.stderr ?? error?.message ?? error);
  const guiUnavailable = /DISPLAY|display|sandbox|Gtk|gtk|X11|xcb/i.test(stderr);
  if (guiUnavailable || error?.code === "ENOENT") {
    console.log(`check-electron-clipboard: SKIP (Electron GUI unavailable here: ${stderr.split("\n")[0].trim()})`);
    process.exit(0);
  }
  console.log(`check-electron-clipboard: FAIL (spawn error: ${stderr.split("\n").slice(0, 3).join(" | ")})`);
  process.exit(1);
}

const line = raw.split("\n").find((item) => item.startsWith("CLIPBOARD_PROBE "));
if (!line) {
  console.log(`check-electron-clipboard: FAIL (no probe result in ${JSON.stringify(raw.slice(0, 200))})`);
  process.exit(1);
}
const payload = JSON.parse(line.slice("CLIPBOARD_PROBE ".length));
if (payload.ok !== true) {
  console.log(`check-electron-clipboard: FAIL (${JSON.stringify(payload)})`);
  process.exit(1);
}
if (typeof payload.markerBytes !== "number" || payload.markerBytes <= 0) {
  console.log(`check-electron-clipboard: FAIL (missing size ${JSON.stringify(payload)})`);
  process.exit(1);
}
console.log(`check-electron-clipboard: OK (real Electron clipboard round-trip, ${payload.markerBytes} bytes)`);
