import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  agentPromptFile,
  buildFeedbackPayload,
  buildIterativeRepairPrompt,
  changedFilesFromBaseline,
  iterativeApplyFeedbackInvocation,
  parseChangedFilesFromAgentOutput,
  trialBaselineFile,
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

// 3. Baseline hash diff: package.json is never counted, edits and new files are.
const baselineFile = trialBaselineFile(runDir);
const baselineCount = writeTrialBaseline(workspace, baselineFile);
if (baselineCount !== 2) throw new Error(`Baseline must cover 2 source files, got ${baselineCount}`);
writeFileSync(join(workspace, "src", "app.js"), "module.exports = 42; // repaired\n", "utf8");
writeFileSync(join(workspace, "src", "new.js"), "module.exports = 3;\n", "utf8");
const changed = changedFilesFromBaseline(workspace, baselineFile);
if (changed.includes("package.json")) throw new Error(`Manifest must never be an agent change: ${JSON.stringify(changed)}`);
if (!changed.includes("src/app.js") || !changed.includes("src/new.js")) {
  throw new Error(`Edited and new files must be reported: ${JSON.stringify(changed)}`);
}
if (changed.includes("src/util.js")) throw new Error(`Untouched file must stay clean: ${JSON.stringify(changed)}`);

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

console.log("check-iterative-agent: OK");
