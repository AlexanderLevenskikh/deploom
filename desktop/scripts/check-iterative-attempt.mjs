import { mkdirSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  attemptFilePath,
  attemptLogPath,
  beginPlan,
  clearCancelRequest,
  readAttempt,
  readAttemptLogTail,
  readRunLogTail,
  recordAttemptLog,
  requestCancel,
  resumeAttempt,
  startAttempt,
  trimAttemptLog,
  updateAttempt,
  writeAttempt,
} from "../dist-electron/iterative-attempt.js";

import { attemptActiveElapsed } from "../dist-electron/iterative-activity.js";

const root = mkdtempSync(join(tmpdir(), "iter-attempt-contract-"));

// 1. startAttempt writes a NEW durable record (fresh attemptId) whose journal
// reflects the pre-C0 reality: starting/preflight, no run created yet.
const runA = join(root, "run-a");
const first = startAttempt(runA, "DemoApp", "roadmap", undefined, 3);
if (!first.attemptId || first.projectName !== "DemoApp") throw new Error("startAttempt fields");
if (first.status !== "starting" || first.stage !== "preflight" || first.runCreated !== false) {
  throw new Error(`startAttempt must begin starting/preflight/runCreated=false: ${JSON.stringify(first)}`);
}
if (first.targetsCount !== 3 || JSON.stringify(first.stepsDone) !== "[]") throw new Error("startAttempt counts");
if (!readAttempt(runA)) throw new Error("record must survive a re-read");
const same = readAttempt(runA);
if (same.attemptId !== first.attemptId) throw new Error("record identity must be stable");

// 2. updateAttempt patches fields and bumps the heartbeat so a stalled child is
// visible instead of a silent freeze.
const before = readAttempt(runA).lastHeartbeatAt;
const upd = updateAttempt(runA, { status: "running", stage: "begin", phase: "CAPTURE" });
if (upd.status !== "running" || upd.phase !== "CAPTURE") throw new Error("updateAttempt patch");
if (!(upd.lastHeartbeatAt >= before)) throw new Error("updateAttempt must bump heartbeat");

// 3. resumeAttempt keeps the ORIGINAL attemptId (run identity) across restart.
const afterResume = resumeAttempt(runA, "DemoApp");
if (afterResume.attemptId !== first.attemptId) throw new Error("resumeAttempt must keep attemptId");
if (afterResume.status !== "running" || afterResume.stage !== "drive") throw new Error("resumeAttempt must flip to running/drive");
if (afterResume.cancelRequested !== false) throw new Error("resumeAttempt must clear a stale cancel request");
const freshDir = join(root, "run-fresh");
const freshResume = resumeAttempt(freshDir, "NewApp");
if (freshResume.attemptId === first.attemptId || freshResume.projectName !== "NewApp") {
  throw new Error("resumeAttempt without a journal must start a NEW attempt");
}

// 3b. P2 (#2): the attempt record is WORKSPACE-scoped — workspaceId survives
// start and resume so the live stream can partition same-named projects.
const wsDir = join(root, "run-ws");
const wsAttempt = startAttempt(wsDir, "DemoApp", "roadmap", undefined, 0, "ws-7");
if (wsAttempt.workspaceId !== "ws-7") throw new Error("startAttempt must persist workspaceId");
const wsResumed = resumeAttempt(wsDir, "DemoApp", "ws-7");
if (wsResumed.workspaceId !== "ws-7" || wsResumed.attemptId !== wsAttempt.attemptId) {
  throw new Error("resumeAttempt must keep the workspace-scoped record");
}

// 4. Cooperative cancel: requestCancel is durable and visible; clearCancelRequest
// removes it; neither ever touches the run/checkpoint state.
requestCancel(runA);
if (readAttempt(runA).cancelRequested !== true) throw new Error("requestCancel must persist");
clearCancelRequest(runA);
if (readAttempt(runA).cancelRequested !== false) throw new Error("clearCancelRequest must reset");

// 5. Log journaling: record/trim/tail cap, and startAttempt resets the log.
// The cap is BYTE-based (MAX_LOG_BYTES); the LINE cap only kicks in once the
// byte cap is exceeded, so a short diagnostic log is never shredded.
const runB = join(root, "run-b");
startAttempt(runB, "DemoApp", "discovery", { parallelism: 8, timeoutSeconds: 240, maxPackages: 60 }, 0);
if (readAttemptLogTail(runB) !== "") throw new Error("fresh attempt must have no log");
recordAttemptLog(runB, "discovery line\n");
recordAttemptLog(runB, "second line\n");
trimAttemptLog(runB);
const tail = readAttemptLogTail(runB);
if (!tail.includes("second line")) throw new Error("log tail must contain appended lines");
if (readAttemptLogTail(runB, 8).length > 16) throw new Error("byte cap tail must be short");
// Short lines stay under the byte cap, so 810 short lines are KEPT (no line
// shredding while the artifact is small).
for (let i = 0; i < 810; i += 1) recordAttemptLog(runB, `line-${i}\n`);
trimAttemptLog(runB);
const shortLog = readFileSync(attemptLogPath(runB), "utf8");
if (shortLog.split(/\n/).length < 400) throw new Error("a small log must not be line-trimmed");
// Once the BYTE cap is crossed, trim keeps only the newest MAX_LOG_LINES lines.
const big = join(root, "run-big");
startAttempt(big, "DemoApp", "discovery", { parallelism: 8, timeoutSeconds: 240, maxPackages: 60 }, 0);
for (let i = 0; i < 1600; i += 1) recordAttemptLog(big, `payload ${"x".repeat(300)} ${i}\n`);
trimAttemptLog(big);
const bigLog = readFileSync(attemptLogPath(big), "utf8").split(/\n/);
if (bigLog.length > 802) throw new Error(`log must be capped to MAX_LOG_LINES after the byte cap, got ${bigLog.length}`);
if (!bigLog.some((line) => line.endsWith(" 1599"))) throw new Error("trim must keep the newest lines");
if (bigLog.some((line) => line.endsWith(" 0"))) throw new Error("trim must drop the oldest lines after the byte cap");
if (readFileSync(attemptLogPath(big)).length > 128 * 1024) throw new Error("attempt log must stay within its byte cap");
const retainedIndices = bigLog.filter((line) => line.startsWith("payload ")).map((line) => Number(line.match(/ (\d+)$/)?.[1]));
if (retainedIndices.length < 100 || retainedIndices.at(-1) !== 1599 || retainedIndices.some((value, index) => index > 0 && value !== retainedIndices[index - 1] + 1)) {
  throw new Error("trim must keep a contiguous newest block within the byte budget");
}
const bigTail = readAttemptLogTail(big);
if (bigTail.length > 16 * 1024 + 2) throw new Error("UI tail must be capped to the 16KB budget");
// A NEW attempt clears the log (no cross-run leakage of a previous child).
startAttempt(runB, "DemoApp", "roadmap", undefined, 1);
if (readAttemptLogTail(runB) !== "") throw new Error("startAttempt must clear the previous log");

