// Dev-only rendered regression fixture; never imported by the production entry.
// Real FlowWorkspace/IterativeTaskPanel, synthetic IPC and an intentionally stale
// legacy failure. Exercises current/log/history ownership without a migration.
import { createRoot } from 'react-dom/client'
import { useEffect, useRef, useState, type ComponentProps } from 'react'
import { WorkspaceDialog } from '../components/WorkspaceDialog'
import { createIterativeAutopilot } from '../../electron/iterative-autopilot'
import { FlowWorkspace } from '../components/FlowWorkspace'
import { LanguageProvider, useLanguage } from '../i18n'
import type { IterativeAttemptView, IterativeStatusOutcome, WorkspaceDetails } from '../types'
import '../index.css'
import '../App.css'

const project = { name: 'Demo.UI', path: 'C:/demo/front' }
const details: WorkspaceDetails = {
  workspace: { id: 'qa', name: 'Demo workspace', path: 'C:/demo', templateRemote: '', toolRemote: '', settingsPath: '.dependency-roadmap/settings.json', agent: 'codex' },
  projects: [project], settingsExists: true, dashboardExists: false, promptStale: false,
  git: { branch: 'master', dirty: true, summary: ['old generated log'] }, projectLevels: {},
  teamState: { schemaVersion: 1, updatedAt: '', projects: { 'Demo.UI': { lastAction: 'baseline', status: 'failed', updatedAt: '', completedActions: ['preflight'], recovery: { kind: 'hard', code: 'LEGACY_ONLY_ERROR', message: 'SOURCE_CHECKOUT_DIRTY: previous FLOW only', action: 'baseline', updatedAt: '' } } } },
}
const noop = async () => {}
const getTask = async () => ({ present: false, missing: ['run.json'], stale: false })
const outcome = async () => ({ ok: true })
const models = async () => ['default']
const getIntent: ComponentProps<typeof FlowWorkspace>['onGetBaselineIntentPlan'] = async () => ({ candidates: [], intent: { schemaVersion: 2, policies: {}, controlMode: 'AUTONOMOUS', targetLevel: 'yellow' } })

