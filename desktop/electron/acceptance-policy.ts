import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

export type AcceptancePolicy = {
  maxKnownCritical: number
  maxKnownHigh: number
}

export type AcceptanceVerdict = {
  status: 'ACCEPTED' | 'REMEDIATION_REQUIRED' | 'UNKNOWN'
  accepted: boolean
  evidenceComplete: boolean
  dependencyEvidenceFresh: boolean
  critical?: number
  high?: number
  criticalPackages: string[]
  highPackages: string[]
  auditGeneratedAt?: string
  auditEngine?: string
  reasons: string[]
  policy: AcceptancePolicy
}

export const DEFAULT_ACCEPTANCE_POLICY: AcceptancePolicy = Object.freeze({ maxKnownCritical: 0, maxKnownHigh: 1 })
// BLOCK_ACCEPTANCE_POLICY_CLOSURE_V1
export const MAX_ACCEPTANCE_AUDIT_AGE_MS = 24 * 60 * 60 * 1000
export const MAX_ACCEPTANCE_AUDIT_FUTURE_SKEW_MS = 5 * 60 * 1000

const DEPENDENCY_INPUT_FILES = ['package.json', 'yarn.lock', 'pnpm-lock.yaml', 'package-lock.json', 'npm-shrinkwrap.json'] as const

function count(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined
  return Math.max(0, Math.trunc(value))
}

function authoritativeAuditEvidence(audit: Record<string, unknown>): boolean {
  const engine = String(audit.engine ?? '').trim()
  if (engine === 'yarn-inventory') {
    const inventory = audit.canonicalInventory && typeof audit.canonicalInventory === 'object' ? audit.canonicalInventory as Record<string, unknown> : {}
    return audit.trusted === true && inventory.complete === true
  }
  if (engine === 'npm-lock-bridge') {
    const reconciliation = audit.bridgeReconciliation && typeof audit.bridgeReconciliation === 'object' ? audit.bridgeReconciliation as Record<string, unknown> : {}
    return audit.trusted === true && audit.accuracy === 'canonical-yarn-match' && reconciliation.faithful === true
  }
  if (engine === 'yarn-native' || engine === 'npm-native' || engine === 'pnpm-native') return true
  // Future engines must opt into release authority explicitly rather than
  // becoming authoritative merely because their output happened to parse.
  return audit.trusted === true && audit.authoritative === true
}

function boundedCount(value: unknown, fallback: number): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(0, Math.min(99, Math.trunc(parsed)))
}

export function normalizeAcceptancePolicy(value: unknown): AcceptancePolicy {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  return {
    // Critical is a product safety invariant, not a user-tunable tolerance.
    // Persisted legacy values are intentionally ignored.
    maxKnownCritical: 0,
    maxKnownHigh: boundedCount(raw.maxKnownHigh, DEFAULT_ACCEPTANCE_POLICY.maxKnownHigh),
  }
}

export function dependencyInputIdentity(projectPath: string): { hash: string; files: string[] } {
  const files = DEPENDENCY_INPUT_FILES.filter((name) => existsSync(join(projectPath, name)))
  const hash = createHash('sha256')
  for (const name of files) {
    hash.update(name, 'utf8')
    hash.update('\0', 'utf8')
    hash.update(readFileSync(join(projectPath, name)))
    hash.update('\0', 'utf8')
  }
  return { hash: hash.digest('hex'), files: [...files] }
}

