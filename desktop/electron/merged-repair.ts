import { existsSync, readFileSync } from 'node:fs'

export type MergedRepairResult = {
  status: 'repaired' | 'replan-required' | 'blocked'
  reason: string
}

export function readMergedRepairResult(path: string): MergedRepairResult | undefined {
  if (!existsSync(path)) return undefined
  try {
    const value = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
    const status = String(value.status ?? '').trim().toLowerCase()
    const reason = typeof value.reason === 'string' ? value.reason.trim() : ''
    if (!reason) return undefined
    if (status === 'repaired' || status === 'replan-required' || status === 'blocked') return { status, reason } as MergedRepairResult
  } catch { /* caller handles a missing/malformed result conservatively */ }
  return undefined
}

export function buildMergedRepairPrompt(input: {
  projectName: string
  projectPath: string
  mergedBranch: string
  savedPromptPath: string
  resultPath: string
  failure: string
  verificationCommands: string[]
}): string {
  return `# Dependency migration — merged integration repair

Project: ${input.projectName}
Repository: ${input.projectPath}
Merged branch: ${input.mergedBranch}
Approved dependency plan: ${input.savedPromptPath}
Machine result: ${input.resultPath}

## Why you are here

The orchestrator merged an approved work branch and then independently ran project verification. The cumulative merged tree is red:

${input.failure}

Verification commands:
${input.verificationCommands.map((command) => `- ${command}`).join('\n') || '- none configured'}

## Goal

Repair the cumulative dependency migration on ${input.mergedBranch} now, before release. Diagnose the failing checks, make the smallest migration-required compatibility fix, rerun the failing checks, and leave a clean committed merged branch.

## Hard rules

1. Stay on ${input.mergedBranch}. Do not create/switch/delete branches, merge, rebase, reset, stash, push, or run the release tool.
2. Read the saved prompt/manifest and current migration docs before editing. Direct dependency targets/actions are immutable. Do not silently upgrade/downgrade/remove/substitute a planned package and do not activate an excluded/deferred package.
3. You MAY make minimal source/config compatibility changes that are necessary for the already-approved dependency targets. Dependency selection belongs to the deterministic control plane: do not add/remove direct dependencies, change an approved target, edit overrides/resolutions to force another transitive version, or otherwise repair the dependency graph yourself.
4. If evidence shows that passing verification requires a different direct/transitive dependency assignment, an excluded/deferred direct dependency, or a broader product refactor, do not fake a green build. Return replan-required with concise dependency evidence; the orchestrator will treat that as dependency evidence for deterministic verification/re-solving rather than permission for the repair agent to choose versions. If the cumulative failure cannot yet be localized safely, it must remain an explicit replan blocker rather than become an unproven solver clause.
5. Do not suppress checks, weaken tsconfig/lint rules, skip hooks, delete tests, or blanket-format unrelated files merely to make the gate green.
6. Run the failing verification commands after the fix and update cumulative migration docs/review notes with the actual root cause and evidence.
7. Commit a successful integration repair through the repository's dependency tool skip-hooks wrapper used for intermediate migration commits. Leave HEAD on ${input.mergedBranch} and the worktree clean.
8. Before finishing, write exactly one JSON object to ${input.resultPath} (no markdown):
   - {"status":"repaired","reason":"..."} only after relevant verification is green and the repair is committed;
   - {"status":"replan-required","reason":"..."} if the immutable Branch plan/scope itself is incompatible;
   - {"status":"blocked","reason":"..."} if safe diagnosis/repair cannot continue without ambiguity or destructive action.

Human chat text is diagnostic only. The orchestrator decides from the JSON result and then reruns verification itself.
`
}
