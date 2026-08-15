import { buildMergeRecoveryPrompt } from '../dist-electron/merge-recovery.js'

const prompt = buildMergeRecoveryPrompt({
  projectName: 'Demo.App',
  projectPath: 'C:/repo/Demo.App',
  mergedBranch: 'libs-merged',
  sourceBranch: 'libs-group-2',
  conflictFiles: ['package.json', 'src/config.ts'],
  savedPromptPath: 'C:/state/prompt.md',
})

for (const required of [
  'package.json',
  'src/config.ts',
  'libs-merged',
  'libs-group-2',
  'do not upgrade, downgrade, add, remove, or substitute dependency targets',
  'Do not run `git commit`',
  'Diagnose failures instead of stopping at the first error',
  'git diff --name-only --diff-filter=U',
  'MERGE_HEAD still exists',
]) {
  if (!prompt.includes(required)) throw new Error(`Merge recovery prompt is missing: ${required}`)
}

console.log('Merge recovery prompt OK')
