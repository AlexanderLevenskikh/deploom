import { buildAgentExecutionBatches } from "../dist-electron/agent-batching.js";
import { assessAgentContextBudget, estimateAgentInputTokens } from "../dist-electron/agent-context-budget.js";
import { applyDependencyActionsToPackageJson, classifyDependencyMaterializationFailure, dependencyMaterializationInstallSpec } from "../dist-electron/dependency-materialization.js";
import { agentBatchCompletionFingerprint, agentScopeFingerprint, resumableAgentSessionId } from "../dist-electron/agent-session.js";
import { batchRoadmapDocument } from "../dist-electron/roadmap-dossier.js";
import { latestAgentCheckpoint } from "../dist-electron/agent-checkpoint.js";
import { rebindOperationalProjectPath } from "../dist-electron/prompt-paths.js";
import { mkdtempSync, mkdirSync, writeFileSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const manifest = {
  columns: ["package", "shouldUpdate", "compatibilityCohort"],
  rows: [
    ["a", true, ""],
    ["storybook-a", true, "storybook"],
    ["storybook-b", true, "storybook"],
    ["storybook-c", true, "storybook"],
    ["d", true, ""],
    ["e", true, ""],
    ["f", true, ""],
    ["deferred", false, ""],
  ],
};
const batches = buildAgentExecutionBatches(manifest, 4);
const storybookBatch = batches.find((batch) => batch.packages.includes("storybook-a"));
if (!storybookBatch || !["storybook-a", "storybook-b", "storybook-c"].every((name) => storybookBatch.packages.includes(name))) {
  throw new Error(`Compatibility cohort was split: ${JSON.stringify(batches)}`);
}
if (batches.some((batch) => batch.packages.includes("deferred"))) throw new Error("Deferred rows must not consume an execution batch");
if (batches.some((batch) => batch.packages.length > 4 && !batch.compatibilityCohorts.length)) throw new Error(`Non-cohort batch exceeded the limit: ${JSON.stringify(batches)}`);

const semanticBatches = buildAgentExecutionBatches(manifest, 2, { splitCompatibilityCohorts: true });
if (semanticBatches.some((batch) => batch.packages.length > 2)) throw new Error(`Pre-materialized semantic batches must stay bounded even inside a compatibility cohort: ${JSON.stringify(semanticBatches)}`);
const storybookSemantic = semanticBatches.filter((batch) => batch.packages.some((name) => name.startsWith("storybook-")));
if (storybookSemantic.length < 2 || !storybookSemantic.every((batch) => batch.compatibilityCohorts.includes("storybook"))) throw new Error(`Split cohort lost its dependency identity: ${JSON.stringify(storybookSemantic)}`);

const smallBudget = assessAgentContextBudget(["hello"], 100, 10);
if (!smallBudget.ok || estimateAgentInputTokens(["hello"]) <= 0) throw new Error(`Small agent prompt was rejected: ${JSON.stringify(smallBudget)}`);
const hugeBudget = assessAgentContextBudget(["x".repeat(1200)], 100, 10);
if (hugeBudget.ok) throw new Error(`Oversized agent prompt must be rejected before provider launch: ${JSON.stringify(hugeBudget)}`);
const reservedBudget = assessAgentContextBudget(["hello"], 100_000, 2_000, { providerContextLimitTokens: 40_000, outputReserveTokens: 8_000, providerHistoryReserveTokens: 24_000 });
if (reservedBudget.effectiveInputCeilingTokens !== 6_000) throw new Error(`Agent admission must reserve output/provider-history headroom: ${JSON.stringify(reservedBudget)}`);

const packageText = JSON.stringify({ dependencies: { a: "1.0.0" }, devDependencies: { b: "2.0.0" } }, null, 2) + "\n";
const materialized = applyDependencyActionsToPackageJson(packageText, [
  { project: "Demo", package: "a", section: "dependencies", target: "1.5.0", action: "update" },
  { project: "Demo", package: "b", section: "devDependencies", target: "", action: "remove" },
]);
const materializedJson = JSON.parse(materialized.text);
if (materializedJson.dependencies.a !== "1.5.0" || "b" in materializedJson.devDependencies) throw new Error(`Control-plane dependency materialization is wrong: ${materialized.text}`);
if (classifyDependencyMaterializationFailure("HTTP 502 Bad Gateway") !== "infrastructure") throw new Error("Materialization must never learn network failures as dependency constraints");
if (classifyDependencyMaterializationFailure("ERESOLVE conflicting peer dependency") !== "dependency") throw new Error("Materialization must classify deterministic resolver conflicts");
const yarnInstall = dependencyMaterializationInstallSpec("yarn");
if (!yarnInstall.args.includes("--frozen-lockfile")) {
  throw new Error("Yarn materialization must preserve the exact proven lockfile");
}

const pnpmInstall = dependencyMaterializationInstallSpec("pnpm");
if (!pnpmInstall.args.includes("--frozen-lockfile")) {
  throw new Error("pnpm materialization must preserve the exact proven lockfile");
}

const npmInstall = dependencyMaterializationInstallSpec("npm");
if (npmInstall.args[0] !== "ci") {
  throw new Error("npm materialization must use immutable npm ci semantics");
}
const fingerprint = agentScopeFingerprint({ provider: "opencode", project: "Demo", branch: "libs-group-5", scopeHash: "scope-a", model: "corp/code", promptVersion: "2026-08-09T05:26:00Z" });
const saved = { provider: "opencode", id: "ses-safe", interrupted: true, updatedAt: "now", scopeFingerprint: fingerprint };
if (resumableAgentSessionId(saved, "opencode", fingerprint) !== "ses-safe") throw new Error("Exact interrupted session must be resumable");
if (resumableAgentSessionId({ ...saved, scopeFingerprint: "stale" }, "opencode", fingerprint)) throw new Error("Stale scope session must never resume");
if (resumableAgentSessionId({ provider: "opencode", id: "legacy", interrupted: true, updatedAt: "old" }, "opencode", fingerprint)) throw new Error("Legacy un-fingerprinted session must never resume automatically");
if (resumableAgentSessionId({ ...saved, interrupted: false }, "opencode", fingerprint)) throw new Error("Normally completed session must not be treated as resumable");

const roadmap = {
  project_health: { Demo: { status: "yellow" } },
  projects: {
    Demo: [
      { name: "a", breaking_changes: ["full breaking fact"], release_by_target: { "2.0.0": { sources: [1, 2, 3, 4] } } },
      { name: "unrelated", breaking_changes: ["noise"] },
    ],
    Other: [{ name: "other-project-noise", breaking_changes: ["lots of unrelated context"] }],
  },
};
const filtered = batchRoadmapDocument(roadmap, "Demo", ["a"]);
if (filtered.projects.Demo.length !== 1 || filtered.projects.Demo[0].name !== "a") throw new Error(`Batch roadmap did not remove unrelated rows: ${JSON.stringify(filtered)}`);
if (filtered.projects.Demo[0].release_by_target["2.0.0"].sources.length !== 4) throw new Error("Batch roadmap must preserve untruncated release intelligence");
if (filtered.project_health.Demo.status !== "yellow") throw new Error("Project-level roadmap facts must be preserved");
if (filtered.projects.Other) throw new Error("Other projects must not leak into a batch roadmap");
const missingFallsBack = batchRoadmapDocument(roadmap, "Demo", ["missing-package"]);
if (missingFallsBack !== roadmap) throw new Error("Missing batch release intelligence must fall back to the full canonical roadmap");

const canonicalPath = "C:\\Users\\demo\\checkout-form";
const workerPath = "C:\\Temp\\worker\\checkout-form";
const reboundPrompt = rebindOperationalProjectPath(`cwd=${canonicalPath}; json=${JSON.stringify(canonicalPath).slice(1, -1)}`, canonicalPath, workerPath);
if (reboundPrompt.includes(canonicalPath) || reboundPrompt.includes(JSON.stringify(canonicalPath).slice(1, -1)) || !reboundPrompt.includes(workerPath)) throw new Error("Batch prompt must bind every operational project path to its worker worktree");
const workerFingerprint = agentScopeFingerprint({ provider: "opencode", project: "Demo", branch: "libs-group-5", scopeHash: "scope-a", model: "corp/code", promptVersion: `v1|worktree:${workerPath.toLowerCase()}` });
if (resumableAgentSessionId(saved, "opencode", workerFingerprint)) throw new Error("A session created for an old/canonical worktree path must restart with the rebound worker prompt");
const completionA = agentBatchCompletionFingerprint({ project: "Demo", branch: "libs-group-5", scopeHash: "scope-a", promptVersion: "v1|dashboard:1" });
const completionB = agentBatchCompletionFingerprint({ project: "Demo", branch: "libs-group-5", scopeHash: "scope-a", promptVersion: "v1|dashboard:1" });
if (completionA !== completionB) throw new Error("Semantic batch completion marker must be stable across recreated worktrees/providers");

const checkpointRoot = mkdtempSync(join(tmpdir(), "dependency-agent-checkpoint-"));
const runsDir = join(checkpointRoot, "runs");
mkdirSync(runsDir);
const staleCheckpoint = join(runsDir, "20260801_Demo_libs-group-5_state.json");
const currentCheckpoint = join(runsDir, "20260809_Demo_libs-group-5_state.json");
writeFileSync(staleCheckpoint, "{}\n");
writeFileSync(currentCheckpoint, "{}\n");
const checkpointNow = Date.now();
utimesSync(staleCheckpoint, new Date(checkpointNow - 60_000), new Date(checkpointNow - 60_000));
utimesSync(currentCheckpoint, new Date(checkpointNow + 1_000), new Date(checkpointNow + 1_000));
if (latestAgentCheckpoint(runsDir, "Demo", "libs-group-5", checkpointNow) !== currentCheckpoint) throw new Error("Fresh retry must reuse only a checkpoint created by the current batch run");
if (latestAgentCheckpoint(runsDir, "Demo", "libs-group-1", checkpointNow)) throw new Error("Checkpoint from another branch must never leak into a retry");

console.log("Agent efficiency checks passed");
