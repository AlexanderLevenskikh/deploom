import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  ITERATIVE_STATUS_FILENAME,
  decideNextStep,
  iterativeStatusInvocation,
  iterativeStepInvocation,
  parseIterativeStatusPayload,
  readIterativeStatus,
} from "../dist-electron/iterative-runner.js";

const root = mkdtempSync(join(tmpdir(), "iter-runner-contract-"));
const runDir = join(root, "run-empty");
mkdirSync(runDir, { recursive: true });

function payload(run, extra = {}) {
  return {
    run: { runId: "iter-runner-1", ...run },
    activeCheckpoint: { status: "VERIFIED", fullAssignment: { "is-number": "7.0.0", "@scope/deferred": "1.2.0" } },
    candidate: null,
    ledgerSummary: { blocks: 1, deferrals: 1 },
    openRepairRequests: [],
    config: { projectName: "DemoApp", targetLevel: "yellow" },
    ...extra,
  };
}

function expectDecision(run, extra, expectedStep, expectedReasonPart) {
  const decision = decideNextStep(runDir, payload(run, extra));
  if (decision.step !== expectedStep) {
    throw new Error(`phase=${String(run.phase)} expected step ${expectedStep}, got ${String(decision.step)}: ${decision.reason}`);
  }
  if (!decision.reason.includes(expectedReasonPart)) {
    throw new Error(`phase=${String(run.phase)} reason must mention "${expectedReasonPart}", got: ${decision.reason}`);
  }
  return decision;
}

// 1. No durable state -> begin.
const empty = decideNextStep(runDir, {});
if (empty.step !== "begin" || !empty.reason.includes("NO_RUN")) throw new Error("Run-less dir must decide begin");

