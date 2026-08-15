import { teamStatePaths } from "../dist-electron/state-commit.js";

const paths = teamStatePaths(".dependency-roadmap/settings.project.json", {
  groupsConfig: ".team/groups.json",
  historyDir: ".team/history",
  dashboardState: ".team/dashboard.json",
});
for (const expected of [".dependency-roadmap/desktop/flow-state.json", ".dependency-roadmap/settings.project.json", ".team/groups.json", ".team/history", ".team/dashboard.json", "knowledge"]) {
  if (!paths.includes(expected)) throw new Error(`Missing team state path: ${expected}`);
}
if (paths.some((value) => value.includes("desktop/downloads"))) throw new Error(`Local downloads leaked into state paths: ${paths}`);
if (paths.length !== new Set(paths).size) throw new Error(`Duplicate state paths: ${paths}`);
console.log("Team state path selection OK");