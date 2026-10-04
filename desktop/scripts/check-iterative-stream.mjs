// Contract checks for the TESTABLE streaming runner (iterative-stream.ts),
// which main.ts adopts for the long iterative begin/drive steps. These exercise
// the launch boundaries the review asked to cover with a REAL child:
//   - a watchdog KILL (timeout or user cancel) must never surface as a zero
//     exit code or as success — it is a non-zero, timedOut result;
//   - a status/JSON line SPLIT across stdout chunks (or left unterminated at
//     close) must be buffered and reassembled, journaled and emitted once —
//     never dropped and never parsed as two broken fragments;
//   - plain output lines are still journaled line-by-line.
import { startAttempt, updateAttempt, recordAttemptProgress, readAttempt, readRunLogTail, recordAttemptLog, trimAttemptLog, attemptLogPath } from "../dist-electron/iterative-attempt.js";
import { mkdtempSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { startAttemptJournalPolling } from "../dist-electron/iterative-journal-poll.js";
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

// 6b. B6: the attempt.log byte cap holds DURING a chatty child, not only at
// close. A child that streams hundreds of KB while still running must not
// balloon the diagnostic artifact to its full unbounded size: the throttled
// trim keeps it bounded, and the close trim leaves the newest MAX_LOG_LINES.
{
  const dir = join(root, "chatty");
  startAttempt(dir, "demo", "none");
  const journalIo = {
    cancelRequested: () => false,
    recordLine: (d, line) => recordAttemptLog(d, line),
    trimLog: (d) => trimAttemptLog(d),
  };
  const logSize = () => { try { return statSync(attemptLogPath(dir)).size } catch { return 0 } };
  const chatty = `let i=0;const t=setInterval(()=>{console.log('payload ${"x".repeat(300)} '+i);if(++i===4000)clearInterval(t);},1);`;
  const promise = spawnIterativeStreamed(dir, process.execPath, ["-e", chatty], root, 0, journalIo, mkPlatform());
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 900));
  const midSize = logSize();
  if (midSize > 512 * 1024) throw new Error(`attempt.log grew unbounded mid-stream (${midSize} bytes)`);
  const result = await promise;
  if (result.code !== 0 || result.timedOut) throw new Error("chatty child must exit cleanly");
  const kept = statSync(attemptLogPath(dir)).size > 0 ? (await import("node:fs")).readFileSync(attemptLogPath(dir), "utf8").split(/\n/) : [];
  if (kept.length > 802) throw new Error(`attempt.log must be cap-trimmed after the chatty child, got ${kept.length} lines`);
  if (!kept.some((line) => line.endsWith(" 3999"))) throw new Error("trim must keep the newest chatty lines");
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
  if (!panel.includes("return startAttemptJournalPolling(")) {
    throw new Error("the panel must subscribe to the shared live journal poller");
  }
}

