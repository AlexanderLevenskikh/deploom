import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  copyPayloadTooLarge,
  currentTask,
  iterativeExportInvocation,
  iterativeRunDirPath,
  iterativeRunDirRelativePath,
  missingTaskExportInput,
  taskContentMismatch,
  taskCopyPayload,
  taskDispatchable,
  taskStaleness,
} from "../dist-electron/iterative-migration.js";

const DESKTOP = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = resolve(DESKTOP, "..");
const PYTHON = process.platform === "win32" ? "python" : "python3";
const GENERATOR = join(REPO_ROOT, "iterative_migration.py");

function writeJson(path, value) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value), "utf8");
}

function minimalRunDir(root) {
  const runDir = join(root, "run");
  mkdirSync(join(runDir, "checkpoints"), { recursive: true });
  writeJson(join(runDir, "run.json"), {
    schemaVersion: 1,
    runId: "iter-desktop-1",
    workspaceId: "ws-1",
    projectId: "demo-app",
    activeCheckpointId: "C1",
    phase: "READY",
    targetPolicyHash: "ph-1",
  });
  writeJson(join(runDir, "run-config.json"), {
    schemaVersion: 1,
    projectName: "DemoApp",
    projectDir: join(root, "project"),
    workspaceId: "ws-1",
    projectId: "demo-app",
    policyHash: "ph-1",
    targetLevel: "yellow",
    targets: { "is-number": "7.0.0", "@scope/deferred": "2.0.0" },
    verifyConfig: { commands: ["node check.js"] },
  });
  writeJson(join(runDir, "checkpoints", "C1.json"), {
    schemaVersion: 1,
    checkpointId: "C1",
    parentCheckpointId: "C0",
    status: "VERIFIED",
    sourceSnapshotKey: "snap-c1",
    manifestHash: "mh-1",
    lockfileHash: "lh-1",
    resolvedStateKey: "rsk-c1",
    fullAssignment: { "is-number": "7.0.0", "@scope/deferred": "1.2.0" },
    verification: { status: "passed", commands: ["node check.js"] },
    audit: { status: "UNKNOWN", evidenceRef: "" },
  });
  writeJson(join(runDir, "ledger.json"), {
    schemaVersion: 1,
    blocks: [
      {
        kind: "NOT_ACTIONABLE",
        package: "@scope/deferred",
        reason: "PEER_RESOLUTION_DEFERRED: desired=2.0.0; resolved=current 1.2.0; peer cycle",
        baseCheckpointId: "C1",
      },
    ],
    deferrals: [
      { package: "@scope/deferred", kind: "INCONCLUSIVE", reason: "peer cycle unresolved" },
    ],
  });
  return runDir;
}

// 1. The Desktop is a CONSUMER of the Python-produced task artifact. Run the
// real export-task CLI on a minimal durable run dir, then read the artifact
// back through the TypeScript module: exact producer bytes, pointer, manifest
// identities, deferred kept immutable in the remainder.
const root = mkdtempSync(join(tmpdir(), "iter-migration-handoff-"));
const runDir = minimalRunDir(root);
if (!existsSync(GENERATOR)) throw new Error(`Generator missing: ${GENERATOR}`);
execFileSync(PYTHON, [GENERATOR, "--run-dir", runDir, "export-task"], { stdio: "pipe" });

