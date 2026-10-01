import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  iterativeBeginInvocation,
  targetsFromDashboardRows,
  targetsFromDashboardState,
} from "../dist-electron/iterative-begin.js";

// 1. Target extraction mirrors the product's own dashboard fallback: an
// explicit lagPolicyTarget, then lag_target, then a concrete min_lag_<N>m
// boundary for the row's lag threshold, then min_lag_12m. Markers and
// excluded rows are never planned toward.
const rows = [
  { package: "a", current: "1.0.0", lagPolicyTarget: "7.0.0" },
  { package: "b", current: "1.0.0", lag_target: "2.0.0" },
  { package: "c", current: "1.0.0", lagThresholdMonths: 3, min_lag_3m: "3.1.0", min_lag_12m: "4.0.0" },
  { package: "d", current: "1.0.0", min_lag_12m: "5.0.0" },
  { package: "e", current: "1.0.0", lagPolicyTarget: "latest" },
  { package: "f", current: "1.0.0", lagPolicyTarget: "—" },
  { package: "g", current: "1.0.0", lagPolicyTarget: "2.0.0", scopeExcluded: true },
  { package: "h", current: "1.0.0" },
  "not-an-object",
  { name: "i", lagPolicyTarget: "9.0.0" },
];
const targets = targetsFromDashboardRows(rows);
const expected = { a: "7.0.0", b: "2.0.0", c: "3.1.0", d: "5.0.0", i: "9.0.0" };
if (JSON.stringify(targets) !== JSON.stringify(expected)) {
  throw new Error(`Target extraction mismatch: ${JSON.stringify(targets)}`);
}

// 2. Dashboard-state file round-trip, keyed by project.
const root = mkdtempSync(join(tmpdir(), "iter-begin-contract-"));
const statePath = join(root, "dashboard-state.json");
writeFileSync(
  statePath,
  JSON.stringify({
    schemaVersion: 1,
    packageOverrides: {},
    projects: {
      DemoApp: [
        { package: "is-number", current: "6.0.0", lagPolicyTarget: "7.0.0" },
        { package: "is-finite", current: "1.0.0", min_lag_12m: "1.1.0" },
        { package: "deferred", current: "1.2.0", lagPolicyTarget: "2.0.0", scopeExcluded: true },
      ],
    },
  }),
  "utf8",
);
const projectTargets = targetsFromDashboardState(statePath, "DemoApp");
if (JSON.stringify(projectTargets) !== JSON.stringify({ "is-number": "7.0.0", "is-finite": "1.1.0" })) {
  throw new Error(`Dashboard-state targets mismatch: ${JSON.stringify(projectTargets)}`);
}
if (JSON.stringify(targetsFromDashboardState(statePath, "OtherApp")) !== "{}") {
  throw new Error("Unknown project must yield no targets");
}
if (JSON.stringify(targetsFromDashboardState(join(root, "missing.json"), "DemoApp")) !== "{}") {
  throw new Error("Missing dashboard-state must yield no targets");
}

// 3. Invocation shape: --run-dir is a TOP-LEVEL CLI argument (before 'begin'),
// project-dir/target-level are always present, targets-file only when set.
const runDir = join(root, "run");
const withTargets = iterativeBeginInvocation(
  runDir,
  { projectDir: "C:/ws/demo", projectName: "DemoApp", targetLevel: "yellow", targetsFile: "C:/tmp/t.json", workspaceId: "ws-1", projectId: "demo" },
  "iterative_migration.py",
  "python",
);
if (withTargets.command !== "python") throw new Error("Begin command malformed");
if (withTargets.args[1] !== "--run-dir" || withTargets.args[2] !== runDir) {
  throw new Error(`--run-dir must precede the subcommand: ${JSON.stringify(withTargets)}`);
}
if (withTargets.args[3] !== "begin") throw new Error(`begin must follow --run-dir: ${JSON.stringify(withTargets)}`);
if (!withTargets.args.includes("--project-dir") || !withTargets.args.includes("C:/ws/demo")) {
  throw new Error("project-dir missing");
}
if (!withTargets.args.includes("--target-level") || !withTargets.args.includes("yellow")) {
  throw new Error("target-level missing");
}
if (!withTargets.args.includes("--targets-file") || !withTargets.args.includes("C:/tmp/t.json")) {
  throw new Error("targets-file must be passed when set");
}
if (!withTargets.args.includes("--workspace-id") || !withTargets.args.includes("ws-1")) {
  throw new Error("workspace-id missing");
}
const withoutTargets = iterativeBeginInvocation(
  runDir,
  { projectDir: "C:/ws/demo", projectName: "DemoApp", targetLevel: "green" },
  "iterative_migration.py",
  "python",
);
if (withoutTargets.args.includes("--targets-file")) {
  throw new Error("targets-file must be omitted when not set");
}
if (!withoutTargets.args.includes("green")) throw new Error("target level lost");
if (withoutTargets.args.some((arg) => /^--run-id=/.test(arg))) {
  throw new Error("--run-id must not be forced by the Desktop (Python owns run identity)");
}

