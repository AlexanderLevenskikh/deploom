import { adoptEmptyContinuationBranches, adoptHistoricalContinuationBranches, adoptPreferredScopeBranches, buildMigrationProgress, continuationMigrationPlan, countTrackedDirtyChanges, integratedBranchTargets, leftoverConflictMarkerLines, liveGitWorktreeRecords, mergeInProgressNote, mergePackageJsonThreeWay, migrationCompletionIssues, migrationBatchScopeDriftIssues, migrationGroupScopeDriftIssues, migrationPlanFromPrompt, migrationStateSummary, missingGitignorePatterns, nextIncompleteMigrationBranch, recoverContinuationScopeBranches, rebindMigrationPromptBranchIdentity, replaceMigrationPlanInPrompt, rollbackIncompleteMigrationActions, relevantGitStatus, relevantGitStatusLines, proofEnvelopeContentKey, satisfiedScopePackagesFromPrompt, strayUntrackedPaths, threeWayMergeDependencyMaps, validateScopeProofEnvelope } from "../dist-electron/migration-progress.js";

const worktrees = liveGitWorktreeRecords(`worktree C:/repo\nHEAD abc\nbranch refs/heads/main\n\nworktree C:/missing\nHEAD def\nbranch refs/heads/stale\nprunable gitdir file points to non-existent location\n`);
if (worktrees.length !== 1 || worktrees[0].branch !== "main") throw new Error(`Prunable worktree leaked into live Git state: ${JSON.stringify(worktrees)}`);

const prompt = `# Migration\n\n## Branch plan\n\n\`\`\`json\n[{"project":"Demo","base":"libs","merged":"libs-merged","branches":[{"order":1,"branch":"libs-group-1","packages":["a","b"]},{"order":2,"branch":"libs-group-2","packages":["c"]}]}]\n\`\`\``;
const plan = migrationPlanFromPrompt(prompt, "Demo");
if (!plan || plan.baseBranch !== "libs" || plan.branches.length !== 2 || plan.branches[0].label !== "group-1") throw new Error(`Branch plan was not parsed: ${JSON.stringify(plan)}`);
const progress = buildMigrationProgress({ plan, refs: ["master", "libs-group-1", "origin/libs-group-2", "libs-merged"], currentBranch: "libs-group-2", mergedBranches: ["libs-group-1"], dirtyChanges: 3, factsRef: "libs-merged" });
if (progress.createdBranches !== 2 || progress.completedDependencies !== 2 || progress.totalDependencies !== 3 || !progress.dirty || progress.dirtyChanges !== 3) throw new Error(`Migration totals are wrong: ${JSON.stringify(progress)}`);
if (progress.branches.map((branch) => branch.status).join(",") !== "merged,created") throw new Error(`Migration branch statuses are wrong: ${JSON.stringify(progress.branches)}`);

// Checkout is not progress: the branch HEAD happens to sit on must keep the
// status its own content earns, or every branch switch by the agent rewrites
// the panel. This is the regression behind the flickering statuses.
if (progress.branches[1].checkedOut !== true || progress.branches[0].checkedOut !== false) throw new Error(`Checked-out branch was not tracked separately: ${JSON.stringify(progress.branches)}`);
const afterCheckoutSwitch = buildMigrationProgress({ plan, refs: ["master", "libs-group-1", "origin/libs-group-2", "libs-merged"], currentBranch: "master", mergedBranches: ["libs-group-1"], dirtyChanges: 3, factsRef: "libs-merged" });
if (afterCheckoutSwitch.branches.map((branch) => branch.status).join(",") !== progress.branches.map((branch) => branch.status).join(",")) throw new Error(`Branch statuses changed when only HEAD moved: ${JSON.stringify(afterCheckoutSwitch.branches)}`);

// A work branch that already carries every one of its own targets is done and
// waiting for a merge; reporting it the same as an untouched branch is what
// made "готово, но не влито" indistinguishable from "ничего не сделано".
const readyForMerge = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-group-2", mergedBranches: ["libs-group-1"], satisfiedPackages: new Set(["a", "b"]), branchSatisfiedPackages: { "libs-group-2": new Set(["c"]) }, factsRef: "libs-merged" });
if (readyForMerge.branches[1].status !== "ready") throw new Error(`Finished-but-unmerged branch was not reported as ready: ${JSON.stringify(readyForMerge.branches)}`);
if (!migrationCompletionIssues(readyForMerge).some((issue) => issue.includes("готова, но не влита"))) throw new Error(`Ready branch must still block completion: ${JSON.stringify(migrationCompletionIssues(readyForMerge))}`);

// v2 execution contract: deterministic package materialization is not proof
// that semantic source/config migration finished. A clean branch carrying all
// targets remains partial until the orchestrator persists branch completion.
const materializedButNotExecuted = buildMigrationProgress({
  plan,
  refs: ["libs-group-1", "libs-group-2", "libs-merged"],
  currentBranch: "libs-group-2",
  mergedBranches: ["libs-group-1"],
  branchSatisfiedPackages: { "libs-group-2": new Set(["c"]) },
  executionCompletedBranches: new Set(),
  factsRef: "libs-merged",
});
if (materializedButNotExecuted.branches[1].status !== "partial") throw new Error(`Materialized dependency state must not become merge-ready before Executor verification: ${JSON.stringify(materializedButNotExecuted.branches[1])}`);
if (!materializedButNotExecuted.branches[1] || !materializedButNotExecuted.branches[1].packages.length) throw new Error("v2 branch fixture malformed");
const executedAndVerified = buildMigrationProgress({
  plan,
  refs: ["libs-group-1", "libs-group-2", "libs-merged"],
  currentBranch: "libs-group-2",
  mergedBranches: ["libs-group-1"],
  branchSatisfiedPackages: { "libs-group-2": new Set(["c"]) },
  executionCompletedBranches: new Set(["libs-group-2"]),
  factsRef: "libs-merged",
});
if (executedAndVerified.branches[1].status !== "ready") throw new Error(`Verified v2 branch did not become ready: ${JSON.stringify(executedAndVerified.branches[1])}`);

