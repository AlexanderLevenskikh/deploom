// Headless production-coordinator drive for the iterative migration demo.
//
// This runs the SAME coordinator loop that desktop/electron/main.ts wires to
// `flow:iterative:drive`, using the real compiled production modules
// (dist-electron/iterative-runner.js, iterative-stream.js, iterative-attempt.js)
// and the real Python CLI — so a demo driven through this module exercises the
// production decide->status->step loop, not a re-implementation:
//
//   while (true) {
//     status = python status            (parsed envelope or durable status.json)
//     decision = decideNextStep(runDir, payload)
//     agent gate        -> STOP 'agent-gate'   (external agent work, not auto-run)
//     begin (NO_RUN)    -> STOP 'error'        (supervisor never fabricates a run)
//     null + infra      -> STOP 'infra-blocked'(recoverable; operator retries)
//     null + other      -> STOP 'error'        (unknown phase / concluded materialize)
//     step != 0         -> STOP 'error'/'canceled'
//     step === finish   -> STOP 'finished'
//   }
//
// The loop deliberately does NOT act for the application: it never sends
// feedback (INCONCLUSIVE or otherwise) and never edits durable state on behalf
// of the app. External actions (scripted agent repair + apply-feedback,
// plan-next --retry-infra, stop/resume of a project command) belong to the demo
// orchestrator, exactly as in production the agent gate and operator retry sit
// OUTSIDE the drive.
//
// Environment is inherited from the parent, so the orchestrator controls the
// npm cache / registry / offline coordinates per segment.
//
// Usage:
//   node scripts/iterative-node-drive.mjs --run-dir <dir> [--python python] \
//       [--script <iterative_migration.py>]
// Prints one `NODE_DRIVE_RESULT <json>` line on stdout when it stops.
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import {
  decideNextStep,
  iterativeStatusInvocation,
  iterativeStepInvocation,
  parseIterativeStatusPayload,
  readIterativeStatus,
} from "../desktop/dist-electron/iterative-runner.js";
import { spawnIterativeStreamed } from "../desktop/dist-electron/iterative-stream.js";
import {
  readAttempt,
  resumeAttempt,
  updateAttempt,
  recordAttemptLog,
  trimAttemptLog,
  clearCancelRequest,
} from "../desktop/dist-electron/iterative-attempt.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");

function option(name, fallback) {
  const at = process.argv.indexOf(name);
  return at >= 0 ? process.argv[at + 1] : fallback;
}

const runDir = option("--run-dir", "");
const python = option("--python", "python");
const script = option("--script", join(ROOT, "iterative_migration.py"));

if (!runDir) {
  console.error("iterative-node-drive: --run-dir is required");
  process.exit(2);
}

// Headless bindings of the same platform primitives the Desktop drive uses:
// commands come from the production invocation builders; the environment is the
// parent's (offline cache/registry coordinates set by the orchestrator).
const platform = {
  processTreeDetached: () => process.platform !== "win32",
  commandEnvironment: (env) => env,
  resolveSpawnInvocation: (command, args) => ({ command, args }),
  killProcessTree: (child) => {
    try {
      child.kill(process.platform === "win32" ? "SIGKILL" : "SIGTERM");
    } catch { /* already gone */ }
  },
  decodeChunk: (chunk) => chunk.toString("utf8"),
};

const io = {
  cancelRequested: (dir) => readAttempt(dir)?.cancelRequested === true,
  recordLine: (dir, text) => {
    try { recordAttemptLog(dir, text); } catch { /* best-effort */ }
  },
  trimLog: (dir) => {
    try { trimAttemptLog(dir); } catch { /* best-effort */ }
  },
};

function result(payload) {
  console.log(`NODE_DRIVE_RESULT ${JSON.stringify(payload)}`);
}

function fail(runDir, phase, reason, step) {
  try { updateAttempt(runDir, { status: "failed", stage: "drive", lastError: reason, lastStep: step ?? "status" }); } catch { /* best-effort */ }
  return result({ stopped: "error", phase, step: step ?? null, reason, error: reason, attempt: readAttempt(runDir) });
}

