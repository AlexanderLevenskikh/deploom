import { autonomyPolicy, normalizedPlannerFailure } from '../dist-electron/autonomy-policy.js'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const defaults = autonomyPolicy({}, 'Demo')
if (defaults.maxPlannerRevisions !== 6 || defaults.maxIntegrationRepairAttempts !== 4 || defaults.allowBestEffortRelease !== true || defaults.allowResidualPlanDeferral !== true || defaults.allowSupervisorScopeExpansion !== true || defaults.maxParallelGroups !== 2 || defaults.autoDeferApprovalBlockers !== true || defaults.softStopOnAutonomyPlateau !== true) {
  throw new Error(`bad defaults ${JSON.stringify(defaults)}`)
}
const configured = autonomyPolicy({
  autonomy: { maxPlannerRevisions: 9, maxSamePlanRepeats: 2, maxParallelGroups: 4 },
  projects: [{ name: 'Demo', autonomy: { maxPlannerRevisions: 5, maxReleaseRecoveryAttempts: 7, allowResidualPlanDeferral: false, maxParallelGroups: 3, autoDeferApprovalBlockers: false } }],
}, 'Demo')
const eightWorkers = autonomyPolicy({ autonomy: { maxParallelGroups: 8 } }, 'Demo')
if (eightWorkers.maxParallelGroups !== 8) throw new Error(`Configured 8-worker concurrency was silently clamped: ${eightWorkers.maxParallelGroups}`)
if (configured.maxPlannerRevisions !== 5 || configured.maxSamePlanRepeats !== 2 || configured.maxReleaseRecoveryAttempts !== 7 || configured.allowResidualPlanDeferral !== false || configured.maxParallelGroups !== 3 || configured.autoDeferApprovalBlockers !== false) {
  throw new Error(`project override failed ${JSON.stringify(configured)}`)
}
if (normalizedPlannerFailure('abc 13fec0b 19.2.18') !== normalizedPlannerFailure('abc deadbee 18.2.0')) {
  throw new Error('planner failure normalization must ignore incidental sha/version churn')
}

const here = path.dirname(fileURLToPath(import.meta.url))
const main = fs.readFileSync(path.resolve(here, '..', 'electron', 'main.ts'), 'utf8')
for (const required of [
  "planner.status === 'expand-plan'",
  'applySupervisorScopeAdditions',
  'ACCEPTANCE_REMEDIATION_REQUIRED',
  'acceptanceRemediationMessage(acceptance)',
  "acceptance.status === 'UNKNOWN' || acceptance.accepted",
  "planner.status === 'blocked'",
]) {
  if (!main.includes(required)) throw new Error(`autonomous progressive-acceptance contract missing: ${required}`)
}
for (const forbidden of [
  'TARGET_PLAN_INSUFFICIENT',
  'planCanReachYellow === false',
]) {
  if (main.includes(forbidden)) throw new Error(`legacy health-target autonomy gate remains: ${forbidden}`)
}
console.log('Autonomy policy progressive-acceptance contract OK')
