import { readFileSync } from "node:fs";
import { flowNotificationContent, createIterativeNotificationObserver, createFlowNotificationSender } from "../dist-electron/notifications.js";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { runInNewContext } from "node:vm";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { startAttempt, updateAttempt, readAttempt, resumeAttempt, recordAttemptLog } from "../dist-electron/iterative-attempt.js";

const stage = flowNotificationContent({ kind: "stage-complete", projectName: "checkout-form", action: "audit" });
if (!stage.title.includes("Этап завершён") || !stage.body.includes("Независимый аудит")) throw new Error("Stage notification content is invalid");
const group = flowNotificationContent({ kind: "group-complete", projectName: "checkout-form", branch: "deps-demo-group-2", index: 2, total: 5, packages: ["react", "react-dom", "vite", "vitest"] });
if (!group.body.includes("группа 2 из 5") || !group.body.includes("+1")) throw new Error("Group notification content is invalid");
const autopilot = flowNotificationContent({ kind: "autopilot-complete", projectName: "checkout-form", published: true });
if (!autopilot.body.includes("опубликованы")) throw new Error("Autopilot notification content is invalid");
const main = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf8").replaceAll("\r\n", "\n");
const preload = readFileSync(new URL("../electron/preload.cts", import.meta.url), "utf8");
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
for (const required of ["Notification.isSupported()", "flow:set-notifications-enabled", "group-complete", "stage-complete", "app.setAppUserModelId('io.github.alexanderlevenskikh.deploom')"]) {
  if (!main.includes(required)) throw new Error("Main notification wiring missing: " + required);
}
if (!preload.includes("setNotificationsEnabled") || !preload.includes("notifyAutopilotComplete")) throw new Error("Preload notification API is incomplete");
if (!app.includes('className="notification-toggle"') || !app.includes("flow.setNotificationsEnabled")) throw new Error("Header notification toggle is missing");
if (!main.includes("if (exitCode === 0 && !job.autopilot)")) throw new Error("Autopilot must suppress per-stage completion notifications");
console.log("Windows notification contract OK");

// Exercise the actual durable attempt boundary, including resumed cycles and heartbeats.
const dir = mkdtempSync(join(tmpdir(), "deploom-notifications-"));
try {
  const observe = createIterativeNotificationObserver();
  const publish = () => observe(readAttempt(dir));
  startAttempt(dir, "demo", "none", undefined, 0, "workspace-a");
  assert.deepEqual(publish(), []);
  updateAttempt(dir, { status: "running", stage: "begin" });
  assert.deepEqual(publish(), []);
  updateAttempt(dir, { status: "failed", stage: "begin", lastStep: "check", lastError: "CHECK_FAILED" });
  assert.equal(publish()[0].kind, "stage-failed");
  updateAttempt(dir, { reason: "another heartbeat", lastError: "more detail" });
  assert.deepEqual(publish(), [], "same failure must not repeat");
  resumeAttempt(dir, "demo", "workspace-a");
  publish();
  updateAttempt(dir, { status: "failed", stage: "begin", lastStep: "check", lastError: "CHECK_FAILED" });
  assert.equal(publish()[0].kind, "stage-failed", "a real resumed failure needs a new notification");
  startAttempt(dir, "demo", "none", undefined, 0, "workspace-a");
  publish();
  updateAttempt(dir, { status: "done", stage: "begin", lastStep: "checked" });
  assert.equal(publish()[0].stage, "Проверка → Подбор версий");
  assert.deepEqual(publish(), []);
  resumeAttempt(dir, "demo", "workspace-a");
  assert.equal(publish()[0].stage, "Подбор версий → Обновление");
  updateAttempt(dir, { stage: "drive", stepsDone: ["plan-next"] });
  assert.deepEqual(publish(), []);
  updateAttempt(dir, { stepsDone: ["plan-next", "materialize", "verify"] });
  assert.deepEqual(publish(), [], "per-command events must remain silent");
  updateAttempt(dir, { status: "done", lastStep: "cohort-review" });
  assert.deepEqual(publish(), [], "cohort approval is an on-screen action");
  resumeAttempt(dir, "demo", "workspace-a");
  updateAttempt(dir, { stepsDone: [] }); publish();
  updateAttempt(dir, { stage: "drive", stepsDone: ["plan-next", "materialize", "verify"] });
  assert.deepEqual(publish(), [], "next cohort must not replay the major-stage notification");
  updateAttempt(dir, { status: "done", stage: "agent", phase: "delivery", lastStep: "delivery" });
  assert.equal(publish()[0].stage, "Обновление → Результат");
  updateAttempt(dir, { status: "canceled", cancelRequested: true });
  assert.deepEqual(publish(), []);
  startAttempt(dir, "demo", "none", undefined, 0, "workspace-b");
  publish();
  updateAttempt(dir, { status: "failed", stage: "begin", lastError: "OTHER_WORKSPACE" });
  assert.equal(publish()[0].kind, "stage-failed");
  assert.doesNotThrow(() => createIterativeNotificationObserver()({ ...readAttempt(dir), stepsDone: undefined }),
    "incomplete old journal must not break execution");
  assert.deepEqual(createIterativeNotificationObserver()({ ...readAttempt(dir), status: "running", stepsDone: ["verify"] }), [],
    "restart must not replay previously completed steps");
} finally { rmSync(dir, { recursive: true, force: true }); }

