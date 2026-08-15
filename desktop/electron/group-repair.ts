import { scopeActionsFromPrompt } from './migration-progress.js'

export function buildGroupVerificationRepairPrompt(input: {
  projectName: string
  projectPath: string
  branch: string
  savedPromptPath: string
  fullGroupPrompt: string
  failure: string
  verificationCommands: readonly string[]
}): string {
  const actions = scopeActionsFromPrompt(input.fullGroupPrompt, input.projectName)
  const assignment = actions.map((row) => ({
    package: row.package,
    section: row.section,
    action: row.action,
    target: row.target,
  }))
  return `# Dependency migration — bounded group verification repair

Project: ${input.projectName}
Repository: ${input.projectPath}
Branch: ${input.branch}
Approved full plan (read only if a specific fact is needed): ${input.savedPromptPath}

## Immutable dependency assignment

The deterministic control plane already owns package-version decisions. The following direct assignment is immutable:

\`\`\`json
${JSON.stringify(assignment, null, 2)}
\`\`\`

Do **not** change direct targets, add/remove direct dependencies, edit overrides/resolutions to select another transitive version, or regenerate the global dependency plan. Do not revert package/lockfile changes that were materialized by the orchestrator merely because a package is absent from the source-file repair you make now.

If the failure can only be fixed by changing dependency assignment/resolution rather than project source/config, do not improvise a version repair. Write the normal branch checkpoint with \`migrationOutcome.status=replan-required\` and concise dependency evidence; the orchestrator will persist structured \`DEPENDENCY_COMPATIBILITY_EVIDENCE\`, reproduce it against the same source/config state with the pre-update dependency control, localize a minimal structural nogood, and only then return it to exact Z3. The repair agent never chooses another version.

## Verification failure

${input.failure.slice(-9000)}

Verification commands:
${input.verificationCommands.map((command) => `- ${command}`).join('\n') || '- none configured'}

## Goal

Make only the smallest source/config migration required by the immutable assignment, rerun the failing checks, update the existing branch evidence/docs without deleting earlier batch entries, commit the repair through the repository's migration skip-hooks wrapper, and leave ${input.branch} clean.

Do not perform broad dependency analysis or registry/version discovery. Read only files/evidence relevant to the diagnostics above; raw logs should stay in artifacts/state rather than being pasted into chat.
`
}
