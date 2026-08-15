import { assessPromptRevision, buildPlannerPrompt, partitionPlannerDeferrals, readPlannerResult, validateSupervisorScopeAdditions } from "../dist-electron/planner-session.js";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { plannerResultCacheKey, plannerResultCachePath, writePlannerResultCache } from '../dist-electron/planner-result-cache.js';
import { tmpdir } from "node:os";
import { join } from "node:path";

function prompt(rows, branches = [{ branch: "libs-group-1", bucket: "g1", packages: rows.filter((row) => row.shouldUpdate).map((row) => row.package) }]) {
  return [
    "## Exact compact scope manifest",
    "```json",
    JSON.stringify({ targetMode: "yellow", rows }),
    "```",
    "## Branch plan",
    "```json",
    JSON.stringify([{ project: "Demo", base: "main", merged: "libs-merged", branches }]),
    "```",
  ].join("\n");
}
const row = (pkg, target, shouldUpdate = true, extra = {}) => ({ project: "Demo", section: "dependencies", package: pkg, current: "1.0.0", target, action: shouldUpdate ? "update" : "deferred", shouldUpdate, scopeExcluded: false, compatibilityCohort: '', compatibilityNote: '', ...extra });
const previous = prompt([row("react", "19.0.1"), row("vite", "6.0.0")]);
const narrowed = prompt([row("react", "19.0.1", false), row("vite", "6.0.0")], [{ branch: "libs-group-1", bucket: "g1", packages: ["vite"] }]);
const safe = assessPromptRevision(previous, narrowed, "Demo");
if (!safe.safe || safe.removals.length !== 1) throw new Error(`Expected safe narrowing, got ${JSON.stringify(safe)}`);

const lower = assessPromptRevision(previous, prompt([row("react", "18.3.1"), row("vite", "6.0.0")]), "Demo");
if (!lower.safe || !lower.removals.some((item) => item.includes("react"))) throw new Error(`Lower target must be safe narrowing: ${JSON.stringify(lower)}`);

const companionPrevious = prompt([row("vitest", "3.2.6"), row("vite", "—", false)]);
const companionNext = prompt([
  row("vitest", "3.2.6", true, { compatibilityCohort: "auto-peer-vitest-vite", compatibilityNote: "AUTO_PEER_CLOSURE: vitest requires vite>=5" }),
  row("vite", "5.4.0", true, { compatibilityCohort: "auto-peer-vitest-vite", compatibilityNote: "AUTO_PEER_CLOSURE: vitest requires vite>=5" }),
], [{ branch: "libs-group-1", bucket: "g1", packages: ["vitest", "vite"] }]);
const companion = assessPromptRevision(companionPrevious, companionNext, "Demo");
if (!companion.safe || companion.autoAdditions.length !== 1 || !companion.autoAdditions[0].includes("vite")) throw new Error(`Deterministic direct peer companion must be auto-approved: ${JSON.stringify(companion)}`);

const continuationAlias = prompt(
  [row("vitest", "3.2.6")],
  [{ branch: "libs-continuation-7", scopeBranch: "libs-group-1", bucket: "g1", packages: ["vitest"] }],
);
const logicalSameBranch = assessPromptRevision(prompt([row("vitest", "3.2.6")]), continuationAlias, "Demo");
if (logicalSameBranch.reason.includes("перенос") || logicalSameBranch.additions.some((item) => item.startsWith("branch:"))) {
  throw new Error(`Continuation Git alias must preserve logical scope identity: ${JSON.stringify(logicalSameBranch)}`);
}

const residualPrevious = prompt([row("vitest", "3.2.6"), row("jsdom", "—", false)]);
const residualNext = prompt([
  row("vitest", "—", false, { compatibilityNote: "PLANNER_DEFERRED: needs vite>=5" }),
  row("jsdom", "27.1.0", true, { compatibilityNote: "AUTO_RESIDUAL_PLAN: deterministic replacement after narrowing" }),
], [{ branch: "libs-group-2", bucket: "g2", packages: ["jsdom"] }]);
const residual = assessPromptRevision(residualPrevious, residualNext, "Demo");
if (!residual.safe || !residual.autoAdditions.some((item) => item.includes("jsdom")) || !residual.removals.some((item) => item.includes("vitest"))) {
  throw new Error(`Deterministic residual replacement must be auto-approved: ${JSON.stringify(residual)}`);
}

const ordinaryResidualRows = [row("seed", "2.0.0", true), ...Array.from({ length: 36 }, (_, index) => row(`pkg-${index + 1}`, "2.0.0", false))];
const ordinaryResidualPrevious = prompt(ordinaryResidualRows, [{ branch: "libs-group-1", bucket: "g1", packages: ["seed"] }]);
const ordinaryResidualNextRows = [row("seed", "2.0.0", true), ...Array.from({ length: 36 }, (_, index) => row(`pkg-${index + 1}`, "2.0.0", true))];
const ordinaryResidualNext = prompt(ordinaryResidualNextRows, [{ branch: "libs-group-1", bucket: "g1", packages: ordinaryResidualNextRows.map((item) => item.package) }]);
const ordinaryResidual = assessPromptRevision(ordinaryResidualPrevious, ordinaryResidualNext, "Demo");
if (!ordinaryResidual.safe || ordinaryResidual.autoAdditions.length !== 36 || ordinaryResidual.additions.length !== 36) {
  throw new Error(`Deterministic reactivation of existing direct residual rows must not require approval, even for a large plan: ${JSON.stringify(ordinaryResidual)}`);
}

