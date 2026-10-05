import { execFileSync, spawn } from "node:child_process";
import assert from "node:assert/strict";
import ts from "typescript";
import { commandEnvironment, resolveSpawnInvocation, decodeProcessOutputChunk, processTreeDetached } from "../dist-electron/process-launcher.js";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  AGENT_LEASE_FILENAME,
  agentProviderFailure,
  agentLeaseAlive,
  agentLeaseFile,
  agentPromptFile,
  buildFeedbackPayload,
  buildIterativeRepairPrompt,
  cancelWaitingAgentLease,
  changedFilesFromBaseline,
  clearAgentLease,
  decideAgentLeaseDispatch,
  forbiddenTrialViolations,
  iterativeApplyFeedbackInvocation,
  openCodeRuntimeManifestPaths,
  parseAgentOutcome,
  parseChangedFilesFromAgentOutput,
  readAgentLease,
  trialBaselineFile,
  withAgentDispatchLock,
  writeAgentLease,
  writeTrialBaseline,
} from "../dist-electron/iterative-agent.js";
import { parseIterativeStatusPayload, readIterativeStatus } from "../dist-electron/iterative-runner.js";

const DESKTOP = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = resolve(DESKTOP, "..");
const PYTHON = process.platform === "win32" ? "python" : "python3";
const GENERATOR = join(REPO_ROOT, "iterative_migration.py");

function writeJson(path, value) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value), "utf8");
}

// 1. Durable fixture: REPAIRING run with one open repair request and a trial
// workspace the agent would edit.
const root = mkdtempSync(join(tmpdir(), "iter-agent-handoff-"));
const runDir = join(root, "run");
const workspace = join(runDir, "trial", "workspace");
mkdirSync(join(workspace, "src"), { recursive: true });
mkdirSync(join(runDir, "checkpoints"), { recursive: true });
writeJson(join(runDir, "run.json"), {
  schemaVersion: 1, runId: "iter-agent-1", workspaceId: "ws", projectId: "demo-app",
  activeCheckpointId: "C1", activeCandidateId: "C2", phase: "REPAIRING", targetPolicyHash: "ph",
});
writeJson(join(runDir, "run-config.json"), {
  schemaVersion: 1, projectName: "DemoApp", projectDir: root, workspaceId: "ws", projectId: "demo-app",
  policyHash: "ph", targetLevel: "yellow", targets: { "is-number": "7.0.0" },
  verifyConfig: { commands: ["node check.js"] },
});
writeJson(join(runDir, "checkpoints", "C1.json"), {
  schemaVersion: 1, checkpointId: "C1", status: "VERIFIED", fullAssignment: { "is-number": "6.0.0" },
});
writeJson(join(runDir, "trial", "candidate.json"), {
  schemaVersion: 1, candidateId: "C2", runId: "iter-agent-1", baseCheckpointId: "C1",
  policyHash: "ph", fullAssignment: { "is-number": "7.0.0" }, delta: { changed: { "is-number": "7.0.0" } },
  atomicGroups: [["is-number"]], allowedSourceScope: { mode: "source-config", forbiddenRelatives: ["package.json"] },
  stage: "REPAIRING", attemptId: 1, agentSessionId: "", feedbackRevision: 0, budgetConsumed: {},
  materializationRefs: { workspaceRoot: workspace, projectRelative: ".", trialSnapshotKey: "snap-1" },
  resolverContextKey: "", createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-01T00:00:00Z",
});
writeJson(join(runDir, "repair-requests.json"), {
  schemaVersion: 1,
  requests: [{
    schemaVersion: 1, requestId: "repair-1", candidateId: "C2", runId: "iter-agent-1",
    baseCheckpointId: "C1", attemptId: 1, project: "DemoApp", mode: "iterative",
    assignment: [["is-number", "7.0.0"]], fingerprint: "fp", snapshotIdentity: "snap-1",
    failingCommands: [{ command: "npm run build", exitCode: 1 }],
    diagnosticsTail: "ERR! Cannot find module\n  at build.js:1\n",
    reason: "build fails after upgrading is-number", disposition: "repair-or-replan",
  }],
});
writeFileSync(join(workspace, "src", "app.js"), "module.exports = 1;\n", "utf8");
writeFileSync(join(workspace, "src", "util.js"), "module.exports = 2;\n", "utf8");
writeFileSync(join(workspace, "package.json"), JSON.stringify({ dependencies: { "is-number": "7.0.0" } }), "utf8");

