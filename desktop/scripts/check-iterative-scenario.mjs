// Behavioral contract for the single migration scenario / single main button.
// Invokes the REAL pure module (dist-electron/iterative-scenario.js) with
// fixtures — one check per accepted user scenario — instead of grepping the
// sources. Requires the electron TS to be compiled first (precheck: tsc).
import { deriveMainAction, parseIterativeFailure, scenarioPipeline } from "../dist-electron/iterative-scenario.js";

import { createIterativeAutopilot } from '../dist-electron/iterative-autopilot.js';
import { discoveryProgress } from '../dist-electron/iterative-discovery-progress.js';
import assert from 'node:assert/strict';
import { canApplyAttemptRead, iterativeActivity } from "../dist-electron/iterative-activity.js";

let failures = 0;
function expect(name, fn) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (error) {
    failures += 1;
    console.error(`FAIL ${name}: ${error.message}`);
  }
}

function attempt(over) {
  return {
    attemptId: "a-1",
    projectName: "Demo",
    status: "running",
    stage: "begin",
    startedAt: 0,
    lastHeartbeatAt: 0,
    targetSource: "none",
    stepsDone: [],
    runCreated: false,
    ...over,
  };
}

function runner(over) {
  return { present: true, phase: "READY", decision: { step: "plan-next", satisfied: false, reason: "x" }, ...over };
}

const envelope =
  'ITERATIVE_MIGRATION_FAILURE_V1 {"code": "SOURCE_SUBMODULE_INCOMPLETE", "summary": "Git submodule vendor/sub не инициализирован. Подготовьте submodule и повторите проверку.", "command": "git submodule update --init --recursive -- \\"vendor/sub\\"", "fixable": true}';

const controlEnvelope =
  'ITERATIVE_MIGRATION_FAILURE_V1 {"code": "PROJECT_CONTROL_FAILED", "summary": "Контроль текущих зависимостей не пройден: npm run test (exit 1).", "command": "", "fixable": true}';

// 1. New project, nothing run → explicit «Check project» first step.
expect("fresh project acts as check (Проверить проект)", () => {
  const a = deriveMainAction({ inFlight: false, runner: undefined, taskPresent: false, noTargets: false, attempt: undefined });
  if (a.state !== "check") throw new Error(`expected check, got ${a.state}`);
});

// 2. New project without roadmap targets → the understandable no-targets choice.
expect("fresh project without roadmap targets offers the choice", () => {
  const a = deriveMainAction({ inFlight: false, runner: undefined, taskPresent: false, noTargets: true, attempt: undefined });
  if (a.state !== "no-targets") throw new Error(`expected no-targets, got ${a.state}`);
});

// 3. Tab switch (Graph→FLOW) while a real child runs: Electron inFlight wins —
//    management is NOT lost, the action is Stop.
expect("in-flight process keeps Stop after a remount", () => {
  const a = deriveMainAction({ inFlight: true, runner: undefined, taskPresent: false, noTargets: false, attempt: attempt({}) });
  if (a.state !== "running") throw new Error(`expected running, got ${a.state}`);
});

// 4. Durable journal says running but no live child (restart): resume, honest.
expect("restored running journal (no live child) resumes", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({}), taskPresent: false, noTargets: false, attempt: attempt({ runCreated: true }) });
  if (a.state !== "recovered-running") throw new Error(`expected recovered-running, got ${a.state}`);
});

expect("interrupted check without a run retries instead of driving NO_RUN", () => {
  for (const status of ["starting", "running"]) {
    for (const runCreated of [false, true]) {
      const action = deriveMainAction({ inFlight: false, runner: runner({ present: false }), taskPresent: false, noTargets: false, attempt: attempt({ status, runCreated }) });
      if (action.state !== "retry-check") throw Error(`expected retry-check: ${status}, ${runCreated}: ${action.state}`);
    }
  }
});

