import { assessMigrationCheckpoint, unexplainedFailures } from "../dist-electron/migration-verification.js";
import { migrationGatePolicy } from "../dist-electron/migration-gates.js";
import { buildMergedRepairPrompt, readMergedRepairResult } from "../dist-electron/merged-repair.js";
import { buildGroupVerificationRepairPrompt } from "../dist-electron/group-repair.js";
import { baselineVerificationCacheKey, cleanEphemeralVerificationCaches } from "../dist-electron/verification-environment.js";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const group5 = {
  status: "ready-to-commit",
  commands: [
    "BASELINE: lint:types=0, lint:styles=0, build=0",
    "yarn lint:types -> exit 2 (CROSS-COHORT: expected companion group-4)",
    "yarn lint:styles -> exit 1",
    "yarn build -> exit 1",
  ],
};
let assessment = assessMigrationCheckpoint(group5, ["deps-demo-group-1", "deps-demo-group-2", "deps-demo-group-3", "deps-demo-group-5"]);
if (assessment.status !== "replan-required") throw new Error(`Missing CROSS-COHORT branch must force replan, got ${JSON.stringify(assessment)}`);
if (!assessment.missingPlanGroups.includes("group-4")) throw new Error(`Expected missing group-4: ${JSON.stringify(assessment)}`);
if (!assessment.regressions.some((item) => item.command.includes("lint:types") && item.baselineExit === 0 && item.postExit === 2)) {
  throw new Error(`Baseline-green -> post-red lint:types regression was not detected: ${JSON.stringify(assessment)}`);
}

assessment = assessMigrationCheckpoint(group5, ["deps-demo-group-4", "deps-demo-group-5"]);
if (assessment.status !== "repair-required") throw new Error(`Known companion group must still leave red checks repair-required, got ${JSON.stringify(assessment)}`);

assessment = assessMigrationCheckpoint({
  migrationOutcome: { status: "ready" },
  verification: { commands: [
    { command: "yarn lint:types", baseline: "exit 0", post: "exit 0" },
    { command: "yarn build", baseline: "exit 0", post: "exit 0" },
  ] },
}, ["libs-group-1"]);
if (assessment.status !== "pass") throw new Error(`Green structured checkpoint must pass: ${JSON.stringify(assessment)}`);

assessment = assessMigrationCheckpoint({ migrationOutcome: { status: "replan-required", reason: "vite peer is excluded" } }, ["libs-group-1"]);
if (assessment.status !== "replan-required") throw new Error("Explicit replan-required outcome must take precedence");

const packageJson = { scripts: { "lint:types": "tsc", typecheck: "tsc", "lint:styles": "stylelint .", test: "vitest", build: "vite build" } };
let policy = migrationGatePolicy({}, "Demo", packageJson, "npm");
if (policy.source !== "package-scripts") throw new Error(`Expected package-script fallback: ${JSON.stringify(policy)}`);
if (!policy.verificationCommands.includes("npm run lint:types") || policy.verificationCommands.includes("npm run typecheck")) {
  throw new Error(`Focused gate discovery/dedup is wrong: ${JSON.stringify(policy)}`);
}
if (policy.verificationCommands.indexOf("npm run build") > policy.verificationCommands.indexOf("npm run test")) {
  throw new Error(`Build must run before tests can leave cache artifacts in the source tree: ${JSON.stringify(policy)}`);
}
policy = migrationGatePolicy({ release: { finalGateCommands: ["yarn verify"] } }, "Demo", packageJson, "yarn");
if (policy.source !== "release-gates" || policy.verificationCommands[0] !== "yarn verify") throw new Error(`Release gates must be the backwards-compatible fallback: ${JSON.stringify(policy)}`);
policy = migrationGatePolicy({ migration: { verificationCommands: ["yarn lint:types"], integrationVerificationCommands: ["yarn lint:types", "yarn build"] } }, "Demo", packageJson, "yarn");
if (policy.source !== "migration-settings" || policy.integrationVerificationCommands.length !== 2) throw new Error(`Migration settings must win: ${JSON.stringify(policy)}`);

const temp = mkdtempSync(join(tmpdir(), "dependency-merged-repair-"));
const resultPath = join(temp, "result.json");
writeFileSync(resultPath, JSON.stringify({ status: "replan-required", reason: "approved peer is excluded" }));
const result = readMergedRepairResult(resultPath);
if (result?.status !== "replan-required") throw new Error("Merged integration repair must expose a machine-readable replan result");
const prompt = buildMergedRepairPrompt({
  projectName: "Demo", projectPath: "C:/repo", mergedBranch: "libs-merged",
  savedPromptPath: "C:/repo/.dependency-roadmap/prompt.md", resultPath,
  failure: "yarn lint:types -> exit 2", verificationCommands: ["yarn lint:types"],
});
if (!prompt.includes('"status":"replan-required"')) throw new Error("Merged repair prompt must define the machine replan contract");
if (!prompt.includes("Direct dependency targets/actions are immutable")) throw new Error("Merged repair must preserve approved direct targets");
if (!prompt.includes("smallest migration-required compatibility fix")) throw new Error("Merged repair must ask the agent to fix failures immediately rather than defer them to release");
if (prompt.includes("narrowly scoped resolution/override")) throw new Error("Merged repair must not let the LLM choose transitive dependency versions anymore");
if (!prompt.includes("do not add/remove direct dependencies") || !prompt.includes("edit overrides/resolutions")) throw new Error("Merged repair must make dependency control-plane ownership explicit");