// A plan branch left at the base commit is an ancestor of every later commit,
// so ancestry alone declared it merged while nothing had been done on it --
// observed for real on a group branch whose tip was still the base commit and
// whose only package was never updated.
const emptyBranchAtBase = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-merged", mergedBranches: ["libs-group-1", "libs-group-2"], branchSatisfiedPackages: { "libs-group-1": new Set(["a", "b"]), "libs-group-2": new Set() } });
if (emptyBranchAtBase.branches[1].status === "merged") throw new Error(`An untouched branch must not count as merged: ${JSON.stringify(emptyBranchAtBase.branches[1])}`);
if (emptyBranchAtBase.branches[0].status !== "merged") throw new Error(`A genuinely merged branch must stay merged: ${JSON.stringify(emptyBranchAtBase.branches[0])}`);
if (!migrationCompletionIssues(emptyBranchAtBase).some((issue) => issue.includes("libs-group-2"))) throw new Error(`Untouched branch must block completion: ${JSON.stringify(migrationCompletionIssues(emptyBranchAtBase))}`);
if (migrationStateSummary(emptyBranchAtBase).includes("libs-merged: libs-group-1, libs-group-2")) throw new Error(`Resume feedback must not report an untouched branch as merged work`);
// Without per-branch facts we cannot second-guess ancestry, so the old
// behaviour has to survive rather than turn every merged branch unfinished.
const mergedWithoutFacts = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-merged", mergedBranches: ["libs-group-1", "libs-group-2"] });
if (mergedWithoutFacts.branches.some((branch) => branch.status !== "merged")) throw new Error(`Ancestry must still decide when no branch facts are available: ${JSON.stringify(mergedWithoutFacts.branches)}`);

const summary = migrationStateSummary(readyForMerge);
if (!summary.includes("libs-group-1") || !summary.includes("libs-group-2") || !summary.includes("libs-merged")) throw new Error(`Resume feedback lost the factual state: ${summary}`);

// An agent that runs out of turns mid-merge leaves conflicts resolved on disk
// but unstaged and uncommitted -- observed for real, with three docs and a
// lockfile fully resolved and MERGE_HEAD still present. The next resume must
// be told directly, not left to rediscover it from a plain dirty-tree count.
if (mergeInProgressNote(false) !== "") throw new Error(`No note is expected when no merge is in progress`);
const note = mergeInProgressNote(true);
if (!note.includes("MERGE_HEAD") || !note.includes("не начинай новый merge")) throw new Error(`Merge-in-progress note must name the state and forbid restarting it: ${note}`);

const integratedOutsideMerged = buildMigrationProgress({ plan, refs: ["master", "libs", "libs-group-1", "libs-group-2"], currentBranch: "libs", mergedBranches: [], integratedBranches: { "libs-group-1": "libs", "libs-group-2": "libs" } });
if (integratedOutsideMerged.branches.map((branch) => branch.status).join(",") !== "integrated,integrated") throw new Error(`Wrong-target integrations were hidden: ${JSON.stringify(integratedOutsideMerged.branches)}`);
const integrationIssues = migrationCompletionIssues(integratedOutsideMerged);
if (!integrationIssues.some((issue) => issue.includes("влита в libs"))) throw new Error(`Wrong integration target was not explained: ${JSON.stringify(integrationIssues)}`);

// Real bug, observed live: the orchestrator creates a work branch as its own
// step before the agent commits anything, so for a real window the branch is
// literally the same commit as its base -- trivially "contained by" base,
// which for-each-ref --contains reports the same as a genuine merge. That
// must never read as "влита не в merged" for a branch that simply has no
// work on it yet; only containment in a *sibling* work branch is meaningful.
const freshlyCreatedContainment = [["libs-group-1", new Set(["libs", "libs-group-1"])]];
const freshBranchTargets = integratedBranchTargets(plan, new Set(), freshlyCreatedContainment);
if (Object.keys(freshBranchTargets).length !== 0) {
  throw new Error(`A branch trivially contained by its own base must not be reported as integrated: ${JSON.stringify(freshBranchTargets)}`);
}
const realIntegrationContainment = [
  ["libs-group-1", new Set(["libs", "libs-group-1", "libs-group-2"])],
  ["libs-group-2", new Set(["libs", "libs-group-2"])],
];
const realIntegrationTargets = integratedBranchTargets(plan, new Set(), realIntegrationContainment);
if (realIntegrationTargets["libs-group-1"] !== "libs-group-2") {
  throw new Error(`A branch genuinely folded into a sibling work branch must still be reported: ${JSON.stringify(realIntegrationTargets)}`);
}
if ("libs-group-2" in realIntegrationTargets) {
  throw new Error(`A branch must not be reported as integrated into itself or the merged branch: ${JSON.stringify(realIntegrationTargets)}`);
}
const alreadyMergedContainment = [["libs-group-1", new Set(["libs", "libs-group-1", "libs-merged"])]];
if (Object.keys(integratedBranchTargets(plan, new Set(["libs-group-1"]), alreadyMergedContainment)).length !== 0) {
  throw new Error("A branch already recorded as merged must never also be reported as integrated elsewhere");
}