// 5. Fixable local prep blocker (uninitialized submodule): blocked with the
//    exact command, main action = re-check.
expect("uninitialized submodule > retry-check with command", () => {
  const a = deriveMainAction({
    inFlight: false,
    runner: undefined,
    taskPresent: false,
    noTargets: false,
    attempt: attempt({ status: "failed", lastError: envelope }),
  });
  if (a.state !== "retry-check") throw new Error(`expected retry-check, got ${a.state}`);
  if (!a.blocker || a.blocker.code !== "SOURCE_SUBMODULE_INCOMPLETE") throw new Error("blocker not parsed");
  if (!a.blocker.command.includes("--init --recursive --") || !a.blocker.command.includes("vendor/sub")) {
    throw new Error(`command missing: ${a.blocker.command}`);
  }
});

// 6. Non-fixable begin failure, no run → re-check (retry without manual cleanup).
expect("non-fixable begin failure allows re-check", () => {
  const a = deriveMainAction({ inFlight: false, runner: undefined, taskPresent: false, noTargets: false, attempt: attempt({ status: "failed", lastError: "BEGIN_PREP_FAILED: bad temp" }) });
  if (a.state !== "check") throw new Error(`expected check, got ${a.state}`);
});

// 7. Agent gate → «Исправить агентом».
expect("agent gate acts as repair-with-agent", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ decision: { step: "agent", satisfied: false, reason: "repair" } }), taskPresent: false, noTargets: false, attempt: undefined });
  if (a.state !== "agent") throw new Error(`expected agent, got ${a.state}`);
});

// 7b. Launch-wait: a provider rate limit / temporary outage parks the repair.
// The 'waiting' journal is NOT an active run (a live child still wins) and NOT
// a failure — the action is the cancelable agent-wait; when no live child runs
// it must never be misread as recovered-running or a failed attempt.
expect("launch-wait parks the repair into agent-waiting", () => {
  const a = deriveMainAction({
    inFlight: false,
    runner: runner({ decision: { step: "agent", satisfied: false, reason: "repair" } }),
    taskPresent: true,
    noTargets: false,
    checked: false,
    attempt: attempt({ status: "waiting", stage: "agent", reason: "Ожидание повторного запуска агента: лимит запросов провайдера; следующая попытка в 12:00", waitUntil: Date.now() + 30_000, waitFailureKind: "rate-limited" }),
  });
  if (a.state !== "agent-waiting") throw new Error(`expected agent-waiting, got ${a.state}`);
  if (!a.reasonShort.includes("12:00")) throw new Error("waiting reason must carry the next-attempt time");
});
expect("a live child still wins over a durable waiting journal", () => {
  const a = deriveMainAction({ inFlight: true, runner: undefined, taskPresent: false, noTargets: false, attempt: attempt({ status: "waiting", stage: "agent" }) });
  if (a.state !== "running") throw new Error(`expected running, got ${a.state}`);
});

// 8. Verified result → «Посмотреть результат».
expect("satisfied run acts as view-result", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ phase: "TERMINAL", decision: { step: "finish", satisfied: true, reason: "done" } }), taskPresent: true, noTargets: false, attempt: undefined });
  if (a.state !== "result") throw new Error(`expected result, got ${a.state}`);
});

// 9. Paused run → start (first) or continue.
expect("fresh run starts the update", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({}), taskPresent: false, noTargets: false, attempt: attempt({ status: "done", runCreated: true, stepsDone: [] }) });
  if (a.state !== "start-run") throw new Error(`expected start-run, got ${a.state}`);
  if (!a.firstRun) throw new Error("fresh run must be flagged firstRun");
});
expect("paused run continues the update", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({}), taskPresent: false, noTargets: false, attempt: attempt({ status: "done", runCreated: true, stepsDone: ["plan-next"] }) });
  if (a.state !== "continue-run") throw new Error(`expected continue-run, got ${a.state}`);
});

// 10. Parse the failure envelope (command + fixable), also from a wrapped reason.
expect("failure envelope parses command/fixable", () => {
  const parsed = parseIterativeFailure(`wrapped\n${envelope}\ntail`);
  if (!parsed) throw new Error("envelope not parsed");
  if (parsed.code !== "SOURCE_SUBMODULE_INCOMPLETE" || !parsed.fixable) throw new Error("code/fixable wrong");
  if (!parsed.command.includes("vendor/sub")) throw new Error("command wrong");
});