const fullGroupPrompt = `## Exact compact scope manifest\n\n\`\`\`json\n${JSON.stringify({ columns: ["project","package","section","target","shouldUpdate","action"], rows: [["Demo","vitest","devDependencies","3.2.6",true,"update"],["Demo","vite","devDependencies","5.4.0",true,"update"]] })}\n\`\`\``;
const boundedRepair = buildGroupVerificationRepairPrompt({
  projectName: "Demo", projectPath: "C:/repo", branch: "cohort-vitest-vite",
  savedPromptPath: "C:/repo/.dependency-roadmap/prompt.md", fullGroupPrompt,
  failure: "TS2769 incompatible UserConfig", verificationCommands: ["yarn lint:types"],
});
if (!boundedRepair.includes('"vitest"') || !boundedRepair.includes('"vite"')) throw new Error("Bounded group repair must carry the immutable assignment without the full release dossier");
if (!boundedRepair.includes("edit overrides/resolutions") || !boundedRepair.includes("DEPENDENCY_COMPATIBILITY_EVIDENCE") || !boundedRepair.includes("exact Z3")) throw new Error("Bounded group repair must return dependency contradictions through deterministic evidence to exact Z3");

// Real incident: yarn lint:scripts failed on deps-demo-merged with a chronic
// .eslintrc.js/tsconfig.json mismatch unrelated to any updated package. No
// group's own focused checks ever ran it, so its evidence has no defined
// baselineExit anywhere -- allowKnownBaselineFailures can never forgive it,
// and 3 real repair attempts were burned trying to "fix" a pre-existing,
// migration-unrelated config problem. unexplainedFailures is what identifies
// exactly this case as safe to live-probe instead.
const noEvidenceAtAll = unexplainedFailures(
  [{ command: "yarn lint:scripts" }],
  [{ command: "yarn lint:styles", baselineExit: 0 }, { command: "yarn build", baselineExit: 1 }],
);
if (noEvidenceAtAll.length !== 1 || noEvidenceAtAll[0].command !== "yarn lint:scripts") {
  throw new Error(`A failing command with no recorded baseline anywhere must be a live-probe candidate: ${JSON.stringify(noEvidenceAtAll)}`);
}

// A command some group's focused checks DID record -- whether that baseline
// was already tolerated (non-zero) or is a genuine baseline=0-to-failing
// regression -- must never be re-judged by a live probe; the evidence-backed
// classification always wins, live or not.
const alreadyExplained = unexplainedFailures(
  [{ command: "yarn build" }, { command: "yarn lint:types" }],
  [{ command: "yarn build", baselineExit: 1 }, { command: "yarn lint:types", baselineExit: 0 }],
);
if (alreadyExplained.length !== 0) {
  throw new Error(`Commands with a defined baseline anywhere must not be re-probed: ${JSON.stringify(alreadyExplained)}`);
}

// Command-name formatting ("yarn X" vs "X", trailing parens) must not defeat
// the match -- verificationCommandKey already normalizes this for the
// baseline map itself; unexplainedFailures must normalize failing commands
// the same way or it will always call an already-explained command
// "unexplained".
const normalizedMatch = unexplainedFailures(
  [{ command: "yarn lint:styles (stylelint)" }],
  [{ command: "lint:styles", baselineExit: 1 }],
);
if (normalizedMatch.length !== 0) {
  throw new Error(`Command key normalization must match a failing command to its recorded baseline: ${JSON.stringify(normalizedMatch)}`);
}

const cacheTmp = mkdtempSync(join(tmpdir(), "dependency-verification-cache-"));
mkdirSync(join(cacheTmp, "node_modules", ".vite"), { recursive: true });
mkdirSync(join(cacheTmp, "src", "node_modules", ".vitest"), { recursive: true });
const removedCaches = cleanEphemeralVerificationCaches(cacheTmp);
if (!removedCaches.includes("node_modules/.vite") || !removedCaches.includes("src/node_modules/.vitest")) {
  throw new Error(`Deterministic verification must remove stale Vite/Vitest caches before spending an agent repair: ${JSON.stringify(removedCaches)}`);
}
if (baselineVerificationCacheKey("Demo", "main", "yarn lint:types") !== baselineVerificationCacheKey("Demo", "main", "lint:types")) {
  throw new Error("Shared baseline evidence cache must normalize package-manager command spelling");
}

console.log("Migration verification and integration-repair checks passed");
