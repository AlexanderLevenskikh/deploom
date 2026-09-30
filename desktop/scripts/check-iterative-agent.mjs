import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  AGENT_LEASE_FILENAME,
  agentLeaseAlive,
  agentLeaseFile,
  agentPromptFile,
  buildFeedbackPayload,
  buildIterativeRepairPrompt,
  changedFilesFromBaseline,
  clearAgentLease,
  decideAgentLeaseDispatch,
  forbiddenTrialViolations,
  iterativeApplyFeedbackInvocation,
  parseAgentOutcome,
  parseChangedFilesFromAgentOutput,
  readAgentLease,
  trialBaselineFile,
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

clearAgentLease(leasePath);
if (existsSync(leasePath)) throw new Error("clearAgentLease must remove the file");
if (readAgentLease(join(runDir, "trial", "no-such-lease.json")) !== undefined) {
  throw new Error("Missing lease file must read as undefined");
}

console.log("check-iterative-agent: OK");
