import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { copyPayloadTooLarge, currentTask } from "../dist-electron/iterative-migration.js";
import { copyTaskWithVerification } from "../dist-electron/task-clipboard.js";

// The clipboard path is DI-shaped: this check drives the exact function
// main.ts uses for the real Electron clipboard, with a fake writer that can
// also misbehave. "Скопировано" may only surface after a verified read-back.

class FakeWriter {
  constructor({ mismatch = false, failRead = false } = {}) {
    this.mismatch = mismatch;
    this.failRead = failRead;
    this.last = "";
  }
  writeText(text) {
    this.last = text;
  }
  readText() {
    if (this.failRead) throw new Error("clipboard device busy");
    return this.mismatch ? "<tampered>" : this.last;
  }
}

function buildRunDir() {
  const root = mkdtempSync(join(tmpdir(), "task-clipboard-"));
  const runDir = join(root, "run");
  const artifactId = "task-demo-C1-abcdef123456";
  const artifactRoot = join(runDir, "task", artifactId);
  mkdirSync(artifactRoot, { recursive: true });
  const manifest = {
    schemaVersion: 1,
    builder: "iterative_task",
    builderVersion: "v1",
    artifactId,
    languages: ["en", "ru"],
    runId: "iter-clip-1",
    workspaceId: "ws-1",
    projectId: "demo",
    projectName: "Demo",
    baseCheckpointId: "C0",
    targetCheckpointId: "C1",
    policyHash: "ph-1",
    scopeHash: "sc-1",
    commandSetHash: "cs-1",
    exactVersions: { a: "1.0.0" },
    actions: [],
    deferred: [],
    completeness: { policySatisfied: true, denominator: 1, remaining: 0 },
    verification: { status: "passed", commands: ["node check.js"] },
    audit: { status: "UNKNOWN", evidenceRef: "" },
    sourceSnapshotKey: "snap",
    manifestHash: "mh",
    lockfileHash: "lh",
    resolvedStateKey: "rsk",
    contents: {},
    contentHash: "ch-1",
    stale: false,
    createdAt: "2026-09-29T00:00:00Z",
  };
  const body = "# Задание на адаптацию зависимостей\n\nОбновите a до 1.0.0.";
  const ruHash = createHash("sha256").update(body, "utf8").digest("hex");
  manifest.contents = { ru: { contentHash: ruHash, contentBytes: Buffer.byteLength(body, "utf8") } };
  writeFileSync(join(artifactRoot, "task-manifest.json"), JSON.stringify(manifest), "utf8");
  writeFileSync(join(artifactRoot, "task.ru.md"), body, "utf8");
  writeFileSync(
    join(runDir, "task", "current.json"),
    JSON.stringify({ schemaVersion: 1, artifactId, runId: "iter-clip-1", targetCheckpointId: "C1", policyHash: "ph-1", languages: ["ru"] }),
    "utf8",
  );
  return { runDir, root };
}

const { runDir } = buildRunDir();
const task = currentTask(runDir);
if (!task) throw new Error("Fixture task must be readable");

// 1. Success: bytes verified, fingerprint returned, "Copied" allowed.
const ok = copyTaskWithVerification(task, "ru", new FakeWriter());
if (!ok.ok || !ok.fingerprint?.startsWith("task-demo-C1-")) {
  throw new Error(`Successful copy must verify the read-back: ${JSON.stringify(ok)}`);
}

// 2. Tampered clipboard: refuse, never claim "Copied".
const tampered = copyTaskWithVerification(task, "ru", new FakeWriter({ mismatch: true }));
if (tampered.ok || !tampered.error?.includes("VERIFY_MISMATCH")) {
  throw new Error(`Tampered read-back must fail: ${JSON.stringify(tampered)}`);
}

// 3. Device failure on read-back: refuse with an explicit cause.
const broken = copyTaskWithVerification(task, "ru", new FakeWriter({ failRead: true }));
if (broken.ok || broken.error !== "CLIPBOARD_READBACK_UNAVAILABLE") {
  throw new Error(`Read-back failure must be surfaced: ${JSON.stringify(broken)}`);
}

// 4. Missing language: refused before any write.
const noLang = copyTaskWithVerification(task, "en", new FakeWriter());
if (noLang.ok || !noLang.error?.includes("NO_TASK_LANGUAGE")) {
  throw new Error(`Missing language must be refused: ${JSON.stringify(noLang)}`);
}

// 5. Oversized body: refused before any write.
const bigTask = { ...task, text: { ru: "x".repeat(1024 * 1024) }, manifest: { ...task.manifest, contents: { ru: { contentHash: "h", contentBytes: 1024 * 1024 } } } };
const oversized = copyTaskWithVerification(bigTask, "ru", new FakeWriter());
if (oversized.ok || !oversized.error?.includes("TASK_TOO_LARGE")) {
  throw new Error(`Oversized payload must be refused: ${JSON.stringify(oversized)}`);
}
if (copyPayloadTooLarge({ text: "small" })) throw new Error("Small payload must pass");

console.log("check-task-clipboard: OK");