// 2. Prompt contract: task text, the exact repair request, guardrails and the
// output marker are all present; the trial path is named.
const ctx = {
  runId: "iter-agent-1", candidateId: "C2", baseCheckpointId: "C1", attemptId: 1,
  projectName: "DemoApp", targetLevel: "yellow",
  workspaceRoot: workspace, projectRelative: ".",
  assignment: [["is-number", "7.0.0"]],
  repairRequests: [{
    requestId: "repair-1", reason: "build fails after upgrading is-number",
    failingCommands: [{ command: "npm run build", exitCode: 1 }],
    diagnosticsTail: "ERR! Cannot find module",
  }],
  taskText: "Обновите is-number до 7.0.0 и адаптируйте исходники; package.json/lockfile менять нельзя.",
};
const prompt = buildIterativeRepairPrompt(ctx);
for (const needle of [
  "repair-1", "build fails after upgrading is-number", "npm run build", "ERR! Cannot find module",
  "is-number = 7.0.0", "package-lock.json", "CHANGED_FILES:", "изолированного trial", "DemoApp",
]) {
  if (!prompt.includes(needle)) throw new Error(`Prompt must mention "${needle}"`);
}

// 2b. Review P1 (contradictory versions): a CANDIDATE repair reworks the trial
// C2 on top of base checkpoint C1 whose fullAssignment is is-number=6.0.0. The
// checkpoint-built ТЗ would carry that 6.0.0 while the trial installs 7.0.0 —
// so the dispatch withholds the ТЗ for a candidate repair (taskText="").
// The prompt must stay self-sufficient and carry ONLY the trial assignment.
const candidatePrompt = buildIterativeRepairPrompt({ ...ctx, taskText: "" });
if (candidatePrompt.includes("is-number = 6.0.0")) {
  throw new Error("candidate repair prompt must not carry the contradictory checkpoint assignment (is-number = 6.0.0)");
}
if (!candidatePrompt.includes("is-number = 7.0.0")) {
  throw new Error(`candidate repair prompt must carry the exact trial assignment: ${candidatePrompt}`);
}
for (const needle of ["repair-1", "CHANGED_FILES:", "is-number = 7.0.0", "package-lock.json"]) {
  if (!candidatePrompt.includes(needle)) throw new Error(`Candidate repair prompt must stay self-sufficient (${needle})`);
}