// Autopilot reports major transitions and only supervisor-confirmed stops.
{
  const observe = createIterativeNotificationObserver();
  const base = {attemptId:'auto',workspaceId:'w',projectName:'demo',status:'running',stage:'begin',stepsDone:[]};
  assert.deepEqual(observe(base, {autopilot:true}), []);
  assert.equal(observe({...base,phase:'begin.discovery'}, {autopilot:true})[0].stage, 'Проверка → Подбор версий');
  const drive = {...base,stage:'drive'};
  assert.equal(observe(drive, {autopilot:true})[0].stage, 'Подбор версий → Обновление');
  for (let cohort=0;cohort<100;cohort++) {
    assert.deepEqual(observe({...drive,stepsDone:['plan-next','materialize','verify']}, {autopilot:true}), []);
    assert.deepEqual(observe({...drive,status:'failed',stage:'agent',lastError:'FORBIDDEN_MUTATION: runtime'}, {autopilot:true}), []);
    assert.deepEqual(observe(drive, {autopilot:true}), []);
  }
  const failed = {...drive,status:'failed',lastError:'CHECK_FAILED'};
  assert.deepEqual(observe(failed, {autopilot:true}), []);
  assert.equal(observe(failed, {autopilot:true,stopped:true})[0].kind, 'stage-failed');
  assert.deepEqual(observe(failed, {autopilot:true,stopped:true}), []);
}
let enabled = true, supported = true, failures = 0, clicks = 0;
const native = [];
const sender = createFlowNotificationSender({
  enabled: () => enabled, supported: () => supported,
  create: content => { const n = new EventEmitter(); n.show = () => { n.shown = true; }; n.content = content; native.push(n); return n; },
  onFailure: () => { failures++; }, onClick: () => { clicks++; },
});
const failedStage = { kind: "stage-failed", projectName: "demo", stage: "Проверка проекта" };
enabled = false; sender(failedStage); assert.equal(native.length, 0);
enabled = true; supported = false; sender(failedStage); assert.equal(native.length, 0);
supported = true; sender(failedStage); assert.equal(native[0].shown, true);
assert.match(native[0].content.title, /Ошибка/);
native[0].emit("failed", {}, "OS refused notification"); assert.equal(failures, 1);
sender(failedStage); native[1].emit("click"); assert.equal(clicks, 1);
native[1].emit("close");
for (const constructorFailure of [true, false]) {
  const broken = createFlowNotificationSender({ enabled: () => true, supported: () => true,
    create: () => { if (constructorFailure) throw new Error("constructor"); const n = new EventEmitter(); n.show = () => { throw new Error("show"); }; return n; },
    onFailure: () => { failures++; }, onClick: () => {} });
  assert.doesNotThrow(() => broken(failedStage));
}
assert.equal(failures, 3);
const completion = flowNotificationContent({ kind: "iterative-run-complete", projectName: "demo" });
assert.match(completion.title, /Прогон завершён/);
assert.ok(!completion.body.includes("опубликованы"), "stopping a run does not prove publication");
for (const marker of ["broadcastIterativeAttempt(attempt)", "observeIterativeNotifications(attempt, { autopilot: iterativeNotificationAutopilot(attempt), stopped })", "summary.stopped === 'finished'", "FLOW_NOTIFICATION_FAILED", "if (!job.cancelled)"]) {
  assert.ok(main.includes(marker), "Missing notification boundary: " + marker);
}
console.log("Iterative journal, native delivery failures and notification deduplication OK");

