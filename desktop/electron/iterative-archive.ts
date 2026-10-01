// Desktop producer/consumer contract for the Python `archive-repair-run` step
// (P1, v0.2.163 #2). The archive is a RECOVERABLE TRANSACTION owned by Python:
// it frees the slot of a TERMINAL REPAIR_VERIFIED run, keeps the fixed C0
// source snapshot (sources/C0) for the NEXT migration to adopt, writes a
// durable repair-handoff.json + the "project ready" project-check.json, and on
// any failure rolls the already-moved artifacts back so the run stays
// continuable. The Desktop only INVOKES the step and PARSES its result — it
// never re-implements the move.
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

export const ITERATIVE_ARCHIVE_RESULT_EVENT = 'ITERATIVE_ARCHIVE_RESULT_V1'

export type IterativeArchiveResult = {
  archived: boolean
  reason?: string
  archiveDir?: string
}

export type RepairHandoff = {
  terminal?: string
  adopted?: boolean
  superseded?: boolean
}

/** Args for the Python archive step (top-level --run-dir precedes the
 * subcommand, exactly like begin/status). */
export function iterativeArchiveInvocation(runDir: string, scriptPath: string): string[] {
  return [scriptPath, '--run-dir', runDir, 'archive-repair-run']
}

/** Parse the machine envelope. Returns null when no envelope is present (so a
 * clean exit without an envelope is a "not a finished repair", not an error). */
export function parseIterativeArchiveResult(stdout: string): IterativeArchiveResult | null {
  if (!stdout) return null
  const match = new RegExp(`${ITERATIVE_ARCHIVE_RESULT_EVENT} (\\{.*\\})`).exec(stdout)
  if (!match) return null
  try {
    const raw = JSON.parse(match[1]) as Record<string, any>
    return {
      archived: raw.archived === true,
      reason: typeof raw.reason === 'string' ? raw.reason : undefined,
      archiveDir: typeof raw.archiveDir === 'string' ? raw.archiveDir : undefined,
    }
  } catch {
    return null
  }
}

/** Locate the durable repair handoff written by Python during the archive.
 * The handoff is the single record the next migration adopts from. */
export function repairHandoffFilePath(runDir: string): string {
  return join(runDir, 'repair-handoff.json')
}

/** Read the durable repair handoff (best-effort, tolerant of absence/corruption —
 * never an error: the caller decides what a missing handoff means). */
export function readRepairHandoff(runDir: string): RepairHandoff | null {
  const path = repairHandoffFilePath(runDir)
  if (!existsSync(path)) return null
  try {
    const raw = JSON.parse(readFileSync(path, 'utf8')) as Record<string, any>
    return {
      terminal: typeof raw.terminal === 'string' ? raw.terminal : undefined,
      adopted: raw.adopted === true,
      superseded: raw.superseded === true,
    }
  } catch {
    return null
  }
}

/** True when the verified fixed bytes are waiting to seed the NEXT migration:
 * a REPAIR_VERIFIED handoff neither adopted nor superseded by a recapture. */
export function isRepairHandoffPending(runDir: string): boolean {
  const handoff = readRepairHandoff(runDir)
  return Boolean(handoff && handoff.terminal === 'REPAIR_VERIFIED' && !handoff.adopted && !handoff.superseded)
}