// 3. Baseline hash diff covers EVERY significant file (only node_modules/.git
// is skipped), so planner-owned manifests are detected when mutated or
// deleted. Edits, adds AND removes are reported; planner-forbidden mutations
// are classified separately and NEVER ride the changed list.
const baselineFile = trialBaselineFile(runDir);
const scopedA = trialBaselineFile(runDir, { runId: 'run', candidateId: 'A', baseCheckpointId: 'C0' });
const scopedB = trialBaselineFile(runDir, { runId: 'run', candidateId: 'B', baseCheckpointId: 'C1' });
assert.notEqual(scopedA, scopedB, 'Candidate repair baselines must not cross cohorts');
writeTrialBaseline(workspace, scopedA);
const plannerManifest = readFileSync(join(workspace, 'package.json'), 'utf8');
writeFileSync(join(workspace, 'package.json'), JSON.stringify({ dependencies: { 'is-number': '8.0.0' } }));
writeTrialBaseline(workspace, scopedB);
assert.equal(forbiddenTrialViolations(workspace, scopedB).length, 0, 'New planner assignment must not be attributed to the agent');
assert.ok(forbiddenTrialViolations(workspace, scopedA).includes('modified:package.json'), 'Same candidate must preserve manifest protection');
assert.equal(trialBaselineFile(runDir, { runId: 'run', candidateId: 'B', baseCheckpointId: 'C1' }), scopedB, 'Resume must retain exact candidate evidence');
writeFileSync(join(workspace, 'package.json'), plannerManifest);
const baselineCount = writeTrialBaseline(workspace, baselineFile);
if (baselineCount !== 3) throw new Error(`Baseline must cover 3 files (manifest + 2 sources), got ${baselineCount}`);
if (forbiddenTrialViolations(workspace, baselineFile).length !== 0) {
  throw new Error(`Pristine baseline must have no forbidden mutations: ${JSON.stringify(forbiddenTrialViolations(workspace, baselineFile))}`);
}
writeFileSync(join(workspace, "src", "app.js"), "module.exports = 42; // repaired\n", "utf8");
writeFileSync(join(workspace, "src", "new.js"), "module.exports = 3;\n", "utf8");
const changed = changedFilesFromBaseline(workspace, baselineFile);
if (changed.includes("package.json")) throw new Error(`Unchanged manifest must not appear in changed: ${JSON.stringify(changed)}`);
if (!changed.includes("src/app.js") || !changed.includes("src/new.js")) {
  throw new Error(`Edited and new files must be reported: ${JSON.stringify(changed)}`);
}
if (changed.includes("src/util.js")) throw new Error(`Untouched file must stay clean: ${JSON.stringify(changed)}`);
if (forbiddenTrialViolations(workspace, baselineFile).length !== 0) {
  throw new Error(`Edited sources must not be forbidden violations: ${JSON.stringify(forbiddenTrialViolations(workspace, baselineFile))}`);
}
// b) editing OR deleting a planner-owned manifest is a FORBIDDEN mutation.
const manifestPath = join(workspace, "package.json");
writeFileSync(manifestPath, JSON.stringify({ dependencies: { "is-number": "7.0.1" } }), "utf8");
const violations = forbiddenTrialViolations(workspace, baselineFile);
if (violations.length !== 1 || !violations.includes("modified:package.json")) {
  throw new Error(`Manifest edit must be a forbidden mutation: ${JSON.stringify(violations)}`);
}
if (changedFilesFromBaseline(workspace, baselineFile).includes("package.json")) {
  throw new Error(`Forbidden paths must never ride the changed list: ${JSON.stringify(changedFilesFromBaseline(workspace, baselineFile))}`);
}
writeFileSync(manifestPath, JSON.stringify({ dependencies: { "is-number": "7.0.0" } }), "utf8");
if (forbiddenTrialViolations(workspace, baselineFile).length !== 0) {
  throw new Error(`Restored manifest must clear violations: ${JSON.stringify(forbiddenTrialViolations(workspace, baselineFile))}`);
}
// c) deleting a source file is a REAL agent change (removed); deleting the
// manifest is a violation.
rmSync(join(workspace, "src", "util.js"));
if (!changedFilesFromBaseline(workspace, baselineFile).includes("src/util.js")) {
  throw new Error(`Removed source file must be reported as a change: ${JSON.stringify(changedFilesFromBaseline(workspace, baselineFile))}`);
}
writeFileSync(join(workspace, "src", "util.js"), "module.exports = 2;\n", "utf8");
rmSync(manifestPath);
const removedViolations = forbiddenTrialViolations(workspace, baselineFile);
if (removedViolations.length !== 1 || !removedViolations.includes("removed:package.json")) {
  throw new Error(`Deleted manifest must be a forbidden mutation: ${JSON.stringify(removedViolations)}`);
}
writeFileSync(manifestPath, JSON.stringify({ dependencies: { "is-number": "7.0.0" } }), "utf8");
if (forbiddenTrialViolations(workspace, baselineFile).length !== 0) {
  throw new Error(`Restored manifest must clear violations: ${JSON.stringify(forbiddenTrialViolations(workspace, baselineFile))}`);
}

// Runtime metadata must not hide application manifests or count as repair.
const runtimeDir = join(workspace, '.opencode');
const runtimeScope = { provider: 'opencode', projectRelative: '.' };
function pluginRuntime(version) {
  const manifest = { dependencies: { '@opencode-ai/plugin': version } };
  writeJson(join(runtimeDir, 'package.json'), manifest);
  writeJson(join(runtimeDir, 'package-lock.json'), {
    name: '.opencode', lockfileVersion: 3,
    packages: { '': manifest, 'node_modules/@opencode-ai/plugin': { version } },
  });
}
pluginRuntime('1.0.0');
const runtimeBaseline = join(runDir, 'runtime-baseline.json');
writeTrialBaseline(workspace, runtimeBaseline);
pluginRuntime('1.17.14');
assert.equal(forbiddenTrialViolations(workspace, runtimeBaseline).length, 2);
assert.deepEqual(forbiddenTrialViolations(workspace, runtimeBaseline, runtimeScope), []);
assert.equal(openCodeRuntimeManifestPaths(workspace, runtimeScope).size, 2);
assert.equal(openCodeRuntimeManifestPaths(workspace, { ...runtimeScope, provider: 'codex' }).size, 0);
assert.equal(changedFilesFromBaseline(workspace, runtimeBaseline).length, 0, 'Tool bootstrap alone is not a repair');
writeJson(manifestPath, { dependencies: { 'is-number': '8.0.0' } });
assert.deepEqual(forbiddenTrialViolations(workspace, runtimeBaseline, runtimeScope), ['modified:package.json']);
writeJson(manifestPath, { dependencies: { 'is-number': '7.0.0' } });
writeJson(join(runtimeDir, 'package.json'), { dependencies: { '@opencode-ai/plugin': '1.17.14' }, scripts: { test: 'echo bypass' } });
assert.equal(openCodeRuntimeManifestPaths(workspace, runtimeScope).size, 0);
assert.equal(forbiddenTrialViolations(workspace, runtimeBaseline, runtimeScope).length, 2);
pluginRuntime('1.17.14');
const runtimeLockPath = join(runtimeDir, 'package-lock.json');
const matchingLock = JSON.parse(readFileSync(runtimeLockPath, 'utf8'));
writeJson(runtimeLockPath, { ...matchingLock, packages: { ...matchingLock.packages, '': { dependencies: { 'other-package': '1.0.0' } } } });
assert.equal(openCodeRuntimeManifestPaths(workspace, runtimeScope).size, 0, 'Manifest and lock must describe the same tool');
pluginRuntime('1.17.14');
assert.equal(openCodeRuntimeManifestPaths(workspace, { ...runtimeScope, projectRelative: 'src' }).size, 0, 'Only the selected project runtime is exempt');

