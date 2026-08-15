export type RecoveryKind = 'agent' | 'hard' | 'infrastructure'

export type FlowRecoveryIssue = {
  code: string
  kind: RecoveryKind
  action: string
  message: string
  branch?: string
  updatedAt: string
}

const AGENT_RECOVERABLE_CODES = new Set([
  'MIGRATION_DONE_WORKTREE_NOT_CLEAN',
  'AGENT_GIT_PLAN_INCOMPLETE',
  'MERGE_RECOVERY_EXHAUSTED',
  'RELEASE_FINAL_GATE_FAILED',
  'RELEASE_FINAL_GATE_DIRTY',
  'RELEASE_COMMIT_OR_HOOK_FAILED',
  'RELEASE_SQUASH_FAILED',
  'MIGRATION_GROUP_VERIFICATION_FAILED',
  'MIGRATION_REPAIR_EXHAUSTED',
  'MIGRATION_GROUP_NOT_READY',
  'MIGRATION_GROUP_FAILED',
  'MIGRATION_REPLAN_REQUIRED',
  'MIGRATION_PLAN_APPROVAL_REQUIRED',
  'MIGRATION_REPLAN_BLOCKED',
  'MIGRATION_REPLAN_STALLED',
  'MIGRATION_REPLAN_EXHAUSTED',
  'MIGRATION_AUTONOMOUS_RECOVERY_STALLED',
  'RELEASE_RECOVERY_EXHAUSTED',
  'RELEASE_RECOVERY_NEEDS_MIGRATION_REPAIR',
  'MERGED_INTEGRATION_REPAIR_BLOCKED',
  'PLANNER_REPLAN_DIRTY',
  'MERGED_INTEGRATION_REPAIR_EXHAUSTED',
  'UNEXPECTED_BRANCHES',
  'RECOVERY_INPUT_MISSING',
  'BATCH_SCOPE_HASH_MISSING',
  'MERGE_RECOVERY_POSTCONDITION_FAILED',
  'RELEASE_SOURCE_CHECKOUT_MISMATCH',
  'RELEASE_TREE_MISMATCH',
  'RELEASE_AUDIT_WORKSPACE_STAGED',
  'RELEASE_AUDIT_WORKSPACE_COMMITTED',
  'RELEASE_EMPTY_SQUASH',
  'RELEASE_BRANCH_ALREADY_EXISTS',
  'RELEASE_CHECKOUT_DIRTY',
  'RELEASE_RECOVERY_BLOCKED',
  'PLANNER_SCOPE_VIOLATION',
  'AGENT_PROMPT_AUTOBUILD_FAILED',
  'MIGRATION_FINAL_VERIFICATION_WRONG_BRANCH',
  'PLANNER_REPLAN_UNSAFE',
  'PLANNER_REPLAN_INPUT_INVALID',
  'GROUP_ALIAS_PLAN_MISSING',
  'GROUP_ALIAS_REWRITE_FAILED',
  'AGENT_BRANCH_SCOPE_VIOLATION',
  'MERGED_INTEGRATION_REPAIR_DIRTY',
  'AGENT_CONTEXT_BUDGET_EXCEEDED',
])

const HARD_STOP_CODES = new Set([
  'MERGE_RECOVERY_UNSAFE_BRANCH',
  'MERGE_RECOVERY_SCOPE_VIOLATION',
  'RELEASE_SOURCE_COMMIT_MISMATCH',
  'RELEASE_MERGED_BRANCH_NOT_FOUND',
  'RELEASE_WORKSPACE_INVALID',
  'RELEASE_WORKSPACE_NOT_TOOL_MANAGED',
  'RELEASE_RECOVERY_UNSAFE_BRANCH',
  'RELEASE_RECOVERY_SCOPE_VIOLATION',
  'RELEASE_TO_MIGRATION_HANDOFF_UNSAFE',
  'BASELINE_PLAN_BROKEN',
])