// 11. P1#4: a REAL standalone check passed (durable, non-run) → ready to start.
expect("checked project (no run yet) acts as ready → Начать обновление", () => {
  const a = deriveMainAction({ inFlight: false, runner: undefined, taskPresent: false, noTargets: false, checked: true, attempt: attempt({ status: "done", lastStep: "checked" }) });
  if (a.state !== "ready") throw new Error(`expected ready, got ${a.state}`);
});

// 12. P1#3: budget-exhausted terminal is NOT a verified result — honest stop.
expect("budget-exhausted terminal is budget-stop, not result", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ phase: "TERMINAL", decision: { step: "finish", satisfied: false, reason: "BUDGET_EXHAUSTED: time budget 120s exhausted; applied+verified kept" } }), taskPresent: true, noTargets: false, checked: false, attempt: undefined });
  if (a.state !== "budget-stop") throw new Error(`expected budget-stop, got ${a.state}`);
});

// 13. P2 (#3): BLOCKED_BASELINE is an HONEST blocker terminal — never "часть
//     обновлений применена", so it is NOT the generic partial state.
expect("BLOCKED_BASELINE terminal is blocked, not partial", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ phase: "TERMINAL", decision: { step: "finish", satisfied: false, reason: "BLOCKED_BASELINE" } }), taskPresent: true, noTargets: false, checked: false, attempt: undefined });
  if (a.state !== "blocked") throw new Error(`expected blocked, got ${a.state}`);
});

// 14. P1#3: policy satisfied but the independent audit is still pending → the
//     migration is NOT done yet; the action continues, never claims a result.
expect("satisfied policy with pending audit continues, never result", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ phase: "READY", decision: { step: "audit", satisfied: true, reason: "policy satisfied; run the independent audit before finalizing" } }), taskPresent: true, noTargets: false, checked: false, attempt: attempt({ status: "done", runCreated: true, stepsDone: ["plan-next"] }) });
  if (a.state !== "continue-run") throw new Error(`expected continue-run, got ${a.state}`);
});

// 15. P2#5: a FRESH fixable blocker must win over an OLD "no targets" state —
//     a new error never hides behind the previous no-targets explanation.
expect("fresh fixable blocker beats old no-targets (P2#5)", () => {
  const a = deriveMainAction({ inFlight: false, runner: undefined, taskPresent: false, noTargets: true, checked: false, attempt: attempt({ status: "failed", lastError: envelope, lastStep: "no-targets" }) });
  if (a.state !== "retry-check") throw new Error(`expected retry-check, got ${a.state}`);
});

// 16. P2#5: without a fresh blocker the old no-targets choice still stands.
expect("no fresh blocker keeps the no-targets choice", () => {
  const a = deriveMainAction({ inFlight: false, runner: undefined, taskPresent: false, noTargets: true, checked: false, attempt: attempt({ status: "failed", lastError: "BEGIN_PREP_FAILED: bad temp", lastStep: "no-targets" }) });
  if (a.state !== "no-targets") throw new Error(`expected no-targets, got ${a.state}`);
});

// 17. P2 (#2): a RED current-state control (PROJECT_CONTROL_FAILED) must reach
//     the repair agent, not a dead-end re-check.
expect("red current-state control offers the repair agent (P2#2)", () => {
  const a = deriveMainAction({ inFlight: false, runner: undefined, taskPresent: false, noTargets: false, checked: false, attempt: attempt({ status: "failed", lastError: controlEnvelope }) });
  if (a.state !== "repair-current") throw new Error(`expected repair-current, got ${a.state}`);
  if (!a.blocker || a.blocker.code !== "PROJECT_CONTROL_FAILED") throw new Error("control blocker not parsed");
});

// 18. P2 (#2): the fresh red control beats an OLD no-targets explanation.
expect("red control beats old no-targets (P2#2)", () => {
  const a = deriveMainAction({ inFlight: false, runner: undefined, taskPresent: false, noTargets: true, checked: false, attempt: attempt({ status: "failed", lastError: controlEnvelope, lastStep: "no-targets" }) });
  if (a.state !== "repair-current") throw new Error(`expected repair-current, got ${a.state}`);
});

