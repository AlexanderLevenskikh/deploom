import { baselineBootstrapCommandForLockfiles, injectExecutorBatchContext } from '../dist-electron/executor-context.js'

const legacyPrompt = `# Dependency roadmap — compact autonomous task

Roadmap facts: C:\\facts\\batch-roadmap.json

## Rules

1. Save the compact manifest below **unchanged** to \`C:\\history\\runs\\<timestamp>_Demo_scope.json\`; validator supports \`compact-v1\`.
8. For each branch: baseline → install → checks.

## Exact compact scope manifest

\`\`\`json
{"schemaVersion":2,"rows":[]}
\`\`\`
`
const scopePath = 'C:\\state\\group-prompts\\Demo-branch-batch-1-scope.json'
const prompt = injectExecutorBatchContext(legacyPrompt, {
  batchIndex: 0,
  batchCount: 1,
  packages: ['jsdom', 'vitest'],
  scopeHash: 'f4981ff8',
  scopeManifestPath: scopePath,
  roadmapPath: 'C:\\state\\group-prompts\\Demo-branch-batch-1-roadmap.json',
  shell: 'windows-powershell',
  baselineBootstrapCommand: 'yarn install --frozen-lockfile',
})

const required = [
  'Batch: **1/1**',
  'jsdom, vitest',
  'f4981ff8',
  'Windows PowerShell',
  'yarn install --frozen-lockfile',
  'node_modules being absent in a Git worktree is expected',
  'mark baseline as unknown and require its post-update run to pass',
  'Never edit package.json and then call a later result “baseline”',
  'do not scan the canonical full roadmap or history',
  'do **not** create a duplicate scope file',
]
for (const needle of required) {
  if (!prompt.includes(needle)) throw new Error(`Executor contract missing ${JSON.stringify(needle)}`)
}
if (prompt.includes('Save the compact manifest below **unchanged**')) throw new Error('Desktop executor must replace the legacy scope-save instruction')
const desktopAwarePrompt = legacyPrompt.replace(
  '1. Save the compact manifest below **unchanged** to `C:\\history\\runs\\<timestamp>_Demo_scope.json`; validator supports `compact-v1`.',
  '1. When run through Dependency Flow Desktop, the executor contract at the top of the materialized batch prompt provides the authoritative pre-written scope-manifest path; use that file and **do not create a duplicate**. Outside Desktop, save the compact manifest below unchanged to `C:\\history\\runs\\<timestamp>_Demo_scope.json`; validator supports `compact-v1`.',
)
const desktopAware = injectExecutorBatchContext(desktopAwarePrompt, { batchIndex: 0, batchCount: 1, packages: ['jsdom', 'vitest'], scopeHash: 'f4981ff8', scopeManifestPath: scopePath, shell: 'windows-powershell' })
if (desktopAware.includes('Outside Desktop, save the compact manifest')) throw new Error('Materialized Desktop prompt must replace the generic scope-save fallback with the exact pre-written scope path')
if (baselineBootstrapCommandForLockfiles({ hasYarnLock: true }) !== 'yarn install --frozen-lockfile') throw new Error('Yarn baseline bootstrap must be frozen')
if (baselineBootstrapCommandForLockfiles({ hasPackageLock: true }) !== 'npm ci') throw new Error('npm baseline bootstrap must use npm ci')
if (baselineBootstrapCommandForLockfiles({ hasPnpmLock: true }) !== 'pnpm install --frozen-lockfile') throw new Error('pnpm baseline bootstrap must be frozen')

console.log('Executor batch context contract OK')
