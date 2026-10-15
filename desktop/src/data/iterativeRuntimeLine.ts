import type { IterativeStatusOutcome } from '../types'

export function iterativeRuntimeLine(runner: IterativeStatusOutcome | undefined, unset: string): string | undefined {
  const runtime = runner?.runtime
  if (!runner?.present || !runtime?.effectiveVersion) return
  const manager = [runtime.packageManager, runtime.packageManagerVersion].filter(Boolean).join(' ')
  const platform = [runtime.platform, runtime.arch].filter(Boolean).join('/')
  const evidence = [runtime.source, manager, platform].filter(Boolean).join(' · ')
  return 'Node/CI: ' + (runtime.requested || unset) + ' → ' + runtime.effectiveVersion + (evidence ? ' · ' + evidence : '')
}