// 19. P2 (#2): a red run (BOOTSTRAP_REPAIR) first materializes the trial — the
//     action stays the repair flow, never "Начать обновление".
expect("bootstrap-materialize keeps the repair action (P2#2)", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ phase: "BOOTSTRAP_REPAIR", decision: { step: "bootstrap-materialize", satisfied: false, reason: "bootstrap C0 repair needs an isolated trial" } }), taskPresent: false, noTargets: false, checked: false, attempt: attempt({ status: "done", runCreated: true }) });
  if (a.state !== "repair-current") throw new Error(`expected repair-current, got ${a.state}`);
});

// 20. P2 (#4): an unsatisfied finish whose saved terminalOutcome is COMPLETE
//     resolves to the verified result (the runner now reads COMPLETE as
//     satisfied; this pins the derive side of that contract).
expect("COMPLETE terminal outcome shows the verified result (P2#4)", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ phase: "TERMINAL", decision: { step: "finish", satisfied: true, reason: "COMPLETE" } }), taskPresent: true, noTargets: false, checked: false, attempt: undefined });
  if (a.state !== "result") throw new Error(`expected result, got ${a.state}`);
});

// 21. P2 (#3): PARTIAL_VERIFIED stays the honest partial (some updates ARE
//     applied + verified, so the existing partial wording is right here).
expect("PARTIAL_VERIFIED terminal is the honest partial", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ phase: "TERMINAL", decision: { step: "finish", satisfied: false, reason: "PARTIAL_VERIFIED" } }), taskPresent: true, noTargets: false, checked: false, attempt: undefined });
  if (a.state !== "partial") throw new Error(`expected partial, got ${a.state}`);
});

// 22. P2 (#3): NO_VERIFIED_UPGRADE is NOT "часть обновлений применена" — the
//     run ended without ANY verified upgrade.
expect("NO_VERIFIED_UPGRADE terminal is no-upgrade, not partial", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ phase: "TERMINAL", decision: { step: "finish", satisfied: false, reason: "NO_VERIFIED_UPGRADE" } }), taskPresent: false, noTargets: false, checked: false, attempt: undefined });
  if (a.state !== "no-upgrade") throw new Error(`expected no-upgrade, got ${a.state}`);
});

// 23. P1 finding 1 / P2 (#1): the repair of the CURRENT state reached its
//     terminal REPAIR_VERIFIED — that is a repair success, NOT a partial
//     migration; the next action is starting the real update.
expect("REPAIR_VERIFIED terminal acts as repair-done → Начать обновление", () => {
  const a = deriveMainAction({ inFlight: false, runner: runner({ phase: "TERMINAL", decision: { step: "finish", satisfied: false, reason: "REPAIR_VERIFIED" } }), taskPresent: false, noTargets: false, checked: false, attempt: undefined });
  if (a.state !== "repair-done") throw new Error(`expected repair-done, got ${a.state}`);
});

// 24. User UX: the one-card pipeline reads as a linear flow. The pure mapping
//     pins which stage is current and which are done for key scenario states —
//     the user must always SEE progress, never a dead end.
expect("pipeline: fresh project is at the check stage", () => {
  const p = scenarioPipeline("check", false);
  if (p.current !== "check" || p.completed.length !== 0) throw new Error(`fresh pipeline ${JSON.stringify(p)}`);
});
expect("pipeline: verified result completes the flow", () => {
  const p = scenarioPipeline("result", false);
  if (p.current !== "result" || JSON.stringify(p.completed) !== JSON.stringify(["check", "plan", "upgrade"])) throw new Error(`result pipeline ${JSON.stringify(p)}`);
});
expect("pipeline: running update is at the upgrade stage", () => {
  const p = scenarioPipeline("running", true, { attempt: { stage: "drive" }, runner: { present: true } });
  if (p.current !== "upgrade" || JSON.stringify(p.completed) !== JSON.stringify(["check", "plan"])) throw new Error(`running pipeline ${JSON.stringify(p)}`);
});
expect("pipeline: repair-done moves to planning the real update", () => {
  const p = scenarioPipeline("repair-done", false);
  if (p.current !== "plan" || JSON.stringify(p.completed) !== JSON.stringify(["check"])) throw new Error(`repair-done pipeline ${JSON.stringify(p)}`);
});
expect("pipeline: blocked lands on the result, honestly", () => {
  const p = scenarioPipeline("blocked", false);
  if (p.current !== "result" || JSON.stringify(p.completed) !== JSON.stringify(["check", "plan"])) throw new Error(`blocked pipeline ${JSON.stringify(p)}`);
});