// 4. Feedback payload carries ONLY the durable candidate identity.
const feedback = buildFeedbackPayload(ctx, changed, "READY_FOR_VERIFY", "source adapted to is-number 7.0.0");
if (feedback.candidateId !== "C2" || feedback.runId !== "iter-agent-1") throw new Error("Feedback identities lost");
if (feedback.baseCheckpointId !== "C1" || feedback.attemptId !== 1) throw new Error("Feedback base/attempt lost");
if (feedback.kind !== "READY_FOR_VERIFY") throw new Error("Feedback kind lost");
if (!Array.isArray(feedback.changedFiles) || feedback.changedFiles.length !== 2) {
  throw new Error(`Feedback changedFiles wrong: ${JSON.stringify(feedback.changedFiles)}`);
}
const feedbackFile = join(runDir, "feedback.json");
writeJson(feedbackFile, feedback);

// 5. The AUTHORITATIVE validator accepts it: real apply-feedback CLI, then
// the real status CLI proves the run moved to VERIFYING.
const invocation = iterativeApplyFeedbackInvocation(runDir, feedbackFile, GENERATOR, PYTHON);
if (invocation.args[1] !== "--run-dir" || invocation.args[2] !== runDir) {
  throw new Error(`apply-feedback invocation must carry --run-dir first: ${JSON.stringify(invocation)}`);
}
if (!invocation.args.includes("apply-feedback") || !invocation.args.includes("--feedback-file")) {
  throw new Error(`apply-feedback invocation malformed: ${JSON.stringify(invocation)}`);
}
execFileSync(invocation.command, invocation.args, { stdio: "pipe" });
const status = execFileSync(PYTHON, [GENERATOR, "--run-dir", runDir, "status"], { encoding: "utf8" });
const statusPayload = parseIterativeStatusPayload(status) ?? readIterativeStatus(runDir);
if (!statusPayload || statusPayload.run.phase !== "VERIFYING") {
  throw new Error(`apply-feedback must move REPAIRING -> VERIFYING, phase=${statusPayload?.run.phase}`);
}

// Guard recovery uses the REAL Python CLI: disputed bytes never become a
// checkpoint or incompatibility clause; scheduling resumes from C1.
const rejectedDir = join(root, 'run-rejected');
const savedCheckpoint = readFileSync(join(runDir, 'checkpoints', 'C1.json'));
writeJson(join(rejectedDir, 'run.json'), { ...JSON.parse(readFileSync(join(runDir,'run.json'),'utf8')), phase:'REPAIRING' });
writeFileSync(join(rejectedDir,'run-config.json'),readFileSync(join(runDir,'run-config.json')));
mkdirSync(join(rejectedDir,'checkpoints'),{recursive:true});
writeFileSync(join(rejectedDir,'checkpoints','C1.json'),savedCheckpoint);
writeJson(join(rejectedDir,'trial','candidate.json'),{...JSON.parse(readFileSync(join(runDir,'trial','candidate.json'),'utf8')),stage:'REPAIRING'});
const discardFile = join(rejectedDir,'discard-feedback.json');
writeJson(discardFile,buildFeedbackPayload(ctx,[],'INCONCLUSIVE','Protected-file guard rejected this trial'));
execFileSync(PYTHON,[GENERATOR,'--run-dir',rejectedDir,'apply-feedback','--feedback-file',discardFile],{encoding:'utf8'});
const rejectedState = parseIterativeStatusPayload(execFileSync(PYTHON,[GENERATOR,'--run-dir',rejectedDir,'status'],{encoding:'utf8'})) ?? readIterativeStatus(rejectedDir);
assert.equal(rejectedState.run.phase,'READY');
assert.equal(rejectedState.run.activeCheckpointId,'C1');
assert.equal(rejectedState.run.activeCandidateId,null);
assert.equal(JSON.parse(readFileSync(join(rejectedDir,'trial','candidate.json'),'utf8')).stage,'REJECTED');
assert.deepEqual(readFileSync(join(rejectedDir,'checkpoints','C1.json')),savedCheckpoint);
assert.deepEqual(JSON.parse(readFileSync(join(rejectedDir,'ledger.json'),'utf8')).blocks,[]);