const legacyStart = main.indexOf("    if (!job.cancelled) {\n      if (exitCode === 0");
assert.ok(legacyStart >= 0);
const legacyEnd = main.indexOf("\n  }\n}\n\nfunction isInside", legacyStart);
assert.ok(legacyEnd > legacyStart);
const legacyBoundary = main.slice(legacyStart, legacyEnd);
for (const [exitCode, canceled, auto, expected] of [
  [0, false, false, "stage-complete"], [1, false, false, "stage-failed"],
  [0, true, false, undefined], [1, true, false, undefined],
  [0, false, true, undefined], [1, false, true, "stage-failed"],
]) {
  const sent = [];
  runInNewContext(legacyBoundary, { exitCode, errorMessage: "failure", BASELINE_DECISION_MARKER: "decision",
    job: { cancelled: canceled, autopilot: auto, projectName: "demo", workspace: { name: "demo" }, action: "audit" },
    showFlowNotification: event => sent.push(event) });
  assert.equal(sent[0]?.kind, expected);
  assert.ok(sent.length <= 1);
}
console.log("Actual legacy completion boundary: cancellation and error handling OK");


// Execute the production Autopilot stop callback against a real journal.
const stoppedStart = main.indexOf("    onStopped: (input, summary) => {");
const stoppedEnd = main.indexOf("\n    writeRetry:", stoppedStart);
assert.ok(stoppedStart >= 0 && stoppedEnd > stoppedStart);
const stoppedCallback = main.slice(stoppedStart, stoppedEnd).trim().replace(/^onStopped: /, "").replace(/,$/, "");
const stoppedDir = mkdtempSync(join(tmpdir(), "deploom-notifications-stop-"));
try {
  const seen = createIterativeNotificationObserver(), events = [];
  const stopped = runInNewContext("(" + stoppedCallback + ")", {
    autopilotRunDir: () => stoppedDir, updateAttempt, readAttempt, recordAttemptLog,
    publishIterativeAttempt: (_directory, stopped = false) => { const record = readAttempt(stoppedDir); if (record) events.push(...seen(record, {autopilot:true, stopped})); },
    showFlowNotification: event => events.push(event),
  });
  const scope = { workspaceId: "workspace", projectName: "demo" };
  startAttempt(stoppedDir, "demo", "none", undefined, 0, "workspace");
  seen(readAttempt(stoppedDir));
  stopped(scope, { stopped: "paused" }); stopped(scope, { stopped: "canceled" });
  assert.deepEqual(events, []);
  stopped(scope, { stopped: "finished" });
  assert.equal(events.pop().kind, "iterative-run-complete");
  updateAttempt(stoppedDir, { status: "running", stage: "drive" });
  seen(readAttempt(stoppedDir));
  stopped(scope, { stopped: "error", error: "STATUS_UNAVAILABLE" });
  assert.equal(events.pop().kind, "stage-failed");
  stopped(scope, { stopped: "error", error: "STATUS_UNAVAILABLE" });
  assert.deepEqual(events, [], "Autopilot callback must not duplicate a journal failure");
  updateAttempt(stoppedDir, { status: "canceled", cancelRequested: true, lastError: undefined });
  seen(readAttempt(stoppedDir));
  stopped(scope, { stopped: "error", error: "DELIVERY_CANCELED" });
  assert.equal(readAttempt(stoppedDir).status, "canceled");
  assert.equal(readAttempt(stoppedDir).lastError, undefined);
  assert.deepEqual(events, [], "Manual stop must not become a failure notification");
} finally { rmSync(stoppedDir, { recursive: true, force: true }); }
console.log("Actual iterative Autopilot stop boundary OK");