expect("pipeline: live check has no completed stages even with stale old verdict", () => {
  const p = scenarioPipeline("running", true, { attempt: { stage: "begin", phase: "begin.check", targetSource: "none" }, runner: { present: false } });
  if (p.current !== "check" || p.completed.length !== 0) throw new Error(JSON.stringify(p));
});
expect("pipeline: discovery is planning, never an update", () => {
  const p = scenarioPipeline("running", false, { attempt: { stage: "begin", phase: "begin.discovery-progress", targetSource: "discovery" }, runner: { present: false } });
  if (p.current !== "plan" || p.completed.length !== 0) throw new Error(JSON.stringify(p));
});
expect("pipeline: bootstrap agent still repairs the initial check", () => {
  const p = scenarioPipeline("agent", false, { runner: { present: true, phase: "BOOTSTRAP_REPAIR" } });
  if (p.current !== "check" || p.completed.length !== 0) throw new Error(JSON.stringify(p));
});
expect("pipeline: an agent launch-wait stays on the repair path (agent-waiting)", () => {
  const p = scenarioPipeline("agent-waiting", false, { runner: { present: true, phase: "BOOTSTRAP_REPAIR" } });
  if (p.current !== "check" || p.completed.length !== 0) throw new Error(JSON.stringify(p));
  const upgrade = scenarioPipeline("agent-waiting", false, { runner: { present: true, phase: "REPAIRING" } });
  if (upgrade.current !== "upgrade" || JSON.stringify(upgrade.completed) !== JSON.stringify(["check", "plan"])) throw new Error(JSON.stringify(upgrade));
});
expect("pipeline: generic busy flag cannot manufacture completion", () => {
  const p = scenarioPipeline("running", false);
  if (p.current !== "check" || p.completed.length !== 0) throw new Error(JSON.stringify(p));
});

expect("activity: capture reports actual files and bytes, never an invented percent", () => {
  const result = iterativeActivity(attempt({ phase: "source-materialization: source capture sealed-manifest: files=25362, bytes=4589080824", targetSource: "discovery", packageProgress: { processed: 20, total: 30 } }), true, "en");
  if (result.title !== "Preparing an isolated project copy" || !result.detail.includes("25,362") || !result.detail.includes("4.27") || result.percent !== undefined) throw new Error(JSON.stringify(result));
});
expect("activity: discovery has a measured denominator", () => {
  const result = iterativeActivity(attempt({ phase: "begin.discovery-progress", packageProgress: { processed: 12, total: 30 } }), true, "ru");
  if (result.percent !== 40 || !result.detail.includes("12/30")) throw new Error(JSON.stringify(result));
});
expect("activity: resolver and project checks remain indeterminate", () => {
  for (const [phase, title] of [["resolver-install: running", "Installing dependencies in the verification copy"], ["lifecycle: npm run test", "Running project checks"]]) {
    const result = iterativeActivity(attempt({ phase }), true, "en");
    if (result.title !== title || result.percent !== undefined) throw new Error(JSON.stringify(result));
  }
});
expect("activity: a restored running journal never pretends a child is working", () => {
  const result = iterativeActivity(attempt({ phase: "begin.discovery-progress", packageProgress: { processed: 12, total: 30 } }), false, "en");
  if (result.title !== "Work interrupted — ready to resume" || result.percent !== undefined) throw new Error(JSON.stringify(result));
});
expect("activity: failed and canceled attempts drop stale progress", () => {
  for (const status of ["failed", "canceled"]) {
    const result = iterativeActivity(attempt({ status, phase: "begin.discovery-progress", packageProgress: { processed: 12, total: 30 } }), false, "en");
    if (result.percent !== undefined || result.detail) throw new Error(JSON.stringify(result));
  }
});
expect("activity: a waiting agent-launch shows the wait, not an interruption", () => {
  const result = iterativeActivity(attempt({ status: "waiting", stage: "agent" }), false, "en");
  if (result.title !== "Waiting for agent access to recover" || result.detail) throw new Error(JSON.stringify(result));
});
expect("activity: malformed counters cannot manufacture progress", () => {
  for (const counter of [{ processed: 10, total: 0 }, { processed: -1, total: 10 }, { processed: 11, total: 10 }, { processed: NaN, total: 10 }]) {
    const result = iterativeActivity(attempt({ phase: "begin.discovery-progress", packageProgress: counter }), true, "en");
    if (result.percent !== undefined) throw new Error(JSON.stringify(result));
  }
});

