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
import {
  applyRepairReVerification,
  buildRepairSettingsFile,
  createIsolatedRepairCheckout,
  resolveGitHead,
} from "../dist-electron/repair-checkout.js";

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
  if (fresh.attemptsUsed !== 0 || fresh.phase !== undefined) {
    throw new Error(`fresh episode must start with no attempts and no phase: ${JSON.stringify(fresh)}`);
  }

  const openPrevious = { ...fresh, cycle: 2, agentSessionId: "session-abc", attemptsUsed: 1, phase: "agent-running", dispatchedAt: new Date().toISOString() };
  const resumed = nextRepairCycleState({ previous: openPrevious, envelope, workspaceId: "ws", project: "Demo" });
  if (resumed.cycle !== 2 || resumed.agentSessionId !== "session-abc") {
    throw new Error(`restart must resume the SAME episode and session: ${JSON.stringify(resumed)}`);
  }
  if (resumed.attemptsUsed !== 1 || resumed.phase !== "agent-running") {
    throw new Error(`restart must resume the durable attempts/phase: ${JSON.stringify(resumed)}`);
  }

  const closedPrevious = { ...fresh, outcome: "exhausted", completedAt: new Date().toISOString() };
  const afterClosed = nextRepairCycleState({ previous: closedPrevious, envelope, workspaceId: "ws", project: "Demo" });
  if (afterClosed.cycle !== 2 || afterClosed.agentSessionId !== undefined || afterClosed.attemptsUsed !== 0) {
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
//    Baseline CLI. All scenarios run the new API (ensureRepairCheckout,
//    originalPath, sourceBranch, onSessionId) -- absent them the run crashes
//    before doing anything (the R7 P2 reproduction).
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
  midRunState: undefined,
  async dispatch(requests, resumeSessionId, onSessionId) {
    this.dispatchCalls += 1;
    this.dispatchSessionArgs.push(resumeSessionId ?? null);
    if (process.env.REPAIR_DISPATCH_DEBUG === "1") {
      console.log(`[debug] dispatch #${this.dispatchCalls}: resumeSessionId=${JSON.stringify(resumeSessionId)}`);
    }
    // The production agent reports its session id on its FIRST output line,
    // while it is still running. Surface it through onSessionId BEFORE the
    // agent returns so the dispatcher persists it durably mid-run (R6 P1#2).
    const session = resumeSessionId ? "resumed-session-abc" : "fixture-session-cyc1";
    onSessionId?.(session);
    const resultPath = join(fixtureDir, `agent-result-${this.dispatchCalls}.json`);
    const args = resumeSessionId ? [resultPath, "resume"] : [resultPath];
    const ran = runNode(fakeRepairAgent, args, fixtureDir);
    if (session) currentSessions.push(session);
    // Capture what the dispatcher has DURABLY persisted at this moment (still
    // inside the agent call, before it resolved).
    this.midRunState = readRepairDispatchState(stateDir, "Demo");
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

// A shared fake of the production ensureRepairCheckout adapter: creates the
// checkout dir + a settings file and returns the durable paths. Scenarios that
// must PROVE the adapter is skipped (restart resume) override it with a throw.
const fakeEnsureRepairCheckout = async ({ stateDir, project }) => {
  const checkoutDir = join(stateDir, `repair-checkout-${project}`);
  if (!existsSync(checkoutDir)) mkdirSync(checkoutDir, { recursive: true });
  const settingsPath = join(stateDir, `repair-settings-${project}.json`);
  if (!existsSync(settingsPath)) {
    writeFileSync(settingsPath, JSON.stringify({
      schemaVersion: 1,
      projects: [{ name: project, path: checkoutDir, git: { sourceBranch: "master" } }],
    }, null, 2) + "\n", "utf8");
  }
  return { path: checkoutDir, sourceCommit: "a".repeat(40), settingsPath };
};

const baseRunInput = (overrides) => ({
  envelope: undefined,
  previous: undefined,
  stateDir,
  workspaceId: "ws",
  project: "Demo",
  originalPath: join(fixtureDir, "source-repo"),
  sourceBranch: "master",
  ensureRepairCheckout: fakeEnsureRepairCheckout,
  onEvent: () => {},
  dispatchRepairAgent: async ({ requests, resumeSessionId, onSessionId }) => harness.dispatch(requests, resumeSessionId, onSessionId),
  runReVerification: async () => harness.reVerify(),
  readOpenRequests: () => harness.readOpen(),
  ...overrides,
});

const resetHarness = () => {
  currentSessions = [];
  harness.dispatchCalls = 0;
  harness.dispatchSessionArgs = [];
  harness.reVerifyCalls = 0;
  harness.midRunState = undefined;
};

// --- Scenario A: fresh dispatch -> repair -> authoritative re-verify -> repaired.
{
  resetHarness();
  writeFileSync(gateFile, "open", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  if (!envelope) throw new Error("fake baseline must produce a terminal envelope on run 1");
  const result = await runBaselineRepairCycles(baseRunInput({
    envelope,
    runReVerification: async () => {
      writeFileSync(gateFile, "closed", "utf8");
      return harness.reVerify();
    },
  }));
  if (result.outcome !== "repaired") throw new Error(`scenario A must repair, got ${result.outcome}`);
  if (result.cyclesUsed !== 1) throw new Error(`scenario A must finish in one cycle, got ${result.cyclesUsed}`);
  if (harness.dispatchCalls !== 1 || harness.reVerifyCalls !== 1) {
    throw new Error(`scenario A call counts wrong: dispatch=${harness.dispatchCalls}, reVerify=${harness.reVerifyCalls}`);
  }
  if (harness.dispatchSessionArgs[0] !== null) throw new Error("fresh dispatch must not resume a session");
  const persisted = readRepairDispatchState(stateDir, "Demo");
  if (!persisted || persisted.outcome !== "repaired" || persisted.cycle !== 1) {
    throw new Error(`dispatch state must record repaired: ${JSON.stringify(persisted)}`);
  }
  if (persisted.agentSessionId !== "fixture-session-cyc1") throw new Error("agent session id must be persisted");
  if (persisted.attemptsUsed !== 1) throw new Error(`scenario A must credit exactly one attempt, got ${persisted.attemptsUsed}`);
  if (persisted.repairCheckoutPath !== join(stateDir, "repair-checkout-Demo")) {
    throw new Error(`scenario A must record the repair checkout path: ${JSON.stringify(persisted.repairCheckoutPath)}`);
  }
  if (existsSync(handoffFile)) throw new Error("resolved repair must leave no open handoff");
}

// --- Scenario B: restart mid-episode, agent still running -> the SAME episode
//     resumes the SAME session WITHOUT consuming a new attempt (R6 P1#2).
{
  resetHarness();
  const checkoutDir = join(stateDir, "repair-checkout-Demo");
  mkdirSync(checkoutDir, { recursive: true });
  const seeded = {
    schemaVersion: 1,
    workspaceId: "ws",
    project: "Demo",
    cycle: 2,
    requests: [REQUEST_A2],
    attemptsUsed: 1,
    phase: "agent-running",
    agentSessionId: "session-before-restart",
    lastTerminalRunId: "baseline-run-1",
    repairCheckoutPath: checkoutDir,
    repairSourceCommit: "a".repeat(40),
    repairSettingsPath: join(stateDir, "repair-settings-Demo.json"),
    dispatchedAt: new Date().toISOString(),
  };
  writeRepairDispatchState(stateDir, seeded);
  writeFileSync(gateFile, "open", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  const result = await runBaselineRepairCycles(baseRunInput({
    envelope,
    previous: readRepairDispatchState(stateDir, "Demo"),
    ensureRepairCheckout: async () => { throw new Error("restarted mid-agent episode must NOT recreate the repair checkout"); },
    runReVerification: async () => {
      writeFileSync(gateFile, "closed", "utf8");
      return harness.reVerify();
    },
  }));
  if (result.outcome !== "repaired") throw new Error(`scenario B must repair after restart, got ${result.outcome}`);
  if (result.state.cycle !== 2) throw new Error(`restart must continue cycle 2, got ${result.state.cycle}`);
  if (result.state.attemptsUsed !== 1) {
    throw new Error(`restart resume must NOT consume a new attempt, got attemptsUsed=${result.state.attemptsUsed}`);
  }
  if (harness.dispatchSessionArgs[0] !== "session-before-restart") {
    throw new Error(`restart must resume the persisted session, got ${JSON.stringify(harness.dispatchSessionArgs)}`);
  }
  if (readRepairDispatchState(stateDir, "Demo")?.outcome !== "repaired") throw new Error("scenario B state must be closed repaired");
}

// --- Scenario C: blocked agent -> blocked outcome, nothing re-verified.
{
  resetHarness();
  clearRepairDispatchState(stateDir, "Demo");
  writeFileSync(gateFile, "open", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  if (!envelope) throw new Error("scenario E fake must produce a terminal envelope");
  // The crash happens BEFORE the authoritative re-verification; after restore
  // the verifier will close the handoff (gate flips to closed).
  writeFileSync(gateFile, "closed", "utf8");
  const result = await runBaselineRepairCycles(baseRunInput({
    envelope,
    dispatchRepairAgent: async () => ({ status: "blocked", reason: "safe repair impossible" }),
    runReVerification: async () => { throw new Error("blocked agent must not trigger re-verification"); },
  }));
  if (result.outcome !== "blocked") throw new Error(`scenario C must be blocked, got ${result.outcome}`);
  if (harness.reVerifyCalls !== 0) throw new Error("blocked must skip re-verification");
  if (existsSync(handoffFile) === false) throw new Error("blocked repair must leave the handoff open");
}

// --- Scenario D (R5 #1 through the real chain): the exact tuple A@2 is the
//     closure key. A re-verification that still leaves A@2 open is NOT
//     repaired; one that leaves only a different tuple open IS repaired.
{
  resetHarness();
  clearRepairDispatchState(stateDir, "Demo");
  writeFileSync(gateFile, "stillopen", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  if (!envelope) throw new Error("stillopen fake must produce a terminal envelope");
  const notRepaired = await runBaselineRepairCycles(baseRunInput({
    envelope,
    maxCycles: 2,
    runReVerification: async () => {
      // First re-verify keeps the tuple open (stillopen gate); second closes.
      if (harness.reVerifyCalls === 0) writeFileSync(gateFile, "stillopen", "utf8");
      else writeFileSync(gateFile, "closed", "utf8");
      harness.reVerifyCalls += 1;
      return harness.reVerify();
    },
  }));
  if (notRepaired.outcome !== "repaired") throw new Error(`scenario D must repair on the second cycle, got ${notRepaired.outcome}`);
  if (notRepaired.cyclesUsed !== 2) throw new Error(`scenario D must take two cycles, got ${notRepaired.cyclesUsed}`);
  if (harness.dispatchCalls !== 2) throw new Error("scenario D must dispatch the agent twice");
  if (notRepaired.state.attemptsUsed !== 2) throw new Error(`scenario D must credit two attempts, got ${notRepaired.state.attemptsUsed}`);
  if (harness.dispatchSessionArgs[1] !== "fixture-session-cyc1") {
    throw new Error(`scenario D second dispatch must resume the first session, got ${JSON.stringify(harness.dispatchSessionArgs)}`);
  }
}

// --- Scenario E (R6 P1#2): the session id + phase + attempt are persisted
//     DURABLY while the agent is still running -- the durable file, written by
//     the REAL dispatcher before the agent returned, is what a restart reads.
{
  resetHarness();
  clearRepairDispatchState(stateDir, "Demo");
  writeFileSync(gateFile, "open", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  if (!envelope) throw new Error("scenario E fake must produce a terminal envelope");
  // The crash happens BEFORE the authoritative re-verification; after restore
  // the verifier will close the handoff (gate flips to closed).
  writeFileSync(gateFile, "closed", "utf8");
  // First run: the agent calls onSessionId and then the app "dies" (the call
  // never returns a result). Anything written after that point would be lost;
  // only what was persisted BEFORE the crash survives.
  const crashError = await runBaselineRepairCycles(baseRunInput({
    envelope,
    dispatchRepairAgent: async ({ onSessionId }) => {
      onSessionId?.("session-e1");
      throw new Error("simulated app crash mid-agent");
    },
  })).then(
    () => { throw new Error("crashing agent must abort the first run"); },
    (error) => error,
  );
  if (!/simulated app crash/.test(String(crashError?.message ?? crashError))) {
    throw new Error(`first run must abort with the simulated crash: ${String(crashError?.message ?? crashError)}`);
  }
  const crashed = readRepairDispatchState(stateDir, "Demo");
  if (!crashed) throw new Error("crash must leave a durable dispatch state");
  if (crashed.phase !== "agent-running") throw new Error(`crash must leave phase agent-running, got ${JSON.stringify(crashed.phase)}`);
  if (crashed.attemptsUsed !== 1) throw new Error(`crash must credit the attempt, got ${crashed.attemptsUsed}`);
  if (crashed.agentSessionId !== "session-e1") throw new Error(`crash must persist the session id, got ${crashed.agentSessionId}`);
  // Restart: the SAME dispatcher reads the file and resumes the SAME attempt.
  const restarted = await runBaselineRepairCycles(baseRunInput({
    envelope,
    previous: readRepairDispatchState(stateDir, "Demo"),
  }));
  if (restarted.outcome !== "repaired") throw new Error(`restart after mid-agent crash must repair, got ${restarted.outcome}`);
  if (restarted.state.attemptsUsed !== 1) throw new Error(`restart must NOT consume a new attempt, got attemptsUsed=${restarted.state.attemptsUsed}`);
  if (harness.dispatchSessionArgs[0] !== "session-e1") {
    throw new Error(`restart must resume session session-e1, got ${JSON.stringify(harness.dispatchSessionArgs)}`);
  }
}

// --- Scenario E2 (R6 P1#2): a crash on the LAST allowed attempt must resume
//     the in-flight attempt -- never a premature exhausted (the R7 reproduction
//     phase=agent-running, attemptsUsed=maxCycles => exhausted).
{
  resetHarness();
  clearRepairDispatchState(stateDir, "Demo");
  writeFileSync(gateFile, "open", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  if (!envelope) throw new Error("scenario E2 fake must produce a terminal envelope");
  writeFileSync(gateFile, "closed", "utf8");
  const maxCycles = 2;
  // Attempt 1 crashes mid-run.
  await runBaselineRepairCycles(baseRunInput({
    envelope,
    maxCycles,
    runReVerification: async () => { throw new Error("must not verify before the agent crashed"); },
    dispatchRepairAgent: async ({ onSessionId }) => { onSessionId?.("session-e2-1"); throw new Error("crash on attempt 1"); },
  })).then(() => { throw new Error("attempt-1 crash must abort"); }, () => {});
  let state = readRepairDispatchState(stateDir, "Demo");
  if (state.attemptsUsed !== 1 || state.phase !== "agent-running") {
    throw new Error(`attempt-1 crash state wrong: ${JSON.stringify(state)}`);
  }
  // Resume attempt 1; its re-verify is stillopen (attempt 1 consumed), then
  // attempt 2 (THE LAST ALLOWED) starts and the agent crashes mid-run again.
  let callsInRun = 0;
  await runBaselineRepairCycles(baseRunInput({
    envelope,
    previous: readRepairDispatchState(stateDir, "Demo"),
    maxCycles,
    runReVerification: async () => {
      writeFileSync(gateFile, "stillopen", "utf8");
      return harness.reVerify();
    },
    dispatchRepairAgent: async ({ resumeSessionId, onSessionId }) => {
      callsInRun += 1;
      if (callsInRun === 1) {
        if (resumeSessionId !== "session-e2-1") throw new Error("attempt-1 resume must reuse session-e2-1");
        return { status: "repaired", reason: "fixture", sessionId: resumeSessionId };
      }
      onSessionId?.("session-e2-2");
      throw new Error("crash on the last allowed attempt");
    },
  })).then(() => { throw new Error("last-attempt crash must abort"); }, () => {});
  state = readRepairDispatchState(stateDir, "Demo");
  if (state.phase !== "agent-running" || state.attemptsUsed !== 2) {
    throw new Error(`last-attempt crash must stay agent-running at attempt 2, got ${JSON.stringify(state)}`);
  }
  // Restart at the exhausted NEW-attempt budget: the in-flight attempt 2 must
  // resume (no new attempt), its re-verification closes -> repaired. This is
  // the exact reproduction the R7 review failed on.
  const last = await runBaselineRepairCycles(baseRunInput({
    envelope,
    previous: readRepairDispatchState(stateDir, "Demo"),
    maxCycles,
    runReVerification: async () => {
      // The previous run left the gate 'stillopen'; the FINAL re-verification
      // must close the repair.
      writeFileSync(gateFile, "closed", "utf8");
      return harness.reVerify();
    },
  }));
  if (last.outcome !== "repaired") throw new Error(`resume of the last in-flight attempt must repair, got ${last.outcome}`);
  if (last.state.attemptsUsed !== 2) throw new Error(`last-attempt resume must NOT consume a new attempt, got attemptsUsed=${last.state.attemptsUsed}`);
  if (harness.dispatchSessionArgs[harness.dispatchSessionArgs.length - 1] !== "session-e2-2") {
    throw new Error(`last-attempt resume must pass session-e2-2, got ${JSON.stringify(harness.dispatchSessionArgs)}`);
  }
}

// --- Scenario F (R6 P1#2): a restart whose durable phase was
//     'verification-pending' goes STRAIGHT back to the re-verification of the
//     ALREADY-CREDITED attempt: no new dispatch, and the attempt number is
//     attemptsUsed, not attemptsUsed + 1.
{
  resetHarness();
  clearRepairDispatchState(stateDir, "Demo");
  const checkoutDir = join(stateDir, "repair-checkout-Demo");
  mkdirSync(checkoutDir, { recursive: true });
  const seeded = {
    schemaVersion: 1,
    workspaceId: "ws",
    project: "Demo",
    cycle: 2,
    requests: [REQUEST_A2],
    attemptsUsed: 2,
    phase: "verification-pending",
    agentSessionId: "session-pending",
    lastTerminalRunId: "baseline-run-1",
    repairCheckoutPath: checkoutDir,
    repairSourceCommit: "a".repeat(40),
    repairSettingsPath: join(stateDir, "repair-settings-Demo.json"),
    dispatchedAt: new Date().toISOString(),
  };
  writeRepairDispatchState(stateDir, seeded);
  writeFileSync(gateFile, "open", "utf8");
  const envelope = extractRepairRequiredEnvelope(runNode(fakeBaselineCli, [stateDir], fixtureDir).stderr);
  if (!envelope) throw new Error("scenario F fake must produce a terminal envelope");
  // After the restore, the re-verification closes the handoff.
  writeFileSync(gateFile, "closed", "utf8");
  const result = await runBaselineRepairCycles(baseRunInput({
    envelope,
    previous: readRepairDispatchState(stateDir, "Demo"),
    ensureRepairCheckout: async () => { throw new Error("verification-pending restore must not recreate the checkout"); },
    dispatchRepairAgent: async () => { throw new Error("verification-pending restore must not dispatch a new agent"); },
    runReVerification: async ({ attempt }) => {
      if (attempt !== 2) throw new Error(`verification-pending restore must reuse the credited attempt (attemptsUsed=2), got ${attempt}`);
      return harness.reVerify();
    },
  }));
  if (result.outcome !== "repaired") throw new Error(`scenario F must repair on the pending re-verify, got ${result.outcome}`);
  if (harness.dispatchCalls !== 0) throw new Error("scenario F must not dispatch a new agent");
  if (harness.reVerifyCalls !== 1) throw new Error(`scenario F must run exactly one re-verification, got ${harness.reVerifyCalls}`);
  if (readRepairDispatchState(stateDir, "Demo")?.attemptsUsed !== 2) throw new Error("scenario F must not consume an extra attempt");
}

// --- Scenario G (R6/R7 P1#1): the Desktop's isolated repair checkout helpers
//     against a REAL git fixture -- clone --shared local-only at the pinned
//     commit, a TRACKED-file edit (the agent's work), the repair settings
//     remap kept in the SAME directory as the original settings (workspace base
//     contract), and the re-verification spec redirection + fail-closed
//     repair-capture env.
{
  const gitFixture = mkdtempSync(join(tmpdir(), "repair-checkout-real-"));
  const src = join(gitFixture, "src");
  mkdirSync(src);
  const runGit = (cwd, ...args) => spawnSync("git", args, { cwd, encoding: "utf8" });
  const gitOk = (label, result) => {
    if (result.status !== 0) throw new Error(`git ${label} failed: ${(result.stderr ?? result.stdout ?? "").toString().slice(-900)}`);
  };
  gitOk("init", runGit(src, "init"));
  gitOk("name", runGit(src, "config", "user.name", "Fixture"));
  gitOk("email", runGit(src, "config", "user.email", "fixture@example.test"));
  gitOk("branch", runGit(src, "checkout", "-b", "master"));
  writeFileSync(join(src, "package.json"), '{"name":"demo","version":"1.0.0"}\n');
  gitOk("add", runGit(src, "add", "package.json"));
  gitOk("commit", runGit(src, "commit", "-m", "initial"));
  const pinned = resolveGitHead(src);
  const checkoutDir = join(gitFixture, "checkout");
  createIsolatedRepairCheckout({ originalPath: src, sourceBranch: "master", sourceCommit: pinned, targetDir: checkoutDir });
  if (resolveGitHead(checkoutDir) !== pinned) throw new Error("repair checkout must sit on the pinned source commit");
  if (runGit(checkoutDir, "branch", "--show-current").stdout.trim() !== "master") throw new Error("repair checkout must be on sourceBranch");
  if (runGit(checkoutDir, "remote").stdout.trim() !== "") throw new Error(`repair checkout must be local-only, got ${JSON.stringify(runGit(checkoutDir, "remote").stdout)}`);
  // The AGENT's edit: a tracked file, exactly the P1#1 scenario.
  writeFileSync(join(checkoutDir, "package.json"), '{"name":"demo","version":"9.9.9"}\n');
  const dirty = runGit(checkoutDir, "status", "--porcelain").stdout.trim();
  if (!dirty.includes("package.json")) throw new Error(`repair checkout must be dirty after the tracked edit: ${dirty}`);

  // Workspace settings + repair settings MUST sit side by side so the generator
  // resolves the same workspace base (R7 P1#1).
  const wsSettingsDir = join(gitFixture, "ws-settings");
  mkdirSync(wsSettingsDir, { recursive: true });
  const sourceSettingsPath = join(wsSettingsDir, "settings.project.json");
  writeFileSync(sourceSettingsPath, JSON.stringify({
    schemaVersion: 1,
    projects: [{ name: "Demo", path: src, git: { sourceBranch: "master" } }],
  }, null, 2) + "\n");
  const repairSettingsPath = join(wsSettingsDir, "repair-settings-Demo.json");
  buildRepairSettingsFile({ sourceSettingsPath, projectName: "Demo", repairCheckoutPath: checkoutDir, targetPath: repairSettingsPath });
  const remapped = JSON.parse(readFileSync(repairSettingsPath, "utf8"));
  if (remapped.projects[0].path !== checkoutDir) throw new Error("repair settings must point at the repair checkout");
  // The same-directory contract is ENFORCED: a target that would shift the
  // workspace base is refused.
  let rejected = false;
  try {
    buildRepairSettingsFile({ sourceSettingsPath, projectName: "Demo", repairCheckoutPath: checkoutDir, targetPath: join(wsSettingsDir, ".dependency-roadmap", "state", "repair-settings-bad.json") });
  } catch (error) {
    rejected = /BASELINE_REPAIR_SETTINGS_WORKSPACE_BASE/.test(String(error?.message ?? error));
  }
  if (!rejected) throw new Error("buildRepairSettingsFile must refuse a target outside the source settings directory (workspace base contract)");

  const baseSpec = { label: "Baseline", command: "python", args: ["gen.py", "--project-settings", sourceSettingsPath, "--only-project", "Demo"], cwd: wsSettingsDir, env: {} };
  const reVerify = applyRepairReVerification(baseSpec, { settingsPath: repairSettingsPath, sourceCommit: pinned });
  if (reVerify.args[reVerify.args.indexOf("--project-settings") + 1] !== repairSettingsPath) throw new Error("re-verify must use the repair settings");
  if (reVerify.env.DEPLOOM_BASELINE_SOURCE_REPAIR !== "1") throw new Error("re-verify must set the repair-capture authorization");
  if (reVerify.env.DEPLOOM_BASELINE_SOURCE_COMMIT !== pinned) throw new Error("re-verify must pin the source commit");
  if (reVerify.env.DEPLOOM_BASELINE_RESUME !== "restart" || reVerify.env.DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE !== "0") {
    throw new Error("re-verify must stay a FRESH authoritative run, not a resume");
  }
  rmSync(gitFixture, { recursive: true, force: true });
}

// ---------------------------------------------------------------------------
console.log("check-repair-dispatch: OK");

try {
  rmSync(tmp, { recursive: true, force: true });
  rmSync(fixtureDir, { recursive: true, force: true });
} catch { /* temp cleanup is best-effort */ }
