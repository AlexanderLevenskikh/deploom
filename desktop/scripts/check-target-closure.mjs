import { scopeExpansionCoverage, shouldUseSupervisorSeed, splitLagBlockers, targetClosureFromRoadmap, targetClosureFromRoadmapWithTargets, targetClosureMessage } from "../dist-electron/target-closure.js";

const red = targetClosureFromRoadmap({
  project_health: { Demo: { status: "red", lag_ok_pct: 57.9, lag_ok_12m: 44, total: 76 } },
  projects: { Demo: [{ name: "missing-a", target_yellow: "2.0.0" }, { name: "excluded", target_yellow: "3.0.0", scope_excluded: true }] },
}, "Demo", "yellow");
if (red.reached || red.remainingPackages.join(",") !== "missing-a") throw new Error(`Red target was accepted: ${JSON.stringify(red)}`);
if (!targetClosureMessage(red).includes("57.9%") || !targetClosureMessage(red).includes("missing-a")) throw new Error(`Closure message is incomplete: ${targetClosureMessage(red)}`);

const yellow = targetClosureFromRoadmap({ project_health: { Demo: { status: "yellow", lag_ok_pct: 80 } }, projects: { Demo: [] } }, "Demo", "yellow");
if (!yellow.reached) throw new Error(`Yellow target was rejected: ${JSON.stringify(yellow)}`);
const green = targetClosureFromRoadmap({ project_health: { Demo: { status: "yellow", lag_ok_pct: 100 } }, projects: { Demo: [] } }, "Demo", "green");
if (green.reached) throw new Error(`Yellow status was accepted as green: ${JSON.stringify(green)}`);
// Modelled on the real checkout-form case that motivated this: 79.2% with one
// package the plan can still fix (jsdom) and several group-4 platform packages
// it cannot. The message must name both sides and say how many are missing,
// instead of leaving the user to guess what "77.8%" is about.
const blocked = targetClosureFromRoadmap({
  project_health: {
    Demo: {
      status: "red", lag_ok_pct: 79.17, lag_ok_12m: 57, total: 72,
      lag_needed_for_yellow: 1, critical: 0, high: 2, excluded: 2,
      lag_blockers: [
        { package: "jsdom", current: "19.0.0", required: "27.0.0", plannedTarget: "27.1.0", group: 5 },
        { package: "react", current: "18.2.0", required: "19.0.1", plannedTarget: "", group: 4 },
        { package: "typescript", current: "5.5.4", required: "5.9.2", plannedTarget: "", group: 4 },
      ],
    },
  },
  projects: { Demo: [{ name: "jsdom", target_yellow: "27.1.0" }] },
}, "Demo", "yellow");
if (blocked.lagBlockers.length !== 3) throw new Error(`Lag blockers were not parsed: ${JSON.stringify(blocked.lagBlockers)}`);
if (blocked.neededForYellow !== 1) throw new Error(`neededForYellow was not carried: ${JSON.stringify(blocked)}`);
const split = splitLagBlockers(blocked);
if (split.fixable.length !== 1 || split.fixable[0].package !== "jsdom") throw new Error(`Fixable blockers are wrong: ${JSON.stringify(split.fixable)}`);
if (split.stuck.length !== 2) throw new Error(`Stuck blockers are wrong: ${JSON.stringify(split.stuck)}`);
const blockedMessage = targetClosureMessage(blocked);
for (const fragment of ["не хватает 1", "jsdom", "react", "нет target", "выгрузите свежий prompt"]) {
  if (!blockedMessage.includes(fragment)) throw new Error(`Blocked-goal message must explain "${fragment}": ${blockedMessage}`);
}

// Nothing the plan can still do: the advice must switch from "run the agent
// again" to "make a scope decision", or the user loops forever on a plan that
// provably cannot reach the threshold.
const exhausted = targetClosureFromRoadmap({
  project_health: {
    Demo: {
      status: "red", lag_ok_pct: 77.8, lag_ok_12m: 56, total: 72, lag_needed_for_yellow: 2,
      lag_blockers: [{ package: "react", current: "18.2.0", required: "19.0.1", plannedTarget: "", group: 4 }],
    },
  },
  projects: { Demo: [] },
}, "Demo", "yellow");
const exhaustedMessage = targetClosureMessage(exhausted);
if (!exhaustedMessage.includes("закрыть цель текущим планом нельзя")) throw new Error(`Exhausted plan must not advise re-running the agent: ${exhaustedMessage}`);