// 6. Staleness is rejected by the real CLI: a feedback bound to a foreign
// candidate must fail with STALE_FEEDBACK and change NOTHING.
const staleDir = join(root, "run-stale");
for (const name of ["run.json", "run-config.json"]) {
  mkdirSync(dirname(join(staleDir, name)), { recursive: true });
  writeFileSync(join(staleDir, name), readFileSync(join(runDir, name)), "utf8");
}
mkdirSync(join(staleDir, "checkpoints"), { recursive: true });
writeFileSync(join(staleDir, "checkpoints", "C1.json"), readFileSync(join(runDir, "checkpoints", "C1.json")));
mkdirSync(join(staleDir, "trial"), { recursive: true });
writeFileSync(join(staleDir, "trial", "candidate.json"), readFileSync(join(runDir, "trial", "candidate.json")));
const staleFeedback = { ...feedback, candidateId: "C9", changedFiles: [] };
const staleFile = join(staleDir, "feedback.json");
writeJson(staleFile, staleFeedback);
let staleExit = 0;
let staleError = "";
try {
  execFileSync(PYTHON, [GENERATOR, "--run-dir", staleDir, "apply-feedback", "--feedback-file", staleFile], { stdio: "pipe" });
} catch (error) {
  staleExit = error.status ?? 1;
  staleError = String(error.stderr ?? "") + String(error.stdout ?? "");
}
if (staleExit === 0 || !staleError.includes("STALE_FEEDBACK_CANDIDATE")) {
  throw new Error(`Foreign-candidate feedback must be rejected as stale (exit=${staleExit}): ${staleError}`);
}

// 7. Agent output marker parsing.
const parsed = parseChangedFilesFromAgentOutput("done\nCHANGED_FILES:\nsrc/app.js\n  src/new.js\nnot a path *\n");
if (parsed.length !== 2 || parsed[0] !== "src/app.js" || parsed[1] !== "src/new.js") {
  throw new Error(`Changed-file parsing off: ${JSON.stringify(parsed)}`);
}
if (parseChangedFilesFromAgentOutput("no marker").length !== 0) {
  throw new Error("Missing marker must yield no change list");
}

// 8. Path helpers live under the durable trial dir.
if (existsSync(trialBaselineFile(runDir)) === false) throw new Error("Baseline file must exist");
if (!agentPromptFile(runDir).endsWith("agent-prompt.md")) throw new Error("Agent prompt path contract broken");