// 2. Phase -> step mapping, grounding on the Python status contract.
// R7: bootstrap repair first materializes the version-neutral trial, then
// becomes an AGENT GATE with the request bytes.
const bootstrapFirst = expectDecision(
  { phase: "BOOTSTRAP_REPAIR", activeCandidateId: null },
  { openRepairRequests: [{ requestId: "boot-1", summary: "C0 control verification failed" }] },
  "bootstrap-materialize",
  "trial",
);
if (bootstrapFirst.bootstrap === true) throw new Error("Bootstrap without a trial must not be an agent gate yet");
const bootstrap = expectDecision(
  { phase: "BOOTSTRAP_REPAIR", activeCandidateId: null, bootstrapRefs: { workspaceRoot: "C:/run/trial/bootstrap", projectRelative: "." } },
  { openRepairRequests: [{ requestId: "boot-1", summary: "C0 control verification failed" }] },
  "agent",
  "repair",
);
if (bootstrap.bootstrap !== true || bootstrap.repairRequests.length !== 1) {
  throw new Error(`Bootstrap repair must be an agent GATE with the request bytes: ${JSON.stringify(bootstrap)}`);
}
expectDecision({ phase: "PLANNING", activeCandidateId: "C2" }, {}, "materialize", "materialize");
// MATERIALIZING without a MATERIALIZED stage resumes the same materialize step
// (B1: a concluded attemptResult must instead stop, covered below).
expectDecision({ phase: "MATERIALIZING", activeCandidateId: "C2" }, {}, "materialize", "exact versions");
// R2: a MATERIALIZED candidate moves to precheck, never re-materialize.
const materialized = expectDecision(
  { phase: "MATERIALIZING", activeCandidateId: "C2" },
  { candidate: { candidateId: "C2", stage: "MATERIALIZED" } },
  "precheck",
  "diagnostic",
);
if (materialized.bootstrap === true) throw new Error("MATERIALIZING+MATERIALIZED must not be a bootstrap gate");
expectDecision({ phase: "PRECHECK" }, {}, "precheck", "diagnostic");
// A passed precheck (stage PRECHECKED) proceeds to the authoritative verify.
expectDecision(
  { phase: "PRECHECK", activeCandidateId: "C2" },
  { candidate: { candidateId: "C2", stage: "PRECHECKED" } },
  "verify-exact",
  "precheck passed",
);
expectDecision(
  { phase: "VERIFYING" },
  {},
  "verify-exact",
  "authoritative",
);
// R4: a VERIFYING-stage candidate left after a kill/restart resumes the SAME
// candidate through verify-exact (re-entrant on the durable candidate identity).
expectDecision(
  { phase: "VERIFYING", activeCandidateId: "C2" },
  { candidate: { candidateId: "C2", stage: "VERIFYING" } },
  "verify-exact",
  "same exact candidate",
);
expectDecision({ phase: "TERMINAL", terminal: "BUDGET_EXHAUSTED", activeCandidateId: null }, {}, "finish", "BUDGET_EXHAUSTED");
// P2 (#4): the saved structured terminalOutcome decides satisfaction. COMPLETE
// is satisfied (a finished, independently-audited, policy-satisfied migration);
// PARTIAL_VERIFIED / BLOCKED_BASELINE stay unsatisfied — never "проверено".
const completeTerminal = decideNextStep(
  runDir,
  payload({ phase: "TERMINAL", terminal: "COMPLETE", terminalOutcome: { outcome: "COMPLETE", satisfied: true, auditStatus: "PASS", acceptedCheckpoints: 3 }, activeCandidateId: null }),
);
if (completeTerminal.step !== "finish" || completeTerminal.satisfied !== true) {
  throw new Error(`COMPLETE terminal must be satisfied finish: ${JSON.stringify(completeTerminal)}`);
}
if (!completeTerminal.reason.includes("COMPLETE")) throw new Error(`reason must carry the outcome: ${completeTerminal.reason}`);
for (const partialOutcome of ["PARTIAL_VERIFIED", "BLOCKED_BASELINE", "NO_VERIFIED_UPGRADE", "REPAIR_VERIFIED"]) {
  const partialTerminal = decideNextStep(
    runDir,
    payload({ phase: "TERMINAL", terminal: partialOutcome, terminalOutcome: { outcome: partialOutcome, satisfied: false }, activeCandidateId: null }),
  );
  if (partialTerminal.step !== "finish" || partialTerminal.satisfied !== false) {
    throw new Error(`${partialOutcome} terminal must NOT be satisfied: ${JSON.stringify(partialTerminal)}`);
  }
}
// READY-normalised terminal (the same durable outcome, phase folded to READY).
const completeReadyTerminal = decideNextStep(
  runDir,
  payload({ phase: "READY", terminal: "COMPLETE", terminalOutcome: { outcome: "COMPLETE", satisfied: true, auditStatus: "PASS" }, activeCandidateId: null }),
);
if (completeReadyTerminal.step !== "finish" || completeReadyTerminal.satisfied !== true) {
  throw new Error(`READY+terminal COMPLETE must be satisfied finish: ${JSON.stringify(completeReadyTerminal)}`);
}
expectDecision(
  { phase: "READY", activeCandidateId: null, activeCheckpointId: "C1" },
  {},
  "plan-next",
  "actionable",
);

// 3. In-flight candidate keeps the loop from skipping straight to plan-next.
const inflight = expectDecision(
  { phase: "READY", activeCandidateId: "C2" },
  { candidate: { candidateId: "C2", stage: "MATERIALIZED" } },
  "precheck",
  "in-flight",
);
if (inflight.satisfied !== undefined) throw new Error("In-flight candidate must not report satisfied");

// 3b. A consumed leftover (VERIFYING/ACCEPTED/REJECTED) in READY is skipped:
// the acceptance transaction already moved the pointer, the next action is
// fresh planning for the remaining targets.
const leftover = expectDecision(
  { phase: "READY", activeCandidateId: "C2" },
  { candidate: { candidateId: "C2", stage: "VERIFYING" } },
  "plan-next",
  "actionable",
);
if (leftover.step !== "plan-next") throw new Error(`Consumed leftover must fall through to plan-next: ${JSON.stringify(leftover)}`);
for (const consumedStage of ["ACCEPTED", "REJECTED"]) {
  const consumed = expectDecision(
    { phase: "READY", activeCandidateId: "C2" },
    { candidate: { candidateId: "C2", stage: consumedStage } },
    "plan-next",
    "actionable",
  );
  if (consumed.step !== "plan-next") {
    throw new Error(`READY + ${consumedStage} leftover must plan-next: ${JSON.stringify(consumed)}`);
  }
}

