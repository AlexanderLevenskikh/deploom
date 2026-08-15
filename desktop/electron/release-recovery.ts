import { existsSync, readFileSync } from 'node:fs'

export type ReleaseRecoveryResult = {
  status: 'repaired' | 'migration-repair-required' | 'blocked'
  reason: string
}

export function readReleaseRecoveryResult(path: string): ReleaseRecoveryResult | undefined {
  if (!existsSync(path)) return undefined
  try {
    const value = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
    const status = String(value.status ?? '').trim().toLowerCase()
    const reason = typeof value.reason === 'string' ? value.reason.trim() : ''
    if (!reason) return undefined
    if (status === 'repaired' || status === 'migration-repair-required' || status === 'blocked') {
      return { status, reason } as ReleaseRecoveryResult
    }
  } catch { /* malformed result is treated as missing and legacy text fallback may still work */ }
  return undefined
}

export function buildReleaseRecoveryPrompt(input: {
  projectName: string
  projectPath: string
  releaseBranch: string
  mergedBranch: string
  savedPromptPath?: string
  resultPath: string
  failure?: string
  userNote: string
}): string {
  return `# Dependency migration — release recovery

You are repairing a prepared release branch after the dependency migration itself has already completed.

Project: ${input.projectName}
Repository: ${input.projectPath}
Release branch: ${input.releaseBranch}
Verified merged branch: ${input.mergedBranch}
${input.savedPromptPath ? `Approved dependency plan: ${input.savedPromptPath}` : ''}
Machine result: ${input.resultPath}
${input.failure ? `Previous FLOW failure: ${input.failure}` : ''}

## User instruction

${input.userNote}

## Goal

Diagnose and repair the preserved release worktree so the existing release command can be safely retried with repository hooks enabled.

## Hard rules

Editor/OS state (\`.idea/\`, \`.vs/\`, \`.vscode/\`, \`.fleet/\`, swap/user files, \`.DS_Store\`, \`Thumbs.db\`) is out of scope: do not modify, stash, commit, or treat it as a blocker.

1. Start with \`git status --short\`, \`git diff\`, and \`git diff --cached\`. Read hook/gate configuration and the previous failure before changing anything.
2. Stay on ${input.releaseBranch}. Do not switch/create/delete branches, reset, stash, rebase, merge, push, or run a release command.
3. Do not run \`git commit\` or amend. The orchestrator owns the single final release commit and will retry it after you finish.
4. Preserve the approved dependency targets and the content already integrated into ${input.mergedBranch}. No dependency refresh, target substitution, unrelated refactor, cleanup, formatting sweep, or test rewrite.
5. The staged release tree must remain equivalent to the already verified ${input.mergedBranch} (apart from tool-managed release docs/audit cleanup). Do not implement a new source/config compatibility fix only on the release branch.
6. Run the relevant existing checks/hook command(s). You may repair/re-stage generated or already-approved release content when that preserves the merged tree. Never bypass hooks or checks.
7. Stage every intended release repair with \`git add\`. Leave the branch ready for a normal commit, but uncommitted.
8. If the failure proves that source/config code in ${input.mergedBranch} itself needs a new fix, do not create a release-only fix. If safe repair otherwise requires changing approved scope or discarding ambiguous work, stop without destructive commands.
9. Before finishing, write exactly one JSON object to \`${input.resultPath}\` (no markdown) using one of these statuses:
   - \`{"status":"repaired","reason":"..."}\` only when the release worktree itself was safely repaired and is ready for the orchestrator to retry the release command;
   - \`{"status":"migration-repair-required","reason":"..."}\` when the defect is inherent to ${input.mergedBranch} and requires migration/source/config repair upstream;
   - \`{"status":"blocked","reason":"..."}\` when safe recovery would violate the approved scope or discard ambiguous work.
   This machine result is mandatory. Human prose in chat is diagnostic only; the orchestrator makes its decision from this JSON.

## Done means

- HEAD is still on ${input.releaseBranch};
- the intended release changes are staged;
- there are no unexplained unstaged/untracked changes;
- relevant checks pass or the machine result explicitly reports migration repair / blocked;
- no commit/push/reset/stash was performed;
- ${input.resultPath} contains the final machine-readable recovery result.
`
}