expect("journal: delayed reads cannot resurrect an old attempt or a completed process", () => {
  const current = attempt({ attemptId: "new", workspaceId: "w", lastHeartbeatAt: 100, status: "failed" });
  for (const incoming of [attempt({ attemptId: "old", workspaceId: "w", lastHeartbeatAt: 200 }), attempt({ attemptId: "new", workspaceId: "other", lastHeartbeatAt: 200 }), attempt({ attemptId: "new", workspaceId: "w", lastHeartbeatAt: 90 }), attempt({ attemptId: "new", workspaceId: "w", lastHeartbeatAt: 100 })]) {
    if (canApplyAttemptRead(current, incoming, "Demo", "w")) throw new Error(JSON.stringify(incoming));
  }
  if (!canApplyAttemptRead(current, { ...current, status: "running", lastHeartbeatAt: 200 }, "Demo", "w")) throw new Error("fresh resume was rejected");
  if (!canApplyAttemptRead(undefined, current, "Demo", "w")) throw new Error("initial read was rejected");
});


expect('pipeline: terminal check marks green before async status catches up', () => {
  assert.equal(deriveMainAction({ inFlight: false, attempt: { status: 'done', lastStep: 'checked' }, runner: { present: false }, checked: false }).state, 'ready');
});
expect('pipeline: a concluded check fills its connector even while IPC returns', () => {
  assert.deepEqual(scenarioPipeline('running', true, { attempt: { status: 'done', stage: 'begin', lastStep: 'checked' } }), { current: 'plan', completed: ['check'] });
  assert.deepEqual(scenarioPipeline('running', true, { attempt: { stage: 'begin', targetSource: 'roadmap', phase: 'begin.c0-verify' } }), { current: 'plan', completed: ['check'] });
});
expect('pipeline: C0 after discovery never rewinds a passed check', () => {
  const p = scenarioPipeline('running', true, { attempt: { stage: 'begin', targetSource: 'discovery', phase: 'begin.c0-verify', discoveryCompleted: true }, runner: { present: false } });
  assert.equal(p.current, 'plan'); assert.deepEqual(p.completed, ['check']);
});
expect('progress: discovery -> capture -> discovery-result remains at completion', () => {
  let p = discoveryProgress(undefined, { event: 'begin.discovery-progress', processed: 12, total: 30 });
  p = discoveryProgress(p, { event: 'begin.discovery-progress', processed: 30, total: 30 });
  p = discoveryProgress(p, { event: 'begin.capture', managedDependencies: 30 });
  assert.deepEqual(p, { processed: 30, total: 30 });
  p = discoveryProgress(p, { event: 'begin.discovery' });
  assert.deepEqual(p, { processed: 30, total: 30 });
  assert.equal(discoveryProgress(p, { event: 'begin.discovery-progress', processed: 0, total: 40 }), undefined);
  assert.deepEqual(discoveryProgress(p, { event: 'begin.discovery-progress', processed: 0, total: 30 }), p);
});
expect('activity: finished discovery is preparation, never a zero search', () => {
  const a = iterativeActivity(attempt({ phase: 'begin.discovery', discoveryCompleted: true, packageProgress: { processed: 30, total: 30 } }), true, 'en');
  assert.equal(a.title, 'Preparing the verified starting state for the update'); assert.equal(a.percent, undefined);
});
expect('activity: exact verification exposes the command and measured check counter', () => {
  const a = iterativeActivity(attempt({stage:'drive',phase:'iterative migration verify-exact: project check 2/4 started: yarn lint'}),true,'ru');
  assert.equal(a.title,'Выполняем проверки проекта'); assert.ok(a.detail.includes('yarn lint')); assert.ok(a.detail.includes('2/4')); assert.equal(a.percent,undefined);
});
const scope = { workspaceId: 'w', projectName: 'Demo', autopilot: true };
const owner = () => createIterativeAutopilot(s => `${s.workspaceId}:${s.projectName}`);
const calls = [];
const auto = owner();
const status = auto.register('status', async () => ({ ok: true, decision: { step: 'agent' } }));
auto.register('agent', async () => { calls.push('agent'); return { ok: true }; });
let driveNumber = 0;
auto.register('drive', async () => { calls.push('drive'); return ++driveNumber === 1 ? { ok: true, stopped: 'agent-gate', repairRequests: [{ requestId: 'r1' }] } : { ok: true, stopped: 'finished' }; });
const begin = auto.register('begin', async (_event, i) => { calls.push(i.checkOnly ? 'check' : 'begin'); assert.equal((await status(null, i)).autopilotActive, true); return i.checkOnly ? { ok: true, checked: { ok: true } } : { ok: true }; });
const full = await begin(null, { ...scope, checkOnly: true });
assert.equal(full.autopilot.stopped, 'finished'); assert.deepEqual(calls, ['check', 'begin', 'drive', 'agent', 'drive']); assert.equal((await status(null, scope)).autopilotActive, false);
for (const blocked of [{ ok: false, error: 'INFRA' }, { ok: true, checked: { ok: false } }, { ok: true, noTargets: true }]) {
 const a = owner(); let continued = 0;
 a.register('drive', async () => { continued++; return { ok: true }; });
 const b = a.register('begin', async () => blocked);
 await b(null, scope); assert.equal(continued, 0);
}
const paused = owner(); let releaseCheck;
const waitCheck = new Promise(resolve => { releaseCheck = resolve; });
let extraCalls = 0;
const pauseBegin = paused.register('begin', async () => { await waitCheck; return { ok: true, checked: { ok: true } }; });
paused.register('drive', async () => { extraCalls++; return { ok: true }; });
const cancel = paused.register('cancel', async () => ({ ok: true }));
const pending = pauseBegin(null, scope);
assert.equal((await pauseBegin(null, { ...scope, autopilot: false })).error, 'AUTOPILOT_IN_PROGRESS');
await cancel(null, scope); releaseCheck(); assert.equal((await pending).autopilot.stopped, 'canceled'); assert.equal(extraCalls, 0);
const repeat = owner(); let dispatched = 0;
repeat.register('status', async () => ({ ok: true, decision: { step: 'agent' } }));
repeat.register('agent', async () => { dispatched++; return { ok: true }; });
const repeatDrive = repeat.register('drive', async () => ({ ok: true, stopped: 'agent-gate', repairRequests: [{ requestId: 'same' }] }));
assert.equal((await repeatDrive(null, scope)).autopilot.error, 'AUTOPILOT_REPEATED_REPAIR_GATE'); assert.equal(dispatched, 1);
const adopt = owner(); let finishManual; let adoptedCalls = 0;
const manualWait = new Promise(resolve => { finishManual = resolve; });
adopt.register('status', async () => ({ok:true}));
const manual = adopt.register('begin', async () => { await manualWait; return {ok:true}; });
adopt.register('drive', async () => { adoptedCalls++; return {ok:true,stopped:'finished'}; });
const manualPending = manual(null, {...scope,autopilot:false});
assert.equal(adopt.setEnabled(scope,true).active,true);
finishManual(); assert.equal((await manualPending).autopilot.stopped,'finished'); assert.equal(adoptedCalls,1);
const disableOwner = owner(); let finishDrive; let disabledAgents = 0;
const driveWait = new Promise(resolve => { finishDrive = resolve; });
disableOwner.register('agent', async () => { disabledAgents++; return {ok:true}; });
const disableDrive = disableOwner.register('drive', async () => { await driveWait; return {ok:true,stopped:'agent-gate',repairRequests:[{requestId:'disabled-gate'}]}; });
const disablePending = disableDrive(null,scope);
disableOwner.setEnabled(scope,false); finishDrive();
assert.equal((await disablePending).stopped,'agent-gate'); assert.equal(disabledAgents,0);
const reading = owner(); let resolveRead; let readCount = 0;
const sharedRead = new Promise(resolve => { resolveRead = resolve; });
const readStatus = reading.register('status', async () => { readCount++; await sharedRead; return {ok:true}; });
const reads = [readStatus(null,scope),readStatus(null,scope)]; resolveRead(); await Promise.all(reads); assert.equal(readCount,1);
// A manual infra retry must not propagate to subsequent automatic drives.
const retryOwner = owner(); const retries = [];
retryOwner.register('status', async () => ({ok:true, decision:{step:'agent'}}));
retryOwner.register('agent', async () => ({ok:true}));
const retryDrive = retryOwner.register('drive', async (_event, input) => {
 retries.push(input.retryInfra === true);
 return retries.length === 1 ? {ok:true, stopped:'agent-gate', repairRequests:[{requestId:'retry'}]} : {ok:true, stopped:'finished'};
});
assert.equal((await retryDrive(null,{...scope,retryInfra:true})).autopilot.stopped,'finished');
assert.deepEqual(retries,[true,false]);
// Disputed repair bytes are rejected; other cohorts continue without another
// paid repair of the same candidate. Exercise both initial and later agent gates.
for (const initialAgent of [false, true]) {
 const recovery = owner(); const recoveryFlags = []; let agentCalls = 0;
 recovery.register('status', async () => ({ok:true,decision:{step:'agent'}}));
 const repair = recovery.register('agent', async () => { agentCalls++; return {ok:false,error:'FORBIDDEN_MUTATION: modified:package.json'}; });
 const driving = recovery.register('drive', async (_event, input) => {
  recoveryFlags.push(input.discardCandidate === true);
  return input.discardCandidate ? {ok:true,stopped:'finished'} : {ok:true,stopped:'agent-gate',repairRequests:[{requestId:'bad-trial'}]};
 });
 const completed = await (initialAgent ? repair(null,scope) : driving(null,scope));
 assert.equal(completed.autopilot.stopped,'finished');
 assert.equal(completed.ok,true);
 assert.equal(agentCalls,1);
 assert.deepEqual(recoveryFlags,initialAgent ? [true] : [false,true]);
}
const failureOwner = owner(); let failureDrives = 0;
failureOwner.register('drive',async () => { failureDrives++; return {ok:true}; });
const providerFailed = failureOwner.register('agent',async () => ({ok:false,error:'AGENT_PROVIDER_ERROR: unavailable'}));
assert.equal((await providerFailed(null,scope)).autopilot.stopped,'error');
assert.equal(failureDrives,0,'Unknown provider failures must not discard a candidate');
// Waiting is a suspended agent call, not a failed migration or repeated gate.
for (const first of ['agent','drive']) {
 const waiting = owner(); let calls = 0; let drives = 0;
 waiting.register('status',async()=>({ok:true,decision:{step:'agent'}}));
 const agent = waiting.register('agent',async()=> ++calls === 1 ? {ok:false,waiting:true,retryAt:Date.now()+20} : {ok:true});
 const drive = waiting.register('drive',async()=> ++drives === 1 && first === 'drive' ? {ok:true,stopped:'agent-gate',repairRequests:[{requestId:'waiting'}]} : {ok:true,stopped:'finished'});
 const result = await (first === 'agent' ? agent : drive)(null,scope);
 assert.equal(result.autopilot.stopped,'finished'); assert.equal(calls,2);
}
for (const stop of ['cancel','pause']) {
 const waiting = owner(); let calls = 0;
 const agent = waiting.register('agent',async()=>{calls++;return {ok:false,waiting:true,retryAt:Date.now()+60_000};});
 const cancel = waiting.register('cancel',async()=>({ok:true}));
 const pending = agent(null,scope);
 await new Promise(resolve=>setTimeout(resolve,20));
 assert.equal(waiting.hasSession(scope),true);
 if (stop === 'cancel') await cancel(null,scope); else waiting.setEnabled(scope,false);
 const result = await pending;
 assert.equal(result.autopilot.stopped,stop==='cancel'?'canceled':'paused');
 assert.equal(calls,1); assert.equal(waiting.hasSession(scope),false);
}
console.log('autopilot: continuation, waiting, pause, cancel and repeated-gate boundaries OK');
await import('./iterative-autopilot-lifecycle-fixture.mjs');

if (failures > 0) {
  console.error(`${failures} scenario contract check(s) FAILED`);
  process.exit(1);
}
console.log("iterative-scenario contract OK");