const supervisorPrevious = prompt([row("postcss-scss", "—", false)]);
const supervisorNext = prompt([row("postcss-scss", "4.0.7", true, { compatibilityNote: "SUPERVISOR_SCOPE_EXPANSION: goal-seeking existing direct dependency" })]);
const supervisorExpansion = assessPromptRevision(supervisorPrevious, supervisorNext, "Demo");
if (!supervisorExpansion.safe || !supervisorExpansion.autoAdditions.some((item) => item.includes("postcss-scss"))) throw new Error(`Supervisor existing-direct expansion must be auto-approved: ${JSON.stringify(supervisorExpansion)}`);
const validated = validateSupervisorScopeAdditions(supervisorPrevious, "Demo", ["postcss-scss@4.0.7", "new-package@2.0.0", "postcss-scss@0.9.0"]);
if (validated.accepted.length !== 1 || validated.accepted[0].package !== "postcss-scss" || validated.rejected.length !== 2) throw new Error(`Supervisor proposal validation failed: ${JSON.stringify(validated)}`);


const deferralPrompt = prompt([
  row("postcss-scss", "4.0.7", true),
  row("@company/react-ui", "5.4.2", false),
  row("react", "19.0.1", false),
  row("@company/react-ui-addons", "5.0.0", false, { action: "excluded", scopeExcluded: true }),
]);
const partition = partitionPlannerDeferrals(
  deferralPrompt,
  "Demo",
  new Set(["postcss-scss"]),
  ["postcss-scss", "@company/react-ui@4.26.0", "react@18.2.0", "@company/react-ui-addons@4.8.0"],
);
if (partition.apply.join(",") !== "postcss-scss" || partition.ignored.length !== 3 || partition.rejected.length) {
  throw new Error("Already deferred/excluded version-qualified planner rows must be safe no-ops: " + JSON.stringify(partition));
}
const inventedDeferral = partitionPlannerDeferrals(deferralPrompt, "Demo", new Set(["postcss-scss"]), ["invented-package@1.0.0"]);
if (inventedDeferral.rejected.length !== 1) throw new Error("Unknown planner deferral must still require approval");
for (const candidate of [
  prompt([row("react", "19.0.1"), row("vite", "6.0.0"), row("react-dom", "19.0.1")]),
  prompt([row("react", "20.0.0"), row("vite", "6.0.0")]),
  prompt([row("react", "19.0.1"), row("vite", "6.0.0")], [{ branch: "libs-group-2", bucket: "g2", packages: ["react", "vite"] }]),
]) {
  const assessment = assessPromptRevision(previous, candidate, "Demo");
  if (assessment.safe || (!assessment.additions.length && !assessment.reason.includes("перенос"))) throw new Error(`Expansion/topology change must require approval: ${JSON.stringify(assessment)}`);
}
const same = assessPromptRevision(previous, previous, "Demo");
if (same.safe || same.changed) throw new Error("Unchanged replan must stall");

const dir = mkdtempSync(join(tmpdir(), "planner-session-"));
const resultPath = join(dir, "result.json");
writeFileSync(resultPath, JSON.stringify({ status: "expand-plan", reason: "goal needs another direct package", proposedScopeAdditions: ["postcss-scss@4.0.7"], executorGuidance: "keep verification green" }));
const parsedResult = readPlannerResult(resultPath);
if (parsedResult?.status !== "expand-plan" || parsedResult.proposedScopeAdditions?.[0] !== "postcss-scss@4.0.7") throw new Error("Planner machine result not parsed");
writeFileSync(resultPath, JSON.stringify({ status: "invented", reason: "bad" }));
if (readPlannerResult(resultPath)) throw new Error("Unknown planner status must be rejected");

const cacheIdentity = { projectName: 'Demo', promptMarkdown: previous, normalizedFailure: 'TARGET_PLAN_INSUFFICIENT', git: { branch: 'main', head: 'abc', status: '', refs: 'refs/heads/main abc' } };
const cacheKey = plannerResultCacheKey(cacheIdentity);
if (cacheKey !== plannerResultCacheKey(cacheIdentity) || cacheKey === plannerResultCacheKey({ ...cacheIdentity, normalizedFailure: 'OTHER' })) throw new Error('Planner cache identity must be deterministic and invalidate on failure changes');
const cachePath = plannerResultCachePath(dir, cacheKey);
writePlannerResultCache(cachePath, parsedResult);
if (readPlannerResult(cachePath)?.status !== 'expand-plan') throw new Error('Persisted Planner result must remain machine-readable after restart');

const plannerPrompt = buildPlannerPrompt({ projectName: "Demo", projectPath: "C:/tmp/repo", failure: "MIGRATION_REPLAN_REQUIRED", savedPromptPath: "C:/tmp/prompt.md", resultPath });
for (const required of ["Нельзя менять файлы проекта", "expand-plan", "existing direct", "deferPackages", "TARGET_PLAN_INSUFFICIENT", "невалидным machine result", "Human prose не является управляющим сигналом"]) {
  if (!plannerPrompt.includes(required)) throw new Error(`Planner contract missing: ${required}`);
}
const mainSource = readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8');
for (const required of ['PLANNER_ATTEMPT_TIMEOUT_MS = 6 * 60_000', 'timeoutMs: PLANNER_ATTEMPT_TIMEOUT_MS', "setBranchRuntime(job, branch, 'planning'", 'runIndependentPlannerWithProgress', 'status: relevantGitStatus(status.stdout)', 'Planner machine-result переиспользован после перезапуска', "planner-results', job.workspace.id"]) {
  if (!mainSource.includes(required)) throw new Error(`Planner runtime contract missing: ${required}`);
}
console.log("Planner session contract OK");