if (!readRunLogTail(runB).includes('discovery line')) throw new Error('Cumulative log lost a previous attempt');
if (!readFileSync(join(big, 'run.log'), 'utf8').includes(`payload ${"x".repeat(300)} 0\n`)) throw new Error('Trimming the UI tail discarded cumulative history');

// 6. beginPlan: the pure plan keeps "no silent discovery" honest. Already-exists
// and in-progress win over target-count logic; roadmap targets start; empty
// targets with mode 'none' becomes no-targets (nothing runs); empty targets with
// explicit mode 'auto' becomes bounded discovery.
const cases = [
  [{ runPresent: true, stepInFlight: false, targetsCount: 0, discoveryMode: "none" }, "already-exists"],
  [{ runPresent: true, stepInFlight: true, targetsCount: 5, discoveryMode: "auto" }, "already-exists"],
  [{ runPresent: false, stepInFlight: true, targetsCount: 0, discoveryMode: "none" }, "in-progress"],
  [{ runPresent: false, stepInFlight: false, targetsCount: 0, discoveryMode: "none" }, "no-targets"],
  [{ runPresent: false, stepInFlight: false, targetsCount: 0, discoveryMode: "auto" }, "discovery"],
  [{ runPresent: false, stepInFlight: false, targetsCount: 3, discoveryMode: "none" }, "start"],
];
for (const [input, expected] of cases) {
  const plan = beginPlan(input);
  if (plan.kind !== expected) {
    throw new Error(`beginPlan(${JSON.stringify(input)}) = ${plan.kind}, expected ${expected}`);
  }
}

// 7. writeAttempt + attemptFilePath round-trip and atomicity (no leftover tmp).
const runC = join(root, "run-c");
writeAttempt(runC, { ...readAttempt(runA), runCreated: true });
if (!readAttempt(runC)?.runCreated) throw new Error("writeAttempt round-trip");
const leftover = runC && attemptFilePath(runC).endsWith("attempt.json") ? "attempt.json" : "";
if (!leftover) throw new Error("attempt path malformed");
mkdirSync(runC, { recursive: true });

// Active execution excludes pauses, stopped UI time and offline restarts.
const clockDir = join(root, "clock");
const originalNow = Date.now;
let clock = 1000;
Date.now = () => clock;
try {
  startAttempt(clockDir, "Clock", "roadmap");
  clock = 6000;
  updateAttempt(clockDir, { status: "running" });
  clock = 11000;
  requestCancel(clockDir);
  const stopped = readAttempt(clockDir);
  if (stopped.activeElapsedMs !== 10000 || stopped.activeSince !== undefined) throw new Error("stop must freeze active time immediately");
  if (attemptActiveElapsed(stopped, true, 99999999) !== 10000) throw new Error("stopped UI must not tick");
  clock = 3600000;
  resumeAttempt(clockDir, "Clock");
  clock += 5000;
  updateAttempt(clockDir, { status: "done" });
  const done = readAttempt(clockDir);
  if (done.activeElapsedMs !== 15000 || done.finishedAt !== clock) throw new Error("resume counted the paused hour or completion did not freeze");
  clock += 3600000;
  updateAttempt(clockDir, { reason: "late metadata" });
  if (attemptActiveElapsed(readAttempt(clockDir), false, clock) !== 15000) throw new Error("terminal metadata extended elapsed time");
  resumeAttempt(clockDir, "Clock");
  clock += 3000;
  updateAttempt(clockDir, { phase: "heartbeat" });
  const interrupted = readAttempt(clockDir);
  if (attemptActiveElapsed(interrupted, false, clock + 3600000) !== 18000) throw new Error("interrupted UI counted offline time");
  clock += 3600000;
  resumeAttempt(clockDir, "Clock");
  if (attemptActiveElapsed(readAttempt(clockDir), true, clock) !== 18000) throw new Error("restart counted offline time");
  if (attemptActiveElapsed({ status: "done", lastHeartbeatAt: 9999999 }, false, clock) !== 0) throw new Error("legacy elapsed was invented");
} finally { Date.now = originalNow; }

console.log("check-iterative-attempt: OK");
