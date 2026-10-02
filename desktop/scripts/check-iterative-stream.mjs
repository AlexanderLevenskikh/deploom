// Contract checks for the TESTABLE streaming runner (iterative-stream.ts),
// which main.ts adopts for the long iterative begin/drive steps. These exercise
// the launch boundaries the review asked to cover with a REAL child:
//   - a watchdog KILL (timeout or user cancel) must never surface as a zero
//     exit code or as success — it is a non-zero, timedOut result;
//   - a status/JSON line SPLIT across stdout chunks (or left unterminated at
//     close) must be buffered and reassembled, journaled and emitted once —
//     never dropped and never parsed as two broken fragments;
//   - plain output lines are still journaled line-by-line.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { collectStreamedLines, spawnIterativeStreamed } from "../dist-electron/iterative-stream.js";

const SLEEPY = `setTimeout(() => {}, 60000)`;
const STATUS_NO_NEWLINE = `process.stdout.write('ITERATIVE_MIGRATION_STATUS_V1 {"event":"begin.discovery-progress","processed":1,"total":9}')`;
const STATUS_TWO_WRITES = `process.stdout.write('ITERATIVE_MIGRATION_STATUS_V1 {"event":"begin.'); setTimeout(() => process.stdout.write('discovery-progress","processed":2,"total":4}\\n'), 40)`;
const PLAIN = `console.log('plain line')`;

const root = mkdtempSync(join(tmpdir(), "iter-stream-contract-"));

const mkPlatform = () => ({
  processTreeDetached: () => false,
  commandEnvironment: (env) => env,
  resolveSpawnInvocation: (command, args) => ({ command, args }),
  killProcessTree: (child) => { try { child.kill(); } catch { /* already gone */ } },
  decodeChunk: (chunk) => chunk.toString("utf8"),
});

const mkIo = (overrides = {}) => {
  const lines = [];
  return {
    lines,
    io: {
      cancelRequested: () => false,
      recordLine: (_dir, text) => { lines.push(text); },
      trimLog: () => {},
      ...overrides,
    },
  };
};

// 1. Line buffering primitive: a JSON status line arriving in TWO chunks must
// be reassembled; only COMPLETE lines are emitted; a trailing fragment stays
// buffered for the next chunk or the close flush.
{
  const first = collectStreamedLines("", 'ITERATIVE_MIGRATION_STATUS_V1 {"event":"begin.');
  if (first.lines.length !== 0) throw new Error("an incomplete line must not be emitted early");
  const second = collectStreamedLines(first.carry, 'discovery-progress","processed":1,"total":9}\nnext\npartial');
  if (second.lines.length !== 2) throw new Error("only complete lines are emitted");
  if (!second.lines[0].startsWith("ITERATIVE_MIGRATION_STATUS_V1") || !second.lines[0].includes('"total":9')) {
    throw new Error("a JSON line split across chunks must be reassembled whole");
  }
  if (second.lines[1] !== "next") throw new Error("a whole plain line must pass through");
  if (second.carry !== "partial") throw new Error("a trailing fragment must stay buffered");
}

// 2. Timeout: the watchdog kills a child that ignores its budget. The kill must
// NEVER surface as a zero exit code (that is the review repro that let a
// timed-out begin claim "C0 captured") and must be signalled as timedOut.
{
  const { io } = mkIo();
  const result = await spawnIterativeStreamed(join(root, "t"), process.execPath, ["-e", SLEEPY], root, 250, io, mkPlatform());
  if (result.code === 0) throw new Error("a watchdog kill must never report a zero exit code");
  if (!result.timedOut) throw new Error("a watchdog kill must be signalled as timedOut");
}

// 3. Unterminated status line flushed on close: a child that exits WITHOUT a
// trailing newline still has its last status line journaled and parsed once.
{
  const events = [];
  const { io, lines } = mkIo();
  const result = await spawnIterativeStreamed(join(root, "s"), process.execPath, ["-e", STATUS_NO_NEWLINE], root, 10_000, io, mkPlatform(), (payload) => events.push(payload));
  if (result.code !== 0 || result.timedOut) throw new Error("clean exit expected");
  if (events.length !== 1 || events[0].event !== "begin.discovery-progress" || events[0].total !== 9) {
    throw new Error("an unterminated status line must be flushed and parsed on close");
  }
  if (!lines.some((line) => line.includes("ITERATIVE_MIGRATION_STATUS_V1"))) throw new Error("the flushed line must be journaled too");
}