// 3b. R8: the independent audit needs the ACTUAL user policy — dashboard-state
// path and the goal audit thresholds are forwarded when the Desktop has them,
// and never invented when it does not.
const withPolicy = iterativeBeginInvocation(
  runDir,
  {
    projectDir: "C:/ws/demo",
    projectName: "DemoApp",
    targetLevel: "yellow",
    dashboardStatePath: "C:/ws/.dependency-roadmap/state/dashboard-state.json",
    auditPolicy: { lagPolicyMonths: 6, minLagOkPct: 90, maxKnownHigh: 2 },
  },
  "iterative_migration.py",
  "python",
);
for (const pair of [
  ["--dashboard-state", "C:/ws/.dependency-roadmap/state/dashboard-state.json"],
  ["--lag-months", "6"],
  ["--min-lag-ok-pct", "90"],
  ["--max-known-high", "2"],
]) {
  if (!withPolicy.args.includes(pair[0]) || !withPolicy.args.includes(pair[1])) {
    throw new Error(`Audit policy arg ${pair[0]} must be forwarded when set`);
  }
}
const withoutPolicy = iterativeBeginInvocation(
  runDir,
  { projectDir: "C:/ws/demo", projectName: "DemoApp", targetLevel: "yellow" },
  "iterative_migration.py",
  "python",
);
for (const flag of ["--dashboard-state", "--lag-months", "--min-lag-ok-pct", "--max-known-high"]) {
  if (withoutPolicy.args.includes(flag)) {
    throw new Error(`Audit policy arg ${flag} must be omitted when the Desktop has no policy`);
  }
}

// 3c. D3: an explicit Node.js for project/CI is forwarded as --requested-node;
// an unset one must produce NO runtime claim (no flag, no invented value).
const withNode = iterativeBeginInvocation(
  runDir,
  { projectDir: "C:/ws/demo", projectName: "DemoApp", targetLevel: "yellow", requestedNode: "22" },
  "iterative_migration.py",
  "python",
);
if (!withNode.args.includes("--requested-node") || !withNode.args.includes("22")) {
  throw new Error("--requested-node must be forwarded when the project has an explicit Node.js");
}
const withExactNode = iterativeBeginInvocation(
  runDir,
  { projectDir: "C:/ws/demo", projectName: "DemoApp", targetLevel: "yellow", requestedNode: "22.18.0" },
  "iterative_migration.py",
  "python",
);
if (!withExactNode.args.includes("22.18.0")) {
  throw new Error("exact --requested-node version must be forwarded verbatim");
}
const withoutNode = iterativeBeginInvocation(
  runDir,
  { projectDir: "C:/ws/demo", projectName: "DemoApp", targetLevel: "yellow" },
  "iterative_migration.py",
  "python",
);
if (withoutNode.args.includes("--requested-node")) {
  throw new Error("--requested-node must be omitted when the project has no explicit Node.js");
}

// 4. P1 (#1): --adopt-repair-source is forwarded ONLY when the migration must
// continue from a just-archived verified repair — never invented by the builder.
const withAdopt = iterativeBeginInvocation(
  runDir,
  { projectDir: "C:/ws/demo", projectName: "DemoApp", targetLevel: "yellow", adoptRepairSource: true },
  "iterative_migration.py",
  "python",
);
if (!withAdopt.args.includes("--adopt-repair-source")) {
  throw new Error("--adopt-repair-source must be forwarded when the repair was just archived");
}
const withoutAdopt = iterativeBeginInvocation(
  runDir,
  { projectDir: "C:/ws/demo", projectName: "DemoApp", targetLevel: "yellow" },
  "iterative_migration.py",
  "python",
);
if (withoutAdopt.args.includes("--adopt-repair-source")) {
  throw new Error("--adopt-repair-source must be omitted when no finished repair was archived");
}
const adoptedWithNode = iterativeBeginInvocation(
  runDir,
  { projectDir: "C:/ws/demo", projectName: "DemoApp", targetLevel: "yellow", requestedNode: "22", adoptRepairSource: true },
  "iterative_migration.py",
  "python",
);
if (!adoptedWithNode.args.includes("--adopt-repair-source") || !adoptedWithNode.args.includes("--requested-node")) {
  throw new Error("adopt and requested-node are independent flags and must both be forwarded");
}

console.log("check-iterative-begin: OK");
