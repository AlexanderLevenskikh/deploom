import { applyBranchBase, preferNewestProjectLevels, projectLevelsFromHistorySnapshots, projectLevelsFromRoadmap } from "../dist-electron/project-settings.js";

const original = {
  name: "Demo",
  git: {
    sourceBranch: "master",
    baseBranch: "old-libs",
    branchPrefix: "old-libs",
    mergedBranch: "old-libs-merged",
    push: false,
  },
};
const updated = applyBranchBase(original, "deps-demo", true);

if (updated.git.sourceBranch !== "master") throw new Error("sourceBranch must remain unchanged");
if (updated.git.baseBranch !== "deps-demo") throw new Error("baseBranch was not updated");
if (updated.git.branchPrefix !== "deps-demo") throw new Error("branchPrefix was not updated");
if (updated.git.mergedBranch !== "deps-demo-merged") throw new Error("mergedBranch was not derived");
if (updated.git.push !== true) throw new Error("push flag was not updated");
if (original.git.baseBranch !== "old-libs") throw new Error("project settings were mutated in place");

const levels = projectLevelsFromRoadmap({
  project_health: {
    Demo: { status: "yellow", lag_ok_pct: 78.2 },
    Ignored: { status: "unknown", lag_ok_pct: 100 },
  },
  projects: { Demo: [{ name: "done", target_yellow: "—" }, { name: "remaining", target_yellow: "2.0.0", target_green: "3.0.0" }] },
}, '2026-08-13T12:00:00.000Z');
if (levels.Demo?.status !== "yellow" || levels.Demo?.lagOkPct !== 78.2 || levels.Demo?.remainingYellow !== 1 || levels.Demo?.remainingGreen !== 1) {
  throw new Error("Current project level was not parsed");
}
if (levels.Ignored) throw new Error("Unknown project statuses must be ignored");
const historicalLevels = projectLevelsFromHistorySnapshots([
  { capturedAt: '2026-08-13T13:00:00.000Z', projects: { Demo: { health: { status: "yellow", lag_ok_pct: 78.2 } } } },
  { capturedAt: '2026-08-12T13:00:00.000Z', projects: { Demo: { health: { status: "red", lag_ok_pct: 12 } }, Legacy: { health: { status: "green", lag_ok_pct: 100 } } } },
]);
if (historicalLevels.Demo?.status !== "yellow" || historicalLevels.Legacy?.status !== "green") throw new Error(`Latest historical levels were not preserved: ${JSON.stringify(historicalLevels)}`);
const newest = preferNewestProjectLevels(historicalLevels, levels);
if (newest.Demo.status !== 'yellow' || newest.Demo.measuredAt !== '2026-08-13T13:00:00.000Z') throw new Error(`Newer history measurement must beat stale roadmap: ${JSON.stringify(newest)}`);
const refreshed = preferNewestProjectLevels(historicalLevels, projectLevelsFromRoadmap({ project_health: { Demo: { status: 'green', lag_ok_pct: 100 } } }, '2026-08-13T14:00:00.000Z'));
if (refreshed.Demo.status !== 'green') throw new Error(`Fresh roadmap must replace history: ${JSON.stringify(refreshed)}`);

console.log("Project branch settings and current level parsing OK");