// 8. Exercise the actual renderer poller, without opening the inline output.
// It must refresh, survive a failed IPC read, serialize reads, and dispose safely.
{
  const observed = [];
  let reads = 0;
  let ready;
  const refreshed = new Promise(resolve => { ready = resolve; });
  const stop = startAttemptJournalPolling(async () => {
    if (++reads === 1) throw Error("transient read failure");
    return `current log ${reads}`;
  }, line => { observed.push(line); if (observed.length === 2) ready(); }, 5);
  let deadline;
  try {
    await Promise.race([refreshed, new Promise((_, reject) => { deadline = setTimeout(() => reject(Error("journal did not refresh")), 2000); })]);
    if (observed[0] !== "current log 2" || observed[1] !== "current log 3") throw Error("fresh messages were not applied");
  } finally { stop(); clearTimeout(deadline); }
  const before = reads;
  await new Promise(resolve => setTimeout(resolve, 20));
  if (reads !== before) throw Error("disposed journal still polls");
}
{
  let releaseRead;
  let reads = 0;
  let applications = 0;
  const delayed = new Promise(resolve => { releaseRead = resolve; });
  const stop = startAttemptJournalPolling(async () => { reads++; return await delayed; }, () => { applications++; }, 5);
  try {
    await new Promise(resolve => setTimeout(resolve, 25));
    if (reads !== 1) throw Error("journal reads overlap");
  } finally { stop(); releaseRead("late old attempt"); }
  await new Promise(resolve => setTimeout(resolve, 0));
  if (applications !== 0) throw Error("late IPC reply updated a disposed view");
}
// Real Python reporter -> streamed journal -> atomic phase/heartbeat projection.
{
  const dir = join(root, 'python-progress');
  startAttempt(dir, 'demo', 'none');
  updateAttempt(dir, {status:'running',stage:'drive',phase:'PRECHECK'});
  const events = [];
  const script = "import time,json; from iterative_progress import MigrationProgress; emit=lambda e: print('ITERATIVE_MIGRATION_STATUS_V1 '+json.dumps(e),flush=True)\nwith MigrationProgress(emit,operation='verify-exact',message='Preparing verification',run_id='r',candidate_id='c',interval=0.02) as progress:\n time.sleep(0.08)\n progress('project check 1/4 started: yarn typecheck')\n time.sleep(0.04)";
  const { io } = mkIo({recordLine: (_dir, line) => { const state=recordAttemptProgress(dir,line); if(state) events.push(state); }});
  const result = await spawnIterativeStreamed(dir, process.platform==='win32'?'python':'python3', ['-c',script], join(process.cwd(),'..'), 10000, io, mkPlatform());
  if(result.code!==0) throw Error(result.stderr);
  if(events.length<3 || !readAttempt(dir).phase.includes('yarn typecheck')) throw Error('Progress must update the durable phase while Python runs');
  if(!readRunLogTail(dir).includes('"heartbeat": true')) throw Error('Quiet work must reach the cumulative log');
  const before = readAttempt(dir);
  updateAttempt(dir,{status:'done'});
  if(recordAttemptProgress(dir,'ITERATIVE_MIGRATION_STATUS_V1 {"event":"migration.progress","message":"late"}\n')) throw Error('Late progress resurrected a terminal attempt');
  if(readAttempt(dir).phase!==before.phase) throw Error('Late progress changed a terminal phase');
}
// Long lines (including a newline-free stream) used to defeat both the RAM
// bound and the artifact cap. Full evidence must remain in cumulative run.log.
{
  const dir = join(root, "long-lines");
  startAttempt(dir, "demo", "none");
  const journalIo = { cancelRequested: () => false,
    recordLine: (d, line) => recordAttemptLog(d, line), trimLog: (d) => trimAttemptLog(d) };
  const result = await spawnIterativeStreamed(dir, process.execPath,
    ["-e", `for(let i=0;i<1024;i++)process.stdout.write('x'.repeat(8192)+'\\n');process.stdout.write('z'.repeat(2*1024*1024));process.stderr.write('e'.repeat(2*1024*1024));`],
    root, 10000, journalIo, mkPlatform());
  if(result.code!==0) throw Error(`long-line child failed: code=${result.code}, timedOut=${result.timedOut}, stderr=${result.stderr.slice(-300)}`);
  if(Buffer.byteLength(result.stdout)>1024*1024 || Buffer.byteLength(result.stderr)>1024*1024) throw Error("capture is not byte-bounded");
  if(statSync(attemptLogPath(dir)).size>128*1024) throw Error("long lines defeated the attempt.log byte cap");
  if(statSync(join(dir,"run.log")).size<12*1024*1024) throw Error("full cumulative evidence was lost");
}
// Batching plain output must not swallow or reorder durable status events.
{
  const status = n => 'ITERATIVE_MIGRATION_STATUS_V1 ' + JSON.stringify({event:'migration.progress',message:`stage ${n}`}) + '\n';
  const payload = 'first\nsecond\n' + status(1) + 'middle\n' + status(2) + 'last\n';
  const { io, lines } = mkIo();
  const events = [];
  const result = await spawnIterativeStreamed(root, process.execPath,
    ['-e', `process.stdout.write(${JSON.stringify(payload)})`], root, 10000, io, mkPlatform(), event => events.push(event));
  if(result.code!==0 || lines.join('')!==payload) throw Error('batching lost or reordered journal bytes');
  if(events.map(event=>event.message).join(',')!=='stage 1,stage 2') throw Error('batching lost status events');
  const statusRecords = lines.filter(line=>line.startsWith('ITERATIVE_MIGRATION_STATUS_V1 '));
  if(statusRecords.length!==2 || statusRecords.some(line=>line.trim().includes('\n'))) throw Error('status records must remain individually parseable');
}
console.log("check-iterative-stream: OK");
