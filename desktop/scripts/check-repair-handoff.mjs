import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  findOpenRepairRequest,
  openRepairRequests,
  parseRepairRequests,
  repairHandoffPath,
  repairHandoffStateRelativePath,
  requestSummary,
} from "../dist-electron/repair-handoff.js";

const requestShape = {
  schemaVersion: 1,
  runId: "baseline-run-42",
  requests: [
    {
      requestId: "rq-1",
      project: "Demo",
      mode: "yellow",
      assignment: { a: "2.0.0" },
      fingerprint: "fp-a2",
      snapshotIdentity: "snap-broken",
      failingCommands: [{ command: "yarn lint", exitCode: 1 }],
      diagnosticsTail: "error TS2322",
      reason: "project",
      disposition: "repair-or-replan",
    },
  ],
};

// 1. Tolerance: a missing handoff file is an empty scan, never an error.
const empty = mkdtempSync(join(tmpdir(), "repair-handoff-empty-"));
const none = openRepairRequests(empty);
if (none.file !== undefined || none.requests.length !== 0 || none.runId !== "") {
  throw new Error(`Missing handoff must scan empty, got ${JSON.stringify(none)}`);
}

// 2. Parsing mirrors the generator's to_json contract.
const text = JSON.stringify(requestShape);
const parsed = parseRepairRequests(text);
if (parsed.length !== 1) throw new Error("Single repair request must parse");
if (parsed[0].requestId !== "rq-1" || parsed[0].project !== "Demo" || parsed[0].mode !== "yellow") {
  throw new Error(`Repair request fields lost: ${JSON.stringify(parsed[0])}`);
}
if (parsed[0].assignment.a !== "2.0.0") throw new Error("Assignment must survive");
if (parsed[0].failingCommands[0].command !== "yarn lint" || parsed[0].failingCommands[0].exitCode !== 1) {
  throw new Error("Failing commands must survive");
}
if (parsed[0].snapshotIdentity !== "snap-broken" || parsed[0].reason !== "project") {
  throw new Error("Snapshot identity / reason must survive");
}
if (!requestSummary(parsed[0]).includes('a@2.0.0')) throw new Error("Summary must render the assignment");
if (!requestSummary(parsed[0]).includes('yarn lint:1')) throw new Error("Summary must render failing commands");

// 3. Robustness: a broken/corrupt handoff never crashes the Executor.
if (parseRepairRequests("not-json{{{").length !== 0) throw new Error("Corrupt handoff must parse to empty");
if (parseRepairRequests(JSON.stringify({ requests: [{ fingerprint: "" }] })).length !== 0) {
  throw new Error("A request without a fingerprint is not machine-actionable");
}

// 4. Matching an assessment to an open request by project/mode/fingerprint.
const match = findOpenRepairRequest(parsed, { project: "Demo", mode: "yellow" });
if (match?.requestId !== "rq-1") throw new Error(`findOpenRepairRequest must match: ${JSON.stringify(match)}`);
if (findOpenRepairRequest(parsed, { project: "Other" }) !== undefined) throw new Error("Wrong project must not match");
if (findOpenRepairRequest(parsed, { fingerprint: "fp-other" }) !== undefined) throw new Error("Wrong fingerprint must not match");

// 4b. R4/R5: the single producer-consumer path contract. The Desktop consumer
// must be pointed at the exact relative location the Python producer writes
// (<settings base>/.dependency-roadmap/state/repair-requests.json).
if (repairHandoffStateRelativePath !== ".dependency-roadmap/state/repair-requests.json") {
  throw new Error(`Producer-consumer relative path contract broken: ${repairHandoffStateRelativePath}`);
}

// 4c. R5: the Desktop consumer is READ-ONLY. It carries no resolver and no
// name-based matcher: closing a repair request is the exclusive job of the
// authoritative Baseline verifier (project-green full-state verification).
// A package-name/batch-pass match is NOT an assignment check, so the API for
// removing requests must not exist on the consumer side at all.
if (typeof matchingRepairRequests !== "undefined") {
  throw new Error("R5: the Desktop consumer must not export a name-based repair matcher");
}

// 5. Open scanning is stable: a request present in the durable file is always
// reported as long as the producer has not resolved it authoritatively.
const dir = mkdtempSync(join(tmpdir(), "repair-handoff-"));
const file = repairHandoffPath(dir);
writeFileSync(file, text, "utf8");
const scan = openRepairRequests(dir);
if (scan.file !== file || scan.requests.length !== 1 || scan.runId !== "baseline-run-42") {
  throw new Error(`openRepairRequests must locate the handoff: ${JSON.stringify(scan)}`);
}
// The consumer never rewrites the file: after any scan the durable bytes stay
// identical (resolution is owned by the Python verifier, not the Executor).
if (readFileSync(file, "utf8") !== text) {
  throw new Error("R5: scanning the handoff must leave the durable file untouched");
}

console.log("check-repair-handoff: OK");
