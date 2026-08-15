export function buildMergeRecoveryPrompt(input: {
  projectName: string
  projectPath: string
  mergedBranch: string
  sourceBranch?: string
  conflictFiles: string[]
  savedPromptPath?: string
}): string {
  const conflicts = input.conflictFiles.length
    ? input.conflictFiles.map((file) => `- ${file}`).join('\n')
    : '- Read the unresolved paths from `git status --short` / `git diff --name-only --diff-filter=U`.'

  return `# Dependency migration — merge recovery

You are repairing an already-started Git merge for project ${input.projectName}.

Repository: ${input.projectPath}
Current/target merged branch: ${input.mergedBranch}
Incoming work branch: ${input.sourceBranch || 'determine from MERGE_MSG / Git state'}
${input.savedPromptPath ? `Approved dependency plan: ${input.savedPromptPath}` : ''}

## Unresolved paths seen by the orchestrator

${conflicts}

## Goal

Resolve the existing merge semantically and leave it ready for the orchestrator to commit.

## Hard rules

Editor/OS state (\`.idea/\`, \`.vs/\`, \`.vscode/\`, \`.fleet/\`, swap/user files, \`.DS_Store\`, \`Thumbs.db\`) is out of scope: do not modify, stash, commit, or treat it as a blocker.

1. Start by reading \`git status\`, \`git diff --name-only --diff-filter=U\`, \`MERGE_MSG\`, both conflict sides, and the approved dependency plan when provided.
2. Preserve the intended changes from all already-merged groups and the incoming group. The approved package targets are immutable: do not upgrade, downgrade, add, remove, or substitute dependency targets on your own.
3. Do not create/switch/delete branches. Do not abort the merge. Do not run \`git commit\`, \`git merge --continue\`, \`git rebase\`, push, or release commands.
4. Resolve conflict markers. For package manifests/lockfiles, reconcile the manifest first and regenerate the canonical lockfile with the repository's normal package manager when that is safer than hand-editing.
5. Keep changes limited to resolving the merge and package-required compatibility repairs exposed by that merge. No unrelated cleanup, formatting sweep, dependency refresh, or refactor.
6. Diagnose failures instead of stopping at the first error. Run the relevant existing build/typecheck/lint/test checks for the affected area, fix in-scope failures, and rerun them. Never bypass a failing check.
7. Stage the resolved files with \`git add\`, but leave the merge uncommitted. The orchestrator will verify the index and create the merge commit itself.
8. If a blocker cannot be safely resolved without changing the approved dependency scope, leave the repository in a diagnosable state and report \`MERGE_RECOVERY_BLOCKED: <reason>\`.

## Done means

- \`git diff --name-only --diff-filter=U\` is empty;
- no conflict markers remain in the staged diff;
- relevant existing checks pass, or a concrete scope-preserving blocker is reported;
- HEAD is still on ${input.mergedBranch};
- MERGE_HEAD still exists because you did not commit it.
`
}
