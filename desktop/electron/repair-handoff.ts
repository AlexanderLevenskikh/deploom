import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

// R5 (2026-09-28): the Desktop Executor is a READ-ONLY consumer of the
// durable repair handoff. It parses and surfaces open repair requests for
// feedback, and it carries NO resolution capability on purpose: a repair
// request is a SOURCE/CONFIG ticket whose only valid close is a fresh
// project-green verification of the exact cumulative assignment on the
// current snapshot, performed by the authoritative Baseline verifier
// (generator-side resolve, guarded by project_result.ok). Any removal keyed
// on package names / batch passes here would close tickets the Executor has
// not proven. There is deliberately no resolver or matcher exported.

export type RepairFailureCommand = {
  command: string
  exitCode: number
}

export type RepairRequest = {
  requestId: string
  project: string
  mode: string
  assignment: Record<string, string>
  fingerprint: string
  snapshotIdentity: string
  failingCommands: RepairFailureCommand[]
  diagnosticsTail: string
  reason: string
  disposition?: string
}

export type RepairHandoffFile = {
  schemaVersion: number
  runId: string
  requests: RepairRequest[]
}

export type RepairHandoffScan = {
  file?: string
  runId: string
  requests: RepairRequest[]
}

// R5 item 3: the Desktop-side detection signal. The generator ends a mode
// with the machine-readable `repair-required-terminal` progress event
// (DEPLOOM_PROGRESS_V2 on stderr) and still exits 0 -- so a plain "exit code
// 0" cannot distinguish a green Baseline from a source/config-repairable
// result. This envelope carries the exact durable request set the terminal
// was written with (project/mode/fingerprint + assignment), which is the
// only thing a repair dispatch must hand to the repair agent.
export type RepairRequiredEnvelope = {
  runId: string
  project: string
  mode: string
  assignment: string
  terminalStatus: string
  requests: RepairRequest[]
}

function parseProgressJson(line: string): unknown {
  const start = line.indexOf('{')
  if (start < 0) return undefined
  try {
    return JSON.parse(line.slice(start))
  } catch {
    return undefined
  }
}

export function extractRepairRequiredEnvelope(output: string): RepairRequiredEnvelope | undefined {
  let latest: RepairRequiredEnvelope | undefined
  for (const line of String(output || '').split(/\r?\n/)) {
    const value = parseProgressJson(line)
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue
    const record = value as Record<string, unknown>
    if (record.phase !== 'repair-required-terminal') continue
    const rawRequests = Array.isArray(record.repairRequests) ? record.repairRequests : []
    const requests: RepairRequest[] = []
    for (const item of rawRequests) {
      const parsed = parseRequest(item)
      if (parsed) requests.push(parsed)
    }
    latest = {
      runId: asString(record.runId),
      project: asString(record.project),
      mode: asString(record.mode),
      assignment: asString(record.assignment),
      terminalStatus: asString(record.terminalStatus) || 'REPAIR_REQUIRED',
      requests,
    }
  }
  return latest
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function parseRequest(raw: unknown): RepairRequest | undefined {
  const request = asRecord(raw)
  const fingerprint = asString(request.fingerprint).trim()
  if (!fingerprint) return undefined
  const assignment: Record<string, string> = {}
  const rawAssignment = asRecord(request.assignment)
  for (const [name, version] of Object.entries(rawAssignment)) {
    assignment[String(name)] = String(version)
  }
  const failingCommands: RepairFailureCommand[] = []
  if (Array.isArray(request.failingCommands)) {
    for (const item of request.failingCommands) {
      const failure = asRecord(item)
      const command = asString(failure.command).trim()
      if (!command) continue
      const exitCode = typeof failure.exitCode === 'number' ? failure.exitCode
        : Number(String(failure.exitCode ?? '')) || 0
      failingCommands.push({ command, exitCode })
    }
  }
  return {
    requestId: asString(request.requestId),
    project: asString(request.project),
    mode: asString(request.mode),
    assignment,
    fingerprint,
    snapshotIdentity: asString(request.snapshotIdentity),
    failingCommands,
    diagnosticsTail: asString(request.diagnosticsTail),
    reason: asString(request.reason) || 'project',
    ...(typeof request.disposition === 'string' ? { disposition: request.disposition } : {}),
  }
}

export function parseRepairRequests(text: string): RepairRequest[] {
  let payload: unknown
  try {
    payload = JSON.parse(text || '{}')
  } catch {
    return []
  }
  const root = asRecord(payload)
  const requests = root.requests
  if (!Array.isArray(requests)) return []
  const result: RepairRequest[] = []
  for (const item of requests) {
    const parsed = parseRequest(item)
    if (parsed) result.push(parsed)
  }
  return result
}

export function repairHandoffPath(artifactsDir: string): string {
  return join(artifactsDir, 'repair-requests.json')
}

export function openRepairRequests(artifactsDir: string): RepairHandoffScan {
  const file = repairHandoffPath(artifactsDir)
  if (!existsSync(file)) return { runId: '', requests: [] }
  try {
    const text = readFileSync(file, 'utf8')
    let runId = ''
    try {
      const payload = asRecord(JSON.parse(text || '{}'))
      runId = asString(payload.runId)
    } catch {
      runId = ''
    }
    return { file, runId, requests: parseRepairRequests(text) }
  } catch {
    return { file, runId: '', requests: [] }
  }
}

export function findOpenRepairRequest(
  requests: readonly RepairRequest[],
  match: { project?: string; mode?: string; fingerprint?: string } = {},
): RepairRequest | undefined {
  for (const request of requests) {
    if (match.project !== undefined && request.project !== match.project) continue
    if (match.mode !== undefined && request.mode !== match.mode) continue
    if (match.fingerprint !== undefined && request.fingerprint !== match.fingerprint) continue
    return request
  }
  return undefined
}

// The durable repair-handoff file lives next to the Baseline producer's
// verification-progress state, under the workspace settings base:
//   <settings base>/.dependency-roadmap/state/repair-requests.json
// Desktop and producer MUST agree on this single relative contract.
export const repairHandoffStateRelativeDir = ['.dependency-roadmap', 'state']
export const repairHandoffStateRelativePath = [...repairHandoffStateRelativeDir, 'repair-requests.json'].join('/')

export function requestSummary(request: RepairRequest): string {
  const commands = request.failingCommands.length
    ? `; failing=[${request.failingCommands.map((item) => `${item.command}:${item.exitCode}`).join(', ')}]`
    : ''
  const pairs = Object.entries(request.assignment)
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
    .map(([name, version]) => `${name}@${version}`)
    .join(', ')
  return `${request.requestId}: ${request.project}/${request.mode} {${pairs}} ${commands}`
}