// A real target-capacity regression: 52/73 is 71.2%; one successful planned
// lag fix can only produce 53/73 = 72.6%, not Yellow. Autopilot must be able
// to reject its "to result" promise before spending an agent session.
const insufficient = targetClosureFromRoadmap({
  project_health: {
    Demo: {
      status: "red", lag_ok_pct: 71.2, lag_ok_12m: 52, total: 73, lag_needed_for_yellow: 7, critical: 1,
      lag_blockers: [
        { package: "postcss-scss", plannedTarget: "4.0.7" },
        { package: "react", plannedTarget: "" },
      ],
    },
  },
  projects: { Demo: [{ name: "postcss-scss", target_yellow: "4.0.7" }] },
}, "Demo", "yellow");
if (insufficient.planCanReachYellow !== false || insufficient.neededBeyondCurrentPlan !== 6) throw new Error(`Insufficient plan capacity was missed: ${JSON.stringify(insufficient)}`);
if (Math.abs((insufficient.maxLagOkPctAfterPlan ?? 0) - 72.6027) > 0.01) throw new Error(`Wrong maximum health after plan: ${JSON.stringify(insufficient)}`);
if (!targetClosureMessage(insufficient).includes("максимум 72.6%") || !targetClosureMessage(insufficient).includes("всё ещё не хватает 6")) throw new Error(`Capacity warning is incomplete: ${targetClosureMessage(insufficient)}`);

const staleRoadmap = {
  project_health: {
    Demo: {
      status: 'red', lag_ok_pct: 72.6, lag_ok_12m: 53, total: 73, lag_needed_for_yellow: 6, critical: 1, yellow_projected_lag_ok: 53,
      lag_blockers: [
        { package: 'vitest', current: '0.30.1', required: '3.2.5', plannedTargetYellow: '' },
        { package: 'vite', current: '4.3.9', required: '5.4.20', plannedTargetYellow: '' },
        ...Array.from({ length: 5 }, (_, index) => ({ package: `candidate-${index + 1}`, current: '1.0.0', required: '2.0.0', plannedTargetYellow: '' })),
      ],
    },
  },
  projects: { Demo: [
    { name: 'vitest', current_vulns: 'C:1', min_no_critical: '3.2.6', target_yellow: '—' },
    { name: 'vite', target_yellow: '—' },
    ...Array.from({ length: 5 }, (_, index) => ({ name: `candidate-${index + 1}`, target_yellow: '—' })),
  ] },
};
const promptAware = targetClosureFromRoadmapWithTargets(staleRoadmap, 'Demo', 'yellow', {
  vitest: '3.2.6', vite: '6.4.2',
  ...Object.fromEntries(Array.from({ length: 5 }, (_, index) => [`candidate-${index + 1}`, '2.0.0'])),
});
if (promptAware.planCanReachYellow !== true || promptAware.neededBeyondCurrentPlan !== 0 || promptAware.uncoveredCriticalPackages?.length || promptAware.remainingPackages.length !== 7) {
  throw new Error(`Saved prompt targets must override stale roadmap capacity: ${JSON.stringify(promptAware)}`);
}

// Prefer the generator's exact post-compatibility projection over counting
// targets. A companion/security target may be actionable without reaching its
// own lag boundary; counting it used to create false Yellow capacity and
// repeated Supervisor cycles.
const exactProjection = targetClosureFromRoadmap({
  project_health: {
    Demo: {
      status: "red", lag_ok_pct: 70, lag_ok_12m: 7, total: 10, lag_needed_for_yellow: 1,
      yellow_projected_lag_ok: 7,
      lag_blockers: [{ package: "companion", current: "1.0.0", required: "3.0.0", plannedTargetYellow: "2.0.0" }],
    },
  },
  projects: { Demo: [{ name: "companion", target_yellow: "2.0.0" }] },
}, "Demo", "yellow");
if (exactProjection.maxLagOkPctAfterPlan !== 70 || exactProjection.neededBeyondCurrentPlan !== 1) throw new Error(`Exact generator projection was ignored: ${JSON.stringify(exactProjection)}`);

if (!shouldUseSupervisorSeed(insufficient, "yellow", true)) throw new Error("A valid prior plan must seed goal-seeking when the fresh executable plan is mathematically insufficient");
if (shouldUseSupervisorSeed(insufficient, "yellow", false)) throw new Error("Goal-seeking must not invent a seed when no valid prior Branch plan exists");
if (shouldUseSupervisorSeed(yellow, "yellow", true)) throw new Error("A reached goal must never reuse a stale Supervisor seed");
// Critical is an independent Yellow gate. A plan is sufficient only when its
// exact target reaches min_no_critical for every currently Critical package.
const criticalCovered = targetClosureFromRoadmap({
  project_health: { Demo: { status: "red", lag_ok_pct: 80, lag_ok_12m: 8, total: 10, lag_needed_for_yellow: 0, critical: 1 } },
  projects: { Demo: [{ name: "vitest", current_vulns: "C:1", min_no_critical: "3.2.5", target_yellow: "3.2.6" }] },
}, "Demo", "yellow");
if (criticalCovered.planCanReachYellow !== true || criticalCovered.uncoveredCriticalPackages?.length) throw new Error(`Covered Critical target rejected: ${JSON.stringify(criticalCovered)}`);
const criticalUncovered = targetClosureFromRoadmap({
  project_health: { Demo: { status: "red", lag_ok_pct: 80, lag_ok_12m: 8, total: 10, lag_needed_for_yellow: 0, critical: 1 } },
  projects: { Demo: [{ name: "vitest", current_vulns: "C:1", min_no_critical: "3.2.5", target_yellow: "—" }] },
}, "Demo", "yellow");
if (criticalUncovered.planCanReachYellow !== false || criticalUncovered.uncoveredCriticalPackages?.[0] !== "vitest") throw new Error(`Uncovered Critical target accepted: ${JSON.stringify(criticalUncovered)}`);
if (!targetClosureMessage(criticalUncovered).includes("не устраняет Critical: vitest")) throw new Error(`Critical gap missing from message: ${targetClosureMessage(criticalUncovered)}`);

