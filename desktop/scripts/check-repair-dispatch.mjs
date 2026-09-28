import { mkdirSync, mkdtempSync, writeFileSync, readFileSync, existsSync, unlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import {
  extractRepairRequiredEnvelope,
  openRepairRequests,
} from "../dist-electron/repair-handoff.js";
import {
  clearRepairDispatchState,
  nextRepairCycleState,
  readRepairDispatchState,
  repairReturnPoint,
  repairDispatchStatePath,
  routeNeedsRepair,
  runBaselineRepairCycles,
  sameRepairTuple,
  writeRepairDispatchState,
} from "../dist-electron/repair-dispatch.js";

const REQUEST_A2 = {
  requestId: "rq-a2",
  project: "Demo",
  mode: "yellow",
  assignment: { a: "2.0.0" },
  fingerprint: "fp-a2",
  snapshotIdentity: "snap-broken",
  failingCommands: [{ command: "yarn lint", exitCode: 1 }],
  diagnosticsTail: "error TS2322",
  reason: "project-source-config-repair",
  disposition: "repair-or-replan",
};
const REQUEST_A3 = {
  requestId: "rq-a3",
  project: "Demo",
  mode: "yellow",
  assignment: { a: "3.0.0" },
  fingerprint: "fp-a3",
  snapshotIdentity: "snap-broken",
  failingCommands: [{ command: "yarn lint", exitCode: 1 }],
  diagnosticsTail: "error TS2322",
  reason: "project-source-config-repair",
  disposition: "repair-or-replan",
};

function envelopeFor(requests, runId) {
  const project = requests[0]?.project ?? "Demo";
  const mode = requests[0]?.mode ?? "yellow";
  const assignment = requests[0]?.fingerprint ?? "fp";
  const line = {
    type: "deploom-baseline-progress",
    schemaVersion: 2,
    runId: `baseline-run-${runId}`,
    project,
    mode,
    phase: "repair-required-terminal",
    assignment,
    terminalStatus: "REPAIR_REQUIRED",
    repairRequests: requests,
    startedAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  return `DEPLOOM_PROGRESS_V2 ${JSON.stringify(line)}`;
}

const tmp = mkdtempSync(join(tmpdir(), "repair-dispatch-"));

// ---------------------------------------------------------------------------
// 1. Detection: extractRepairRequiredEnvelope reads the generator's machine
//    progress channel (DEPLOOM_PROGRESS_V2 on stderr), including the exit-0
//    repair-required terminal.
// ---------------------------------------------------------------------------
{
  const output = [
    `DEPLOOM_PROGRESS_V2 ${JSON.stringify({ type: "deploom-baseline-progress", phase: "solve-and-verify-started" })}`,
    envelopeFor([REQUEST_A2], 1),
  ].join("\n");
  const envelope = extractRepairRequiredEnvelope(output);
  if (!envelope || envelope.terminalStatus !== "REPAIR_REQUIRED") throw new Error("terminal envelope must parse");
  if (envelope.runId !== "baseline-run-1" || envelope.project !== "Demo" || envelope.mode !== "yellow") {
    throw new Error(`envelope header fields lost: ${JSON.stringify(envelope)}`);
  }
  if (envelope.requests.length !== 1 || envelope.requests[0].fingerprint !== "fp-a2") {
    throw new Error(`envelope repairRequests must carry the exact durable set: ${JSON.stringify(envelope.requests)}`);
  }
  // Only the LAST terminal envelope matters (a run may loop within one output).
  const repeated = `${envelopeFor([REQUEST_A2], 1)}\n${envelopeFor([REQUEST_A3], 2)}`;
  const last = extractRepairRequiredEnvelope(repeated);
  if (last.runId !== "baseline-run-2" || last.requests[0].fingerprint !== "fp-a3") {
    throw new Error("last terminal envelope must win");
  }
  // A run that never hits the terminal must not look repairable.
  if (extractRepairRequiredEnvelope("DEPLOOM_PROGRESS_V2 " + JSON.stringify({ phase: "mode-passed" })) !== undefined) {
    throw new Error("non-terminal output must not produce an envelope");
  }
  if (extractRepairRequiredEnvelope("not a json line") !== undefined) throw new Error("garbage output must not parse");
}

// ---------------------------------------------------------------------------
// 2. State machine: routing, restart-aware episode bookkeeping, return point.
// ---------------------------------------------------------------------------
{
  if (routeNeedsRepair(undefined)) throw new Error("no envelope must not route to repair");
  const envelope = extractRepairRequiredEnvelope(envelopeFor([REQUEST_A2], 1));
  if (!routeNeedsRepair(envelope)) throw new Error("REPAIR_REQUIRED envelope must route to repair");

  const fresh = nextRepairCycleState({ previous: undefined, envelope, workspaceId: "ws", project: "Demo" });
  if (fresh.cycle !== 1 || fresh.requests.length !== 1 || fresh.agentSessionId !== undefined) {
    throw new Error(`fresh episode must start at cycle 1: ${JSON.stringify(fresh)}`);
  }

  const openPrevious = { ...fresh, cycle: 2, agentSessionId: "session-abc", dispatchedAt: new Date().toISOString() };
  const resumed = nextRepairCycleState({ previous: openPrevious, envelope, workspaceId: "ws", project: "Demo" });
  if (resumed.cycle !== 2 || resumed.agentSessionId !== "session-abc") {
    throw new Error(`restart must resume the SAME episode and session: ${JSON.stringify(resumed)}`);
  }

  const closedPrevious = { ...fresh, outcome: "exhausted", completedAt: new Date().toISOString() };
  const afterClosed = nextRepairCycleState({ previous: closedPrevious, envelope, workspaceId: "ws", project: "Demo" });
  if (afterClosed.cycle !== 2 || afterClosed.agentSessionId !== undefined) {
    throw new Error("a closed episode must start a fresh continuation");
  }

  // Return point: exact-tuple closure only.
  const returnA2 = repairReturnPoint(fresh, [], undefined);
  if (!returnA2.repaired || returnA2.stillOpen.length !== 0) throw new Error("all targeted tuple gone -> repaired");
  const returnOpen = repairReturnPoint(fresh, [REQUEST_A2], undefined);
  if (returnOpen.repaired) throw new Error("targeted tuple still open -> NOT repaired");
  // R5 #1: a same-name, different-version request is a DIFFERENT tuple. It
  // neither blocks A@2's closure nor closes A@2 itself.
  const returnNameOnly = repairReturnPoint(fresh, [REQUEST_A3], undefined);
  if (!returnNameOnly.repaired) throw new Error("same-name different-tuple must not block exact-tuple closure");
  const noEnvelope = repairReturnPoint(fresh, [], extractRepairRequiredEnvelope(envelopeFor([REQUEST_A3], 9)));
  if (noEnvelope.repaired) throw new Error("a still-REPAIR_REQUIRED re-verification is not a repair");
  if (!sameRepairTuple(REQUEST_A2, { ...REQUEST_A2 })) throw new Error("identical request must be the same tuple");
  if (sameRepairTuple(REQUEST_A2, REQUEST_A3)) throw new Error("different version must be a different tuple");
}

// ---------------------------------------------------------------------------
// 3. Integration: the full chain against fake CLI/agent executables.
//    The fakes are injected through the SAME runBaselineRepairCycles call
//    shape the production adapter (main.ts) uses with the real agent + real
//    Baseline CLI.
// ---------------------------------------------------------------------------
const fixtureDir = mkdtempSync(join(tmpdir(), "repair-dispatch-fixture-"));
const stateDir = join(fixtureDir, ".dependency-roadmap", "state");
mkdirSync(stateDir, { recursive: true });
const handoffFile = join(stateDir, "repair-requests.json");
const gateFile = join(stateDir, "verify-gate.json");
const requestsFile = handoffFile;

const fakeBaselineCli = join(fixtureDir, "fake-baseline-cli.mjs");
writeFileSync(fakeBaselineCli, `
import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { join } from "node:path";
const stateDir = process.argv[2];
const gate = join(stateDir, "verify-gate.json");
const requestsFile = join(stateDir, "repair-requests.json");
const gateValue = existsSync(gate) ? readFileSync(gate, "utf8").trim() : "open";
if (gateValue === "open" || gateValue === "stillopen") {
  const request = gateValue === "stillopen"
    ? ${JSON.stringify(REQUEST_A2)}
    : ${JSON.stringify(REQUEST_A2)};
  const keep = gateValue === "stillopen"
    ? [${JSON.stringify(REQUEST_A2)}]
    : [${JSON.stringify(REQUEST_A2)}];
  writeFileSync(requestsFile, JSON.stringify({ schemaVersion: 1, runId: "baseline-run-1", requests: keep }));
  const payload = { type: "deploom-baseline-progress", schemaVersion: 2, runId: "baseline-run-1", project: request.project, mode: request.mode, phase: "repair-required-terminal", assignment: request.fingerprint, terminalStatus: "REPAIR_REQUIRED", repairRequests: keep };
  process.stderr.write("DEPLOOM_PROGRESS_V2 " + JSON.stringify(payload) + "\\n");
} else {
  // gateValue === "closed": authoritative verifier resolved the repair.
  if (existsSync(requestsFile)) unlinkSync(requestsFile);
  process.stderr.write("DEPLOOM_PROGRESS_V2 " + JSON.stringify({ type: "deploom-baseline-progress", phase: "mode-passed", terminalStatus: "SAT_PROVEN" }) + "\\n");
}
process.exit(0);
`);

const fakeRepairAgent = join(fixtureDir, "fake-repair-agent.mjs");
writeFileSync(fakeRepairAgent, `
import { writeFileSync } from "node:fs";
const [resultPath, mode] = process.argv.slice(2);
writeFileSync(resultPath, JSON.stringify({ status: "repaired", reason: "fixture source repaired" }));
if (mode === "resume") process.stderr.write("session:resumed-session-abc\\n");
else process.stderr.write("session:fixture-session-cyc1\\n");
process.exit(0);
`);

const runNode = (script, args, cwd) => {
  const result = spawnSync(process.execPath, [script, ...args], { cwd, encoding: "utf8" });
  return { code: result.status, stderr: result.stderr ?? "", stdout: result.stdout ?? "" };
};

let currentSessions = [];
const harness = {
  dispatchCalls: 0,
  dispatchSessionArgs: [],
  reVerifyCalls: 0,
  async dispatch(requests, resumeSessionId) {
    this.dispatchCalls += 1;
    this.dispatchSessionArgs.push(resumeSessionId ?? null);
    if (process.env.REPAIR_DISPATCH_DEBUG === "1") {
      console.log(`[debug] dispatch #${this.dispatchCalls}: resumeSessionId=${JSON.stringify(resumeSessionId)}`);
    }
    const resultPath = join(fixtureDir, `agent-result-${this.dispatchCalls}.json`);
    const args = resumeSessionId ? [resultPath, "resume"] : [resultPath];
    const ran = runNode(fakeRepairAgent, args, fixtureDir);
    const session = /session:([^\r\n]+)/.exec(ran.stderr)?.[1];
    if (session) currentSessions.push(session);
    return { status: "repaired", reason: "fixture", sessionId: session };
  },
  async reVerify() {
    this.reVerifyCalls += 1;
    const ran = runNode(fakeBaselineCli, [stateDir], fixtureDir);
    return { output: `${ran.stderr}\n${ran.stdout}` };
  },
  readOpen() {
    return openRepairRequests(stateDir).requests;
  },
};

// --- Scenario A: fresh dispatch -> repair -> authoritative re-verify -> repaired.
{
  currentSessions = [];
  harness.dispatchCalls = 0;
  harness.dispatchSessionArgs = [];
  harness.reVerifyCalls = 0;
  writeFileSync(gateFile, "open", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  if (!envelope) throw new Error("fake baseline must produce a terminal envelope on run 1");
  const stateDirForRun = stateDir;
  const result = await runBaselineRepairCycles({
    envelope,
    previous: undefined,
    stateDir: stateDirForRun,
    workspaceId: "ws",
    project: "Demo",
    onEvent: () => {},
    dispatchRepairAgent: async ({ requests, resumeSessionId }) => harness.dispatch(requests, resumeSessionId),
    runReVerification: async () => {
      writeFileSync(gateFile, "closed", "utf8");
      return harness.reVerify();
    },
    readOpenRequests: () => harness.readOpen(),
  });
  if (result.outcome !== "repaired") throw new Error(`scenario A must repair, got ${result.outcome}`);
  if (result.cyclesUsed !== 1) throw new Error(`scenario A must finish in one cycle, got ${result.cyclesUsed}`);
  if (harness.dispatchCalls !== 1 || harness.reVerifyCalls !== 1) {
    throw new Error(`scenario A call counts wrong: dispatch=${harness.dispatchCalls}, reVerify=${harness.reVerifyCalls}`);
  }
  if (harness.dispatchSessionArgs[0] !== null) throw new Error("fresh dispatch must not resume a session");
  const persisted = readRepairDispatchState(stateDirForRun, "Demo");
  if (!persisted || persisted.outcome !== "repaired" || persisted.cycle !== 1) {
    throw new Error(`dispatch state must record repaired: ${JSON.stringify(persisted)}`);
  }
  if (persisted.agentSessionId !== "fixture-session-cyc1") throw new Error("agent session id must be persisted");
  if (existsSync(handoffFile)) throw new Error("resolved repair must leave no open handoff");
}

// --- Scenario B: restart mid-episode -> the durable state resumes the same
//     episode and the same agent session instead of starting over.
{
  currentSessions = [];
  harness.dispatchCalls = 0;
  harness.dispatchSessionArgs = [];
  harness.reVerifyCalls = 0;
  // Seed a still-open dispatch state exactly as a killed app would leave it.
  const seeded = {
    schemaVersion: 1,
    workspaceId: "ws",
    project: "Demo",
    cycle: 2,
    requests: [REQUEST_A2],
    agentSessionId: "session-before-restart",
    lastTerminalRunId: "baseline-run-1",
    dispatchedAt: new Date().toISOString(),
  };
  writeRepairDispatchState(stateDir, seeded);
  writeFileSync(gateFile, "open", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  const result = await runBaselineRepairCycles({
    envelope,
    previous: readRepairDispatchState(stateDir, "Demo"),
    stateDir,
    workspaceId: "ws",
    project: "Demo",
    onEvent: () => {},
    dispatchRepairAgent: async ({ requests, resumeSessionId }) => harness.dispatch(requests, resumeSessionId),
    runReVerification: async () => {
      writeFileSync(gateFile, "closed", "utf8");
      return harness.reVerify();
    },
    readOpenRequests: () => harness.readOpen(),
  });
  if (result.outcome !== "repaired") throw new Error(`scenario B must repair after restart, got ${result.outcome}`);
  if (result.state.cycle !== 2) throw new Error(`restart must continue cycle 2, got ${result.state.cycle}`);
  if (harness.dispatchSessionArgs[0] !== "session-before-restart") {
    throw new Error(`restart must resume the persisted session, got ${JSON.stringify(harness.dispatchSessionArgs)}`);
  }
  if (readRepairDispatchState(stateDir, "Demo")?.outcome !== "repaired") throw new Error("scenario B state must be closed repaired");
}

// --- Scenario C: blocked agent -> blocked outcome, nothing re-verified.
{
  harness.dispatchCalls = 0;
  harness.dispatchSessionArgs = [];
  harness.reVerifyCalls = 0;
  clearRepairDispatchState(stateDir, "Demo");
  writeFileSync(gateFile, "open", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  const result = await runBaselineRepairCycles({
    envelope,
    previous: undefined,
    stateDir,
    workspaceId: "ws",
    project: "Demo",
    onEvent: () => {},
    dispatchRepairAgent: async () => ({ status: "blocked", reason: "safe repair impossible" }),
    runReVerification: async () => { throw new Error("blocked agent must not trigger re-verification"); },
    readOpenRequests: () => harness.readOpen(),
  });
  if (result.outcome !== "blocked") throw new Error(`scenario C must be blocked, got ${result.outcome}`);
  if (harness.reVerifyCalls !== 0) throw new Error("blocked must skip re-verification");
  if (existsSync(handoffFile) === false) throw new Error("blocked repair must leave the handoff open");
}

// --- Scenario D (R5 #1 through the real chain): the exact tuple A@2 is the
//     closure key. A re-verification that still leaves A@2 open is NOT
//     repaired; one that leaves only a different tuple open IS repaired.
{
  harness.dispatchCalls = 0;
  harness.dispatchSessionArgs = [];
  harness.reVerifyCalls = 0;
  clearRepairDispatchState(stateDir, "Demo");
  writeFileSync(gateFile, "stillopen", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  if (!envelope) throw new Error("stillopen fake must produce a terminal envelope");
  // Run with re-verification still leaving A@2 open -> must NOT repair;
  // the next agent dispatch must be a RESUME (same episode), not a fresh one.
  const notRepaired = await runBaselineRepairCycles({
    envelope,
    previous: undefined,
    stateDir,
    workspaceId: "ws",
    project: "Demo",
    maxCycles: 2,
    onEvent: () => {},
    dispatchRepairAgent: async ({ resumeSessionId }) => harness.dispatch([REQUEST_A2], resumeSessionId),
    runReVerification: async () => {
      // First re-verify keeps the tuple open (stillopen gate); second closes.
      if (harness.reVerifyCalls === 0) writeFileSync(gateFile, "stillopen", "utf8");
      else writeFileSync(gateFile, "closed", "utf8");
      harness.reVerifyCalls += 1;
      return harness.reVerify();
    },
    readOpenRequests: () => harness.readOpen(),
  });
  if (notRepaired.outcome !== "repaired") throw new Error(`scenario D must repair on the second cycle, got ${notRepaired.outcome}`);
  if (notRepaired.cyclesUsed !== 2) throw new Error(`scenario D must take two cycles, got ${notRepaired.cyclesUsed}`);
  if (harness.dispatchCalls !== 2) throw new Error("scenario D must dispatch the agent twice");
  if (harness.dispatchSessionArgs[1] !== "fixture-session-cyc1") {
    throw new Error(`scenario D second dispatch must resume the first session, got ${JSON.stringify(harness.dispatchSessionArgs)}`);
  }
}

// ---------------------------------------------------------------------------
console.log("check-repair-dispatch: OK");

try {
  rmSync(tmp, { recursive: true, force: true });
  rmSync(fixtureDir, { recursive: true, force: true });
} catch { /* temp cleanup is best-effort */ }
