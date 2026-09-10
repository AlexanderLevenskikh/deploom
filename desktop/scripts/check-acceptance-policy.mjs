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
const { acceptanceVerdictFromManualAudit, dependencyInputIdentity } = await import(pathToFileURL(modulePath).href)

const fixture = path.join(tempRoot, 'project')
fs.mkdirSync(fixture)
fs.writeFileSync(path.join(fixture, 'package.json'), '{"dependencies":{"a":"1.0.0"}}\n')
fs.writeFileSync(path.join(fixture, 'yarn.lock'), 'a@1.0.0:\n  version "1.0.0"\n')
const identity = dependencyInputIdentity(fixture)
const report = (critical, high, overrides = {}) => {
  const { audit: auditOverrides = {}, ...outerOverrides } = overrides
  return ({
  auditComplete: true,
  dependencyInputHash: identity.hash,
  dependencyInputFiles: identity.files,
  generatedAt: new Date(Date.now() - 60_000).toISOString(),
  audit: {
    complete: true,
    engine: 'yarn-inventory',
    trusted: true,
    canonicalInventory: { complete: true },
    packageTotals: { critical, high },
    packageDetails: {
      ...(critical ? { danger: { severity: 'critical' } } : {}),
      ...(high ? { highdep: { severity: 'high' } } : {}),
    },
    ...auditOverrides,
  },
  ...outerOverrides,
})
}

const accepted = acceptanceVerdictFromManualAudit(report(0, 1), identity.hash)
if (!accepted.accepted || accepted.status !== 'ACCEPTED' || accepted.highPackages[0] !== 'highdep') throw new Error('default C0/H1 must accept')
if (acceptanceVerdictFromManualAudit(report(1, 0), identity.hash).status !== 'REMEDIATION_REQUIRED') throw new Error('Critical must block')
if (acceptanceVerdictFromManualAudit(report(1, 0), identity.hash, { maxKnownCritical: 99, maxKnownHigh: 99 }).accepted) throw new Error('Critical=0 is a backend invariant and cannot be relaxed')
if (acceptanceVerdictFromManualAudit(report(0, 2), identity.hash).status !== 'REMEDIATION_REQUIRED') throw new Error('H2 must block default policy')
if (acceptanceVerdictFromManualAudit(report(0, 1), identity.hash, { maxKnownCritical: 0, maxKnownHigh: 0 }).accepted) throw new Error('stricter H0 policy must block H1')
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

fs.writeFileSync(path.join(fixture, 'package.json'), '{"dependencies":{"a":"1.0.1"}}\n')
const changed = dependencyInputIdentity(fixture)
const stale = acceptanceVerdictFromManualAudit(report(0, 0), changed.hash)
if (stale.status !== 'UNKNOWN' || stale.dependencyEvidenceFresh) throw new Error('changed dependency inputs must invalidate audit authority')

fs.rmSync(tempRoot, { recursive: true, force: true })
console.log('Acceptance policy scenarios OK')