// 4. Policy satisfied on a READY checkpoint: R8 — the independent audit of the
// accepted checkpoint must run BEFORE the finish. The first decision is
// `audit` (no recorded PASS/FAIL yet); only after the audit has written its
// evidence does the decision become `finish`.
const satisfiedDir = join(root, "run-satisfied");
mkdirSync(satisfiedDir, { recursive: true });
writeFileSync(
  join(satisfiedDir, "run-config.json"),
  JSON.stringify({ schemaVersion: 1, projectName: "DemoApp", targets: { "is-number": "7.0.0" } }),
  "utf8",
);
const satisfiedUnaudited = decideNextStep(satisfiedDir, payload({ phase: "READY", activeCandidateId: null }, {
  activeCheckpoint: { status: "VERIFIED", fullAssignment: { "is-number": "7.0.0" } },
}));
if (satisfiedUnaudited.step !== "audit" || satisfiedUnaudited.satisfied !== true) {
  throw new Error(`Satisfied policy must cross the audit first: ${JSON.stringify(satisfiedUnaudited)}`);
}
const satisfiedAudited = decideNextStep(satisfiedDir, payload({ phase: "READY", activeCandidateId: null }, {
  activeCheckpoint: { status: "VERIFIED", fullAssignment: { "is-number": "7.0.0" }, audit: { status: "PASS" } },
}));
if (satisfiedAudited.step !== "finish" || satisfiedAudited.satisfied !== true) {
  throw new Error(`Satisfied policy with PASS audit must finish: ${JSON.stringify(satisfiedAudited)}`);
}
const satisfiedAuditedFail = decideNextStep(satisfiedDir, payload({ phase: "READY", activeCandidateId: null }, {
  activeCheckpoint: { status: "VERIFIED", fullAssignment: { "is-number": "7.0.0" }, audit: { status: "FAIL" } },
}));
if (satisfiedAuditedFail.step !== "finish") {
  throw new Error(`A recorded FAIL audit still lets the terminal finish run: ${JSON.stringify(satisfiedAuditedFail)}`);
}
const partial = decideNextStep(satisfiedDir, payload({ phase: "READY", activeCandidateId: null }, {
  activeCheckpoint: { status: "VERIFIED", fullAssignment: { "is-number": "6.0.0" } },
}));
if (partial.step !== "plan-next") throw new Error("Unsatisfied policy must plan-next");

// 5. Repairing = agent GATE with the exact open repair requests handed over.
const repair = expectDecision(
  { phase: "REPAIRING", activeCandidateId: "C2" },
  { openRepairRequests: [{ requestId: "req-1", summary: "lockfile contains stale is-number 6.0.0" }] },
  "agent",
  "request(s)",
);
if (repair.repairRequests?.length !== 1 || repair.repairRequests[0].requestId !== "req-1") {
  throw new Error(`Repair requests must be handed to the agent: ${JSON.stringify(repair.repairRequests)}`);
}
const repairTerse = expectDecision(
  { phase: "REPAIRING" },
  { openRepairRequests: [{ requestId: "req-2", summary: "x" }] },
  "agent",
  "isolated trial",
);
if (repairTerse.repairRequests.length !== 1) throw new Error("Terse repair request must still be surfaced");

// 6. Unknown phase -> explicit triage, never an invented step.
const unknown = decideNextStep(runDir, payload({ phase: "MYSTERY" }));
if (unknown.step !== null || !unknown.reason.includes("UNKNOWN_PHASE")) {
  throw new Error(`Unknown phase must refuse a step: ${JSON.stringify(unknown)}`);
}

