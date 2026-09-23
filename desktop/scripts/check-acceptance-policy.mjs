import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import ts from 'typescript'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const desktop = path.resolve(here, '..')
const source = fs.readFileSync(path.join(desktop, 'electron', 'acceptance-policy.ts'), 'utf8')
const javascript = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
  fileName: 'acceptance-policy.ts',
  reportDiagnostics: true,
})
if (javascript.diagnostics?.length) throw new Error('acceptance-policy transpile failed')
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'deploom-acceptance-'))
const modulePath = path.join(tempRoot, 'acceptance-policy.mjs')
fs.writeFileSync(modulePath, javascript.outputText)
const { acceptanceVerdictFromManualAudit, dependencyInputIdentity, mergeTargetPolicy } = await import(pathToFileURL(modulePath).href)

const fixture = path.join(tempRoot, 'project')
fs.mkdirSync(fixture)
fs.writeFileSync(path.join(fixture, 'package.json'), '{"dependencies":{"a":"1.0.0"}}\n')
fs.writeFileSync(path.join(fixture, 'yarn.lock'), 'a@1.0.0:\n  version "1.0.0"\n')
const identity = dependencyInputIdentity(fixture)

// F1: a realistic audit producer payload is fully bound: a fresh input hash,
// a goal-policy snapshot and a measured lag-compliance share. Reports missing
// any of these are no longer ACCEPTED -- they are UNKNOWN.
const YELLOW80 = { targetLevel: 'yellow', minLagOkPct: 80, maxKnownCritical: 0, maxKnownHigh: 1, lagPolicyMonths: 12 }
const GREEN100 = { targetLevel: 'green', minLagOkPct: 100, maxKnownCritical: 0, maxKnownHigh: 0, maxKnownModerate: 0, maxKnownLow: 0, lagPolicyMonths: 12 }
const report = (critical, high, overrides = {}) => {
  const { audit: auditOverrides = {}, policy = YELLOW80, ...outerOverrides } = overrides
  return ({
  auditComplete: true,
  dependencyInputHash: identity.hash,
  dependencyInputFiles: identity.files,
  generatedAt: new Date(Date.now() - 60_000).toISOString(),
  // Passing policy: null omits the snapshot entirely (destructure default
  // would otherwise revive YELLOW80 for an explicit undefined).
  ...(policy === null ? {} : { policy }),
  audit: {
    complete: true,
    engine: 'yarn-inventory',
    trusted: true,
    canonicalInventory: { complete: true },
    packageTotals: { critical, high, moderate: 0, low: 0 },
    packageDetails: {
      ...(critical ? { danger: { severity: 'critical' } } : {}),
      ...(high ? { highdep: { severity: 'high' } } : {}),
    },
    lagOkPct: 100,
    lagOk: 1,
    lagTotal: 1,
    lagUnknown: 0,
    ...auditOverrides,
  },
  ...outerOverrides,
})
}

const accepted = acceptanceVerdictFromManualAudit(report(0, 1), identity.hash)
if (!accepted.accepted || accepted.status !== 'ACCEPTED' || accepted.highPackages[0] !== 'highdep') throw new Error('default C0/H1 with lag+policy binding must accept')
if (acceptanceVerdictFromManualAudit(report(1, 0), identity.hash).status !== 'REMEDIATION_REQUIRED') throw new Error('Critical must block')
if (acceptanceVerdictFromManualAudit(report(1, 0), identity.hash, { maxKnownCritical: 99, maxKnownHigh: 99 }).accepted) throw new Error('Critical=0 is a backend invariant and cannot be relaxed')
if (acceptanceVerdictFromManualAudit(report(0, 2), identity.hash).status !== 'REMEDIATION_REQUIRED') throw new Error('H2 must block default policy')
// A stricter H0 policy must block H1 as a KNOWN failure (REMEDIATION_REQUIRED),
// not as an evidence gap.
const strictH0 = acceptanceVerdictFromManualAudit(report(0, 1, { policy: { ...YELLOW80, maxKnownHigh: 0 } }), identity.hash, { ...YELLOW80, maxKnownHigh: 0 })
if (strictH0.status !== 'REMEDIATION_REQUIRED' || strictH0.evidenceComplete !== true) throw new Error(`stricter H0 policy must block H1 as remediation: ${JSON.stringify(strictH0)}`)
const oldAudit = report(0, 0, { generatedAt: '2020-01-01T00:00:00Z' })
if (acceptanceVerdictFromManualAudit(oldAudit, identity.hash).status !== 'UNKNOWN') throw new Error('stale vulnerability evidence must fail closed')
const futureAudit = report(0, 0, { generatedAt: new Date(Date.now() + 10 * 60_000).toISOString() })
if (acceptanceVerdictFromManualAudit(futureAudit, identity.hash).status !== 'UNKNOWN') throw new Error('future-dated vulnerability evidence beyond clock skew must fail closed')

