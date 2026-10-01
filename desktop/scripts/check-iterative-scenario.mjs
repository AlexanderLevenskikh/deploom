// Behavioral contract for the single migration scenario / single main button.
// Invokes the REAL pure module (dist-electron/iterative-scenario.js) with
// fixtures — one check per accepted user scenario — instead of grepping the
// sources. Requires the electron TS to be compiled first (precheck: tsc).
import { deriveMainAction, parseIterativeFailure } from "../dist-electron/iterative-scenario.js";

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
  const a = deriveMainAction({ inFlight: false, runner: undefined, taskPresent: false, noTargets: false, attempt: attempt({ runCreated: true }) });
  if (a.state !== "recovered-running") throw new Error(`expected recovered-running, got ${a.state}`);
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

if (failures > 0) {
  console.error(`${failures} scenario contract check(s) FAILED`);
  process.exit(1);
}
console.log("iterative-scenario contract OK");