// 9. R5: parseAgentOutcome turns raw agent output + exit code + file changes
// into a TYPED kind. Agent text alone never claims a completed repair: a
// READY_FOR_VERIFY marker with a non-zero exit or no real changes degrades to
// INCONCLUSIVE, and scheduling kinds strip changed files.
const files = ["src/app.js"];
const ready = parseAgentOutcome("FEEDBACK_KIND: READY_FOR_VERIFY\nREASON: done", 0, files);
if (ready.kind !== "READY_FOR_VERIFY" || ready.changedFiles.length !== 1) {
  throw new Error(`Ready outcome misparsed: ${JSON.stringify(ready)}`);
}
if (parseAgentOutcome("FEEDBACK_KIND: READY_FOR_VERIFY\nall good", 1, files).kind !== "INCONCLUSIVE") {
  throw new Error("READY_FOR_VERIFY with non-zero exit must degrade to INCONCLUSIVE");
}
if (parseAgentOutcome("FEEDBACK_KIND: READY_FOR_VERIFY", 0, []).kind !== "INCONCLUSIVE") {
  throw new Error("READY_FOR_VERIFY without file changes must degrade to INCONCLUSIVE");
}
const expansion = parseAgentOutcome(
  "FEEDBACK_KIND: NEEDS_COHORT_EXPANSION\nPROPOSALS:\n- is-odd\n- is-even\nREASON: сосед",
  0,
  files,
);
if (expansion.kind !== "NEEDS_COHORT_EXPANSION") throw new Error("NEEDS_COHORT_EXPANSION kind lost");
if (expansion.changedFiles.length !== 0) throw new Error("Scheduling kinds must not carry changed files");
if (expansion.proposals.length !== 2 || expansion.proposals[0] !== "is-odd") {
  throw new Error(`Proposals misparsed: ${JSON.stringify(expansion.proposals)}`);
}
if (parseAgentOutcome("FEEDBACK_KIND: INFRA_BLOCKED\nREASON: disk", 0, files).kind !== "INFRA_BLOCKED") {
  throw new Error("INFRA_BLOCKED kind lost");
}
if (parseAgentOutcome("FEEDBACK_KIND: NEEDS_ALTERNATIVE\nPROPOSALS:\nlib = 2.0.0", 0, files).kind !== "NEEDS_ALTERNATIVE") {
  throw new Error("NEEDS_ALTERNATIVE kind lost");
}
if (parseAgentOutcome("no marker at all", 0, files).kind !== "READY_FOR_VERIFY") {
  throw new Error("Missing marker must default to READY_FOR_VERIFY (then degrade by files/exit)");
}
const streamedAlternative = parseAgentOutcome([
 JSON.stringify({type:'tool_use',part:{state:{output:'FEEDBACK_KIND: READY_FOR_VERIFY'}}}),
 JSON.stringify({type:'text',part:{text:'FEEDBACK_KIND: NEEDS_ALTERNATIVE\nPROPOSALS:\n```\neslint = 9.13.0\n@typescript-eslint/parser = 8.11.0\n```\nREASON: unsupported peers'}}),
 JSON.stringify({type:'step_finish'}),
].join('\n'),0,[]);
assert.equal(streamedAlternative.kind,'NEEDS_ALTERNATIVE');
assert.deepEqual(streamedAlternative.proposals,['eslint = 9.13.0','@typescript-eslint/parser = 8.11.0']);
assert.equal(streamedAlternative.reason,'unsupported peers');
// Proposals ride into the feedback for NEEDS_COHORT_EXPANSION (companions).
const expansionFeedback = buildFeedbackPayload(
  ctx,
  expansion.changedFiles,
  expansion.kind,
  expansion.reason,
  { companions: expansion.proposals },
);
if (expansionFeedback.kind !== "NEEDS_COHORT_EXPANSION") throw new Error("Feedback kind lost");
if (JSON.stringify(expansionFeedback.proposedScope) !== '{"companions":["is-odd","is-even"]}') {
  throw new Error(`Companion proposals lost: ${JSON.stringify(expansionFeedback.proposedScope)}`);
}

// 10. R6: the durable dispatch lease round-trips, expires and clears.
const leasePath = agentLeaseFile(runDir);
if (!leasePath.endsWith(AGENT_LEASE_FILENAME)) throw new Error("Lease path contract broken");
const leaseNow = Date.now();
writeAgentLease(leasePath, {
  schemaVersion: 1, sessionId: "abc123", provider: "opencode", databasePath: "/db",
  runId: "iter-agent-1", candidateId: "C2", attemptId: 1, pid: process.pid,
  startedAt: new Date(leaseNow).toISOString(),
});
const leaseBack = readAgentLease(leasePath);
if (!leaseBack || leaseBack.sessionId !== "abc123" || leaseBack.provider !== "opencode") {
  throw new Error(`Lease did not round-trip: ${JSON.stringify(leaseBack)}`);
}
if (!agentLeaseAlive(leaseBack)) throw new Error("Fresh lease must be alive");
const leaseStart = Date.parse(leaseBack.startedAt);
if (agentLeaseAlive(leaseBack, leaseStart + 3 * 60 * 60 * 1000)) throw new Error("Expired lease must not be alive");

