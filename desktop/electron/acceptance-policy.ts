import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

export type AcceptancePolicy = {
  maxKnownCritical: number
  maxKnownHigh: number
  // The goal criteria are part of the acceptance policy too, so a release
  // gate that claims "green / 100%" actually knows the goal it is enforcing.
  targetLevel?: 'yellow' | 'green'
  minLagOkPct?: number
  // Numeric Moderate/Low tolerances (F1): the green preset pins them at 0 so
  // "green" cannot silently tolerate known M/L findings without the gate
  // knowing about it. Optional: when absent, M/L is not a release criterion.
  maxKnownModerate?: number
  maxKnownLow?: number
  // The publish-date lag threshold (in months) the audit is bound to. Defaults
  // to 12 (the product's standard lag policy) at the use site.
  lagPolicyMonths?: number
}

export type AcceptanceVerdict = {
  status: 'ACCEPTED' | 'REMEDIATION_REQUIRED' | 'UNKNOWN'
  accepted: boolean
  evidenceComplete: boolean
  dependencyEvidenceFresh: boolean
  critical?: number
  high?: number
  moderate?: number
  low?: number
  // A02: unrated/unknown-severity vulnerable package names. Present and >0
  // means absence of Critical/High is NOT proven, so acceptance is impossible.
  unknown?: number
  criticalPackages: string[]
  highPackages: string[]
  moderatePackages?: string[]
  lowPackages?: string[]
  unknownPackages: string[]
  // A11: finding-level (advisory) counts are diagnostic, NOT the gate unit.
  // The gate unit is vulnerable package names (policy.maxKnown*); these remain
  // visible so a package with multiple findings is never confused with a
  // single-finding package.
  criticalFindings?: number
  highFindings?: number
  moderateFindings?: number
  lowFindings?: number
  unknownFindings?: number
  auditGeneratedAt?: string
  auditEngine?: string
  lagOkPct?: number
  lagOk?: number
  lagUnknown?: number
  lagTotal?: number
  reasons: string[]
  policy: AcceptancePolicy
}

export const DEFAULT_ACCEPTANCE_POLICY: AcceptancePolicy = Object.freeze({
  maxKnownCritical: 0,
  maxKnownHigh: 1,
  targetLevel: 'yellow',
  minLagOkPct: 80,
  lagPolicyMonths: 12,
})
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

function optionalBoundedCount(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return undefined
  return Math.max(0, Math.min(99, Math.trunc(parsed)))
}

export function normalizeAcceptancePolicy(value: unknown): AcceptancePolicy {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const rawLagPct = Number(raw.minLagOkPct)
  return {
    // Critical is a product safety invariant, not a user-tunable tolerance.
    // Persisted legacy values are intentionally ignored.
    maxKnownCritical: 0,
    maxKnownHigh: boundedCount(raw.maxKnownHigh, DEFAULT_ACCEPTANCE_POLICY.maxKnownHigh),
    targetLevel: raw.targetLevel === 'green' ? 'green' : 'yellow',
    // 0 is a legitimate user goal, hence Number.isFinite (not `|| 80`): a
    // zero lag gate must not be silently coerced back to the default.
    ...(raw.minLagOkPct !== undefined && raw.minLagOkPct !== null && Number.isFinite(rawLagPct)
      ? { minLagOkPct: Math.max(0, Math.min(100, Math.trunc(rawLagPct))) }
      : {}),
    ...(optionalBoundedCount(raw.maxKnownModerate) !== undefined ? { maxKnownModerate: optionalBoundedCount(raw.maxKnownModerate)! } : {}),
    ...(optionalBoundedCount(raw.maxKnownLow) !== undefined ? { maxKnownLow: optionalBoundedCount(raw.maxKnownLow)! } : {}),
    // Lag threshold is a bounded product choice (3/6/9/12 months); anything
    // else falls back to the product default instead of persisting garbage.
    ...(raw.lagPolicyMonths === 3 || raw.lagPolicyMonths === 6 || raw.lagPolicyMonths === 9 || raw.lagPolicyMonths === 12
      ? { lagPolicyMonths: raw.lagPolicyMonths as number }
      : {}),
  }
}

