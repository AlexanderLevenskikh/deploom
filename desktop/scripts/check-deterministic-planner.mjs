import { deterministicPlannerDecision } from '../dist-electron/deterministic-planner.js'

const plan = {
  project: 'Demo', baseBranch: 'CD-1', mergedBranch: 'CD-1-merged',
  branches: [
    { branch: 'CD-1-continuation-2', label: 'continuation-2', packages: ['eslint', '@typescript-eslint/parser'] },
    { branch: 'CD-1-auto-security-1', label: 'auto-security-1', packages: ['vite', 'vitest'] },
  ],
}
const localFailure = 'MIGRATION_REPLAN_REQUIRED: CD-1-continuation-2 не прошла verification после 3 repair-попыток.'
const local = deterministicPlannerDecision(plan, localFailure, { allowResidualPlanDeferral: true, ownedWorktree: false })
if (local?.kind !== 'defer-cohort' || local.result.deferPackages?.join(',') !== 'eslint,@typescript-eslint/parser') throw new Error(`Known failed cohort must skip the model: ${JSON.stringify(local)}`)
if (deterministicPlannerDecision(plan, localFailure, { allowResidualPlanDeferral: false, ownedWorktree: false })) throw new Error('Disabled deferral must preserve model arbitration')
const stale = deterministicPlannerDecision(plan, 'MIGRATION_REPLAN_REQUIRED: GROUP_SCOPE_DRIFT', { allowResidualPlanDeferral: true, ownedWorktree: false })
if (stale?.kind !== 'refresh' || stale.result.status !== 'refresh-plan') throw new Error(`Stale scope must refresh deterministically: ${JSON.stringify(stale)}`)
const evidence = deterministicPlannerDecision(plan, 'MIGRATION_REPLAN_REQUIRED: DEPENDENCY_COMPATIBILITY_EVIDENCE: evidenceFile=x', { allowResidualPlanDeferral: true, ownedWorktree: false })
if (evidence?.kind !== 'refresh' || !evidence.result.reason.includes('exact Z3')) throw new Error(`Dependency compatibility evidence must bypass LLM version arbitration: ${JSON.stringify(evidence)}`)
const reconcile = deterministicPlannerDecision(plan, 'MIGRATION_REPLAN_REQUIRED: DEPENDENCY_MATERIALIZATION_RECONCILE_REQUIRED: CD-1-continuation-2', { allowResidualPlanDeferral: true, ownedWorktree: false })
if (reconcile?.kind !== 'refresh') throw new Error(`Dirty dependency state must refresh deterministically: ${JSON.stringify(reconcile)}`)
const owned = deterministicPlannerDecision(plan, 'PARALLEL_USER_WORKTREE_BLOCKED', { allowResidualPlanDeferral: true, ownedWorktree: true })
if (owned?.kind !== 'refresh') throw new Error(`Owned worktree must refresh deterministically: ${JSON.stringify(owned)}`)
for (const ambiguous of [
  'MIGRATION_REPLAN_REQUIRED: MERGED_INTEGRATION_REPAIR_SCOPE_VIOLATION CD-1-continuation-2',
  'MIGRATION_REPLAN_REQUIRED: EXTRA_BRANCH_NEEDS_SUPERVISOR CD-1-continuation-2',
  'MIGRATION_REPLAN_REQUIRED: TARGET_PLAN_INSUFFICIENT CD-1-continuation-2',
  'MIGRATION_REPLAN_REQUIRED: unknown peer topology CD-1-continuation-2',
]) {
  if (deterministicPlannerDecision(plan, ambiguous, { allowResidualPlanDeferral: true, ownedWorktree: false })) throw new Error(`Ambiguous failure must reach the model: ${ambiguous}`)
}
console.log('Deterministic planner routing OK')