// 11. #5: the dispatch decision for a durable lease. A live issuing Desktop
// refuses a second session; a DEAD one (app killed mid-run) resumes the SAME
// session instead of spending a second attempt; expired/absent starts fresh.
const stillRunning = { pid: process.pid };
const inProgress = decideAgentLeaseDispatch({ ...stillRunning, schemaVersion: 1, sessionId: "abc123", provider: "opencode", databasePath: "/db", runId: "iter-agent-1", candidateId: "C2", attemptId: 1, startedAt: new Date().toISOString() }, () => true);
if (inProgress.action !== "in-progress") throw new Error(`Live owner must refuse a second dispatch: ${JSON.stringify(inProgress)}`);
const deadOwner = { pid: 1, schemaVersion: 1, sessionId: "abc123", provider: "opencode", databasePath: "/db", runId: "iter-agent-1", candidateId: "C2", attemptId: 1, startedAt: new Date().toISOString() };
const resumed = decideAgentLeaseDispatch(deadOwner, () => false);
if (resumed.action !== "resume" || resumed.sessionId !== "abc123") {
  throw new Error(`Killed owner must resume the same session: ${JSON.stringify(resumed)}`);
}
const noLease = decideAgentLeaseDispatch(undefined, () => false);
if (noLease.action !== "none") throw new Error(`Absent lease must dispatch fresh: ${JSON.stringify(noLease)}`);
const expiredLease = {
  pid: 1, schemaVersion: 1, sessionId: "abc123", provider: "opencode", databasePath: "/db",
  runId: "iter-agent-1", candidateId: "C2", attemptId: 1, startedAt: new Date(leaseStart - 3 * 60 * 60 * 1000).toISOString(),
};
const expired = decideAgentLeaseDispatch(expiredLease, () => true);
if (expired.action !== "none") throw new Error(`Expired lease must dispatch fresh even with a live pid: ${JSON.stringify(expired)}`);
assert.equal(decideAgentLeaseDispatch({...expiredLease,childPid:321},pid=>pid===321).action,'in-progress');
assert.equal(decideAgentLeaseDispatch({...expiredLease,childPid:321},()=>false).action,'none');

// 11b. Launch-wait (rate limit / temporary outage): a PARKED WAIT lease is the
// durable "repair is parked" record. While its retry time is in the future the
// dispatch MUST say 'wait' (never spawn an agent, never consume an attempt);
// once the time passes it says 'retry' carrying the durable attempt counter so
// a restarted Desktop honors the same retry budget. A finite budget exhausted
// ends in 'give-up' with a clear diagnostic; a CONFIRMED rate limit has NO
// budget. A waiting-lease holder stays alive and is NEVER refused as
// in-progress — the parked wait IS the single-owner guard until retry or cancel.
const waitingFuture = { schemaVersion: 1, sessionId: "s1", provider: "opencode", databasePath: "/db", runId: "iter-agent-1", candidateId: "C2", attemptId: 1, pid: process.pid, startedAt: new Date().toISOString(), waiting: true, waitKind: "rate-limited", waitDetail: "429: rate limit; retry after 30s", retryAt: Date.now() + 30_000, retryAfterSeconds: 30, launchAttempts: 2 };
const waitDecision = decideAgentLeaseDispatch(waitingFuture, () => true);
assert.equal(waitDecision.action, "wait", "Future retry time must keep the repair parked");
if (waitDecision.action === "wait" && waitDecision.retryAt !== waitingFuture.retryAt) throw new Error("wait must carry the exact retryAt");
const waitEligible = { ...waitingFuture, retryAt: Date.now() - 1000 };
const retryDecision = decideAgentLeaseDispatch(waitEligible, () => true);
assert.equal(retryDecision.action, "retry", "Passed retry time must relaunch");
if (retryDecision.action === "retry" && retryDecision.launchAttempts !== 2) throw new Error("retry must carry the durable launchAttempts");
assert.equal(decideAgentLeaseDispatch({ ...waitEligible, waitKind: "unknown", launchAttempts: 3 }, () => true).action, "give-up", "Unknown-outage budget exhausted must end with a diagnostic");
assert.equal(decideAgentLeaseDispatch({ ...waitEligible, waitKind: "temporary", launchAttempts: 5 }, () => true).action, "give-up", "Temporary-outage budget exhausted must end with a diagnostic");
assert.equal(decideAgentLeaseDispatch({ ...waitEligible, waitKind: "rate-limited", launchAttempts: 99 }, () => true).action, "retry", "A confirmed rate limit has NO retry budget");
// cancelWaitingAgentLease clears ONLY a parked waiting lease — the in-flight
// protection must never be removable through the wait-cancel path.
const waitFile = join(runDir, "trial", "wait-lease.json");
writeAgentLease(waitFile, waitingFuture);
assert.equal(cancelWaitingAgentLease(waitFile), true, "cancel must clear a waiting lease");
assert.equal(readAgentLease(waitFile), undefined, "canceled wait lease must be gone");
writeAgentLease(leasePath, { schemaVersion: 1, sessionId: "abc123", provider: "opencode", databasePath: "/db", runId: "iter-agent-1", candidateId: "C2", attemptId: 1, pid: process.pid, startedAt: new Date(leaseNow).toISOString() });
assert.equal(cancelWaitingAgentLease(leasePath), false, "an in-flight lease must never be cleared by the wait-cancel path");
assert.ok(readAgentLease(leasePath), "in-flight lease must survive the wait-cancel");

