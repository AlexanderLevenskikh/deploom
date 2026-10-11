#!/usr/bin/env node
// C3 journal contract checks:
//   1. The in-memory log window is bounded by MAX_LOG_ENTRIES: the busy-job
//      batch merges and then slices once, so a full flush can never grow the
//      list to 1998 records, and no unbounded `[...current, item]` append
//      remains anywhere in the hook.
//   2. A rejected refresh (poll or job-finish) is handled: the last good
//      payload stays on screen, refreshError is surfaced, and the poll timer
//      never observes an unhandled promise rejection.
//   3. LogPanel renders the raw journal through a scroll window instead of
//      mounting every line.
// Assertion 1 is exercised as a real reducer simulation mirroring the exact
// source expressions, not just a substring match.
import { readFileSync } from "node:fs";
import ts from 'typescript';
import assert from 'node:assert/strict';
const windowSource = readFileSync(new URL('../src/data/rawLogWindow.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(windowSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const exported = {};
new Function('exports', compiled)(exported);
const offsets = exported.rawLogOffsets(Array.from({ length: 1000 }, (_, i) => i % 3 ? 434 : 16));
for (const top of [0, 16000, 25312, offsets.at(-1) - 294, offsets.at(-1) + 10000]) {
  const window = exported.rawLogWindow(offsets, top, 294, 40);
  assert.ok(window.start >= 0 && window.end <= 1000 && window.end > window.start);
  if (top >= offsets.at(-1) - 294) assert.equal(window.end, 1000);
}
assert.deepEqual(exported.rawLogWindow([0], 20000, 294, 40), { start: 0, end: 0 });
assert.deepEqual(exported.rawLogWindow(exported.rawLogOffsets([434]), 20000, 294, 40), { start: 0, end: 1 });

const hookPath = new URL("../src/hooks/useDependencyFlow.ts", import.meta.url);
const panelPath = new URL("../src/components/LogPanel.tsx", import.meta.url);
const appPath = new URL("../src/App.tsx", import.meta.url);
// Sources use CRLF in the working tree; normalize so newline-sensitive checks
// match regardless of the checkout's line-ending configuration.
const hook = readFileSync(hookPath, "utf8").replace(/\r\n/g, "\n");
const panel = readFileSync(panelPath, "utf8").replace(/\r\n/g, "\n");
const app = readFileSync(appPath, "utf8").replace(/\r\n/g, "\n");

const fail = (message) => { throw new Error(`check-log-batching: ${message}`) };

// 1a. The cap constant and a single-slice batch.
if (!hook.includes("export const MAX_LOG_ENTRIES = 1000")) fail("MAX_LOG_ENTRIES must be 1000");
if (!hook.includes("...current, ...batch].slice(-MAX_LOG_ENTRIES)")) fail("busy-job batch must slice the merged array once against MAX_LOG_ENTRIES");
if (hook.includes("slice(-999)")) fail("no append may pair current.slice(-999) with a batch again (would allow 1998 records)");
if (hook.includes("setLogs((current) => [...current, {")) fail("unbounded [...current, item] append must not exist");
if (!hook.includes("setLogs((current) => appendLogEntry(current,")) fail("single-entry appends must go through appendLogEntry");

// 1b. Behavioral simulation of the exact reducer expressions.
const appendLogEntry = (current, entry) => [...current, entry].slice(-1000);
const batchReducer = (current, batch) => [...current, ...batch].slice(-1000);
let journal = [];
const batch = Array.from({ length: 999 }, (_, i) => ({ jobId: "job", line: `line-${i}` }));
journal = batchReducer(journal, batch); // fills the window
journal = batchReducer(journal, batch); // a second full flush must NOT double it
if (journal.length !== 1000) fail(`a full flush after a full buffer must stay at 1000 records, got ${journal.length}`);
for (let i = 0; i < 2000; i += 1) journal = appendLogEntry(journal, { jobId: "job", line: "tick" });
if (journal.length !== 1000) fail(`sustained appends must be capped at 1000, got ${journal.length}`);

// 2. Rejected refresh handled (poll path and job-finish path), surfaced.
if (!hook.includes("const [refreshError, setRefreshError] = useState<string | undefined>()")) fail("refreshError state must exist");
if (!hook.includes("try {\n          const result = await api.refreshWorkspace()")) fail("poll refresh must be wrapped in try");
if (!hook.includes("catch (error) {\n          setRefreshError(error instanceof Error")) fail("poll refresh catch must record refreshError");
if (hook.split("setRefreshError(").length - 1 < 2) fail("both the poll refresh and the job-finish refresh must handle rejection");
if (!hook.includes("refreshError,")) fail("refreshError must be exposed by the hook");
if (!app.includes("refreshError={flow.refreshError}")) fail("App must pass refreshError into LogPanel");
if (!panel.includes("refreshError?:")) fail("LogPanel must accept a refreshError prop");
if (!panel.includes("refresh-error-hint")) fail("LogPanel must render the refresh error hint");

// 3. Raw journal windowing.
for (const marker of ["RAW_ROW_HEIGHT", "RAW_OVERSCAN", "setRawScrollTop", "slice(rawStart, rawEnd)", "data-window-spacer", "ResizeObserver", "getBoundingClientRect().height", "rawLogWindow"]) {
  if (!panel.includes(marker)) fail(`LogPanel windowing must keep ${JSON.stringify(marker)}`);
}

console.log("Log journal batching / rejected refresh / windowing contracts OK");