// The planned target must follow the goal being pursued, not the generator's
// default mode: a row default mode skipped but the yellow planner picked up is
// still closable by re-running the agent, and mislabelling it as "no target"
// would send the user to change scope instead of finishing the migration.
const modeRoadmap = {
  project_health: {
    Demo: {
      status: "red", lag_ok_pct: 70, lag_ok_12m: 7, total: 10, lag_needed_for_yellow: 1,
      lag_blockers: [{ package: "only-yellow", current: "1.0.0", required: "2.0.0", plannedTarget: "", plannedTargetYellow: "2.1.0", plannedTargetGreen: "" }],
    },
  },
  projects: { Demo: [] },
};
const yellowMode = targetClosureFromRoadmap(modeRoadmap, "Demo", "yellow");
if (splitLagBlockers(yellowMode).fixable.length !== 1) throw new Error(`Yellow-mode target was ignored: ${JSON.stringify(yellowMode.lagBlockers)}`);
if (yellowMode.lagBlockers[0].plannedTarget !== "2.1.0") throw new Error(`Wrong planned target for yellow: ${JSON.stringify(yellowMode.lagBlockers)}`);
const greenMode = targetClosureFromRoadmap(modeRoadmap, "Demo", "green");
if (splitLagBlockers(greenMode).stuck.length !== 1) throw new Error(`Green mode must not borrow the yellow target: ${JSON.stringify(greenMode.lagBlockers)}`);

// Older reports only carry the default-mode field; it must still be honoured.
const legacyMode = targetClosureFromRoadmap({
  project_health: { Demo: { status: "red", lag_blockers: [{ package: "legacy", plannedTarget: "3.0.0" }] } },
  projects: { Demo: [] },
}, "Demo", "yellow");
if (legacyMode.lagBlockers[0].plannedTarget !== "3.0.0") throw new Error(`Legacy plannedTarget was dropped: ${JSON.stringify(legacyMode.lagBlockers)}`);

const bestEffort = targetClosureFromRoadmap({
  project_health: { Demo: { status: "red", critical: 0, lag_blockers: [{ package: "vite", plannedTarget: "" }] } },
  projects: { Demo: [{ name: "vite", target_yellow: "—" }] },
}, "Demo", "yellow");
if (!bestEffort.bestEffortReleaseEligible || !bestEffort.bestEffortReason?.includes("Critical=0")) throw new Error(`Exhausted safe plan must allow best-effort release: ${JSON.stringify(bestEffort)}`);
const criticalBestEffort = targetClosureFromRoadmap({
  project_health: { Demo: { status: "red", critical: 1, lag_blockers: [{ package: "danger", plannedTarget: "" }] } },
  projects: { Demo: [] },
}, "Demo", "yellow");
if (criticalBestEffort.bestEffortReleaseEligible) throw new Error(`Critical vulnerabilities must block best-effort release: ${JSON.stringify(criticalBestEffort)}`);


const expansionClosure = {
  ...insufficient,
  neededBeyondCurrentPlan: 6,
  lagBlockers: [
    { package: "vite", required: "5.4.20" },
    { package: "vitest", required: "3.2.5" },
    { package: "@vitejs/plugin-react", required: "5.0.0" },
    { package: "@vitejs/plugin-basic-ssl", required: "2.1.0" },
    { package: "eslint", required: "9.33.0" },
    { package: "stylelint", required: "16.23.0" },
  ],
};
const insufficientCoverage = scopeExpansionCoverage(expansionClosure, [
  { package: "vite", target: "5.4.20" },
  { package: "vitest", target: "3.2.6" },
  { package: "@vitejs/plugin-react", target: "5.1.1" },
  { package: "@vitejs/plugin-basic-ssl", target: "1.1.0" },
]);
if (insufficientCoverage.covered !== 3 || insufficientCoverage.required !== 6) {
  throw new Error("Vitest cohort must not be mistaken for a complete Yellow plan: " + JSON.stringify(insufficientCoverage));
}
const sufficientCoverage = scopeExpansionCoverage(expansionClosure, [
  { package: "vite", target: "5.4.20" },
  { package: "vitest", target: "3.2.6" },
  { package: "@vitejs/plugin-react", target: "5.1.1" },
  { package: "@vitejs/plugin-basic-ssl", target: "2.1.0" },
  { package: "eslint", target: "9.39.0" },
  { package: "stylelint", target: "16.26.0" },
]);
if (sufficientCoverage.covered !== 6) throw new Error("Complete goal-closing proposal must cover all six lag rows");
console.log("Target closure gate OK");
