import type { MigrationPlan } from './migration-progress.js'
import type { PlannerResult } from './planner-session.js'

const REFRESH_FAILURE = /\b(?:GROUP_SCOPE_DRIFT|GROUP_SCOPE_EMPTY|BATCH_SCOPE_DRIFT|EPHEMERAL_WORKTREE_STATE_STALE|DEPENDENCY_MATERIALIZATION_RECONCILE_REQUIRED|DEPENDENCY_MATERIALIZATION_CONFLICT|DEPENDENCY_MATERIALIZATION_POSTCONDITION|DEPENDENCY_COMPATIBILITY_EVIDENCE)\b/
const AMBIGUOUS_GIT_FAILURE = /\b(?:SCOPE_VIOLATION|EXTRA_BRANCH_NEEDS_SUPERVISOR|MERGE_RECOVERY|INTEGRATION_REPAIR|WRONG_BRANCH|TARGET_(?:GREEN_)?PLAN_INSUFFICIENT)\b/
const LOCAL_GROUP_FAILURE = /(?:\bbatch\b[^\n]*(?:не завершён|verification)|не прошла verification|все execution batch[^\n]*не подтверждена|parallel worker[^\n]*(?:не заверш|without ready)|PARALLEL_USER_WORKTREE_BLOCKED)/i

export type DeterministicPlannerDecision = {
  result: PlannerResult
  kind: 'refresh' | 'defer-cohort'
}

function mentionedBranches(plan: MigrationPlan, failure: string) {
  return plan.branches.filter((branch) => failure.includes(branch.branch))
}

// The model is an exception arbiter, not the default scheduler. Only decisions
// that are reversible and mechanically provable belong here: regenerate stale
// scope, or pause one explicitly named failed cohort while green siblings run.
// Topology/merge ambiguity and goal expansion still require independent
// reasoning plus the existing verifier.
export function deterministicPlannerDecision(
  plan: MigrationPlan,
  failure: string,
  options: { allowResidualPlanDeferral: boolean; ownedWorktree: boolean },
): DeterministicPlannerDecision | undefined {
  if (options.ownedWorktree || REFRESH_FAILURE.test(failure)) {
    return {
      kind: 'refresh',
      result: {
        status: 'refresh-plan',
        reason: options.ownedWorktree
          ? 'DepLoom owns the referenced temp worktree; regenerate residual state without model arbitration.'
          : /DEPENDENCY_COMPATIBILITY_EVIDENCE/.test(failure)
            ? 'Post-Executor dependency evidence must be reproduced/localized by deterministic Verifier and fed back to exact Z3; model arbitration is not version authority.'
            : 'Saved executor scope is mechanically stale; deterministic regeneration is sufficient.',
      },
    }
  }
  if (!options.allowResidualPlanDeferral || AMBIGUOUS_GIT_FAILURE.test(failure) || !LOCAL_GROUP_FAILURE.test(failure)) return undefined
  const branches = mentionedBranches(plan, failure)
  if (branches.length !== 1 || plan.branches.length < 2) return undefined
  const packages = [...new Set(branches[0].packages)]
  if (!packages.length) return undefined
  return {
    kind: 'defer-cohort',
    result: {
      status: 'defer-blockers',
      deferPackages: packages,
      reason: `Группа ${branches[0].branch} исчерпала собственные execution/repair attempts. Её cohort временно отложен без health-exclusion; сохранённый worktree остаётся partial evidence, зелёные siblings продолжаются.`,
    },
  }
}