export function acceptanceVerdictFromManualAudit(
  value: unknown,
  currentDependencyInputHash: string,
  policyValue?: unknown,
  nowMs = Date.now(),
): AcceptanceVerdict {
  const policy = normalizeAcceptancePolicy(policyValue)
  const report = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const audit = report.audit && typeof report.audit === 'object' ? report.audit as Record<string, unknown> : {}
  const packageTotals = audit.packageTotals && typeof audit.packageTotals === 'object' ? audit.packageTotals as Record<string, unknown> : {}
  const critical = count(packageTotals.critical)
  const high = count(packageTotals.high)
  const reportInputHash = typeof report.dependencyInputHash === 'string' ? report.dependencyInputHash.trim() : ''
  const auditComplete = report.auditComplete === true && audit.complete === true
  const auditAuthorityStrong = auditComplete && authoritativeAuditEvidence(audit)
  const packageDetails = audit.packageDetails && typeof audit.packageDetails === 'object' ? audit.packageDetails as Record<string, unknown> : {}
  const packages = audit.packages && typeof audit.packages === 'object' ? audit.packages as Record<string, unknown> : {}
  const severityPackages = (severity: 'critical' | 'high'): string[] => {
    const fromDetails = Object.entries(packageDetails).flatMap(([name, raw]) => {
      if (!raw || typeof raw !== 'object') return []
      return String((raw as Record<string, unknown>).severity ?? '').toLowerCase() === severity ? [name] : []
    })
    if (fromDetails.length) return [...new Set(fromDetails)].sort()
    return Object.entries(packages).flatMap(([name, raw]) => {
      if (!raw || typeof raw !== 'object') return []
      return count((raw as Record<string, unknown>)[severity]) && count((raw as Record<string, unknown>)[severity])! > 0 ? [name] : []
    }).sort()
  }
  const criticalPackages = severityPackages('critical')
  const highPackages = severityPackages('high')
  const dependencyEvidenceFresh = Boolean(reportInputHash && currentDependencyInputHash && reportInputHash === currentDependencyInputHash)
  const auditGeneratedAt = typeof report.generatedAt === 'string' ? report.generatedAt.trim() : ''
  const auditGeneratedAtMs = auditGeneratedAt ? Date.parse(auditGeneratedAt) : Number.NaN
  const reasons: string[] = []

  if (!auditComplete) reasons.push('Vulnerability audit evidence is incomplete.')
  else if (!auditAuthorityStrong) reasons.push('Vulnerability audit graph is not authoritative for release acceptance.')
  if (critical === undefined || high === undefined) reasons.push('Vulnerable-package Critical/High totals are unknown.')
  if (!reportInputHash) reasons.push('Audit report is not bound to dependencyInputHash.')
  else if (!dependencyEvidenceFresh) reasons.push('package.json/lockfile inputs changed after the audit.')
  if (!auditGeneratedAt) reasons.push('Audit report generatedAt is missing.')
  else if (!Number.isFinite(auditGeneratedAtMs)) reasons.push('Audit report generatedAt is invalid.')
  else if (auditGeneratedAtMs > nowMs + MAX_ACCEPTANCE_AUDIT_FUTURE_SKEW_MS) reasons.push('Audit report timestamp is too far in the future.')
  else if (nowMs - auditGeneratedAtMs > MAX_ACCEPTANCE_AUDIT_AGE_MS) reasons.push('Vulnerability audit evidence is older than 24 hours.')

  if (reasons.length) {
    return {
      status: 'UNKNOWN', accepted: false, evidenceComplete: false, dependencyEvidenceFresh,
      ...(critical !== undefined ? { critical } : {}), ...(high !== undefined ? { high } : {}),
      criticalPackages, highPackages,
      ...(auditGeneratedAt ? { auditGeneratedAt } : {}),
      ...(typeof audit.engine === 'string' ? { auditEngine: audit.engine } : {}),
      reasons, policy,
    }
  }

  if ((critical as number) > policy.maxKnownCritical) reasons.push(`Critical=${critical} exceeds ${policy.maxKnownCritical}.`)
  if ((high as number) > policy.maxKnownHigh) reasons.push(`High=${high} exceeds ${policy.maxKnownHigh}.`)
  return {
    status: reasons.length ? 'REMEDIATION_REQUIRED' : 'ACCEPTED',
    accepted: reasons.length === 0,
    evidenceComplete: true,
    dependencyEvidenceFresh: true,
    critical,
    high,
    criticalPackages,
    highPackages,
    ...(auditGeneratedAt ? { auditGeneratedAt } : {}),
    ...(typeof audit.engine === 'string' ? { auditEngine: audit.engine } : {}),
    reasons: reasons.length ? reasons : ['Complete fresh vulnerability evidence satisfies the configured acceptance policy.'],
    policy,
  }
}