clearAgentLease(leasePath);
if (existsSync(leasePath)) throw new Error("clearAgentLease must remove the file");
if (readAgentLease(join(runDir, "trial", "no-such-lease.json")) !== undefined) {
  throw new Error("Missing lease file must read as undefined");
}

assert.equal(agentProviderFailure(JSON.stringify({type:'error',error:{name:'ProviderModelNotFoundError',data:{message:'Model not found: test-provider/obsolete'}}}),0), 'Model not found: test-provider/obsolete');
assert.equal(agentProviderFailure('ordinary repair text',0), undefined);
assert.equal(agentProviderFailure('connection refused',1), 'connection refused');
// Exercise the actual main capture helper against a child that starts only
// after stdin EOF. No model/server is contacted and no tokens are consumed.
const mainSource = readFileSync(join(DESKTOP, "electron/main.ts"), "utf8");
const captureStart = mainSource.indexOf("function spawnCapture(");
const captureEnd = mainSource.indexOf("// Adoption helpers for the TESTABLE streaming runner", captureStart);
assert.ok(captureStart >= 0 && captureEnd > captureStart);
const captureJs = ts.transpileModule(mainSource.slice(captureStart, captureEnd), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
const capture = new Function("spawn", "commandEnvironment", "resolveSpawnInvocation", "processTreeDetached", "decodeProcessOutputChunk", "killProcessTree", captureJs + "; return spawnCapture;")(
  spawn, commandEnvironment, resolveSpawnInvocation, processTreeDetached, decodeProcessOutputChunk, child => child.kill(),
);
const eof = await capture(process.execPath, ["-e", "process.stdin.resume();process.stdin.on('end',()=>{console.log(process.argv[1]);console.error('EOF received')})", "test-provider/explicit-model"], root, 2000);
assert.equal(eof.timedOut, false);
assert.equal(eof.code, 0);
assert.equal(eof.stdout.trim(), "test-provider/explicit-model");
assert.equal(eof.stderr.trim(), "EOF received");
const timeout = await capture(process.execPath, ["-e", "setInterval(()=>{},1000)"], root, 200);
assert.equal(timeout.timedOut, true);
assert.ok(mainSource.includes("if (agentTimedOut)"));
assert.ok(mainSource.includes("AGENT_PROVIDER_TIMEOUT:"));
assert.ok(mainSource.includes("Agent dispatch: provider=${provider}; model=${model"));
const locks = new Set();
for (const action of ["wait", "give-up"]) {
  assert.equal(await withAgentDispatchLock(locks, "scope", async () => action), action);
  assert.equal(locks.size, 0, `${action} must release the project lock`);
}
await assert.rejects(withAgentDispatchLock(locks, "scope", async () => { throw Error("failure"); }));
assert.equal(locks.size, 0);
assert.deepEqual(await withAgentDispatchLock(locks, "scope", async () => withAgentDispatchLock(locks, "scope", async () => "duplicate")), {ok: false, error: "STEP_IN_PROGRESS"});
assert.equal(locks.size, 0);
assert.equal(decideAgentLeaseDispatch({...waitingFuture, startedAt: new Date(Date.now() - 3 * 86400_000).toISOString()}, () => false).action, "wait", "A durable waiting episode must survive a long restart");
assert.equal(decideAgentLeaseDispatch({...waitEligible, sessionId: "ses-real"}, () => false).sessionId, "ses-real");
assert.equal(decideAgentLeaseDispatch({...waitEligible, sessionId: ""}, () => false).sessionId, undefined);
assert.equal(decideAgentLeaseDispatch({...waitEligible, waiting: false, pid: 999, childPid: 321}, pid => pid === 321).action, "in-progress");
await import("./agent-launch-lifecycle-fixture.mjs");
console.log("check-iterative-agent: OK (real child, launch wait, session recovery and cancel)");
