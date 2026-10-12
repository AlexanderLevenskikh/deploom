// Dev-only deterministic regression fixture. Never imported by production index.html.
import { createRoot } from 'react-dom/client'
import { useRef, useState } from 'react'
import { AddProjectDialog } from '../components/AddProjectDialog'
import { LanguageProvider, useLanguage } from '../i18n'
import type { ProjectDiscoveryResponse } from '../types'
import '../index.css'
import '../App.css'

type Scenario = 'partial' | 'bulk' | 'root'
export function AuditDialogHarness() {
  const { setLanguage } = useLanguage()
  const [scenario, setScenario] = useState<Scenario>()
  const calls = useRef<string[]>([])
  const failed = useRef(false)
  const [log, setLog] = useState('')
  const discover = async ({ path }: { path: string }): Promise<ProjectDiscoveryResponse> => {
    await new Promise(resolve => setTimeout(resolve, 30))
    const count = scenario === 'partial' ? 1 : 2
    return { status: scenario === 'root' ? 'explicit' : scenario === 'partial' ? 'single' : 'ambiguous', path: scenario === 'partial' ? path + '/packages/p000' : path, deep: false, report: {
      root: path, maxDepth: 5, candidates: Array.from({ length: count }, (_, i) => ({ path: path + `/packages/p00${i}`, relative: `packages/p00${i}`, packageName: `p00${i}`, depth: 2, lockfileKind: 'npm', lockfileName: 'package-lock.json', lockfilePath: path + '/package-lock.json' })),
      truncated: scenario === 'partial', deeperManifests: 0, deepestSeen: 2, ...(scenario === 'partial' ? { budgetHit: 'time' as const } : {}),
    } }
  }
  const open = (value: Scenario) => { calls.current = []; failed.current = false; setLog(''); setScenario(value) }
  return <><div style={{ padding: 20 }}><button type="button" data-lang="en" onClick={() => setLanguage('en')}>EN</button><button type="button" data-lang="ru" onClick={() => setLanguage('ru')}>RU</button>{(['partial', 'bulk', 'root'] as const).map(value => <button type="button" key={value} data-open={value} onClick={() => open(value)}>Open {value}</button>)}<pre id="harness-log">{log}</pre></div>
    {scenario ? <AddProjectDialog workspaceId="fixture" onClose={() => setScenario(undefined)} onPickDirectory={async () => undefined} onDiscoverProject={discover} onSubmit={async input => {
      calls.current.push(input.name); setLog(JSON.stringify(calls.current))
      if (scenario === 'bulk' && input.name === 'p001' && !failed.current) { failed.current = true; throw new Error('fixture-second-failure') }
    }} /> : null}</>
}
createRoot(document.getElementById('root')!).render(<LanguageProvider><AuditDialogHarness /></LanguageProvider>)