const task = currentTask(runDir);
if (!task) throw new Error("currentTask must resolve the exported task");
const m = task.manifest;
if (m.runId !== "iter-desktop-1" || m.targetCheckpointId !== "C1") {
  throw new Error(`Manifest identities lost: ${JSON.stringify({ runId: m.runId, target: m.targetCheckpointId })}`);
}
if (m.builder !== "iterative_task" || m.schemaVersion !== 1) throw new Error("Builder/schema identity lost");
if (m.exactVersions["is-number"] !== "7.0.0") throw new Error("Exact accepted version must be the assignment's");
if (m.completeness.policySatisfied) throw new Error("Deferred target must keep policy unsatisfied");
if (m.completeness.denominator !== 2 || m.completeness.remaining !== 1) {
  throw new Error(`Completeness off: ${JSON.stringify(m.completeness)}`);
}
const deferred = m.deferred.find((d) => d.package === "@scope/deferred");
if (!deferred || !deferred.reason.includes("PEER_RESOLUTION_DEFERRED")) {
  throw new Error("Explicit deferral must carry its reason in the task");
}
if (deferred.current !== "1.2.0" || deferred.lagPolicyTarget !== "2.0.0") {
  throw new Error(`Deferred row must keep versions: ${JSON.stringify(deferred)}`);
}
if (!task.text.ru || !task.text.en) throw new Error("RU and EN bodies must be published");
if (task.text.en.includes("@scope/deferred") !== true) throw new Error("Deferred package must be visible in EN body");
const copied = taskCopyPayload(task, "ru");
if (!copied) throw new Error("Copy payload must exist for ru");
if (copied.text.length === 0 || !copied.text.includes("@scope/deferred")) {
  throw new Error("Copy payload must carry the task body with its deferred package");
}
if (!copied.fingerprint.startsWith(m.artifactId)) throw new Error("Copy fingerprint must carry the artifact id");
if (m.contents.ru.contentHash === "") throw new Error("Content hash must be bound per language");

// 2. Staleness: alive durable state matching the pointer is NOT stale; the
// refreshed artifact is DISPATCHABLE (identity + verified content hashes).
if (taskStaleness(runDir).stale) throw new Error(`Fresh task must not be stale: ${JSON.stringify(taskStaleness(runDir))}`);
const dispatchable = taskDispatchable(runDir);
if (!dispatchable.ok) throw new Error(`Fresh exported task must be dispatchable: ${JSON.stringify(dispatchable)}`);
if (dispatchable.ok && taskContentMismatch(dispatchable.task)) {
  throw new Error(`Exported task bodies must hash to the manifest: ${taskContentMismatch(dispatchable.task)}`);
}

// 2b. Postfix P1 (#2): corrupting a published body breaks the content hash, so
// the artifact is no longer dispatchable (never old/corrupt text to the agent).
const ruPath = join(runDir, "task", m.artifactId, "task.ru.md");
const ruBytes = readFileSync(ruPath, "utf8");
writeFileSync(ruPath, `${ruBytes}\n<!-- tampered -->`, "utf8");
const tampered = taskDispatchable(runDir);
if (tampered.ok) throw new Error("A body whose content hash does not match must not be dispatchable");
if (!tampered.ok && !tampered.reason.includes("CONTENT_HASH_MISMATCH")) {
  throw new Error(`Corrupted body must be refused by content hash: ${JSON.stringify(tampered)}`);
}
writeFileSync(ruPath, ruBytes, "utf8");
if (!taskDispatchable(runDir).ok) throw new Error("Restoring the exact bytes must restore dispatchability");

// 3. Drift -> stale, dispatch refused.
const configPath = join(runDir, "run-config.json");
const config = JSON.parse(readText(configPath));
config.policyHash = "ph-2";
writeJson(configPath, config);
if (!taskStaleness(runDir).stale) throw new Error("Policy drift must be flagged stale");
const drifted = taskDispatchable(runDir);
if (drifted.ok) throw new Error("A drifted (stale) task must not be dispatchable");
if (drifted.ok === false && !drifted.reason.includes("POLICY_DRIFT")) {
  throw new Error(`Dispatch refusal must name the drift reason: ${JSON.stringify(drifted)}`);
}
config.policyHash = "ph-1";
writeJson(configPath, config);
if (!taskDispatchable(runDir).ok) throw new Error("Restoring the policy must restore dispatchability");

// 4. Insufficient durable state is a concrete diagnosis, not a Baseline start.
const bare = mkdtempSync(join(tmpdir(), "iter-migration-bare-"));
const missing = missingTaskExportInput(bare);
if (!missing.includes("run.json")) throw new Error("Run-less dir must report run.json");
mkdirSync(join(bare, "checkpoints"), { recursive: true });
writeJson(join(bare, "run.json"), { runId: "iter-x", activeCheckpointId: "C1" });
writeJson(join(bare, "run-config.json"), { projectName: "X", targets: { a: "1" } });
const missing2 = missingTaskExportInput(bare);
for (const key of ["run-config.projectDir", "run-config.policyHash", "checkpoint:C1"]) {
  if (!missing2.includes(key)) throw new Error(`Missing list must name ${key}, got ${JSON.stringify(missing2)}`);
}