async function main() {
  // L1: rebuild the journal across a restart (same attemptId, status running).
  try {
    resumeAttempt(runDir, "demo-run");
    updateAttempt(runDir, { status: "running", stage: "drive", lastError: undefined, cancelRequested: false });
  } catch { /* journal is best-effort; durable state is Python-owned */ }

  const steps = [];
  while (true) {
    if (readAttempt(runDir)?.cancelRequested) {
      try {
        clearCancelRequest(runDir);
        updateAttempt(runDir, { status: "canceled", stage: "drive", reason: "canceled by demo operator; verified checkpoint preserved", lastStep: "canceled" });
      } catch { /* best-effort */ }
      return result({ stopped: "canceled", steps, attempt: readAttempt(runDir) });
    }

    const statusInvocation = iterativeStatusInvocation(runDir, script, python);
    const statusResult = await spawnIterativeStreamed(runDir, statusInvocation.command, statusInvocation.args, ROOT, 120_000, io, platform);
    const payload = statusResult.code === 0
      ? (parseIterativeStatusPayload(statusResult.stdout) ?? readIterativeStatus(runDir))
      : undefined;
    if (!payload) {
      const raw = statusResult.timedOut ? "STATUS_TIMEOUT" : (statusResult.stderr.trim() || "STATUS_UNREADABLE");
      return fail(runDir, "", `status unreadable: ${raw.slice(0, 4000)}`);
    }

    const phase = String((payload.run ?? {}).phase ?? "");
    const decision = decideNextStep(runDir, payload);
    try { updateAttempt(runDir, { status: "running", stage: "drive", phase, lastStep: decision.step ?? undefined }); } catch { /* best-effort */ }

    if (decision.step === "agent") {
      try { updateAttempt(runDir, { status: "done", stage: "drive", reason: decision.reason, lastStep: "agent" }); } catch { /* best-effort */ }
      return result({
        stopped: "agent-gate",
        steps,
        phase,
        reason: decision.reason,
        bootstrap: decision.bootstrap,
        repairRequests: decision.repairRequests ?? [],
        attempt: readAttempt(runDir),
      });
    }

    if (decision.step === "begin") {
      try { updateAttempt(runDir, { status: "failed", stage: "drive", lastError: "NO_RUN", lastStep: "begin" }); } catch { /* best-effort */ }
      return fail(runDir, phase, "NO_RUN: begin (C0 capture) is a separate user action; this drive only continues an opened run", "begin");
    }

    if (decision.step === null) {
      if (decision.infraBlocked) {
        try { updateAttempt(runDir, { status: "failed", stage: "drive", lastError: decision.reason, lastStep: "infra-blocked" }); } catch { /* best-effort */ }
        return result({
          stopped: "infra-blocked",
          steps,
          phase,
          reason: decision.reason,
          error: decision.reason,
          attempt: readAttempt(runDir),
        });
      }
      return fail(runDir, phase, decision.reason, "unknown");
    }

    const stepInvocation = iterativeStepInvocation(runDir, decision.step, script, python);
    const stepResult = await spawnIterativeStreamed(runDir, stepInvocation.command, stepInvocation.args, ROOT, 0, io, platform);
    if (stepResult.code !== 0) {
      const canceledByUser = readAttempt(runDir)?.cancelRequested === true;
      const raw = stepResult.timedOut
        ? (canceledByUser ? "CANCELED: step terminated by user cancel" : `STEP_TIMEOUT: step ${decision.step} exceeded its budget`)
        : (stepResult.stderr.trim() || stepResult.stdout.trim() || `STEP_EXIT_${stepResult.code}`);
      try {
        if (canceledByUser) clearCancelRequest(runDir);
        updateAttempt(runDir, {
          status: canceledByUser ? "canceled" : "failed",
          stage: "drive",
          lastError: canceledByUser ? undefined : raw.slice(0, 4000),
          reason: canceledByUser ? "canceled by demo operator" : `step ${decision.step} failed`,
          lastStep: decision.step,
        });
      } catch { /* best-effort */ }
      return result({
        stopped: canceledByUser ? "canceled" : "error",
        steps,
        phase,
        step: decision.step,
        error: raw.slice(0, 4000),
        attempt: readAttempt(runDir),
      });
    }

    steps.push(decision.step);
    try { updateAttempt(runDir, { stepsDone: [...steps] }); } catch { /* best-effort */ }
    if (decision.step === "finish") {
      try { updateAttempt(runDir, { status: "done", stage: "drive", phase: "TERMINAL", reason: decision.reason, lastStep: "finish" }); } catch { /* best-effort */ }
      return result({ stopped: "finished", steps, phase: "TERMINAL", reason: decision.reason, attempt: readAttempt(runDir) });
    }
  }
}

main().catch((error) => {
  console.error(`iterative-node-drive: ${error?.stack ?? error}`);
  process.exit(1);
});
