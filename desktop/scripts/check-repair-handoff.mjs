import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  findOpenRepairRequest,
  matchingRepairRequests,
  openRepairRequests,
  parseRepairRequests,
  repairHandoffPath,
  repairHandoffStateRelativePath,
  requestSummary,
  resolveRepairRequest,
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

// 4b. R4: the single producer-consumer path contract. The Desktop consumer
// must be pointed at the exact relative location the Python producer writes
// (<settings base>/.dependency-roadmap/state/repair-requests.json).
if (repairHandoffStateRelativePath !== ".dependency-roadmap/state/repair-requests.json") {
  throw new Error(`Producer-consumer relative path contract broken: ${repairHandoffStateRelativePath}`);
}

// 4c. R4: matching for resolution requires the FULL assignment of the request
// to be covered by the freshly verified batch in the SAME mode. Requests of the
// SAME project must stay independent: a green batch touching only `a` is proof
// for the a-only request, but NOT for a request that also pins b@3.0.0.
const rqYellowA = { ...requestShape.requests[0], requestId: "rq-yellow-a", mode: "yellow", fingerprint: "fp-a2", assignment: { a: "2.0.0" } };
const rqYellowB = { ...requestShape.requests[0], requestId: "rq-yellow-b", mode: "yellow", fingerprint: "fp-b3", assignment: { a: "2.0.0", b: "3.0.0" } };
const rqGreenC = { ...requestShape.requests[0], requestId: "rq-green-c", mode: "green", fingerprint: "fp-c4", assignment: { c: "1.5.0" } };
const multi = [rqYellowA, rqYellowB, rqGreenC];
const byPartial = matchingRepairRequests(multi, { project: "Demo", mode: "yellow", packages: ["a"] });
if (byPartial.length !== 1 || byPartial[0].requestId !== "rq-yellow-a") {
  throw new Error(`A batch touching only a must match exactly rq-yellow-a, never the a+b request: ${JSON.stringify(byPartial)}`);
}
const byFull = matchingRepairRequests(multi, { project: "Demo", mode: "yellow", packages: ["a", "b"] });
if (byFull.length !== 2 || !byFull.some((r) => r.requestId === "rq-yellow-b")) {
  throw new Error(`A covering yellow batch must match every fully covered request: ${JSON.stringify(byFull)}`);
}
if (matchingRepairRequests(multi, { project: "Demo", mode: "green", packages: ["c"] }).length !== 1) {
  throw new Error("Same-mode full coverage must match the green request");
}
if (matchingRepairRequests(multi, { project: "Demo", mode: "green", packages: ["c", "d"] }).length !== 1) {
  throw new Error("A covering green batch must still match even with extra packages");
}
if (matchingRepairRequests([rqGreenC], { project: "Demo", mode: "yellow", packages: ["c"] }).length !== 0) {
  throw new Error("Wrong mode must never satisfy a request");
}

// 5. Resolving a request rewrites the durable file; resolving the last one
// removes it entirely -- so a restarted run cannot loop on a resolved tuple.
const dir = mkdtempSync(join(tmpdir(), "repair-handoff-"));
const file = repairHandoffPath(dir);
writeFileSync(file, text, "utf8");
const scan = openRepairRequests(dir);
if (scan.file !== file || scan.requests.length !== 1 || scan.runId !== "baseline-run-42") {
  throw new Error(`openRepairRequests must locate the handoff: ${JSON.stringify(scan)}`);
}
const noop = resolveRepairRequest(dir, "does-not-exist");
if (noop.resolved || noop.remaining.length !== 1) throw new Error("Unknown request must not resolve");
const outcome = resolveRepairRequest(dir, "rq-1");
if (!outcome.resolved || outcome.remaining.length !== 0) throw new Error("Known request must resolve out");
if (existsSync(file)) throw new Error("Empty handoff must be removed, not left as an empty array");
const reScanned = openRepairRequests(dir);
if (reScanned.requests.length !== 0 || reScanned.file !== undefined) {
  throw new Error(`Restarted Executor must see no open requests: ${JSON.stringify(reScanned)}`);
}

// 6. Resolving ONE of several keeps the file with the other requests intact.
const two = mkdtempSync(join(tmpdir(), "repair-handoff-two-"));
const fileTwo = repairHandoffPath(two);
writeFileSync(fileTwo, JSON.stringify({ schemaVersion: 1, runId: "runs", requests: [
  requestShape.requests[0],
  { ...requestShape.requests[0], requestId: "rq-2", fingerprint: "fp-b2" },
] }), "utf8");
const partial = resolveRepairRequest(two, "rq-1");
if (partial.resolved !== true || partial.remaining.length !== 1 || partial.remaining[0].requestId !== "rq-2") {
  throw new Error(`Resolving one of two must keep the other: ${JSON.stringify(partial)}`);
}
if (JSON.parse(readFileSync(fileTwo, "utf8")).requests.length !== 1) throw new Error("Durable file must persist the remaining request");

console.log("check-repair-handoff: OK");