// 7. Agent step is a gate: no CLI invocation is invented for it.
if (typeof iterativeStepInvocation === "function") {
  const invocation = iterativeStepInvocation(runDir, "plan-next", "iterative_migration.py", "python");
  if (invocation.command !== "python") throw new Error(`Step invocation malformed: ${JSON.stringify(invocation)}`);
  if (invocation.args[1] !== "--run-dir" || invocation.args[2] !== runDir) {
    throw new Error(`--run-dir must precede the subcommand: ${JSON.stringify(invocation)}`);
  }
  if (!invocation.args.includes("plan-next")) throw new Error(`Step lost: ${JSON.stringify(invocation)}`);
}

// 8. Status consumption: direct JSON print and the statusPath envelope.
const direct = parseIterativeStatusPayload(JSON.stringify(payload({ phase: "PRECHECK" })));
if (!direct || direct.run.phase !== "PRECHECK") throw new Error("Direct status JSON must parse");
const envelopeText = `ITERATIVE_MIGRATION_STATUS_V1 {"event":"status.ready","runId":"iter-runner-1","statusPath":"${join(runDir, ITERATIVE_STATUS_FILENAME).replace(/\\/g, "/")}"}`;
if (parseIterativeStatusPayload(envelopeText) !== undefined) {
  throw new Error("statusPath envelope must defer to the status file");
}
writeFileSync(join(runDir, ITERATIVE_STATUS_FILENAME), JSON.stringify(payload({ phase: "VERIFYING" })), "utf8");
const fromFile = readIterativeStatus(runDir);
if (!fromFile || fromFile.run.phase !== "VERIFYING") throw new Error("readIterativeStatus must read the durable status.json");

// 9. Status invocation shape.
const statusInvocation = iterativeStatusInvocation(runDir, "iterative_migration.py", "python");
if (!statusInvocation.args.includes("status") || !statusInvocation.args.includes("--run-dir")) {
  throw new Error(`Status invocation malformed: ${JSON.stringify(statusInvocation)}`);
}

