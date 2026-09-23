// Dev-only harness (not referenced by index.html, never part of the prod
// bundle): mounts BaselineIntentDialog against a synthetic 77/200-package plan
// so the N6 (footer clipping) and N7 (restart flow) fixes can be verified in a
// real browser DOM. Reachable only at /e2e-harness.html under the vite dev
// server; `vite build` uses index.html as its sole input.
import { createRoot } from 'react-dom/client'
import { useState } from 'react'
import type { BaselineIntentPlan } from '../types'
import { LanguageProvider } from '../i18n'
import { BaselineIntentDialog } from '../components/BaselineIntentDialog'
import '../App.css'

function makePlan(count: number): BaselineIntentPlan {
  const candidates = Array.from({ length: count }, (_, index) => ({
    name: `pkg-${String(index).padStart(3, '0')}`,
    kind: (index % 7 === 0 ? 'dev' : index % 11 === 0 ? 'peer' : 'runtime') as 'runtime' | 'dev' | 'peer',
    requestedSpec: index % 2 === 0 ? '^1.0.0' : '>=1.0.0',
    currentVersion: '1.0.0',
  }))
  return {
    candidates,
    intent: {
      schemaVersion: 2,
      policies: {},
      controlMode: 'AUTONOMOUS',
      budgetMinutes: 30,
      acceptancePolicy: undefined,
      productMode: 'deep',
      targetLevel: 'yellow',
      minLagOkPct: 80,
    },
  }
}

type Resume = 'auto' | 'continue' | 'restart'

function Harness() {
  const [plan, setPlan] = useState<{ plan: BaselineIntentPlan; resume: Resume } | undefined>(undefined)
  const [log, setLog] = useState<string[]>([])
  const append = (line: string) => setLog((current) => [...current, line])

  return (
    <LanguageProvider>
      <div
        onClick={(event) => {
          const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-open]')
          if (!button) return
          const value = button.dataset.open
          if (value === 'auto') setPlan({ plan: makePlan(77), resume: 'auto' })
          else if (value === 'restart') setPlan({ plan: makePlan(77), resume: 'restart' })
          else if (value === 'auto-200') setPlan({ plan: makePlan(200), resume: 'auto' })
        }}
      >
        <div className="controls">
          <button data-open="auto" type="button">Open dialog (77 packages, auto)</button>
          <button data-open="restart" type="button">Open dialog (77 packages, restart)</button>
          <button data-open="auto-200" type="button">Open dialog (200 packages, auto)</button>
        </div>
        <div id="dialog-root">
          {plan ? (
            <BaselineIntentDialog
              key={`${plan.resume}-${plan.plan.candidates.length}`}
              mode="prepare"
              resume={plan.resume}
              plan={plan.plan}
              onCancel={() => { setPlan(undefined); append(`canceled : ${plan.resume}`) }}
              onSubmit={(intent) => {
                append(`submitted : resume=${plan.resume} budgetMinutes=${intent.budgetMinutes ?? 'none'} budgetMinutesExplicit=${intent.budgetMinutesExplicit ?? false}`)
                setPlan(undefined)
                return Promise.resolve()
              }}
            />
          ) : null}
        </div>
        <p id="harness-state">state: {plan ? `open(${plan.resume}, ${plan.plan.candidates.length})` : 'closed'}</p>
        <pre id="harness-log">{log.join('\n')}</pre>
      </div>
    </LanguageProvider>
  )
}

createRoot(document.getElementById('dialog-root') as HTMLElement).render(<Harness />)

declare global {
  interface Window {
    __harnessState?: () => string
  }
}

window.__harnessState = () => document.getElementById('harness-state')?.textContent ?? ''