function Harness() {
  const [showWorkspace, setShowWorkspace] = useState(false)
  const checked = useRef(false)
  const present = useRef(false)
  const canceled = useRef(false)
  const coordinator = useRef(createIterativeAutopilot(s => `${s.workspaceId}:${s.projectName}`))
  const { setLanguage } = useLanguage()
  const [attempt, setAttempt] = useState<IterativeAttemptView>({ attemptId: 'current-1', projectName: project.name, workspaceId: 'qa', status: 'running', stage: 'begin', phase: 'source-materialization: source capture sealed-manifest: files=25362, bytes=4589080824', startedAt: Date.now() - 400_000, lastHeartbeatAt: Date.now(), targetSource: 'discovery', packageProgress: { processed: 12, total: 30 }, stepsDone: [], runCreated: false })
  const current = useRef(attempt)
  current.current = attempt
  const log = useRef('CURRENT_RUN_ONLY: source preparation started\n')
  const reads = useRef(0)
  const scope = useRef<IterativeStatusOutcome['validationScope']>(undefined)
  const api = useRef({
    status: coordinator.current.register('status', async (): Promise<IterativeStatusOutcome> => ({ ok: true, present: present.current, phase: current.current.stage === 'drive' && current.current.status === 'done' ? 'TERMINAL' : present.current ? 'READY' : undefined, decision: current.current.stage === 'drive' && current.current.status === 'done' ? { step: 'finish', phase: 'TERMINAL', reason: 'COMPLETE', satisfied: true } : undefined, checked: { ok: checked.current }, stale: false, inFlight: current.current.status === 'running', attempt: current.current, validationScope: scope.current, validationProfile: { commands: ['npm run typecheck', 'npm run build', 'npm run test'], suggestedUnitCommand: 'npm run test -- --run src', deferredChecks: 'Playwright: requires a separate server' } })),
    attempt: async () => { reads.current++; return { ok: true, present: true, attempt: current.current, attemptLog: log.current } },
    cancel: coordinator.current.register('cancel', async () => { canceled.current = true; setAttempt(value => ({ ...value, status: 'canceled', finishedAt: Date.now() })); return { ok: true } }),
    begin: coordinator.current.register('begin', async (_event, input: { projectName: string; workspaceId?: string; autopilot?: boolean; checkOnly?: boolean }) => {
      canceled.current = false; setAttempt(value => ({ ...value, status: 'running', stage: 'begin', phase: input.checkOnly ? 'begin.check' : 'begin.discovery-progress', discoveryCompleted: false }));
      await new Promise(resolve => setTimeout(resolve, 2200));
      if (canceled.current) return { ok: false, error: 'canceled' };
      if (input.checkOnly) { checked.current = true; setAttempt(value => ({ ...value, status: 'done', lastStep: 'checked', phase: 'begin.check-done' })); return { ok: true, checked: { ok: true } }; }
      present.current = true; setAttempt(value => ({ ...value, status: 'done', lastStep: 'plan-next', discoveryCompleted: true, packageProgress: { processed: 30, total: 30 }, runCreated: true })); return { ok: true, phase: 'READY' };
    }),
    drive: coordinator.current.register('drive', async () => {
      setAttempt(value => ({ ...value, status: 'running', stage: 'drive', phase: 'verify-exact' })); await new Promise(resolve => setTimeout(resolve, 2200));
      if (canceled.current) return { ok: true, stopped: 'canceled' as const, steps: [] };
      setAttempt(value => ({ ...value, status: 'done', stage: 'drive', lastStep: 'finish' })); return { ok: true, stopped: 'finished' as const, steps: [] };
    }),
  })
  useEffect(() => {
    setLanguage('ru')
    const timer = window.setInterval(() => {
      if (current.current.status !== 'running') return
      log.current += `ITERATIVE_MIGRATION_STATUS_V1 ${JSON.stringify({ event: 'begin.check-progress', message: `CURRENT_RUN_ONLY: heartbeat ${Date.now()}` })}\n`
      setAttempt(value => ({ ...value, lastHeartbeatAt: Date.now() }))
    }, 3000)
    return () => window.clearInterval(timer)
  }, [setLanguage])
  const mode = (phase: string, status: IterativeAttemptView['status'] = 'running') => {
    log.current += `ITERATIVE_MIGRATION_STATUS_V1 ${JSON.stringify({ event: 'begin.check-progress', message: phase })}\n`
    setAttempt(value => ({ ...value, phase, status, lastHeartbeatAt: Date.now(), finishedAt: status === 'failed' ? Date.now() : undefined, lastError: status === 'failed' ? 'CURRENT_FAILURE: test command failed' : undefined }))
  }
  const restart = () => {
    log.current = 'NEW_ATTEMPT_ONLY: waiting for fresh output\n'
    setAttempt(value => ({ ...value, attemptId: 'current-2', status: 'running', phase: 'begin.check', lastError: undefined, startedAt: Date.now(), lastHeartbeatAt: Date.now(), finishedAt: undefined }))
  }
  return <main style={{ padding: 20, maxWidth: 1400, height: '100%', overflow: 'auto', margin: 'auto' }}>
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
      <strong>QA: synthetic IPC / actual UI</strong>
      <button onClick={() => mode('source-materialization: source capture sealed-manifest: files=25362, bytes=4589080824')}>Capture</button>
      <button onClick={() => mode('resolver-install: package-manager resolver install: running')}>Install</button>
      <button onClick={() => mode('begin.discovery-progress')}>Discovery 12/30</button>
      <button onClick={() => mode('lifecycle: npm run test', 'failed')}>Fail</button>
      <button onClick={restart}>New attempt</button>
      <button onClick={() => { scope.current = undefined; mode('begin.check-done', 'done') }}>Configure checks</button>
      <button onClick={() => { scope.current = { mode: 'test-nonregression', existingFailures: 1, total: 2 }; mode('begin.check-done', 'done') }}>Known baseline</button>
      <button onClick={() => { checked.current = true; setAttempt(value => ({ ...value, status: 'done', lastStep: 'checked', discoveryCompleted: false })) }}>Check passed</button>
      <button onClick={() => { checked.current = true; setAttempt(value => ({ ...value, phase: 'begin.discovery', status: 'running', discoveryCompleted: true, discoverySkipped: 10, packageProgress: { processed: 30, total: 30 } })) }}>Discovery done</button>
      <button onClick={() => mode('begin.c0-verify')}>C0 verify</button>
      <button onClick={() => setShowWorkspace(true)}>Workspace dialog</button>
      <button onClick={() => setLanguage('en')}>EN</button><button onClick={() => setLanguage('ru')}>RU</button>
    </div>
    <FlowWorkspace details={details} project={project} appVersion="QA" liveIterativeAttempt={attempt}
      onMarkDraftLaunched={() => {}} onResetDraftLaunch={() => {}} onAcknowledgeDraftRun={() => {}}
      onClearBaselineDecision={() => {}} onGetBaselineIntentPlan={getIntent} onGetCurrentDraftResult={async () => undefined}
      onRun={async () => undefined} onSendAgentNote={async () => false} onStartAutopilot={noop} onStopAutopilot={noop} onRecoverWithAgent={noop}
      onOpenDashboard={() => {}} onOpenPath={noop} onChoosePrompt={noop} onUpdateWorkspace={noop} onUpdateProjectBranches={noop} onUpdateProjectNode={noop}
      onListNodeVersions={models} onListAgentModels={models} onGetIterativeTask={getTask} onExportIterativeTask={outcome} onExportLegacyIterativeTask={outcome} onCopyIterativeTask={outcome} onSaveIterativeTask={outcome}
      onIterativeStatus={name => api.current.status(null, { projectName: name })} onIterativeAttempt={api.current.attempt} onIterativeCancel={name => api.current.cancel(null, { projectName: name })}
      onIterativeBegin={(name, input) => api.current.begin(null, { projectName: name, ...input })} onIterativeDrive={(name, autopilot) => api.current.drive(null, { projectName: name, autopilot })} onIterativeAgent={async () => ({ ok: false })}
      logs={[{ jobId: 'old', stream: 'stderr', line: 'SAVED_OLD_FLOW_ONLY: historical error' }]} />
    {showWorkspace ? <WorkspaceDialog onClose={() => setShowWorkspace(false)} onPickDirectory={async () => 'C:/demo/workspaces'} onConnectExisting={async () => {}} onCreate={async () => {}} /> : null}
  </main>
}
const root = createRoot(document.getElementById('root')!)
root.render(<LanguageProvider><Harness /></LanguageProvider>)
if (import.meta.hot) import.meta.hot.dispose(() => root.unmount())