// 10. #1 durable supervisor: the DRIVE IPC must exist, the one-shot STEP IPC
// must be gone (drive replaces it), and the loop must stop ONLY at an agent
// gate / finish / error / user cancel — never invent a step or fabricate a
// run. Static contract on the owning source so a regression in the loop's
// stop conditions is caught here, not in a dialog.
{
  const { readFileSync } = await import("node:fs");
  const main = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf8");
  const types = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");
  if (!/ipcMain\.handle\('flow:iterative:drive'/.test(main)) throw new Error("flow:iterative:drive handler must exist in main.ts");
  if (/ipcMain\.handle\('flow:iterative:step'/.test(main)) throw new Error("one-shot flow:iterative:step must be REMOVED (drive replaces it)");
  const driveStart = main.indexOf('  const runIterativeDriveWithAutopilot =');
  const driveEnd = main.indexOf("  ipcMain.handle('flow:iterative:drive', runIterativeDriveWithAutopilot)", driveStart);
  if (driveStart < 0 || driveEnd <= driveStart) throw new Error('drive owner must be registered as the production IPC handler');
  const drive = main.slice(driveStart, driveEnd);
  if (!drive.includes("stopped: 'agent-gate'")) throw new Error("drive must stop at the agent GATE");
  if (!drive.includes("stopped: 'finished'")) throw new Error("drive must stop at finish");
  if (!drive.includes("stopped: 'error'")) throw new Error("drive must stop on error");
  if (drive.includes("45 * 60 * 1000") || drive.includes("iteration < 50")) {
    throw new Error("drive must not stop on an implicit time or batch budget");
  }
  if (!drive.includes("while (true)") || !drive.includes("stopped: 'canceled'") ||
      !drive.includes("readAttempt(runDir)?.cancelRequested")) {
    throw new Error("unbounded drive must preserve its durable user-cancel gate");
  }
  if (!drive.includes("'NO_RUN'")) throw new Error("drive must refuse a missing run (NO_RUN), never fabricate begin");
  if (!/(?:decideNextStep\(runDir, payload\))/.test(drive)) throw new Error("drive must recompute the decision from durable state each iteration");
  if (!/IterativeDriveOutcome/.test(types)) throw new Error("IterativeDriveOutcome must exist in types.ts");
  if (!/'agent-gate' \| 'finished' \| 'error' \| 'time-budget' \| 'iteration-budget'/.test(types)) {
    throw new Error("IterativeDriveOutcome.stopped union must list every stop reason");
  }
  // P1.1: the task artifact (human-facing) must never block the coordinator.
  // The agent handler and the drive gate rebuild it from the DURABLE state
  // before a gate; copy-task reuses the same refresh. A TASK_STALE rejection
  // on the repair path is a regression.
  if (/TASK_STALE/.test(main)) throw new Error("repair path must not reject a stale task artifact (P1.1: TASK_STALE removed)");
  if (!main.includes("await refreshIterativeTaskArtifact(runDir, generator, python, workspace.path)")) {
    throw new Error("agent/dispatch paths must rebuild the task artifact from durable state (P1.1)");
  }
  // Postfix P1 (#2): the DISPATCH path must tell a missing exportable state
  // from an OPERATIONAL export failure, and must never seed the attempt with
  // stale task bytes. An export failure is a recoverable TASK_EXPORT_FAILED
  // error; only taskDispatchable (run/policy/checkpoint identity + verified
  // content hashes) may supply the task text, otherwise the prompt is
  // self-sufficient from the durable assignment with an empty ТЗ.
  if (!main.includes("taskRefresh.status === 'export-failed'")) {
    throw new Error("repair dispatch must refuse on an operational export failure (postfix P1 #2)");
  }
  if (!main.includes("TASK_EXPORT_FAILED")) throw new Error("the export failure must surface as recoverable TASK_EXPORT_FAILED (postfix P1 #2)");
  if (!main.includes("taskDispatchable(runDir)")) {
    throw new Error("dispatch must verify the task manifest (identity + content hashes) before seeding the prompt (postfix P1 #2)");
  }
  // Review P1 (contradictory versions): a CANDIDATE repair must never be
  // seeded with the checkpoint-built ТЗ (its trial assignment differs), only
  // the version-neutral bootstrap repair attaches task text. The routing must
  // go through the pure repairPromptTaskText(bootstrap, taskDispatchable(...))
  // contract so the decision is checkable without an Electron run.
  if (!main.includes("repairPromptTaskText(bootstrap, taskDispatchable(runDir))")) {
    throw new Error("repair dispatch must route task text by bootstrap vs candidate (review P1: no checkpoint ТЗ for a candidate repair)");
  }
  // Review re-check (launch boundaries): begin/drive adopt the TESTABLE stream
  // runner; a watchdog KILL never surfaces as success — both handlers branch on
  // result.timedOut (explicit timeout reasons), the begin handler confirms a
  // DURABLE C0 payload before success (never trusts a bare exit 0), and the
  // begin in-flight lock is held inside ONE try/catch/finally so a preparation
  // failure releases it and records a failed attempt.
  const begin = main.slice(main.indexOf("flow:iterative:begin"), driveStart);
  if (!main.includes("./iterative-stream.js")) throw new Error("main must adopt the testable stream runner (iterative-stream.js)");
  if (!begin.includes("result.timedOut")) throw new Error("begin must branch on a watchdog timeout, never claim success");
  if (!begin.includes("!refreshed")) throw new Error("begin must confirm a durable C0 payload before success, never trust a bare exit 0");
  if (!/BEGIN_TIMEOUT/.test(begin)) throw new Error("begin must name the timeout explicitly (BEGIN_TIMEOUT)");
  if (!begin.includes("BEGIN_PREP_FAILED")) throw new Error("begin must turn a preparation failure into a clear BEGIN_PREP_FAILED error");
  if (!/finally\s*\{[\s\S]*iterativeStepInFlight\.delete/.test(begin)) throw new Error("begin must release the in-flight lock in a finally");
}

if (!existsSync(runDir)) throw new Error("fixture missing");
console.log("check-iterative-runner: OK");
