import { spawnSync } from "node:child_process";
import { runClipboardProbe } from "./electron-clipboard-runner.mjs";
import { verifyClipboardRunnerContract } from "./electron-clipboard-runner-contract.mjs";
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

await verifyClipboardRunnerContract();
// Existing headless Linux policy is explicit; arbitrary spawn/sandbox failures
// on a GUI-capable host cannot silently skip this acceptance check.
if (process.platform === "linux" && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY) {
  console.log("check-electron-clipboard: SKIP (Linux has no DISPLAY or WAYLAND_DISPLAY)");
  process.exit(0);
}
let electronBin;
try {
  const electronImport = await import("electron");
  electronBin = electronImport.default ?? electronImport;
} catch (error) {
  console.error(`check-electron-clipboard: FAIL (electron package: ${error.message})`);
  process.exit(1);
}
if (typeof electronBin !== "string" || !electronBin) {
  console.error("check-electron-clipboard: FAIL (electron binary path unavailable)");
  process.exit(1);
}
const result = runClipboardProbe(() => spawnSync(electronBin, [PROBE, marker], {
  cwd: DESKTOP, encoding: "utf8", timeout: 90_000, windowsHide: true,
}), marker);
if (!result.ok) {
  console.error(`check-electron-clipboard: FAIL (${JSON.stringify(result)})`);
  process.exit(1);
}
console.log(`check-electron-clipboard: OK (real Electron clipboard round-trip, ${result.markerBytes} bytes; attempts=${result.attempts})`);
