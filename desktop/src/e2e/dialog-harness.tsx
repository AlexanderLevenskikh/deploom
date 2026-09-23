// Dev-only harness (not referenced by index.html, never part of the prod
// bundle): mounts BaselineIntentDialog against a synthetic 77/200-package plan
// so the N6 (footer clipping), N7 (restart flow) and B2 (CTA visibility) checks
// can be verified in a real browser DOM. Reachable only at /e2e-harness.html
// under the vite dev server; `vite build` uses index.html as its sole input.
//
// B2: the harness loads the SAME base theme as the production shell (index.css
// first, then App.css) because `.button.primary` colours come from CSS
// variables defined in index.css -- without it the CTA renders as white text on
// a transparent background and the "footer is in the viewport" check is
// meaningless. The RU/EN toggle and the __harnessStyles probe make the visual
// audit reproducible.
import { createRoot } from 'react-dom/client'
import { useState } from 'react'
import type { BaselineIntentPlan } from '../types'
import { LanguageProvider, useLanguage } from '../i18n'
import { BaselineIntentDialog } from '../components/BaselineIntentDialog'
import '../index.css'
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

function LangToggle() {
  const { language, setLanguage } = useLanguage()
  return (
    <div className="controls" style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
      <span style={{ alignSelf: 'center', color: '#9fb4d0' }}>Language:</span>
      <button type="button" data-lang="en" className={language === 'en' ? 'active' : ''} onClick={() => setLanguage('en')}>EN</button>
      <button type="button" data-lang="ru" className={language === 'ru' ? 'active' : ''} onClick={() => setLanguage('ru')}>RU</button>
    </div>
  )
}

function Harness() {
  const [plan, setPlan] = useState<{ plan: BaselineIntentPlan; resume: Resume } | undefined>(undefined)
  const [log, setLog] = useState<string[]>([])
  const append = (line: string) => setLog((current) => [...current, line])

  return (
    <LanguageProvider>
      <LangToggle />
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
    __harnessStyles?: () => {
      language: string
      primary: { background: string; color: string; top: number; bottom: number; width: number; height: number }
      viewport: { width: number; height: number }
    }
  }
}

window.__harnessState = () => document.getElementById('harness-state')?.textContent ?? ''

window.__harnessStyles = () => {
  const button = document.querySelector<HTMLElement>('.baseline-intent-actions .button.primary')
  const lang = document.documentElement.lang
  if (!button) return { language: lang, primary: null as never, viewport: { width: window.innerWidth, height: window.innerHeight } }
  const style = getComputedStyle(button)
  const rect = button.getBoundingClientRect()
  return {
    language: lang,
    primary: {
      background: style.backgroundColor,
      color: style.color,
      top: Math.round(rect.top),
      bottom: Math.round(rect.bottom),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    },
    viewport: { width: window.innerWidth, height: window.innerHeight },
  }
}