const issues = migrationCompletionIssues(progress);
if (issues.length !== 2 || !issues.join(" ").includes("libs-group-2") || !issues.join(" ").includes("3")) throw new Error(`Incomplete agent result was accepted: ${JSON.stringify(issues)}`);
const completedBeforeRelease = ["preflight", "baseline", "agent", "generate", "audit"];
const rolledBack = rollbackIncompleteMigrationActions(completedBeforeRelease, progress);
if (rolledBack.join(",") !== "preflight,baseline") throw new Error(`Invalid completed actions were preserved: ${rolledBack}`);
const preservedReleaseRetry = rollbackIncompleteMigrationActions(completedBeforeRelease, progress, "release");
if (preservedReleaseRetry.join(",") !== completedBeforeRelease.join(",")) throw new Error(`A failed release attempt erased verified stages: ${preservedReleaseRetry}`);

// A branch matching the plan's own `<stem>N` naming convention (e.g.
// libs-group-6) but absent from the Branch plan itself means the agent
// invented scope beyond what was vetted -- this must block completion even
// if every *planned* branch is already merged, exactly like the real run
// where group-1..5 finished but the agent kept inventing group-6, group-7...
const withExtraGroup = buildMigrationProgress({ plan, refs: ["master", "libs-group-1", "libs-group-2", "libs-merged", "libs-group-6"], currentBranch: "libs-merged", mergedBranches: ["libs-group-1", "libs-group-2"] });
if (withExtraGroup.unexpectedBranches.join(",") !== "libs-group-6") throw new Error(`Unexpected out-of-plan branch was not detected: ${JSON.stringify(withExtraGroup.unexpectedBranches)}`);
const extraGroupIssues = migrationCompletionIssues(withExtraGroup);
if (extraGroupIssues.some((issue) => issue.includes("libs-group-6"))) throw new Error(`Out-of-plan refs are advisory/quarantined and must not block completion by themselves: ${JSON.stringify(extraGroupIssues)}`);
const withoutExtraGroup = buildMigrationProgress({ plan, refs: ["master", "libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-merged", mergedBranches: ["libs-group-1", "libs-group-2"] });
if (withoutExtraGroup.unexpectedBranches.length !== 0) throw new Error(`A clean plan must not report unexpected branches: ${JSON.stringify(withoutExtraGroup.unexpectedBranches)}`);

const fence = String.fromCharCode(96).repeat(3);
const residualPrompt = "# Residual\n\n## Branch plan\n\n" + fence + "json\n[{\"project\":\"Demo\",\"base\":\"libs\",\"merged\":\"libs-merged\",\"branches\":[{\"order\":5,\"branch\":\"libs-group-5\",\"packages\":[\"postcss-scss\"]}]}]\n" + fence;
const residualPlan = migrationPlanFromPrompt(residualPrompt, "Demo");
if (!residualPlan) throw new Error("Residual plan was not parsed");
const legacyContinuationPrompt = residualPrompt.replace("libs-group-5", "libs-continuation-2")
  + "\n\n## Exact compact scope manifest\n\n" + fence + "json\n"
  + "{\"columns\":[\"project\",\"group\",\"package\"],\"rows\":[[\"Demo\",5,\"postcss-scss\"]]}\n" + fence;
const legacyContinuationPlan = migrationPlanFromPrompt(legacyContinuationPrompt, "Demo");
if (!legacyContinuationPlan) throw new Error("Legacy continuation prompt was not parsed");
const recoveredLegacyPlan = recoverContinuationScopeBranches(legacyContinuationPrompt, "Demo", legacyContinuationPlan);
if (recoveredLegacyPlan.branches[0].scopeBranch !== "libs-group-5") {
  throw new Error("Logical group was not recovered from legacy continuation manifest: " + JSON.stringify(recoveredLegacyPlan));
}
const generatedContinuationPlan = continuationMigrationPlan(residualPlan, new Set(["libs-group-5"]), ["libs-group-5", "libs-continuation-1", "libs-merged"]);
if (generatedContinuationPlan.baseBranch !== "libs-merged" || generatedContinuationPlan.branches[0].branch !== "libs-continuation-2" || generatedContinuationPlan.branches[0].scopeBranch !== "libs-group-5") {
  throw new Error("Continuation allocation did not preserve the logical Dashboard group while avoiding an occupied Git branch: " + JSON.stringify(generatedContinuationPlan));
}
const continuationPlan = adoptEmptyContinuationBranches(
  generatedContinuationPlan,
  ["libs-group-5", "libs-continuation-1", "libs-merged"],
  new Set(["libs-continuation-1"]),
);
if (continuationPlan.branches[0].branch !== "libs-continuation-1") {
  throw new Error("Empty continuation alias was not reused: " + JSON.stringify(continuationPlan));
}
const continuationPrompt = replaceMigrationPlanInPrompt(residualPrompt, "Demo", continuationPlan);
const reparsedContinuation = continuationPrompt && migrationPlanFromPrompt(continuationPrompt, "Demo");
if (!reparsedContinuation || reparsedContinuation.baseBranch !== "libs-merged" || reparsedContinuation.branches[0].branch !== "libs-continuation-1" || reparsedContinuation.branches[0].scopeBranch !== "libs-group-5") {
  throw new Error("Continuation Branch plan was not persisted in prompt: " + continuationPrompt);
}
const emptyAliasProgress = buildMigrationProgress({
  plan: generatedContinuationPlan,
  refs: ["libs-group-1", "libs-group-2", "libs-group-3", "libs-group-4", "libs-group-5", "libs-continuation-1", "libs-merged"],
  currentBranch: "libs-continuation-1",
  mergedBranches: [],
  satisfiedPackages: new Set(),
  emptyBranches: ["libs-continuation-1"],
});
if (emptyAliasProgress.unexpectedBranches.length !== 0) {
  throw new Error("Empty continuation alias was treated as scope drift: " + JSON.stringify(emptyAliasProgress.unexpectedBranches));
}
const nonEmptyAlias = buildMigrationProgress({
  plan: generatedContinuationPlan,
  refs: ["libs-continuation-1", "libs-merged"],
  currentBranch: "libs-merged",
  mergedBranches: [],
  satisfiedPackages: new Set(),
});
if (nonEmptyAlias.unexpectedBranches.join(",") !== "libs-continuation-1") {
  throw new Error("A non-empty out-of-plan continuation branch should remain visible for Supervisor adoption: " + JSON.stringify(nonEmptyAlias.unexpectedBranches));
}
if (migrationCompletionIssues(nonEmptyAlias).some((issue) => issue.includes("continuation-1"))) {
  throw new Error("A quarantined continuation ref must not become a user-facing completion blocker: " + JSON.stringify(migrationCompletionIssues(nonEmptyAlias)));
}
const historicalAdopted = adoptHistoricalContinuationBranches(
  generatedContinuationPlan,
  [{ ...generatedContinuationPlan, branches: [{ ...generatedContinuationPlan.branches[0], branch: "libs-continuation-1", scopeBranch: "libs-group-5" }] }],
  new Set(["libs-continuation-1"]),
);
if (historicalAdopted.branches[0].branch !== "libs-continuation-1" || historicalAdopted.branches[0].scopeBranch !== "libs-group-5") {
  throw new Error("A persisted non-merged continuation branch with the exact package scope was not adopted: " + JSON.stringify(historicalAdopted));
}

const scopePrompt = `${prompt}\n\n## Exact compact scope manifest\n\n\`\`\`json\n{"columns":["project","package","section","target","shouldUpdate","action"],"rows":[["Demo","a","dependencies","2.0.0",true,"update"],["Demo","b","dependencies","—",true,"remove"],["Demo","c","devDependencies","3.0.0",true,"update"]]}\n\`\`\``;
const satisfied = satisfiedScopePackagesFromPrompt(scopePrompt, "Demo", { dependencies: { a: "2.0.0" }, devDependencies: { c: "2.5.0" } });
const factChecked = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-merged", mergedBranches: ["libs-group-1", "libs-group-2"], satisfiedPackages: satisfied, factsRef: "libs-merged" });
if (factChecked.completedDependencies !== 2 || factChecked.unmetPackages.join(",") !== "c") throw new Error(`Merged branches hid unmet package targets: ${JSON.stringify(factChecked)}`);
if (!migrationCompletionIssues(factChecked).some((issue) => issue.includes("c"))) throw new Error(`Unmet scope package did not block completion: ${JSON.stringify(factChecked)}`);

// The gate's subject is the merged branch. Reading facts off whatever work
// branch happens to be checked out reported already-merged packages as unmet,
// which is exactly the false AGENT_GIT_PLAN_INCOMPLETE that stopped a run with
// three of five groups genuinely finished.
const mergedFacts = satisfiedScopePackagesFromPrompt(scopePrompt, "Demo", { dependencies: { a: "2.0.0" }, devDependencies: { c: "3.0.0" } });
const workBranchFacts = satisfiedScopePackagesFromPrompt(scopePrompt, "Demo", { dependencies: { a: "1.0.0" }, devDependencies: { c: "3.0.0" } });
const fromMerged = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-group-2", mergedBranches: ["libs-group-1", "libs-group-2"], satisfiedPackages: mergedFacts, factsRef: "libs-merged" });
if (fromMerged.unmetPackages.length !== 0) throw new Error(`Merged-branch facts must clear the gate: ${JSON.stringify(fromMerged.unmetPackages)}`);
if (migrationCompletionIssues(fromMerged).length !== 0) throw new Error(`A genuinely complete migration was rejected: ${JSON.stringify(migrationCompletionIssues(fromMerged))}`);

// Proof conformance is exact. A semver range or a higher version is not the
// assignment the package-manager verifier proved.
const rangedDrift = satisfiedScopePackagesFromPrompt(scopePrompt, "Demo", { dependencies: { a: "^2.0.0" }, devDependencies: { c: "3.0.0" } });
if (rangedDrift?.has("a")) throw new Error("A semver range must not satisfy an exact proven target");
const higherDrift = satisfiedScopePackagesFromPrompt(scopePrompt, "Demo", { dependencies: { a: "2.0.0" }, devDependencies: { c: "3.1.0" } });
if (higherDrift?.has("c")) throw new Error("A higher dependency version must not satisfy an exact proven target");
const fromWorkBranch = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-group-2", mergedBranches: ["libs-group-1", "libs-group-2"], satisfiedPackages: workBranchFacts, factsRef: "libs-merged" });
if (!fromWorkBranch.unmetPackages.includes("a")) throw new Error(`Fixture is wrong: the work branch must lack a merged target`);

// An untrustworthy reading must never be used to erase recorded progress.
const uncertain = buildMigrationProgress({ plan, refs: ["libs-group-1"], currentBranch: "libs-group-1", mergedBranches: [], trustworthy: false });
if (uncertain.trustworthy !== false) throw new Error(`Trust flag was lost: ${JSON.stringify(uncertain)}`);
if (buildMigrationProgress({ plan, refs: [], currentBranch: "", mergedBranches: [] }).trustworthy !== true) throw new Error(`Default reading must be trusted`);

const strayUntrackedOnly = countTrackedDirtyChanges("?? notes.local.txt\n?? .idea/dump.log\n");
if (strayUntrackedOnly !== 0) throw new Error(`Untracked files must not count as dirty: got ${strayUntrackedOnly}`);
const trackedAndUntracked = countTrackedDirtyChanges(" M package.json\nA  yarn.lock\n?? notes.local.txt\n");
if (trackedAndUntracked !== 2) throw new Error(`Tracked changes were not counted correctly: got ${trackedAndUntracked}`);

// IDE/OS noise is observational: it must disappear from every clean/dirty
// decision without being stashed, deleted or otherwise touching user files.
const noisyStatus = "?? .idea/\n?? .vs/Project/v17/.suo\n?? .vscode/settings.json\n?? .fleet/settings.json\n?? Thumbs.db\n?? scratch.txt\n M package.json\n";
const relevant = relevantGitStatusLines(noisyStatus);
if (relevant.join(",") !== "?? scratch.txt, M package.json") throw new Error(`IDE/OS status filtering failed: ${JSON.stringify(relevant)}`);
if (relevantGitStatus("?? .idea/\n?? .vs/\n?? swap.swp\n")) throw new Error('IDE-only status must be clean for every gate');
const strayPaths = strayUntrackedPaths(noisyStatus);
if (strayPaths.join(",") !== ".idea/,.vs/Project/v17/.suo,.vscode/settings.json,.fleet/settings.json,Thumbs.db") throw new Error(`Known IDE/OS paths were not isolated: ${JSON.stringify(strayPaths)}`);
if (strayUntrackedPaths(" M package.json\nA  yarn.lock\n").length !== 0) throw new Error("Tracked-only changes must yield no IDE/OS paths");
if (strayUntrackedPaths("").length !== 0) throw new Error("Empty status must yield no IDE/OS paths");

// Real incident: a .gitignore fix committed on deps-demo-group-1 never
// reached deps-demo-group-2, because group-2 was created from deps-demo (the
// shared base branch) directly, not from group-1. ensureBaseBranchIgnoresKnownArtifacts
// fixes baseBranch itself instead, once, so every future group branch
// inherits it -- missingGitignorePatterns is what decides whether that fix
// is even needed.
const noGitignoreYet = missingGitignorePatterns("", [".idea/", ".vs/"]);
if (noGitignoreYet.join(",") !== ".idea/,.vs/") throw new Error(`A project with no .gitignore must need every canonical pattern: ${JSON.stringify(noGitignoreYet)}`);
const partiallyCovered = missingGitignorePatterns("node_modules/\n.idea/\n", [".idea/", ".vs/", "*.swp"]);
if (partiallyCovered.join(",") !== ".vs/,*.swp") throw new Error(`Already-covered patterns must not be re-flagged as missing: ${JSON.stringify(partiallyCovered)}`);
const fullyCovered = missingGitignorePatterns(".idea/\n.vs/\n", [".idea/", ".vs/"]);
if (fullyCovered.length !== 0) throw new Error(`A .gitignore covering every pattern must report nothing missing: ${JSON.stringify(fullyCovered)}`);

// A group-scoped prompt is regenerated fresh from the live dashboard so its
// manifest hash is computed by the real code, not reimplemented -- but the
// branch plan the user actually reviewed and pinned is the already-saved
// prompt file. If the roadmap changed between those two moments (a fresh
// generate, an edited target), the orchestrator must refuse to hand the
// agent a silently drifted scope rather than trust the live regeneration.
const manifestFor = (target, shouldUpdate = true) => `## Exact compact scope manifest\n\n\`\`\`json\n{"columns":["project","package","target","shouldUpdate"],"rows":[["Demo","a","${target}",${shouldUpdate}],["Demo","b","2.0.0",true]]}\n\`\`\``;
const savedGroupPrompt = `${prompt}\n\n${manifestFor("2.0.0")}`;
const identicalFreshPrompt = `${prompt}\n\n${manifestFor("2.0.0")}`;
if (migrationGroupScopeDriftIssues(identicalFreshPrompt, savedGroupPrompt, "Demo", "libs-group-1").length !== 0) {
  throw new Error("An unchanged scope must not be reported as drifted");
}
const aliasSavedPlan = {
  ...plan,
  baseBranch: "libs-merged",
  branches: plan.branches.map((branch, index) => index === 0
    ? { ...branch, branch: "libs-continuation-1", scopeBranch: "libs-group-1", label: "continuation-1" }
    : branch),
};
const aliasSavedPrompt = replaceMigrationPlanInPrompt(savedGroupPrompt, "Demo", aliasSavedPlan);
if (!aliasSavedPrompt || migrationGroupScopeDriftIssues(identicalFreshPrompt, aliasSavedPrompt, "Demo", "libs-continuation-1", "libs-group-1").length !== 0) {
  throw new Error("A continuation Git alias with the same package targets must pass logical group drift validation");
}
const singleGroupPrompt = `## Branch plan\n\n\`\`\`json\n[{"project":"Demo","base":"libs-merged","merged":"libs-merged","branches":[{"order":1,"branch":"libs-group-1","packages":["a","b"]}]}]\n\`\`\``;
const operationalAliasPrompt = `Work only in libs-group-1.\n\n\`\`\`bash\ngit switch libs-group-1\n\`\`\`\n\n${singleGroupPrompt}\n\n## Immutable evidence\n\n\`\`\`json\n{"logicalBranch":"libs-group-1"}\n\`\`\``;
const reboundAliasPrompt = rebindMigrationPromptBranchIdentity(operationalAliasPrompt, "Demo", "libs-group-1", { ...aliasSavedPlan, branches: [aliasSavedPlan.branches[0]] });
if (!reboundAliasPrompt || !reboundAliasPrompt.includes("Work only in libs-continuation-1") || !reboundAliasPrompt.includes("git switch libs-continuation-1")) {
  throw new Error(`Operational continuation instructions were not rebound: ${reboundAliasPrompt}`);
}
if (!reboundAliasPrompt.includes('"scopeBranch": "libs-group-1"') || !reboundAliasPrompt.includes('{"logicalBranch":"libs-group-1"}')) {
  throw new Error(`Logical scope or immutable JSON was corrupted by branch rebinding: ${reboundAliasPrompt}`);
}
const driftedTargetPrompt = `${prompt}\n\n${manifestFor("3.0.0")}`;
const targetDrift = migrationGroupScopeDriftIssues(driftedTargetPrompt, savedGroupPrompt, "Demo", "libs-group-1");
if (!targetDrift.some((issue) => issue.includes("target") && issue.includes("2.0.0") && issue.includes("3.0.0"))) {
  throw new Error(`Changed target must be reported as drift: ${JSON.stringify(targetDrift)}`);
}
const droppedPackagePrompt = `# Migration\n\n## Branch plan\n\n\`\`\`json\n[{"project":"Demo","base":"libs","merged":"libs-merged","branches":[{"order":1,"branch":"libs-group-1","packages":["a"]},{"order":2,"branch":"libs-group-2","packages":["c"]}]}]\n\`\`\`\n\n${manifestFor("2.0.0")}`;
const packageDrift = migrationGroupScopeDriftIssues(droppedPackagePrompt, savedGroupPrompt, "Demo", "libs-group-1");
if (!packageDrift.some((issue) => issue.includes("набор пакетов"))) {
  throw new Error(`A changed package set must be reported as drift: ${JSON.stringify(packageDrift)}`);
}
const missingBranch = migrationGroupScopeDriftIssues(savedGroupPrompt, savedGroupPrompt, "Demo", "libs-group-9");
if (missingBranch.length === 0) throw new Error("A branch absent from both plans must still be reported, not silently pass");


// A generated execution-batch prompt is intentionally a strict subset of the
// full group prompt. The targets still have to match the reviewed full group
// manifest exactly; batching is a transport optimization, never a replanning
// opportunity.
const fullBatchManifest = `## Exact compact scope manifest

\`\`\`json
{"columns":["project","package","target","shouldUpdate"],"rows":[["Demo","a","2.0.0",true],["Demo","b","2.0.0",true]]}
\`\`\``;
const onePackageBatch = `## Exact compact scope manifest

\`\`\`json
{"columns":["project","package","target","shouldUpdate"],"rows":[["Demo","a","2.0.0",true]]}
\`\`\``;
if (migrationBatchScopeDriftIssues(onePackageBatch, fullBatchManifest, ["a"]).length !== 0) throw new Error("A faithful subset batch must not be reported as drifted");
const batchTargetDrift = `## Exact compact scope manifest

\`\`\`json
{"columns":["project","package","target","shouldUpdate"],"rows":[["Demo","a","3.0.0",true]]}
\`\`\``;
if (!migrationBatchScopeDriftIssues(batchTargetDrift, fullBatchManifest, ["a"]).some((issue) => issue.includes("target"))) throw new Error("A batch must never change the reviewed target");
if (!migrationBatchScopeDriftIssues(onePackageBatch, fullBatchManifest, ["a","b"]).some((issue) => issue.includes("execution batch"))) throw new Error("A dropped package inside a requested batch must be reported");

// The orchestrator loop's own "what's next" pick, in plan order.
const allMergedProgress = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-merged", mergedBranches: ["libs-group-1", "libs-group-2"], satisfiedPackages: new Set(["a", "b", "c"]), factsRef: "libs-merged" });
const dirtyReleaseAfterCompletedMigration = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-release", mergedBranches: ["libs-group-1", "libs-group-2"], satisfiedPackages: new Set(["a", "b", "c"]), dirtyChanges: 8, factsRef: "libs-merged" });
if (migrationCompletionIssues(dirtyReleaseAfterCompletedMigration).length !== 0) throw new Error(`Dirty release branch must not retroactively fail a completed migration: ${JSON.stringify(migrationCompletionIssues(dirtyReleaseAfterCompletedMigration))}`);
const dirtyMergedAfterMigration = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-merged", mergedBranches: ["libs-group-1", "libs-group-2"], satisfiedPackages: new Set(["a", "b", "c"]), dirtyChanges: 8, factsRef: "libs-merged" });
if (!migrationCompletionIssues(dirtyMergedAfterMigration).some((issue) => issue.includes("рабочем дереве ветки миграции"))) throw new Error("Dirty merged branch must still block migration completion");
if (nextIncompleteMigrationBranch(allMergedProgress) !== undefined) throw new Error("A fully merged plan must report no next branch");
if (nextIncompleteMigrationBranch(progress)?.branch !== "libs-group-2") throw new Error(`Wrong next branch picked: ${JSON.stringify(nextIncompleteMigrationBranch(progress))}`);

// Runtime activity is independent from Git topology: parallel worktrees are
// visible even though the canonical checkout points at merged. A branch that
// merely exists must not be called active, and a ready marker is not running.
const realtimeProgress = buildMigrationProgress({
  plan,
  refs: ["libs-group-1", "libs-group-2", "libs-merged"],
  currentBranch: "libs-merged",
  mergedBranches: ["libs-group-1"],
  satisfiedPackages: new Set(["a", "b"]),
  branchSatisfiedPackages: { "libs-group-1": new Set(["a", "b"]), "libs-group-2": new Set(["c"]) },
  runtimeByBranch: { "libs-group-2": { phase: "running", detail: "Batch 1/1", updatedAt: "2026-08-13T00:00:00.000Z" } },
  factsRef: "libs-merged",
});
if (realtimeProgress.activeBranches !== 1 || realtimeProgress.activeDependencies !== 1) throw new Error(`Parallel runtime was not counted: ${JSON.stringify(realtimeProgress)}`);
if (realtimeProgress.readyBranches !== 1 || realtimeProgress.readyDependencies !== 1) throw new Error(`Ready work was not separated from merged facts: ${JSON.stringify(realtimeProgress)}`);
if (realtimeProgress.completedBranches !== 1 || realtimeProgress.completedDependencies !== 2) throw new Error(`Merged facts were mixed with ready/runtime work: ${JSON.stringify(realtimeProgress)}`);
if (realtimeProgress.branches[1].runtime?.phase !== "running") throw new Error("Branch runtime phase was lost");
const nonActiveRuntime = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-group-2", "libs-merged"], currentBranch: "libs-merged", runtimeByBranch: { "libs-group-1": { phase: "queued", updatedAt: "2026-08-13T00:00:00.000Z" }, "libs-group-2": { phase: "failed", updatedAt: "2026-08-13T00:00:00.000Z" } }, factsRef: "libs-merged" });
if (nonActiveRuntime.activeBranches !== 0 || nonActiveRuntime.activeDependencies !== 0) throw new Error(`Queued/failed slots must not be reported as executing: ${JSON.stringify(nonActiveRuntime)}`);
const emptyMergedProgress = buildMigrationProgress({ plan, refs: ["libs-group-1", "libs-merged"], currentBranch: "libs-merged", mergedBranches: ["libs-group-1"], emptyBranches: ["libs-group-1"], factsRef: "libs-merged" });
if (emptyMergedProgress.branches[0].status === "merged") throw new Error("A branch pointing at the empty merged baseline must not be reported as completed");

const liveWorktreeProgress = buildMigrationProgress({
  plan,
  refs: ["libs-group-1", "libs-group-2", "libs-merged"],
  currentBranch: "libs-merged",
  mergedBranches: ["libs-group-1"],
  branchSatisfiedPackages: { "libs-group-2": new Set() },
  branchWorktreeSatisfiedPackages: { "libs-group-2": new Set(["c"]) },
  branchWorktreeDirtyChanges: { "libs-group-2": 2 },
  branchWorktreePaths: { "libs-group-2": "C:/Temp/dependency-flow-worktrees/job/libs-group-2" },
  factsRef: "libs-merged",
});
if (liveWorktreeProgress.branches[1].status !== "changes" || liveWorktreeProgress.branches[1].metPackages !== 1 || liveWorktreeProgress.branches[1].worktreeDirtyChanges !== 2) throw new Error(`Live worktree progress was hidden behind the branch ref: ${JSON.stringify(liveWorktreeProgress.branches[1])}`);
const partialWorktreeProgress = buildMigrationProgress({
  plan,
  refs: ["libs-group-1"],
  currentBranch: "libs-merged",
  mergedBranches: [],
  branchSatisfiedPackages: { "libs-group-1": new Set(["a"]) },
});
if (partialWorktreeProgress.branches[0].status !== "partial") throw new Error(`A partly satisfied branch must have its own status: ${JSON.stringify(partialWorktreeProgress.branches[0])}`);
const aliasPlan = { ...plan, branches: [{ ...plan.branches[0], branch: "libs-continuation-2", scopeBranch: "libs-group-1" }, plan.branches[1]] };
const adoptedLiveScope = adoptPreferredScopeBranches(aliasPlan, { "libs-continuation-2": 1, "libs-group-1": 2 });
if (adoptedLiveScope.branches[0].branch !== "libs-group-1") throw new Error(`Newer dirty scope worktree was not preferred over an older continuation alias: ${JSON.stringify(adoptedLiveScope)}`);
const keptContinuation = adoptPreferredScopeBranches(aliasPlan, { "libs-continuation-2": 2, "libs-group-1": 1 });
if (keptContinuation !== aliasPlan) throw new Error("A less complete scope worktree must not replace a better continuation alias");

// Real, confirmed-common merge shape: two branch-plan groups each bump a
// disjoint set of packages, textually conflicting in the same dependencies
// object even though nothing semantically overlaps.
const disjointMerge = threeWayMergeDependencyMaps(
  { a: "1.0.0", b: "2.0.0", c: "3.0.0" },
  { a: "1.5.0", b: "2.0.0", c: "3.0.0" }, // ours bumped a
  { a: "1.0.0", b: "2.0.0", c: "3.5.0" }, // theirs bumped c
);
if (!disjointMerge.ok || disjointMerge.merged.a !== "1.5.0" || disjointMerge.merged.c !== "3.5.0" || disjointMerge.merged.b !== "2.0.0") {
  throw new Error(`Disjoint dependency bumps must merge cleanly: ${JSON.stringify(disjointMerge)}`);
}
// A genuine conflict -- both sides bump the exact same package differently --
// must never be silently guessed at.
const realConflict = threeWayMergeDependencyMaps({ a: "1.0.0" }, { a: "1.5.0" }, { a: "1.6.0" });
if (realConflict.ok) throw new Error("Two different bumps of the same package must be reported as a real conflict, not merged");
// A package added by only one side (not present in base) must survive.
const additionMerge = threeWayMergeDependencyMaps({ a: "1.0.0" }, { a: "1.0.0", b: "1.0.0" }, { a: "1.0.0" });
if (!additionMerge.ok || additionMerge.merged.b !== "1.0.0") throw new Error(`A package added by only one side must be kept: ${JSON.stringify(additionMerge)}`);
// A package removed by only one side (e.g. @types/uuid dropped when
// upgrading to uuid@9, which ships its own types) must stay removed.
const removalMerge = threeWayMergeDependencyMaps({ a: "1.0.0", b: "1.0.0" }, { a: "1.0.0" }, { a: "1.0.0", b: "1.5.0" });
if (removalMerge.ok) throw new Error("Removing a package on one side while the other side bumps it is a real conflict, not mergeable");

const basePackageJson = JSON.stringify({ name: "demo", scripts: { build: "vite build" }, dependencies: { a: "1.0.0", b: "2.0.0" }, devDependencies: { husky: "4.3.8" } });
const oursPackageJson = JSON.stringify({ name: "demo", scripts: { build: "vite build" }, dependencies: { a: "1.5.0", b: "2.0.0" }, devDependencies: { husky: "4.3.8" } });
const theirsPackageJson = JSON.stringify({ name: "demo", scripts: { build: "vite build" }, dependencies: { a: "1.0.0", b: "2.5.0" }, devDependencies: { husky: "9.1.0" } });
const mergedPackageJson = mergePackageJsonThreeWay(basePackageJson, oursPackageJson, theirsPackageJson);
if (!mergedPackageJson) throw new Error("package.json with disjoint dependency/devDependency bumps must merge automatically");
const mergedParsed = JSON.parse(mergedPackageJson);
if (mergedParsed.dependencies.a !== "1.5.0" || mergedParsed.dependencies.b !== "2.5.0" || mergedParsed.devDependencies.husky !== "9.1.0") {
  throw new Error(`package.json merge dropped a disjoint bump: ${mergedPackageJson}`);
}
if (mergedParsed.scripts.build !== "vite build") throw new Error("Fields outside dependency maps must be preserved verbatim");

// A conflict on the exact same package must not be silently resolved.
const conflictingPackageJson = mergePackageJsonThreeWay(basePackageJson, oursPackageJson, JSON.stringify({ ...JSON.parse(oursPackageJson), dependencies: { a: "1.9.0", b: "2.0.0" } }));
if (conflictingPackageJson) throw new Error("A same-package conflict inside package.json must not be auto-merged");

// A non-dependency change on either side (e.g. a script edit) is outside
// what this function is allowed to resolve -- must bail out, not silently
// drop one side's change.
const scriptsDivergedTheirs = JSON.stringify({ name: "demo", scripts: { build: "vite build --base=/app/" }, dependencies: { a: "1.0.0", b: "2.5.0" }, devDependencies: { husky: "4.3.8" } });
if (mergePackageJsonThreeWay(basePackageJson, oursPackageJson, scriptsDivergedTheirs)) {
  throw new Error("A change outside the dependency fields must not be silently discarded");
}

// Verified against real `git diff --cached --check` output: it exits non-zero
// for any whitespace complaint, and "trailing whitespace" is exactly what a
// Markdown hard line break (two trailing spaces) produces -- which this
// migration's own doc shards legitimately contain. Gating the merge on the
// exit code rejected a correctly resolved merge over the tool's own
// formatting, so only genuine conflict-marker lines may block it.
const realDiffCheckOutput = [
  "conflicted.md:2: leftover conflict marker",
  "conflicted.md:4: leftover conflict marker",
  "docs.md:1: trailing whitespace.",
  "+line one  ",
].join("\n");
const markers = leftoverConflictMarkerLines(realDiffCheckOutput);
if (markers.length !== 2) throw new Error(`Conflict markers were not isolated: ${JSON.stringify(markers)}`);
if (markers.some((line) => line.includes("trailing whitespace"))) throw new Error(`Whitespace complaints must not block a merge: ${JSON.stringify(markers)}`);
if (leftoverConflictMarkerLines("docs.md:1: trailing whitespace.\n+line one  ").length !== 0) {
  throw new Error("A merge whose only complaint is trailing whitespace must be allowed to commit");
}
if (leftoverConflictMarkerLines("").length !== 0) throw new Error("Clean diff --check output must report no markers");

console.log("Migration branch progress OK");
// The prompt must carry the exact Baseline ProofEnvelope across the
// Python -> Dashboard -> Electron boundary. A local scope is only a subset.
const proofEnvelopePayload = {
  schemaVersion: 2,
  proofSchema: 'baseline-proof-v3',
  project: 'Demo',
  mode: 'yellow',
  sourceHead: 'abc123',
  sourceSnapshotKey: 'source-key',
  assignmentKey: 'assignment-key',
  resolverInputKey: 'resolver-key',
  preparationProofKey: 'preparation-key',
  projectProofKey: 'project-key',
  observedResolvedHash: '0'.repeat(64),
  exactDirectAssignment: { a: '2.0.0', '@types/a': '1.0.0' },
  removals: ['@types/a'],
  verificationCommands: ['yarn lint:types'],
  projectChecks: 'adaptive',
  resolverProofStatus: 'passed',
  preparationProofStatus: 'passed',
  projectProofStatus: 'diagnostic-red',
};
const proofEnvelope = {
  ...proofEnvelopePayload,
  envelopeKey: proofEnvelopeContentKey(proofEnvelopePayload),
};
const proofManifest = {
  schemaVersion: 2,
  targetMode: 'yellow',
  proofEnvelopes: { Demo: proofEnvelope },
  columns: ['project','package','section','current','target','shouldUpdate','action'],
  rows: [
    ['Demo','a','dependencies','1.0.0','2.0.0',true,'update'],
    ['Demo','@types/a','devDependencies','1.0.0','1.0.0',true,'remove'],
  ],
};
const proofPrompt = `## Exact compact scope manifest\n\n\`\`\`json\n${JSON.stringify(proofManifest)}\n\`\`\``;
let proofValidation = validateScopeProofEnvelope(proofPrompt, 'Demo');
if (!proofValidation.ok) throw new Error(`Valid ProofEnvelope rejected: ${proofValidation.reason}`);

const targetDriftPrompt = proofPrompt.replace('"2.0.0",true,"update"', '"2.1.0",true,"update"');
proofValidation = validateScopeProofEnvelope(targetDriftPrompt, 'Demo');
if (proofValidation.ok || !proofValidation.reason.includes('assignment mismatch')) throw new Error(`Prompt target drift must fail ProofEnvelope validation: ${proofValidation.reason}`);

const tamperedEnvelope = {
  ...proofEnvelope,
  exactDirectAssignment: { ...proofEnvelope.exactDirectAssignment, a: '2.1.0' },
};
const envelopeDriftPrompt = proofPrompt.replace(JSON.stringify(proofEnvelope), JSON.stringify(tamperedEnvelope));
proofValidation = validateScopeProofEnvelope(envelopeDriftPrompt, 'Demo');
if (proofValidation.ok || !proofValidation.reason.includes('content hash')) throw new Error(`Envelope content drift must fail hash validation: ${proofValidation.reason}`);