// 4. A status line written across two stdout writes yields exactly ONE event.
{
  const events = [];
  const { io } = mkIo();
  const result = await spawnIterativeStreamed(join(root, "w"), process.execPath, ["-e", STATUS_TWO_WRITES], root, 10_000, io, mkPlatform(), (payload) => events.push(payload));
  if (result.code !== 0 || result.timedOut) throw new Error("clean exit expected");
  if (events.length !== 1 || events[0].event !== "begin.discovery-progress" || events[0].processed !== 2) {
    throw new Error(`a status line split across chunks must yield ONE parsed event, got ${events.length}`);
  }
}

// 5. User cancel: flipping cancelRequested mid-run terminates the child and the
// result is a non-zero, timedOut outcome — the caller distinguishes cancel from
// a plain exit, and never treats it as success.
{
  let cancelled = false;
  const { io } = mkIo({ cancelRequested: () => cancelled });
  const promise = spawnIterativeStreamed(join(root, "c"), process.execPath, ["-e", SLEEPY], root, 0, io, mkPlatform());
  setTimeout(() => { cancelled = true; }, 100);
  const result = await promise;
  if (result.code === 0) throw new Error("a user cancel must never report a zero exit code");
  if (!result.timedOut) throw new Error("a user cancel must produce timedOut so callers treat it as a non-success");
}

// 6a. Zero means no overall deadline, rather than an immediate watchdog kill.
{
  const { io, lines } = mkIo();
  const result = await spawnIterativeStreamed(join(root, "unbounded"), process.execPath,
    ["-e", `console.log('preparation started'); setTimeout(() => console.log('prepared'), 800)`],
    root, 0, io, mkPlatform());
  if (result.code !== 0 || result.timedOut) throw Error("unbounded preparation was killed by a watchdog");
  if (!lines.some(line => line.includes('prepared'))) throw Error("preparation completion not journaled");
}

// 6. Plain output lines are journaled line-by-line (decode + record, no loss).
{
  const { io, lines } = mkIo();
  const result = await spawnIterativeStreamed(join(root, "p"), process.execPath, ["-e", PLAIN], root, 10_000, io, mkPlatform());
  if (result.code !== 0 || result.timedOut) throw new Error("clean exit expected");
  if (!lines.some((line) => line.includes("plain line"))) throw new Error("stdout lines must be journaled");
}

// 7. Panel wiring (review re-check P1#4 + P2#5): Cancel is gated on an
// IN-FLIGHT call (honest liveness), never on the restored journal status; the
// coordinator («Продолжить») is reachable from the run-state block BEFORE a
// task exists; the output log toggle appears as soon as an attempt exists and
// auto-refreshes while a run is emitting.
{
  const { readFileSync } = await import("node:fs");
  const main = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf8");
  const iterative = main.slice(main.indexOf("ipcMain.handle('flow:iterative:begin'"));
  if (iterative.includes('workspace.path, 20 * 60_000') || iterative.includes('workspace.path, 1_800_000') ||
      iterative.includes('45 * 60 * 1000') || iterative.includes('iteration < 50')) throw Error('interactive migration still has an overall deadline');
  if (!iterative.includes('const verify = await spawnIterativeStreamed(')) {
    throw Error('bootstrap repair re-verification must use the cancelable unbounded runner');
  }
  const panel = readFileSync(new URL("../src/components/IterativeTaskPanel.tsx", import.meta.url), "utf8");
  if (!panel.includes("const childAlive = stepBusy")) throw new Error("Cancel must be gated on an in-flight call (childAlive), not the journal status");
  if (!panel.includes("Review re-check P1#4: a durable run whose ТЗ does not exist yet")) {
    throw new Error("the run-state block must offer the coordinator before a task exists");
  }
  if (!panel.includes("onClick={() => void toggleLog()}")) throw new Error("the log toggle must be available as soon as an attempt exists");
  if (!panel.includes("Review re-check P2#5: while the output log is open")) {
    throw new Error("the log must auto-refresh while a run is emitting");
  }
}

console.log("check-iterative-stream: OK");
