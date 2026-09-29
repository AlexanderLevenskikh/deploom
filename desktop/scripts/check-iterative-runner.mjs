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
expectDecision({ phase: "BOOTSTRAP_REPAIR" }, {}, "verify-bootstrap", "verify");
expectDecision({ phase: "PLANNING", activeCandidateId: "C2" }, {}, "materialize", "materialize");
expectDecision({ phase: "MATERIALIZING" }, {}, "materialize", "resume");
expectDecision({ phase: "PRECHECK" }, {}, "precheck", "diagnostic");
expectDecision(
  { phase: "VERIFYING" },
  {},
  "verify-exact",
  "authoritative",
);
expectDecision({ phase: "TERMINAL", terminal: "BUDGET_EXHAUSTED", activeCandidateId: null }, {}, "finish", "BUDGET_EXHAUSTED");
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

// 4. Policy satisfied on a READY checkpoint -> finish (targets from run-config).
const satisfiedDir = join(root, "run-satisfied");
mkdirSync(satisfiedDir, { recursive: true });
writeFileSync(
  join(satisfiedDir, "run-config.json"),
  JSON.stringify({ schemaVersion: 1, projectName: "DemoApp", targets: { "is-number": "7.0.0" } }),
  "utf8",
);
const satisfied = decideNextStep(satisfiedDir, payload({ phase: "READY", activeCandidateId: null }, {
  activeCheckpoint: { status: "VERIFIED", fullAssignment: { "is-number": "7.0.0" } },
}));
if (satisfied.step !== "finish" || satisfied.satisfied !== true) {
  throw new Error(`Satisfied policy must finish: ${JSON.stringify(satisfied)}`);
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

if (!existsSync(runDir)) throw new Error("fixture missing");
console.log("check-iterative-runner: OK");