// F1: merge the flat top-level goal fields (the dialog's canonical storage for
// the target level / lag gate) into the nested acceptancePolicy, which is the
// object readAcceptanceVerdict actually enforces. This is THE single merge
// used by production normalizeBaselineIntent and by the cross-language smoke
// tests, so "UI green100 reaches acceptance as green100" is one function, not
// an accident of two call sites drifting apart. Legacy intents with the goal
// only inside acceptancePolicy (or only on the top level) migrate here.
export function mergeTargetPolicy(
  acceptancePolicyValue: unknown,
  topLevel: { targetLevel?: unknown; minLagOkPct?: unknown; lagPolicyMonths?: unknown } = {},
): AcceptancePolicy {
  const policy = normalizeAcceptancePolicy(acceptancePolicyValue)
  const rawLag = Number(topLevel.minLagOkPct)
  const goalLevel: 'yellow' | 'green' =
    topLevel.targetLevel === 'green' ? 'green' : topLevel.targetLevel === 'yellow' ? 'yellow' : (policy.targetLevel ?? 'yellow')
  const goalLagPct = Number.isFinite(rawLag)
    ? Math.max(0, Math.min(100, Math.trunc(rawLag)))
    : (policy.minLagOkPct ?? 80)
  const rawLagMonths = topLevel.lagPolicyMonths
  const goalLagMonths = rawLagMonths === 3 || rawLagMonths === 6 || rawLagMonths === 9 || rawLagMonths === 12
    ? rawLagMonths
    : (policy.lagPolicyMonths ?? 12)
  return {
    ...policy,
    targetLevel: goalLevel,
    minLagOkPct: goalLagPct,
    ...(goalLagMonths ? { lagPolicyMonths: goalLagMonths as number } : {}),
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
  const moderate = count(packageTotals.moderate)
  const low = count(packageTotals.low)
  const unknown = count(packageTotals.unknown)
  // A11: finding-level (advisory) totals stay diagnostic while the gate counts
  // vulnerable package names. packageTotals = affected package names;
  // advisoryTotals = unique advisory findings (a package may carry several).
  const advisoryTotals = audit.advisoryTotals && typeof audit.advisoryTotals === 'object' ? audit.advisoryTotals as Record<string, unknown> : {}
  const criticalFindings = count(advisoryTotals.critical)
  const highFindings = count(advisoryTotals.high)
  const moderateFindings = count(advisoryTotals.moderate)
  const lowFindings = count(advisoryTotals.low)
  const unknownFindings = count(advisoryTotals.unknown)
  // F1: the lag-policy compliance share is a mandatory acceptance criterion.
  // When the audit payload does not carry it (lagOkPct/compliancePct), the
  // report simply cannot prove the freshness goal, so no score is fabricated
  // and the verdict is UNKNOWN rather than a silent pass.
  const auditLagOkPct = typeof audit.lagOkPct === 'number' && Number.isFinite(audit.lagOkPct)
    ? Math.max(0, Math.min(100, audit.lagOkPct))
    : typeof audit.compliancePct === 'number' && Number.isFinite(audit.compliancePct)
      ? Math.max(0, Math.min(100, audit.compliancePct))
      : undefined
  const goalLagFloor = policy.targetLevel === 'green' ? 100 : (policy.minLagOkPct ?? 80)
  const reportInputHash = typeof report.dependencyInputHash === 'string' ? report.dependencyInputHash.trim() : ''
  const auditComplete = report.auditComplete === true && audit.complete === true
  const auditAuthorityStrong = auditComplete && authoritativeAuditEvidence(audit)
  const packageDetails = audit.packageDetails && typeof audit.packageDetails === 'object' ? audit.packageDetails as Record<string, unknown> : {}
  const packages = audit.packages && typeof audit.packages === 'object' ? audit.packages as Record<string, unknown> : {}
  const severityPackages = (severity: 'critical' | 'high' | 'moderate' | 'low' | 'unknown'): string[] => {
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
  const moderatePackages = severityPackages('moderate')
  const lowPackages = severityPackages('low')
  const unknownPackages = severityPackages('unknown')
  // A02: the producer counts unrated advisory records in packageTotals.unknown
  // and marks such packages with severity 'unknown'. When the total is absent
  // but unrated packages are enumerated, their count still proves presence.
  const effectiveUnknown = unknown !== undefined ? unknown : unknownPackages.length
  const dependencyEvidenceFresh = Boolean(reportInputHash && currentDependencyInputHash && reportInputHash === currentDependencyInputHash)
  const auditGeneratedAt = typeof report.generatedAt === 'string' ? report.generatedAt.trim() : ''
  const auditGeneratedAtMs = auditGeneratedAt ? Date.parse(auditGeneratedAt) : Number.NaN
  const lagOk = typeof audit.lagOk === 'number' && Number.isFinite(audit.lagOk) ? Math.max(0, Math.trunc(audit.lagOk)) : undefined
  const lagUnknown = typeof audit.lagUnknown === 'number' && Number.isFinite(audit.lagUnknown) ? Math.max(0, Math.trunc(audit.lagUnknown)) : undefined
  const lagTotal = typeof audit.lagTotal === 'number' && Number.isFinite(audit.lagTotal) ? Math.max(0, Math.trunc(audit.lagTotal)) : undefined

  // F1: the audit report must be bound to the exact goal policy it was
  // produced under (same dependency inputs via hash AND same target/lag/High
  // limits). Without a matching policy snapshot the lag+security evidence
  // cannot vouch for the current goal, even when it is fresh and complete.
  // A13: the snapshot is validated STRICTLY (types, ranges, enums) before any
  // comparison. An invalid value is never silently parsed into "absent" and
  // replaced by current defaults -- malformed evidence fails closed.
  const reportPolicy = report.policy && typeof report.policy === 'object' ? report.policy as Record<string, unknown> : undefined
  const policyMismatch: string[] = []
  const policySchemaGaps: string[] = []
  // Returns the parsed number, or undefined when absent/invalid. A gap is
  // recorded for REQUIRED fields so the verdict fails closed instead of
  // comparing nothing; optional fields (moderate/low limits) are only
  // validated when the current goal policy actually demands a numeric limit.
  const strictBounded = (field: string, min: number, max: number, label: string, required: boolean): number | undefined => {
    const raw = reportPolicy![field]
    if (raw === undefined || raw === null || raw === '') { if (required) policySchemaGaps.push(label); return undefined }
    const parsed = Number(raw)
    if (!Number.isFinite(parsed) || parsed < min || parsed > max) { policySchemaGaps.push(`invalid ${label}`); return undefined }
    return Math.trunc(parsed)
  }
  if (reportPolicy) {
    const reportTarget = reportPolicy.targetLevel
    if (reportTarget !== 'green' && reportTarget !== 'yellow') {
      policySchemaGaps.push(reportTarget === undefined || reportTarget === null || reportTarget === '' ? 'targetLevel' : 'invalid targetLevel')
    } else {
      if (reportTarget !== (policy.targetLevel ?? 'yellow')) policyMismatch.push(`targetLevel=${reportTarget}`)
    }
    const reportLag = strictBounded('minLagOkPct', 0, 100, 'minLagOkPct', true)
    if (reportLag !== undefined && reportLag !== (policy.minLagOkPct ?? 80)) policyMismatch.push(`minLagOkPct=${reportLag}`)
    const reportLagMonths = strictBounded('lagPolicyMonths', 3, 12, 'lagPolicyMonths', true)
    if (reportLagMonths !== undefined && ![3, 6, 9, 12].includes(reportLagMonths)) policySchemaGaps.push('invalid lagPolicyMonths')
    else if (reportLagMonths !== undefined && reportLagMonths !== (policy.lagPolicyMonths ?? 12)) policyMismatch.push(`lagPolicyMonths=${reportLagMonths}`)
    // A13: maxKnownCritical must be a valid bounded number AND equal the
    // enforced 0 — the Critical tolerance is a product invariant, never
    // "present but different".
    const reportCritical = strictBounded('maxKnownCritical', 0, 99, 'maxKnownCritical', true)
    if (reportCritical !== undefined && reportCritical !== policy.maxKnownCritical) policyMismatch.push(`maxKnownCritical=${reportCritical}`)
    const reportHigh = strictBounded('maxKnownHigh', 0, 99, 'maxKnownHigh', true)
    if (reportHigh !== undefined && reportHigh !== policy.maxKnownHigh) policyMismatch.push(`maxKnownHigh=${reportHigh}`)
    const reportModerate = strictBounded('maxKnownModerate', 0, 99, 'maxKnownModerate', policy.maxKnownModerate !== undefined)
    if (reportModerate !== undefined && reportModerate !== policy.maxKnownModerate) policyMismatch.push(`maxKnownModerate=${reportModerate}`)
    const reportLow = strictBounded('maxKnownLow', 0, 99, 'maxKnownLow', policy.maxKnownLow !== undefined)
    if (reportLow !== undefined && reportLow !== policy.maxKnownLow) policyMismatch.push(`maxKnownLow=${reportLow}`)
  }

  // Missing or weak evidence -> UNKNOWN (fail closed, nothing fabricated).
  const unverifiable: string[] = []
  if (!auditComplete) unverifiable.push('Vulnerability audit evidence is incomplete.')
  else if (!auditAuthorityStrong) unverifiable.push('Vulnerability audit graph is not authoritative for release acceptance.')
  if (critical === undefined || high === undefined) unverifiable.push('Vulnerable-package Critical/High totals are unknown.')
  // A02: unrated/unknown-severity advisories make "no Critical/High" unproven.
  // A missing unknown total is a missing count, not a proven zero; both fail
  // closed. A proven U=0 report still accepts.
  if (unknown === undefined && unknownPackages.length === 0) unverifiable.push('Unknown-severity (unrated) vulnerability total is missing from the audit report.')
  else if (effectiveUnknown > 0) unverifiable.push(`Unrated/unknown-severity vulnerabilities present (${effectiveUnknown}): absence of Critical/High is not proven.`)
  if (!reportInputHash) unverifiable.push('Audit report is not bound to dependencyInputHash.')
  else if (!dependencyEvidenceFresh) unverifiable.push('package.json/lockfile inputs changed after the audit.')
  if (!auditGeneratedAt) unverifiable.push('Audit report generatedAt is missing.')
  else if (!Number.isFinite(auditGeneratedAtMs)) unverifiable.push('Audit report generatedAt is invalid.')
  else if (auditGeneratedAtMs > nowMs + MAX_ACCEPTANCE_AUDIT_FUTURE_SKEW_MS) unverifiable.push('Audit report timestamp is too far in the future.')
  else if (nowMs - auditGeneratedAtMs > MAX_ACCEPTANCE_AUDIT_AGE_MS) unverifiable.push('Vulnerability audit evidence is older than 24 hours.')
  if (auditLagOkPct === undefined) unverifiable.push('Lag-policy compliance evidence is missing from the audit report (lagOkPct/compliancePct).')
  if (!reportPolicy) unverifiable.push('Audit report has no goal-policy snapshot (report.policy) to bind the evidence to.')
  else if (policyMismatch.length) unverifiable.push(`Audit report was produced under a different goal policy (${policyMismatch.join(', ')}).`)
  else if (policySchemaGaps.length) unverifiable.push(`Audit report policy snapshot is incomplete (missing ${policySchemaGaps.join(', ')}) and cannot be bound to the current goal.`)
  if (policy.maxKnownModerate !== undefined && moderate === undefined) unverifiable.push('Moderate totals are missing from the audit report (policy demands a numeric Moderate limit).')
  if (policy.maxKnownLow !== undefined && low === undefined) unverifiable.push('Low totals are missing from the audit report (policy demands a numeric Low limit).')

  if (unverifiable.length) {
    return {
      status: 'UNKNOWN', accepted: false, evidenceComplete: false, dependencyEvidenceFresh,
      ...(critical !== undefined ? { critical } : {}), ...(high !== undefined ? { high } : {}),
      ...(moderate !== undefined ? { moderate } : {}), ...(low !== undefined ? { low } : {}),
      ...(effectiveUnknown > 0 ? { unknown: effectiveUnknown } : {}),
      criticalPackages, highPackages,
      ...(moderatePackages.length ? { moderatePackages } : {}), ...(lowPackages.length ? { lowPackages } : {}),
      unknownPackages,
      ...(criticalFindings !== undefined ? { criticalFindings } : {}), ...(highFindings !== undefined ? { highFindings } : {}),
      ...(moderateFindings !== undefined ? { moderateFindings } : {}), ...(lowFindings !== undefined ? { lowFindings } : {}),
      ...(unknownFindings !== undefined ? { unknownFindings } : {}),
      ...(auditGeneratedAt ? { auditGeneratedAt } : {}),
      ...(typeof audit.engine === 'string' ? { auditEngine: audit.engine } : {}),
      ...(auditLagOkPct !== undefined ? { lagOkPct: auditLagOkPct } : {}),
      ...(lagOk !== undefined ? { lagOk } : {}), ...(lagUnknown !== undefined ? { lagUnknown } : {}), ...(lagTotal !== undefined ? { lagTotal } : {}),
      reasons: unverifiable, policy,
    }
  }

  // Known, verifiable failures of the configured goal -> REMEDIATION_REQUIRED.
  const gates: string[] = []
  if (policy.targetLevel === 'green' && (policy.maxKnownHigh ?? 1) > 0) {
    gates.push(`Green goal requires High=0 but the policy allows High>${policy.maxKnownHigh}.`)
  }
  if ((critical as number) > policy.maxKnownCritical) gates.push(`Critical=${critical} exceeds ${policy.maxKnownCritical}.`)
  if ((high as number) > policy.maxKnownHigh) gates.push(`High=${high} exceeds ${policy.maxKnownHigh}.`)
  if (policy.maxKnownModerate !== undefined && moderate !== undefined && moderate > policy.maxKnownModerate) {
    gates.push(`Moderate=${moderate} exceeds ${policy.maxKnownModerate}.`)
  }
  if (policy.maxKnownLow !== undefined && low !== undefined && low > policy.maxKnownLow) {
    gates.push(`Low=${low} exceeds ${policy.maxKnownLow}.`)
  }
  if (auditLagOkPct !== undefined && auditLagOkPct < goalLagFloor) {
    gates.push(`Lag-policy compliance ${auditLagOkPct}% is below the target gate ${goalLagFloor}% (${policy.targetLevel}).`)
  }

  if (gates.length) {
    return {
      status: 'REMEDIATION_REQUIRED', accepted: false, evidenceComplete: true, dependencyEvidenceFresh: true,
      critical, high,
      ...(moderate !== undefined ? { moderate } : {}), ...(low !== undefined ? { low } : {}),
      criticalPackages, highPackages,
      ...(moderatePackages.length ? { moderatePackages } : {}), ...(lowPackages.length ? { lowPackages } : {}),
      unknownPackages,
      ...(criticalFindings !== undefined ? { criticalFindings } : {}), ...(highFindings !== undefined ? { highFindings } : {}),
      ...(moderateFindings !== undefined ? { moderateFindings } : {}), ...(lowFindings !== undefined ? { lowFindings } : {}),
      ...(unknownFindings !== undefined ? { unknownFindings } : {}),
      ...(auditGeneratedAt ? { auditGeneratedAt } : {}),
      ...(typeof audit.engine === 'string' ? { auditEngine: audit.engine } : {}),
      ...(auditLagOkPct !== undefined ? { lagOkPct: auditLagOkPct } : {}),
      ...(lagOk !== undefined ? { lagOk } : {}), ...(lagUnknown !== undefined ? { lagUnknown } : {}), ...(lagTotal !== undefined ? { lagTotal } : {}),
      reasons: gates, policy,
    }
  }
  return {
    status: 'ACCEPTED', accepted: true, evidenceComplete: true, dependencyEvidenceFresh: true,
    critical, high,
    ...(moderate !== undefined ? { moderate } : {}), ...(low !== undefined ? { low } : {}),
    criticalPackages, highPackages,
    ...(moderatePackages.length ? { moderatePackages } : {}), ...(lowPackages.length ? { lowPackages } : {}),
    unknownPackages,
    ...(criticalFindings !== undefined ? { criticalFindings } : {}), ...(highFindings !== undefined ? { highFindings } : {}),
    ...(moderateFindings !== undefined ? { moderateFindings } : {}), ...(lowFindings !== undefined ? { lowFindings } : {}),
    ...(unknownFindings !== undefined ? { unknownFindings } : {}),
    ...(auditGeneratedAt ? { auditGeneratedAt } : {}),
    ...(typeof audit.engine === 'string' ? { auditEngine: audit.engine } : {}),
    ...(auditLagOkPct !== undefined ? { lagOkPct: auditLagOkPct } : {}),
    ...(lagOk !== undefined ? { lagOk } : {}), ...(lagUnknown !== undefined ? { lagUnknown } : {}), ...(lagTotal !== undefined ? { lagTotal } : {}),
    reasons: ['Complete fresh vulnerability and lag evidence satisfies the configured acceptance policy.'],
    policy,
  }
}
