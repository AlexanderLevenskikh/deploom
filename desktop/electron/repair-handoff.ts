import { existsSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

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

function writeHandoff(file: string | undefined, requests: RepairRequest[], runId: string): void {
  if (!file) return
  const dir = dirname(file)
  if (!existsSync(dir)) return
  if (!requests.length) {
    if (existsSync(file)) rmSync(file)
    return
  }
  const payload: RepairHandoffFile = { schemaVersion: 1, runId, requests }
  const temp = `${file}.resolve-${process.pid}.tmp`
  writeFileSync(temp, JSON.stringify(payload, null, 2), 'utf8')
  renameSync(temp, file)
}

export type ResolveOutcome = {
  resolved: boolean
  remaining: RepairRequest[]
  file?: string
}

export function resolveRepairRequest(artifactsDir: string, requestId: string): ResolveOutcome {
  const scan = openRepairRequests(artifactsDir)
  if (!scan.file) return { resolved: false, remaining: [], file: undefined }
  const remaining = scan.requests.filter((request) => request.requestId !== requestId)
  const resolved = remaining.length !== scan.requests.length
  if (resolved) writeHandoff(scan.file, remaining, scan.runId)
  return { resolved, remaining, file: scan.file }
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

// R4: only a request whose FULL assignment is covered by a freshly verified
// cumulative state may be closed. A green pass over ANY subset of the project
// (wrong mode, unrelated packages) is not proof that another open request was
// fixed -- an unrelated batch must leave it untouched.
export function matchingRepairRequests(
  requests: readonly RepairRequest[],
  evidence: { project: string; mode: string; packages: readonly string[] },
): RepairRequest[] {
  const packageSet = new Set(evidence.packages)
  return requests.filter((request) => {
    if (request.project !== evidence.project) return false
    if (request.mode !== evidence.mode) return false
    const requestPackages = Object.keys(request.assignment)
    if (!requestPackages.length) return false
    return requestPackages.every((name) => packageSet.has(name))
  })
}

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