const approximateBridge = report(0, 0, { audit: { engine: 'npm-lock-bridge', trusted: true, accuracy: 'npm-resolved-approximation', bridgeReconciliation: { faithful: false } } })
if (acceptanceVerdictFromManualAudit(approximateBridge, identity.hash).status !== 'UNKNOWN') throw new Error('approximate npm bridge must not become release authority')
const faithfulBridge = report(0, 0, { audit: { engine: 'npm-lock-bridge', trusted: true, accuracy: 'canonical-yarn-match', bridgeReconciliation: { faithful: true } } })
if (!acceptanceVerdictFromManualAudit(faithfulBridge, identity.hash).accepted) throw new Error('faithful canonical npm bridge should be eligible for acceptance')
if (acceptanceVerdictFromManualAudit(report(0, 0, { auditComplete: false }), identity.hash).status !== 'UNKNOWN') throw new Error('incomplete audit must fail closed')
if (acceptanceVerdictFromManualAudit({ auditComplete: true, audit: { complete: true, packageTotals: { critical: 0, high: 0 } } }, identity.hash).status !== 'UNKNOWN') throw new Error('legacy unbound audit must fail closed')

// F1: missing lag evidence is NOT a silent pass anymore -- it is UNKNOWN.
const missingLag = acceptanceVerdictFromManualAudit(report(0, 0, { audit: { lagOkPct: undefined } }), identity.hash)
if (missingLag.status !== 'UNKNOWN' || missingLag.evidenceComplete) throw new Error(`missing lag evidence must be UNKNOWN: ${JSON.stringify(missingLag)}`)
if (!missingLag.reasons.some((reason) => reason.includes('Lag-policy compliance evidence is missing'))) throw new Error('missing-lag reason must be explicit')

// F1: evidence produced under a different goal policy is not bound -> UNKNOWN.
const mismatchedPolicy = acceptanceVerdictFromManualAudit(report(0, 0, { policy: { ...YELLOW80, minLagOkPct: 80 } }), identity.hash, { ...YELLOW80, minLagOkPct: 90 })
if (mismatchedPolicy.status !== 'UNKNOWN' || mismatchedPolicy.accepted) throw new Error(`policy-mismatched audit must be UNKNOWN: ${JSON.stringify(mismatchedPolicy)}`)
const missingPolicy = acceptanceVerdictFromManualAudit(report(0, 0, { policy: null }), identity.hash)
if (missingPolicy.status !== 'UNKNOWN' || !missingPolicy.reasons.some((reason) => reason.includes('goal-policy snapshot'))) throw new Error(`missing report.policy must be UNKNOWN: ${JSON.stringify(missingPolicy)}`)

// F1: known failure 80 vs yellow90 -> REMEDIATION_REQUIRED, not UNKNOWN.
const yellow80Of90 = acceptanceVerdictFromManualAudit(
  report(0, 0, { policy: { ...YELLOW80, minLagOkPct: 90 }, audit: { lagOkPct: 80 } }),
  identity.hash,
  { ...YELLOW80, minLagOkPct: 90 },
)
if (yellow80Of90.status !== 'REMEDIATION_REQUIRED' || yellow80Of90.evidenceComplete !== true) throw new Error(`known 80<90 must be REMEDIATION_REQUIRED: ${JSON.stringify(yellow80Of90)}`)
const yellow80Of80 = acceptanceVerdictFromManualAudit(report(0, 0, { audit: { lagOkPct: 80 } }), identity.hash, YELLOW80)
if (yellow80Of80.status !== 'ACCEPTED') throw new Error('yellow80 with 80% compliance must accept')

// F1: green100 reaches acceptance only as green100/H0/M0/L0.
const greenAccepted = acceptanceVerdictFromManualAudit(report(0, 0, { policy: GREEN100, lagOkPct: 100 }), identity.hash, GREEN100)
if (greenAccepted.status !== 'ACCEPTED') throw new Error(`green100/H0 must accept: ${JSON.stringify(greenAccepted)}`)
const greenWithHigh = acceptanceVerdictFromManualAudit(report(0, 1, { policy: GREEN100, lagOkPct: 100 }), identity.hash, GREEN100)
if (greenWithHigh.status !== 'REMEDIATION_REQUIRED') throw new Error(`H1 at green must not be accepted: ${JSON.stringify(greenWithHigh)}`)
const greenPolicyAllowsHigh = acceptanceVerdictFromManualAudit(
  report(0, 1, { policy: { ...GREEN100, maxKnownHigh: 1 } }),
  identity.hash,
  { ...GREEN100, maxKnownHigh: 1 },
)
if (greenPolicyAllowsHigh.status !== 'REMEDIATION_REQUIRED' || !greenPolicyAllowsHigh.reasons.some((reason) => reason.includes('Green goal requires High=0'))) throw new Error(`green with High>0 in policy must not accept: ${JSON.stringify(greenPolicyAllowsHigh)}`)
const greenModerate = acceptanceVerdictFromManualAudit(
  report(0, 0, { policy: GREEN100, audit: { packageTotals: { critical: 0, high: 0, moderate: 1, low: 0 } } }),
  identity.hash,
  GREEN100,
)
if (greenModerate.status !== 'REMEDIATION_REQUIRED') throw new Error(`Moderate=1 must block green preset: ${JSON.stringify(greenModerate)}`)
// A green policy that demands numeric M/L limits needs the M/L totals; missing
// them is an evidence gap (UNKNOWN), not a passing gate.
const greenMissingML = acceptanceVerdictFromManualAudit(
  report(0, 0, { policy: GREEN100, audit: { packageTotals: { critical: 0, high: 0 } } }),
  identity.hash,
  GREEN100,
)
if (greenMissingML.status !== 'UNKNOWN' || !greenMissingML.reasons.some((reason) => reason.includes('Moderate totals are missing'))) {
  throw new Error(`missing Moderate totals under green preset must be UNKNOWN: ${JSON.stringify(greenMissingML)}`)
}