// 5. Copy guard: oversized payload is refused before the clipboard.
const big = { text: "x".repeat(1024 * 1024) };
if (!copyPayloadTooLarge(big)) throw new Error("Oversized payload must be refused");
if (copyPayloadTooLarge({ text: "small" })) throw new Error("Small payload must pass");

// 6. Run-dir location contract (producer-consumer): the Desktop points the
// Python CLI at <base>/.dependency-roadmap/iterative/<token>.
if (iterativeRunDirPath(root, "DemoApp") !== join(root, iterativeRunDirRelativePath("DemoApp"))) {
  throw new Error("Run-dir path contract broken");
}
const runRel = iterativeRunDirRelativePath("DemoApp");
if (!runRel.startsWith(".dependency-roadmap/iterative/")) throw new Error("Run-dir must live under .dependency-roadmap/iterative");

// 7. Invocation shape for the export step. --run-dir is a TOP-LEVEL CLI
// argument, so it must precede the subcommand; a consumer that appends it
// after 'export-task' fails the real parser with "required: --run-dir".
const invocation = iterativeExportInvocation(runDir, GENERATOR, PYTHON, "both");
if (invocation.args[0] !== GENERATOR) {
  throw new Error(`Export invocation malformed: ${JSON.stringify(invocation)}`);
}
if (invocation.args[1] !== "--run-dir" || invocation.args[2] !== runDir) {
  throw new Error(`--run-dir must precede the subcommand: ${JSON.stringify(invocation)}`);
}
const subcommandIndex = invocation.args.indexOf("export-task");
if (subcommandIndex < 3) throw new Error(`export-task must follow --run-dir: ${JSON.stringify(invocation)}`);

// 8. R9 legacy import: the CLI accepts --legacy-dashboard-state on the REAL
// parser and the produced artifact carries artifactSource=legacy-baseline,
// so the Desktop consumer keeps it usable (copy/save/preview) even though
// there is no run.json yet — it must NOT be flagged stale.
const legacyRunDir = join(root, "legacy");
const legacyDash = join(root, "legacy-dashboard-state.json");
writeJson(legacyDash, {
  schemaVersion: 3,
  projectName: "LegacyApp",
  updatedAt: "2026-01-15T12:00:00Z",
  projects: {
    LegacyApp: [
      { package: "is-number", current_version: "1.2.3", lagPolicyTarget: "7.0.0", lagThresholdMonths: 6 },
      {
        package: "@example/widgets",
        current_version: "1.2.0",
        lagPolicyTarget: "2.1.0",
        planner_deferred: true,
        planner_deferred_reason: "PEER_RESOLUTION_DEFERRED: peer cycle unresolved",
      },
    ],
  },
  issues: [],
});
execFileSync(
  PYTHON,
  [GENERATOR, "--run-dir", legacyRunDir, "export-task", "--language", "both", "--legacy-dashboard-state", legacyDash, "--project-name", "LegacyApp"],
  { stdio: "pipe" },
);
const legacyTask = currentTask(legacyRunDir);
if (!legacyTask) throw new Error("Legacy import must publish a task artifact");
if (legacyTask.manifest.artifactSource !== "legacy-baseline") {
  throw new Error(`Legacy manifest must carry artifactSource=legacy-baseline, got ${legacyTask.manifest.artifactSource}`);
}
if (legacyTask.manifest.verification.status !== "legacy-exported-not-reverified") {
  throw new Error("Legacy import must not invent approval");
}
if (taskStaleness(legacyRunDir).stale) {
  throw new Error("Legacy artifact with no run.json must stay usable (not stale)");
}
const legacyPayload = taskCopyPayload(legacyTask, "ru");
if (!legacyPayload || !legacyPayload.text.includes("is-number")) {
  throw new Error("Legacy copy payload must carry the imported target");
}
if (!legacyPayload.text.includes("7.0.0")) {
  throw new Error("Legacy copy payload must show the imported goal version 7.0.0");
}

console.log("check-iterative-migration: OK");

function readText(path) {
  return readFileSync(path, "utf8");
}
