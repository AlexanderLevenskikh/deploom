export type AcceptancePolicy = {
  /** Maximum number of vulnerable package names with Critical severity. */
  maxKnownCritical: number
  /** Maximum number of vulnerable package names with High severity. */
  maxKnownHigh: number
}

export type AcceptanceAuditEvidence = {
  generatedAt?: string
  auditComplete: boolean
  dependencyInputHash?: string
  currentDependencyInputHash?: string
  critical?: number
  high?: number
  engine?: string
  /** True only when the audit graph is authoritative for the package-manager state. */
  authorityTrusted?: boolean
}

export type AcceptanceVerdict = {
  status: 'ACCEPTED' | 'REMEDIATION_REQUIRED' | 'UNKNOWN'
  accepted: boolean
  evidenceComplete: boolean
  dependencyEvidenceFresh: boolean
  critical?: number
  high?: number
  reasons: string[]
  policy: AcceptancePolicy
}

export const DEFAULT_ACCEPTANCE_POLICY: AcceptancePolicy = Object.freeze({
  maxKnownCritical: 0,
  maxKnownHigh: 1,
})

function boundedCount(value: unknown, fallback: number): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(0, Math.min(99, Math.trunc(parsed)))
}

export function normalizeAcceptancePolicy(value: Partial<AcceptancePolicy> | undefined): AcceptancePolicy {
  return {
    maxKnownCritical: boundedCount(value?.maxKnownCritical, DEFAULT_ACCEPTANCE_POLICY.maxKnownCritical),
    maxKnownHigh: boundedCount(value?.maxKnownHigh, DEFAULT_ACCEPTANCE_POLICY.maxKnownHigh),
  }
}

export function assessAcceptance(
  evidence: AcceptanceAuditEvidence | undefined,
  policyValue?: Partial<AcceptancePolicy>,
): AcceptanceVerdict {
  const policy = normalizeAcceptancePolicy(policyValue)
  const critical = typeof evidence?.critical === 'number' && Number.isFinite(evidence.critical) ? Math.max(0, Math.trunc(evidence.critical)) : undefined
  const high = typeof evidence?.high === 'number' && Number.isFinite(evidence.high) ? Math.max(0, Math.trunc(evidence.high)) : undefined
  const inputHash = String(evidence?.dependencyInputHash ?? '').trim()
  const currentInputHash = String(evidence?.currentDependencyInputHash ?? '').trim()
  const hashBound = Boolean(inputHash && currentInputHash)
  const dependencyEvidenceFresh = hashBound && inputHash === currentInputHash
  const reasons: string[] = []

  if (!evidence?.auditComplete) reasons.push('vulnerability audit is incomplete')
  else if (evidence.authorityTrusted !== true) reasons.push('vulnerability audit graph is not authoritative for release acceptance')
  if (critical === undefined || high === undefined) reasons.push('vulnerability package totals are unknown')
  if (!hashBound) reasons.push('audit is not bound to the current dependency inputs')
  else if (!dependencyEvidenceFresh) reasons.push('dependency inputs changed after the audit')

  if (reasons.length) {
    return {
      status: 'UNKNOWN', accepted: false, evidenceComplete: false, dependencyEvidenceFresh,
      ...(critical !== undefined ? { critical } : {}), ...(high !== undefined ? { high } : {}),
      reasons, policy,
    }
  }

  if ((critical as number) > policy.maxKnownCritical) {
    reasons.push(`Critical vulnerable packages ${(critical as number)} exceed allowed ${policy.maxKnownCritical}`)
  }
  if ((high as number) > policy.maxKnownHigh) {
    reasons.push(`High vulnerable packages ${(high as number)} exceed allowed ${policy.maxKnownHigh}`)
  }
  if (reasons.length) {
    return {
      status: 'REMEDIATION_REQUIRED', accepted: false, evidenceComplete: true, dependencyEvidenceFresh: true,
      critical, high, reasons, policy,
    }
  }
  return {
    status: 'ACCEPTED', accepted: true, evidenceComplete: true, dependencyEvidenceFresh: true,
    critical, high, reasons: ['vulnerability audit is complete, fresh, and within the configured acceptance policy'], policy,
  }
}

export function normalizeBudgetMinutes(value: unknown, fallback = 30): number {
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.max(5, Math.min(240, Math.round(parsed)))
}