// F1: the single merge used by production normalizeBaselineIntent. A real
// UI-shaped payload (goal on the top level, limits nested) must reach the
// nested acceptancePolicy as the SAME green100/H0/M0/L0 policy the verdict
// enforces -- otherwise readAcceptanceVerdict would keep seeing the default.
const uiGreen = mergeTargetPolicy(
  { maxKnownCritical: 0, maxKnownHigh: 0, targetLevel: 'green', minLagOkPct: 100, maxKnownModerate: 0, maxKnownLow: 0, lagPolicyMonths: 12 },
  { targetLevel: 'green', minLagOkPct: 100, lagPolicyMonths: 12 },
)
if (uiGreen.targetLevel !== 'green' || uiGreen.minLagOkPct !== 100 || uiGreen.maxKnownHigh !== 0 || uiGreen.maxKnownLow !== 0 || uiGreen.lagPolicyMonths !== 12) {
  throw new Error(`UI-shaped green payload must merge to green100/H0/M0/L0: ${JSON.stringify(uiGreen)}`)
}
const uiGreenVerdict = acceptanceVerdictFromManualAudit(report(0, 0, { policy: uiGreen }), identity.hash, uiGreen)
if (uiGreenVerdict.status !== 'ACCEPTED') throw new Error(`UI green100 must reach acceptance as ACCEPTED: ${JSON.stringify(uiGreenVerdict)}`)
// Top-level goal wins over the (older) nested goal, with migration.
const topWins = mergeTargetPolicy(
  { maxKnownCritical: 0, maxKnownHigh: 1, targetLevel: 'green', minLagOkPct: 90 },
  { targetLevel: 'green', minLagOkPct: 100 },
)
if (topWins.targetLevel !== 'green' || topWins.minLagOkPct !== 100) throw new Error(`top-level goal must win the merge: ${JSON.stringify(topWins)}`)
// Legacy intent with the goal only inside acceptancePolicy migrates intact.
const legacyNested = mergeTargetPolicy({ maxKnownCritical: 0, maxKnownHigh: 1, targetLevel: 'green', minLagOkPct: 100 }, {})
if (legacyNested.targetLevel !== 'green' || legacyNested.minLagOkPct !== 100) throw new Error(`legacy nested policy must migrate: ${JSON.stringify(legacyNested)}`)
// 0 is a legitimate lag gate and must never be coerced back to 80.
const zeroGoal = mergeTargetPolicy({ maxKnownCritical: 0, maxKnownHigh: 1 }, { targetLevel: 'yellow', minLagOkPct: 0 })
if (zeroGoal.minLagOkPct !== 0) throw new Error(`0% lag gate must survive the merge: ${JSON.stringify(zeroGoal)}`)
// A green goal whose merged policy still allows High>0 must NOT be accepted.
const greenHighLegacy = mergeTargetPolicy({ maxKnownCritical: 0, maxKnownHigh: 1 }, { targetLevel: 'green', minLagOkPct: 100 })
const greenHighVerdict = acceptanceVerdictFromManualAudit(report(0, 1, { policy: { ...greenHighLegacy, maxKnownHigh: 1 } }), identity.hash, greenHighLegacy)
if (greenHighVerdict.accepted) throw new Error(`green with High>0 must never accept: ${JSON.stringify(greenHighVerdict)}`)

fs.writeFileSync(path.join(fixture, 'package.json'), '{"dependencies":{"a":"1.0.1"}}\n')
const changed = dependencyInputIdentity(fixture)
const stale = acceptanceVerdictFromManualAudit(report(0, 0), changed.hash)
if (stale.status !== 'UNKNOWN' || stale.dependencyEvidenceFresh) throw new Error('changed dependency inputs must invalidate audit authority')

fs.rmSync(tempRoot, { recursive: true, force: true })
console.log('Acceptance policy scenarios OK')