const INFRASTRUCTURE_CODES = new Set([
  'OPENCODE_SERVER_START_FAILED',
  'OPENCODE_SQLITE_BUSY',
  'PROMPT_HARNESS_TIMEOUT',
  'MERGE_RECOVERY_GIT_STATE_FAILED',
  'RELEASE_GIT_COMMAND_FAILED',
  'RELEASE_SOURCE_REF_UNAVAILABLE',
  'RELEASE_PUSH_OR_HOOK_FAILED',
  'RELEASE_RECOVERY_GIT_STATE_FAILED',
  'MERGED_INTEGRATION_REPAIR_GIT_STATE_FAILED',
  'MERGED_VERIFICATION_GIT_STATE_FAILED',
  'RELEASE_TO_MIGRATION_HANDOFF_FAILED',
  'PLANNER_GIT_STATE_FAILED',
  'PLANNER_WORKTREE_FAILED',
  'PLANNER_RESULT_MISSING',
  'PLANNER_WORKSPACE_STATE_MISSING',
  'PLANNER_BASE_CHECKOUT_FAILED',
  'PLANNER_REGENERATION_FAILED',
  'PARALLEL_WORKER_BOOTSTRAP_FAILED',
  'INFRA_PACKAGE_MANAGER_NOT_FOUND',
  'BASELINE_VERIFY_INFRA_ERROR',
  'INFRA_PACKAGE_MATERIALIZATION_FAILED',
  'DEPENDENCY_MATERIALIZATION_FAILED',
  'DEPENDENCY_MATERIALIZATION_GIT_STATE_FAILED',
])

export function flowErrorCode(message: string): string {
  const explicit = /(?:^|\n)([A-Z][A-Z0-9_]{3,}):/.exec(message)
  if (explicit) return explicit[1]
  if (/созданы ветки вне Branch plan/i.test(message)) return 'UNEXPECTED_BRANCHES'
  if (/не удалось надёжно прочитать состояние Git|не удалось проверить изменения проекта перед запуском агента/i.test(message)) return 'GIT_STATE_UNTRUSTWORTHY'
  if (/выгрузите prompt|prompt.*не найден|Dashboard HTML не найден|не удалось прочитать Branch plan из сохранённого prompt/i.test(message)) return 'RECOVERY_INPUT_MISSING'
  return 'FLOW_COMMAND_FAILED'
}

export function classifyFlowRecovery(message: string, action: string): { code: string; kind: RecoveryKind } {
  // Infrastructure can be nested inside a migration/parallel wrapper. Prefer
  // the concrete infrastructure cause over the outer orchestration label so
  // an OpenCode transport/SQLite failure can never be mistaken for evidence
  // that the immutable dependency plan is wrong.
  const nestedInfrastructure = [...INFRASTRUCTURE_CODES].find((candidate) =>
    message.includes(`${candidate}:`)
  )
  if (nestedInfrastructure) return { code: nestedInfrastructure, kind: 'infrastructure' }
  if (/(?:database\s+is\s+locked|SQLITE_BUSY|SQLITE_LOCKED|database table is locked)/i.test(message)) {
    return { code: 'OPENCODE_SQLITE_BUSY', kind: 'infrastructure' }
  }

  const code = flowErrorCode(message)
  // Extra refs are evidence for the Supervisor, not a user-facing stop. They
  // cannot enter merged unless the deterministic orchestrator adopts/merges
  // them, so quarantine/replan internally first. Historical continuation refs
  // are especially common after residual replans.
  if (code === 'AGENT_GIT_PLAN_INCOMPLETE' && /ветки вне Branch plan/i.test(message)) {
    return { code: 'UNEXPECTED_BRANCHES', kind: 'agent' }
  }
  if (AGENT_RECOVERABLE_CODES.has(code)) return { code, kind: 'agent' }
  if (HARD_STOP_CODES.has(code)) return { code, kind: 'hard' }
  if (INFRASTRUCTURE_CODES.has(code) || code === 'GIT_STATE_UNTRUSTWORTHY') return { code, kind: 'infrastructure' }

  // A non-zero agent/release command with preserved Git state is useful
  // context for a human-requested recovery, but unknown failures in lifecycle
  // stages stay conservative.
  if (action === 'agent' || action === 'release') return { code, kind: 'agent' }
  return { code, kind: 'hard' }
}
