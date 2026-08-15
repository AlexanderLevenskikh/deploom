export type ExecutorBatchContext = {
  batchIndex: number
  batchCount: number
  packages: readonly string[]
  scopeHash: string
  scopeManifestPath: string
  roadmapPath?: string
  shell: 'windows-powershell' | 'posix'
  baselineBootstrapCommand?: string
}

function portablePath(value: string): string {
  return value.replace(/\\/g, '/')
}

export function baselineBootstrapCommandForLockfiles(input: {
  hasYarnLock?: boolean
  hasPackageLock?: boolean
  hasPnpmLock?: boolean
}): string | undefined {
  if (input.hasYarnLock) return 'yarn install --frozen-lockfile'
  if (input.hasPackageLock) return 'npm ci'
  if (input.hasPnpmLock) return 'pnpm install --frozen-lockfile'
  return undefined
}

function replaceLegacyScopeSaveRule(prompt: string, scopeManifestPath: string): string {
  const scopePath = portablePath(scopeManifestPath)
  const replacement = `1. Dependency Flow already materialized this exact compact manifest at \`${scopePath}\`. Do not create another *_scope.json for this batch. Verify the file/hash before edits and reference this exact path from checkpoint evidence.`
  return prompt
    .replace(
      /1\. Save the compact manifest below \*\*unchanged\*\* to `[^`]+`; validator supports `compact-v1`\./,
      replacement,
    )
    .replace(
      /1\. When run through Dependency Flow Desktop,[^\n]+validator supports `compact-v1`\./,
      replacement,
    )
}

export function injectExecutorBatchContext(prompt: string, context: ExecutorBatchContext): string {
  const batchNumber = context.batchIndex + 1
  const packageList = context.packages.join(', ')
  const shell = context.shell === 'windows-powershell' ? 'Windows PowerShell' : 'POSIX shell'
  const scopePath = portablePath(context.scopeManifestPath)
  const roadmapPath = context.roadmapPath ? portablePath(context.roadmapPath) : undefined
  const bootstrap = context.baselineBootstrapCommand
    ? `If a baseline comparison is genuinely required for a check that has no saved fact, bootstrap the CURRENT checkout first with \`${context.baselineBootstrapCommand}\` and run only that missing check before any target edit.`
    : 'If a baseline comparison is genuinely required for a check that has no saved fact, install the CURRENT manifest/lockfile in the package manager\'s immutable/frozen mode and run only that missing check before any target edit.'

  const contract = `## Dependency Flow executor contract — authoritative\n\n` +
    `- Batch: **${batchNumber}/${context.batchCount}**. Packages: **${packageList || '<none>'}**. Do not infer batch number, scope or continuation state from history files or older sessions.\n` +
    `- Scope hash: \`${context.scopeHash}\`. Exact scope manifest is already materialized at \`${scopePath}\`; do **not** create a duplicate scope file.\n` +
    `- Shell: **${shell}**. Use commands native to this shell; on Windows do not try grep/sed/awk/bash pipelines. Prefer direct file-reading tools when available.\n` +
    `${roadmapPath ? `- Release facts: \`${roadmapPath}\` is already filtered to this batch. Read each package entry once; do not scan the canonical full roadmap or history unless this batch file explicitly lacks a required fact.\n` : ''}` +
    `- Baseline facts produced by Dependency Flow solve-and-verify are authoritative when present in release/preflight evidence. Reuse them in checkpoint evidence instead of rerunning the same baseline checks. node_modules being absent in a Git worktree is expected.\n` +
    `- Do not invent a broad pre-update test/build pass merely because node_modules is absent. For a check without saved baseline evidence, either mark baseline as unknown and require its post-update run to pass, or capture a real current-state baseline only when comparison is necessary. ${bootstrap}\n` +
    `- **Never edit package.json and then call a later result “baseline”.** After the available current-state baseline facts are recorded, apply only the immutable targets in this batch, install, migrate minimally, and run post-update checks.\n` +
    `- Use the configured project registry/package-manager settings as given. Do not inspect .npmrc/yarn registry settings merely for reassurance; inspect them only when install/registry verification actually fails.\n\n`

  return `${contract}${replaceLegacyScopeSaveRule(prompt, context.scopeManifestPath)}`
}
