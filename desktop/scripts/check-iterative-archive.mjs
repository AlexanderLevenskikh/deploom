// Desktop contract for the Python `archive-repair-run` step (P1, v0.2.163).
// The archive is a RECOVERABLE TRANSACTION: a finished repair (TERMINAL
// REPAIR_VERIFIED) frees its run slot, keeps the fixed C0 source snapshot for
// the next migration to adopt, writes repair-handoff.json + project-check.json,
// and on ANY failure rolls the already-moved artifacts back so the run stays
// continuable. The Desktop only invokes the step and parses its result — it
// never re-implements the move, so an honest parse + args contract is the
// production boundary the Desktop owns.
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  iterativeArchiveInvocation,
  isRepairHandoffPending,
  parseIterativeArchiveResult,
  readRepairHandoff,
  repairHandoffFilePath,
} from "../dist-electron/iterative-archive.js";

// 1. Invocation shape: --run-dir is a TOP-LEVEL CLI argument (before the
// subcommand), the step is spelled exactly `archive-repair-run`.
const args = iterativeArchiveInvocation("C:/ws/x/.dependency-roadmap/iterative/tok/run", "iterative_migration.py");
if (args[0] !== "iterative_migration.py") throw new Error("script path must come first");
if (args[1] !== "--run-dir" || args[2] !== "C:/ws/x/.dependency-roadmap/iterative/tok/run") {
  throw new Error(`--run-dir must precede the subcommand: ${JSON.stringify(args)}`);
}
if (args[3] !== "archive-repair-run") throw new Error(`subcommand must be archive-repair-run: ${JSON.stringify(args)}`);

// 2. A successful archive parses as archived:true with the evidence refs.
const ok = `ITERATIVE_ARCHIVE_RESULT_V1 {"archived":true,"runId":"iter-r1","archiveDir":"C:/x/run/repair-archive/iter-r1-1","handoffPath":"C:/x/run/repair-handoff.json"}`;
const parsedOk = parseIterativeArchiveResult(ok);
if (!parsedOk || parsedOk.archived !== true) throw new Error("successful archive must parse as archived");
if (parsedOk.archiveDir !== "C:/x/run/repair-archive/iter-r1-1") throw new Error("archiveDir must be forwarded");

// 3. "Not a finished repair" is NOT an error: clean envelope, archived:false,
// and the slot stays occupied (the caller keeps the run).
const notRepair = `ITERATIVE_ARCHIVE_RESULT_V1 {"archived":false,"reason":"NOT_REPAIR_VERIFIED","phase":"VERIFYING"}`;
const parsedNo = parseIterativeArchiveResult(notRepair);
if (!parsedNo || parsedNo.archived !== false) throw new Error("not-a-repair must parse as archived:false");
if (parsedNo.reason !== "NOT_REPAIR_VERIFIED") throw new Error("reason must be forwarded");

// 4. No envelope / corrupt envelope => null (never a fabricated success or a
// crash). A failed archive surfaces through a NON-ZERO child exit instead, and
// the rollback keeps the run continuable — the parse must stay neutral.
if (parseIterativeArchiveResult("") !== null) throw new Error("empty stdout must parse to null");
if (parseIterativeArchiveResult("some progress line\n") !== null) throw new Error("no envelope must parse to null");
if (parseIterativeArchiveResult('ITERATIVE_ARCHIVE_RESULT_V1 not-json{{{') !== null) {
  throw new Error("corrupt envelope must parse to null");
}

// 5. The handoff lives at the fixed durable location the next begin reads.
if (repairHandoffFilePath("C:/x/run") !== join("C:/x/run", "repair-handoff.json")) {
  throw new Error("repair-handoff.json path contract broken");
}

// 6. Real files round-trip: written handoff/verdict are exactly the artifacts
// the Python step leaves behind (paths, not content, are the Desktop contract).
const root = mkdtempSync(join(tmpdir(), "iter-archive-contract-"));
writeFileSync(join(root, "repair-handoff.json"), JSON.stringify({ terminal: "REPAIR_VERIFIED", adopted: false }), "utf8");
const fs = await import("node:fs");
if (!fs.existsSync(repairHandoffFilePath(root))) throw new Error("handoff file must be locatable after the archive");

// 7. P1 (#1): a REPAIR_VERIFIED handoff that is neither adopted nor superseded
// is PENDING — the fixed bytes must seed the next migration. Adopted or
// superseded handoffs are not adoptable; a missing/corrupt handoff never is.
if (!isRepairHandoffPending(root)) throw new Error("fresh REPAIR_VERIFIED handoff must be pending");
const adoptedHandoff = mkdtempSync(join(tmpdir(), "iter-archive-adopted-"));
writeFileSync(join(adoptedHandoff, "repair-handoff.json"), JSON.stringify({ terminal: "REPAIR_VERIFIED", adopted: true }), "utf8");
if (isRepairHandoffPending(adoptedHandoff)) throw new Error("an adopted handoff must not be pending");
const supersededHandoff = mkdtempSync(join(tmpdir(), "iter-archive-superseded-"));
writeFileSync(join(supersededHandoff, "repair-handoff.json"), JSON.stringify({ terminal: "REPAIR_VERIFIED", superseded: true, supersededBy: "recapture" }), "utf8");
if (isRepairHandoffPending(supersededHandoff)) throw new Error("a superseded handoff must not be pending");
const absent = mkdtempSync(join(tmpdir(), "iter-archive-absent-"));
if (isRepairHandoffPending(absent)) throw new Error("a missing handoff must not be pending");
const corrupt = mkdtempSync(join(tmpdir(), "iter-archive-corrupt-"));
writeFileSync(join(corrupt, "repair-handoff.json"), "not-json{{{", "utf8");
if (readRepairHandoff(corrupt) !== null) throw new Error("a corrupt handoff must read as null");

console.log("check-iterative-archive: OK");
