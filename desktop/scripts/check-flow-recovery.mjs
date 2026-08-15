import { classifyFlowRecovery } from "../dist-electron/flow-recovery.js";
import { buildReleaseRecoveryPrompt, readReleaseRecoveryResult } from "../dist-electron/release-recovery.js";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const cases = [
  ["MIGRATION_DONE_WORKTREE_NOT_CLEAN: 8 files", "agent", "agent"],
  ["RELEASE_FINAL_GATE_FAILED: yarn test failed", "release", "agent"],
  ["RELEASE_FINAL_GATE_DIRTY: generated file", "release", "agent"],
  ["RELEASE_COMMIT_OR_HOOK_FAILED: pre-commit failed", "release", "agent"],
  ["RELEASE_SQUASH_FAILED: conflict", "release", "agent"],
  ["MERGE_RECOVERY_EXHAUSTED: unresolved", "agent", "agent"],
  ["MIGRATION_GROUP_VERIFICATION_FAILED: lint red", "agent", "agent"],
  ["MERGED_INTEGRATION_REPAIR_EXHAUSTED: merged red", "agent", "agent"],
  ["MIGRATION_REPLAN_REQUIRED: vite peer excluded", "agent", "agent"],
  ["PLANNER_REPLAN_DIRTY: executor left bounded changes", "agent", "agent"],
  ["PLANNER_REPLAN_UNSAFE: merge in progress", "agent", "agent"],
  ["PLANNER_RESULT_MISSING: invalid machine JSON", "agent", "infrastructure"],
  ["AGENT_BRANCH_SCOPE_VIOLATION: wrong branch", "agent", "agent"],
  ["MIGRATION_PLAN_APPROVAL_REQUIRED: new dependency", "agent", "agent"],
  ["MIGRATION_AUTONOMOUS_RECOVERY_STALLED: repeated", "agent", "agent"],
  ["RELEASE_RECOVERY_EXHAUSTED: repeated", "release", "agent"],
  ["MERGED_INTEGRATION_REPAIR_SCOPE_VIOLATION: wrong branch", "agent", "agent"],
  ["MERGED_VERIFICATION_GIT_STATE_FAILED: status unreadable", "agent", "infrastructure"],
  ["MIGRATION_FINAL_VERIFICATION_WRONG_BRANCH: on release", "agent", "agent"],
  ["RELEASE_TO_MIGRATION_HANDOFF_UNSAFE: unstaged files", "recover", "hard"],
  ["RELEASE_TO_MIGRATION_HANDOFF_FAILED: git switch failed", "recover", "infrastructure"],
  ["RELEASE_SOURCE_REF_UNAVAILABLE: fetch failed", "release", "infrastructure"],
  ["GROUP_SCOPE_DRIFT: target changed", "agent", "agent"],
  ["BATCH_SCOPE_DRIFT: target changed", "agent", "agent"],
  ["AGENT_GIT_PLAN_INCOMPLETE: созданы ветки вне Branch plan: libs-group-99", "agent", "agent"],
  ["MERGE_RECOVERY_SCOPE_VIOLATION: unknown head", "agent", "hard"],
  ["MERGE_RECOVERY_UNSAFE_BRANCH: unknown active merge checkout", "agent", "hard"],
  ["RELEASE_MERGED_BRANCH_NOT_FOUND: missing merged", "release", "hard"],
  ["RELEASE_WORKSPACE_INVALID: invalid audit workspace", "release", "hard"],
  ["RELEASE_WORKSPACE_NOT_TOOL_MANAGED: foreign workspace", "release", "hard"],
  ["RELEASE_RECOVERY_UNSAFE_BRANCH: wrong dirty branch", "recover", "hard"],
  ["RELEASE_RECOVERY_SCOPE_VIOLATION: pinned head mismatch", "recover", "hard"],
  ["RELEASE_SOURCE_COMMIT_MISMATCH: source moved", "release", "hard"],
  ["RELEASE_BRANCH_ALREADY_EXISTS: stale branch", "release", "agent"],
  ["RELEASE_CHECKOUT_DIRTY: hook left files after commit", "release", "agent"],
  ["RELEASE_TREE_MISMATCH: source-only release fix", "release", "agent"],
  ["RELEASE_RECOVERY_NEEDS_MIGRATION_REPAIR: source fix required", "recover", "agent"],
  ["RELEASE_RECOVERY_BLOCKED: ambiguous state", "recover", "agent"],
  ["OPENCODE_SERVER_START_FAILED: timeout", "agent", "infrastructure"],
  ["OPENCODE_SQLITE_BUSY: database is locked", "agent", "infrastructure"],
  ["MIGRATION_GROUP_FAILED: worker failed. OPENCODE_SERVER_START_FAILED: database is locked", "agent", "infrastructure"],
  ["PROMPT_HARNESS_TIMEOUT: dashboard prompt export exceeded 45000ms", "agent", "infrastructure"],
  ["PARALLEL_WORKER_BOOTSTRAP_FAILED: yarn install", "agent", "infrastructure"],
  ["AGENT_PROMPT_AUTOBUILD_FAILED: no executable Branch plan and no valid supervisor seed", "agent", "agent"],
  ["MERGE_RECOVERY_GIT_STATE_FAILED: timeout", "agent", "infrastructure"],
  ["BATCH_SCOPE_HASH_MISSING: stale prompt", "agent", "agent"],
  ["MERGE_RECOVERY_POSTCONDITION_FAILED: unresolved remains", "agent", "agent"],
  ["RELEASE_SOURCE_CHECKOUT_MISMATCH: wrong clean checkout", "release", "agent"],
  ["PLANNER_SCOPE_VIOLATION: planner touched refs", "agent", "agent"],
  ["GROUP_ALIAS_PLAN_MISSING: stale continuation alias", "agent", "agent"],
  ["RECOVERY_INPUT_MISSING: prompt not found", "agent", "agent"],
];
for (const [message, action, expected] of cases) {
  const actual = classifyFlowRecovery(message, action);
  if (actual.kind !== expected) throw new Error(`${message}: expected ${expected}, got ${JSON.stringify(actual)}`);
}
const releasePrompt = buildReleaseRecoveryPrompt({
  projectName: "Demo",
  projectPath: "C:/repo",
  releaseBranch: "libs-release",
  mergedBranch: "libs-merged",
  failure: "RELEASE_COMMIT_OR_HOOK_FAILED: lint",
  userNote: "разберись и доведи release",
  resultPath: "C:/tmp/recovery-result.json",
});
if (!releasePrompt.includes("migration-repair-required")) throw new Error("Release recovery must use a machine-readable migration-repair result instead of conversational text");
if (!releasePrompt.includes("Do not run `git commit`")) throw new Error("Release recovery agent must not own the final commit");
if (!releasePrompt.includes("staged release tree must remain equivalent")) throw new Error("Release recovery must preserve the merged-tree invariant");

const recoveryTmp = mkdtempSync(join(tmpdir(), "dependency-release-recovery-"));
const recoveryResult = join(recoveryTmp, "result.json");
writeFileSync(recoveryResult, JSON.stringify({ status: "migration-repair-required", reason: "merged source is red" }));
const parsedRecovery = readReleaseRecoveryResult(recoveryResult);
if (parsedRecovery?.status !== "migration-repair-required") throw new Error("Release recovery machine result was not parsed");
if (!releasePrompt.includes("Human prose in chat is diagnostic only")) throw new Error("Release recovery must not use Markdown text as its primary control channel");

const mainSource = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf8");
for (const retryContract of [
  "function nonRetryableDeterministicFailure",
  "BASELINE_VERIFY_INCONCLUSIVE_PROJECT_ERROR",
  "!nonRetryableDeterministicFailure(result)",
]) {
  if (!mainSource.includes(retryContract)) throw new Error(`Deterministic Baseline retry contract missing: ${retryContract}`);
}

for (const required of [
  "handoffPreparedReleaseToMigrationRepair",
  "RELEASE_TO_MIGRATION_HANDOFF_UNSAFE",
  "git', args: ['-C', project.path, 'reset', '--hard', 'HEAD']",
  "await runMigrationAgentLoop(job)",
  "Новый финальный release после migration integration repair",
  "MIGRATION_FINAL_VERIFICATION_WRONG_BRANCH",
]) {
  if (!mainSource.includes(required)) throw new Error(`Recovery handoff/final verification contract missing: ${required}`);
}

console.log("Flow recovery classification OK");
