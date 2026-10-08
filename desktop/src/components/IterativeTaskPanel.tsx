import { Check, ChevronDown, ChevronUp, Clipboard, ExternalLink, FileText, RefreshCw, Rocket, Save, Wrench, X } from 'lucide-react'
import { Fragment, useCallback, useEffect, useRef, useState } from 'react'

import { iterativeLogMessages } from '../data/iterativeLogMessages'
import { useLanguage } from '../i18n'
import { MigrationChecksPanel } from './MigrationChecksPanel'
import { AuditGoalSummary } from './AuditGoalSummary'
import { validateMigrationProfile, validationProfilesEqual, type MigrationValidationProfile } from '../../electron/migration-validation-profile'
import { attemptActiveElapsed, canApplyAttemptRead, iterativeActivity } from '../../electron/iterative-activity'
import { startAttemptJournalPolling } from '../../electron/iterative-journal-poll'
import { deriveMainAction, parseIterativeFailure, scenarioPipeline, PIPELINE_STAGES, type ScenarioMainActionState, type ScenarioSignal } from '../../electron/iterative-scenario'
import type { IterativeAgentOutcome, IterativeAttemptView, IterativeBeginOutcome, IterativeDriveOutcome, IterativeStatusOutcome, IterativeTaskActionOutcome, IterativeTaskSnapshot } from '../types'

type Props = {
  workspaceId?: string
  projectName: string
  refreshKey?: string
  appVersion?: string
  onGet: (projectName: string) => Promise<IterativeTaskSnapshot>
  onExport: (projectName: string) => Promise<IterativeTaskActionOutcome>
  onExportLegacy: (projectName: string) => Promise<IterativeTaskActionOutcome>
  onCopy: (projectName: string, language?: string) => Promise<IterativeTaskActionOutcome>
  onSave: (projectName: string, language?: string) => Promise<IterativeTaskActionOutcome>
  onStatus: (projectName: string) => Promise<IterativeStatusOutcome>
  onDrive: (projectName: string, autopilot?: boolean, retryInfra?: boolean, discardCandidate?: boolean, resumeTerminal?: boolean) => Promise<IterativeDriveOutcome>
  onBegin: (projectName: string, discovery?: { autopilot?: boolean; mode: 'auto' | 'none'; timeoutSeconds?: number; parallelism?: number; maxPackages?: number; validationProfile?: { commands: string[]; unitCommand?: string; compareExistingFailures?: boolean; deferredChecks?: string }; checkOnly?: boolean; repair?: boolean; restart?: boolean }) => Promise<IterativeBeginOutcome>
  onAgent: (projectName: string, autopilot?: boolean, cleanupNotes?: boolean) => Promise<IterativeAgentOutcome>
  onAttempt: (projectName: string) => Promise<{ ok: boolean; present: boolean; attempt?: IterativeAttemptView; attemptLog?: string; error?: string }>
  onCancel: (projectName: string) => Promise<{ ok: boolean }>
  liveAttempt?: IterativeAttemptView
  onConfigureScope?: () => void
  onBeforeStart?: () => Promise<void>
  onOpenPath: (path?: string) => Promise<void>
  // P1#1: the panel is the single scenario owner; it mirrors the ONE main action
  // to the enclosing workspace hero so the legacy FLOW button and the panel stop
  // being two competing launchers.
  onScenario?: (signal: ScenarioSignal | undefined) => void
  // P1#1: an external (legacy) job is running for the same checkout — the single
  // control must not let the user start a second, conflicting scenario.
  disabledExternal?: boolean
  onStatusView?: (status: IterativeStatusOutcome | undefined) => void
}

type PanelState =
  | { phase: 'loading' }
  | { phase: 'ready'; snapshot: IterativeTaskSnapshot }
  | { phase: 'error'; message: string }

const fmtElapsed = (ms: number): string => {
  const total = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

/** One primary label per scenario state (user-facing; internal terms only in
 * the diagnostics below). */
const MAIN_LABEL: Record<ScenarioMainActionState, [string, string]> = {
  check: ['Проверить проект', 'Check project'],
  'retry-check': ['Повторить проверку', 'Re-check'],
  'repair-current': ['Исправить текущий проект агентом', 'Repair the project with an agent'],
  'no-targets': ['Подобрать обновления автоматически', 'Find updates automatically'],
  ready: ['Начать обновление', 'Start the update'],
  'start-run': ['Начать обновление', 'Start the update'],
  'continue-run': ['Продолжить обновление', 'Continue the update'],
  running: ['Остановить', 'Stop'],
  'recovered-running': ['Продолжить обновление', 'Continue the update'],
  agent: ['Исправить агентом', 'Repair with an agent'],
  // Launch-wait: a provider rate limit / temporary outage parked the repair.
  // The only useful user control while waiting is canceling the wait.
  'agent-waiting': ['Отменить ожидание', 'Cancel wait'],
  result: ['Посмотреть результат', 'View the result'],
  partial: ['Посмотреть результат', 'View the result'],
  'budget-stop': ['Посмотреть результат', 'View the result'],
  // P2 (#1): the repair of the CURRENT state is done and confirmed — the next
  // explicit step is starting the real migration (a fresh run).
  'repair-done': ['Начать обновление', 'Start the update'],
  // P2 (#3): an honest terminal — the run ended WITHOUT a confirmed upgrade.
  blocked: ['Посмотреть результат', 'View the result'],
  'no-upgrade': ['Посмотреть результат', 'View the result'],
}

/** User-facing explanation under the main button (no internal step names). */
const MAIN_DESCRIPTION: Record<ScenarioMainActionState, [string, string]> = {
  check: ['Проверим установку зависимостей и команды проекта. После успешной проверки можно подобрать обновления.', 'Check dependency installation and project commands. Once the check passes, find available updates.'],
  'retry-check': ['Исправьте причину и повторите проверку — состояние не требует ручной чистки.', 'Fix the cause and re-check — no state files need manual cleanup.'],
  'repair-current': ['Контроль текущих зависимостей не проходит. Откроем прогон: агент исправит проект в изолированном окружении, авторитетная проверка повторится на исправленных байтах.', 'The current-state control fails. We will open the run: the agent repairs the project in an isolated checkout and the authoritative check re-runs on the repaired bytes.'],
  'no-targets': ['Выберите, как найти обновления: подобрать автоматически или настроить состав и политику.', 'Choose how to find updates: automatically, or by configuring scope and policy.'],
  ready: ['Проверка пройдена — обновление ещё не запускалось. Начните, когда будете готовы.', 'The check passed — the migration has not started yet. Start when ready.'],
  'start-run': ['Проект проверен. Начните обновление — применим и проверим изменения по группам.', 'The project is verified. Start the update — changes will be applied and verified in groups.'],
  'continue-run': ['Работа приостановлена. Продолжите — применим и проверим следующую группу.', 'The update is paused. Continue — the next group will be applied and verified.'],
  running: ['Идёт работа. Остановить можно в любой момент; проверенный результат сохраняется.', 'Work is running. You can stop at any time; the verified result is kept.'],
  'recovered-running': ['Работа была прервана перезапуском; процесс не запущен. Продолжите, чтобы возобновить её.', 'Work was interrupted by a restart; no process is running. Continue to resume it.'],
  agent: ['Нужны исправления в изолированном окружении. Запустите агента.', 'Repairs are needed in an isolated environment. Run the agent.'],
  // Launch-wait: the durable attempt.reason carries the exact cause + the next
  // attempt time; the static line is the fallback when the journal is absent.
  'agent-waiting': ['Провайдер агента временно недоступен (лимит запросов или сбой). Ремонт продолжит сам — попытка не расходуется. Можно отменить ожидание.', 'The agent provider is temporarily unavailable (rate limit or outage). The repair continues on its own — no attempt is spent. You can cancel the wait.'],
  result: ['Обновления проверены. Откройте результат и задание.', 'The updates are verified. Open the result and the assignment.'],
  partial: ['Часть обновлений применена и проверена, но не все цели выполнены. Откройте результат.', 'Some updates were applied and verified, but not all goals were reached. Open the result.'],
  'budget-stop': ['Работа остановилась по бюджету; проверенное сохранено, часть целей не выполнена. Откройте результат.', 'Stopped by the budget; the verified work is kept, some goals were not reached. Open the result.'],
  // P2 (#1): the repaired current state is an honest precondition for the
  // migration, not a partial upgrade — starting is a separate, explicit action.
  'repair-done': ['Исходное состояние исправлено и подтверждено. Начните обновление.', 'The current state is fixed and confirmed. Start the update.'],
  // P2 (#3): blocked / no-upgrade are NOT "часть обновлений применена" — the
  // run ended without a confirmed upgrade.
  blocked: ['Обновление заблокировано: подтверждающая проверка не пройдена. Откройте результат.', 'The update is blocked: no confirming check passed. Open the result.'],
  'no-upgrade': ['Проверенных обновлений не найдено — обновление не выполнялось. Откройте результат.', 'No verified updates were found — the update did not run. Open the result.'],
}

/** User-facing activity phrase for a live attempt. */
const activityText = (attempt?: IterativeAttemptView, text?: (ru: string, en: string) => string): string => {
  const t = text ?? ((ru: string) => ru)
  if (!attempt) return t('Подготовка…', 'Preparing…')
  if (attempt.status === 'failed') return t('Попытка не завершилась', 'The attempt failed')
  if (attempt.status === 'canceled') return t('Работа остановлена', 'Work was stopped')
  if (attempt.status === 'waiting') return t('Ожидание повторного запуска агента', 'Waiting for the agent to relaunch')
  if (attempt.status === 'done' && attempt.lastStep === 'agent') return t('Нужны исправления — обновление приостановлено', 'Repairs needed — update paused')
  if (attempt.status === 'done') return t(attempt.lastStep === 'no-targets' ? 'План обновления ещё не выбран' : 'Этап завершён', attempt.lastStep === 'no-targets' ? 'No update plan selected yet' : 'Stage completed')
  const stage = attempt.stage
  if (stage === 'begin') {
    if (attempt.targetSource === 'discovery') return t('Подбираем версии…', 'Finding versions…')
    return t('Проверяем текущие зависимости', 'Checking current dependencies')
  }
  if (stage === 'drive') return t('Обновляем пакеты и проверяем результат', 'Updating packages and verifying the result')
  if (stage === 'agent') return t('Нужны исправления (агент работает в изолированном окружении)', 'Repairs needed (agent works in an isolated environment)')
  if (stage === 'preflight') return t('Предварительная проверка проекта', 'Pre-flight project check')
  return t('Работа выполняется', 'Work is in progress')
}

export function IterativeTaskPanel({ workspaceId, projectName, refreshKey, appVersion, onGet, onExport, onCopy, onSave, onStatus, onDrive, onBegin, onExportLegacy, onAgent, onAttempt, onCancel, liveAttempt, onConfigureScope, onBeforeStart, onOpenPath, onScenario, disabledExternal, onStatusView }: Props) {
  const { text, language } = useLanguage()
  const [state, setState] = useState<PanelState>({ phase: 'loading' })
  const [validationDraft, setValidationDraft] = useState<MigrationValidationProfile>()
  useEffect(() => setValidationDraft(undefined), [workspaceId, projectName])
  const [autopilot, setAutopilot] = useState(false)
  useEffect(() => setAutopilot(false), [workspaceId, projectName])
  const [busy, setBusy] = useState<string>()
  const [copied, setCopied] = useState(false)
  const [note, setNote] = useState<string>()
  const [showDialog, setShowDialog] = useState(false)
  const [runner, setRunner] = useState<IterativeStatusOutcome>()
  const [runnerError, setRunnerError] = useState<string>()
  useEffect(() => { onStatusView?.(runner) }, [onStatusView, runner])
  const [stepBusy, setStepBusy] = useState(false)
  // L1: durable attempt journal — seeded from the IPC read, then merged with the
  // live event stream from main.ts so the first click is observable without a poll.
  const [attempt, setAttempt] = useState<IterativeAttemptView>()
  const [attemptLog, setAttemptLog] = useState<string>()
  const [logAttemptId, setLogAttemptId] = useState<string>()
  const currentAttemptRef = useRef<IterativeAttemptView | undefined>(undefined)
  currentAttemptRef.current = attempt
  const [showLog, setShowLog] = useState(false)
  const [noTargetsStep, setNoTargetsStep] = useState(false)
  const [showDiag, setShowDiag] = useState(false)
  const [nowTick, setNowTick] = useState(Date.now())
  const loadSeq = useRef(0)

  // D2.4: display-only view of the durable runtime contract — requested vs
  // effective + the CI evidence source (installed runtime that committed).
  const runtimeLine = (runner?: IterativeStatusOutcome) => {
    const rt = runner?.runtime
    if (!runner?.present || !rt?.effectiveVersion) return null
    const manager = [rt.packageManager, rt.packageManagerVersion].filter(Boolean).join(' ')
    const platform = [rt.platform, rt.arch].filter(Boolean).join('/')
    const evidence = [rt.source, manager, platform].filter(Boolean).join(' · ')
    return (
      <span>
        {text('Node/CI', 'Node/CI')}: {rt.requested || text('не задан', 'unset')} → {rt.effectiveVersion}
        {evidence ? ` · ${evidence}` : ''}
      </span>
    )
  }

  // L4: never swallow a status error. Keep the last good runner AND remember the
  // failure reason so the UI shows it next to the action buttons.
  const runnerScope = useRef(`${workspaceId ?? ''}:${projectName}`)
  runnerScope.current = `${workspaceId ?? ''}:${projectName}`
  const runnerSeq = useRef(0)
  useEffect(() => {
    runnerScope.current = `${workspaceId ?? ''}:${projectName}`
    return () => { runnerScope.current = '' }
  }, [workspaceId, projectName])
  const refreshRunner = useCallback(async () => {
    const scopeKey = `${workspaceId ?? ''}:${projectName}`
    const seq = ++runnerSeq.current
    try {
      const status = await onStatus(projectName)
      if (runnerScope.current !== scopeKey || runnerSeq.current !== seq) return undefined
      setRunner(current => status.ok ? status : current ?? status)
      setRunnerError(status.ok || !status.error ? undefined : status.error)
      return status
    } catch (error) {
      if (runnerScope.current !== scopeKey || runnerSeq.current !== seq) return undefined
      setRunnerError(error instanceof Error ? error.message : String(error))
      return undefined
    }
  }, [onStatus, projectName, workspaceId])

  useEffect(() => {
    if (!runner?.audit?.running) return
    const timer = window.setInterval(() => void refreshRunner(), 2_000)
    return () => window.clearInterval(timer)
  }, [runner?.audit?.running, refreshRunner])

  const load = useCallback(async () => {
    const seq = ++loadSeq.current
    setState({ phase: 'loading' })
    try {
      const snapshot = await onGet(projectName)
      if (loadSeq.current !== seq) return
      setState({ phase: 'ready', snapshot })
    } catch (error) {
      if (loadSeq.current !== seq) return
      setState({ phase: 'error', message: error instanceof Error ? error.message : String(error) })
    }
  }, [onGet, projectName])

  useEffect(() => {
    void load()
    void refreshRunner()
  }, [load, refreshRunner, refreshKey])

  // Review P2: the component is NOT remounted by project identity, so a switch
  // must RESET every per-project view — a foreign attempt/log/note must never
  // leak into the next project. Combined with the late-response guards below, a
  // stale answer cannot overwrite the new project's state.
  const projectKey = `${workspaceId ?? ''}:${projectName}`
  const prevProjectKey = useRef(projectKey)
  useEffect(() => {
    if (prevProjectKey.current === projectKey) return
    prevProjectKey.current = projectKey
    setAttempt(undefined)
    setAttemptLog(undefined)
    setShowLog(false)
    setNoTargetsStep(false)
    setNote(undefined)
    setRunnerError(undefined)
    setCopied(false)
    setShowDiag(false)
  }, [projectKey])

  // L1: the journal survives a Desktop restart — re-seed from disk on mount.
  useEffect(() => {
    let alive = true
    void (async () => {
      try {
        const result = await onAttempt(projectName)
        if (!alive) return
        // Guard late responses: only apply an attempt that belongs to THIS
        // project (the read may have been issued for the previous project).
        if (result.attempt && canApplyAttemptRead(currentAttemptRef.current, result.attempt, projectName, workspaceId)) {
          setAttempt(result.attempt)
          setLogAttemptId(result.attempt.attemptId)
          setAttemptLog(result.attemptLog ?? '')
        }
        if (result.attempt?.projectName === projectName && result.attempt?.lastStep === 'no-targets') setNoTargetsStep(true)
      } catch {
        // journal read is best-effort; the live event stream still updates us
      }
    })()
    return () => { alive = false }
  }, [onAttempt, projectName, workspaceId, refreshKey])

  // L1: live event stream carries the newest journal snapshot for THIS project.
  // Review re-check (#3): when a launch FOREIGN to this component instance
  // finishes (e.g. the panel was remounted while the child ran), its terminal
  // attempt event must ALSO release the Electron in-flight lock on this side —
  // otherwise a remounted panel would show «Остановить» forever. The status
  // re-read refreshes runner.inFlight (and the decision) from Electron.
  // P2 (#2): the stream is WORKSPACE-scoped — a same-named project of a
  // different workspace must never leak into this panel.
  const liveAttemptStatus = useRef<string | undefined>(undefined)
  const liveAttemptId = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (!liveAttempt || liveAttempt.projectName !== projectName) return
    if (workspaceId !== undefined && liveAttempt.workspaceId !== undefined && liveAttempt.workspaceId !== workspaceId) return
    const previous = liveAttemptStatus.current
    const previousId = liveAttemptId.current
    liveAttemptStatus.current = liveAttempt.status
    liveAttemptId.current = liveAttempt.attemptId
    setAttempt(liveAttempt)
    if (liveAttempt.lastStep === 'no-targets') setNoTargetsStep(true)
    const nowTerminal = liveAttempt.status === 'done' || liveAttempt.status === 'failed' || liveAttempt.status === 'canceled'
    const leftLive = (previous === 'running' || previous === 'starting') && liveAttempt.status !== previous
    if (nowTerminal || leftLive || previousId !== liveAttempt.attemptId || ((liveAttempt.status === 'running' || liveAttempt.status === 'starting') && previous !== liveAttempt.status)) void refreshRunner()
  }, [liveAttempt, projectName, workspaceId, refreshRunner])

  // Elapsed counter while an attempt is alive.
  const attemptAlive = Boolean(attempt && (attempt.status === 'starting' || attempt.status === 'running'))
  useEffect(() => {
    if (!attemptAlive) return
    const timer = window.setInterval(() => setNowTick(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [attemptAlive, attempt?.status, attempt?.attemptId])

  // Review P1: liveness comes from ELECTRON, where the real in-flight begin/drive
  // child is known — never from a locally remounted React component. `stepBusy`
  // covers THIS panel's own in-flight call; `runner.inFlight` restores control
  // after FLOW → Graph → FLOW (the component remounts, the child keeps running).
  // Review re-check (#3): the in-flight snapshot can outlive the child in a
  // foreign launch. The durable attempt journal is the completion signal: once
  // it is terminal (done/failed/canceled) the lock is released here even before
  // the async status re-read lands, so «Остановить» can never stick forever.
  const attemptTerminal = Boolean(attempt && (attempt.status === 'done' || attempt.status === 'failed' || attempt.status === 'canceled'))
  const childAlive = stepBusy || runner?.autopilotActive === true || (runner?.inFlight === true && !attemptTerminal)

  // Both output surfaces share this attempt-scoped journal tail. Poll even
  // when the inline output is collapsed; ignore late reads from a prior run.
  useEffect(() => {
    if (!(attemptAlive || childAlive)) return
    return startAttemptJournalPolling(async () => {
      const requestedId = currentAttemptRef.current?.attemptId
      const result = await onAttempt(projectName)
      if (!result.attempt || !canApplyAttemptRead(currentAttemptRef.current, result.attempt, projectName, workspaceId)) return
      if (currentAttemptRef.current?.attemptId !== requestedId || (requestedId && result.attempt.attemptId !== requestedId)) return
      return result
    }, result => {
      if (!result?.attempt) return
      setLogAttemptId(result.attempt.attemptId)
      setAttemptLog(result.attemptLog ?? '')
      setAttempt(current => current && current.lastHeartbeatAt > result.attempt!.lastHeartbeatAt ? current : result.attempt)
    })
  }, [attemptAlive, childAlive, attempt?.attemptId, onAttempt, projectName, workspaceId])

  // Fetch the terminal tail even when neither output section is expanded.
  useEffect(() => {
    if (!attemptTerminal) return
    let alive = true
    const id = attempt?.attemptId
    void onAttempt(projectName).then(result => {
      if (!alive || !canApplyAttemptRead(currentAttemptRef.current, result.attempt, projectName, workspaceId) || result.attempt?.attemptId !== id || currentAttemptRef.current?.attemptId !== id) return
      setLogAttemptId(id)
      setAttemptLog(result.attemptLog ?? '')
    }).catch(() => {})
    return () => { alive = false }
  }, [attemptTerminal, attempt?.attemptId, onAttempt, projectName, workspaceId])

  const run = async (kind: string, action: () => Promise<IterativeTaskActionOutcome>) => {
    setBusy(kind)
    setNote(undefined)
    try {
      const outcome = await action()
      if (outcome.ok) {
        if (kind === 'copy') {
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1600)
        }
        setNote(outcome.path)
        if (kind === 'export' || kind === 'export-view') {
          await load()
          if (kind === 'export-view') setShowDialog(true)
        }
      } else {
        setNote(outcome.error ?? 'Ошибка')
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy(undefined)
    }
  }

  // #1: the durable supervisor drives plan-next -> materialize -> precheck ->
  // verify-exact -> next cohort WITHOUT manual internal steps, stopping ONLY
  // at an agent gate, finish, error or budget. Restart-safe: every iteration
  // re-reads the durable Python state.
  // Review re-check P1#4: a durable run whose ТЗ does not exist yet is
  // CONTINUABLE — the supervisor drives plan-next/…/gate and the gate rebuilds
  // the task artifact itself, so the coordinator must stay reachable from the
  // single main action, not from a separate technical button.
  const autopilotNote = (summary: { stopped: string; error?: string }) => summary.error
    ? text(`Автопилот остановился: ${summary.error}`, `Autopilot stopped: ${summary.error}`)
    : summary.stopped === 'finished'
      ? text('Автопилот завершил работу. Откройте результат.', 'Autopilot finished. Open the result.')
      : summary.stopped === 'canceled'
        ? text('Автопилот остановлен. Проверенный результат сохранён.', 'Autopilot stopped. The verified result is kept.')
        : text(`Автопилот приостановлен (${summary.stopped}). Проверьте результат и следующий шаг.`, `Autopilot paused (${summary.stopped}). Review the outcome and next step.`)

  const driveNow = async (discardCandidate = false, resumeTerminal = false) => {
    setStepBusy(true)
    setNote(undefined)
    try {
      await onBeforeStart?.()
      const outcome = await onDrive(projectName, runner?.autopilotEnabled || runner?.autopilotActive || autopilot, runner?.decision?.infraBlocked === true, discardCandidate, resumeTerminal)
      if (outcome.autopilot) {
        setNote(autopilotNote(outcome.autopilot))
      } else if (!outcome.ok) {
        setNote(outcome.error ?? 'Ошибка')
      } else if (outcome.stopped === 'agent-gate') {
        setNote(
          text(
            `Нужны исправления: ${outcome.reason ?? 'агент ждёт запуска'}`,
            `Repairs needed: ${outcome.reason ?? 'awaiting the agent'}`,
          ),
        )
      } else if (outcome.stopped === 'canceled') {
        setNote(text(`Остановлено пользователем. Проверенный результат сохранён — продолжить можно позже.`, `Canceled by user. The verified result is kept — you can continue later.`))
      } else if (outcome.stopped === 'finished') {
        setNote(text('Обновление завершено: изменения применены и проверены.', 'The update finished: changes are applied and verified.'))
      } else {
        setNote(
          text(
            `Остановлено: ${outcome.reason ?? ''}`,
            `Stopped: ${outcome.reason ?? ''}`,
          ),
        )
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setStepBusy(false)
      void refreshRunner()
      void load()
    }
  }

  const beginNow = async (discovery?: { autopilot?: boolean; mode: 'auto' | 'none'; timeoutSeconds?: number; parallelism?: number; maxPackages?: number; validationProfile?: { commands: string[]; unitCommand?: string; compareExistingFailures?: boolean; deferredChecks?: string }; checkOnly?: boolean; repair?: boolean; restart?: boolean }) => {
    setStepBusy(true)
    setNote(undefined)
    // P2#5: a fresh attempt supersedes the previous "no targets" explanation.
    setNoTargetsStep(false)
    try {
      const profile = validationDraft ?? runner?.validationProfile
      const validationProfile = runner?.present && !discovery?.restart ? undefined : profile && { ...profile, commands: profile.commands.map(c => c.trim()).filter(Boolean) }
      await onBeforeStart?.()
      const outcome = await onBegin(projectName, { mode: discovery?.mode ?? 'none', autopilot: runner?.autopilotEnabled || runner?.autopilotActive || autopilot, ...discovery, validationProfile })
      if (outcome.autopilot) {
        setNote(autopilotNote(outcome.autopilot))
      } else if (outcome.ok) {
        if (outcome.checked) {
          // P1#4: standalone "Проверить проект" — nothing was started. Show the
          // durable verdict and let the user explicitly pick the next step: a
          // green verdict → "Начать обновление"; a failed control → the repair
          // agent; an inconclusive result → repeat the check. The scenario
          // reacts to the journal, this note just explains.
          setNoTargetsStep(false)
          setNote(
            outcome.checked.ok
              ? text('Проект проверен — можно начать обновление.', 'The project is checked — you can start the update.')
              : outcome.checked.control?.status === 'failed'
                ? text('Контроль текущих зависимостей не пройден; обновление не запускалось. Можно исправить проект агентом.', 'The current-dependencies control fails; nothing was started. The project can be repaired by an agent.')
                : text('Проверка не дала определённого результата; обновление не запускалось. Повторите проверку.', 'The check was inconclusive; nothing was started. Re-run the check.'),
          )
        } else if (outcome.noTargets) {
          setNoTargetsStep(true)
          setNote(
            text(
              'Сохранённых целей нет — выберите, как подобрать обновления.',
              'No saved targets — choose how to find updates.',
            ),
          )
        } else {
          const report = outcome.discovery
          const unsettled = Boolean(report && report.discovered.length === 0 && (report.unavailable.length > 0 || report.budgetSkipped.length > 0))
          if (unsettled) {
            setNote(
              text(
                'Не удалось установить доступные обновления (registry недоступен или бюджет поиска исчерпан). Результат не засчитан как завершённая миграция.',
                'Could not establish available updates (registry unavailable or the search budget was exhausted). Not counted as a completed migration.',
              ),
            )
          } else if (outcome.phase === 'BOOTSTRAP_REPAIR') {
            // P2 (#2): the red C0 just opened the repair track — the run exists
            // and the isolated trial gets materialized up to the agent gate.
            setNote(
              text(
                'Контроль текущих зависимостей не пройден. Открыт прогон ремонта: готовим изолированное окружение, затем агент исправит проект.',
                'The current-state control fails. The repair run is open: preparing the isolated checkout, then the agent repairs the project.',
              ),
            )
            await driveNow()
          } else {
            setNote(text('Проверка завершена — продолжаю обновление.', 'Check finished — continuing the update.'))
            await driveNow()
          }
        }
      } else if ((outcome.error ?? '').startsWith('RUN_ALREADY_EXISTS')) {
        setNote(text('Прогон уже существует — продолжите с его состояния.', 'A run already exists — continue from its state.'))
        void refreshRunner()
      } else {
        setNote(outcome.error ?? 'Ошибка')
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setStepBusy(false)
      void refreshRunner()
      void load()
    }
  }

  // L1: user cancel is cooperative — main.ts polls cancelRequested every 500ms
  // and stops the current spawn; the durable run + verified checkpoint survive.
  const cancelNow = async () => {
    setNote(undefined)
    try {
      await onCancel(projectName)
      setNote(text('Отправлен запрос остановки…', 'Stop requested…'))
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    }
  }

  // R9: import the user's OLD saved Baseline result (dashboard-state JSON)
  // into the shared task contract with the CURRENT builder — no rerun, no
  // invented proof (verification stays "legacy-exported-not-reverified").
  const exportLegacyNow = async () => {
    setStepBusy(true)
    setNote(undefined)
    try {
      const outcome = await onExportLegacy(projectName)
      if (outcome.ok) {
        setNote(
          text(
            `Импортировано из сохранённого результата: ${outcome.artifactId ?? ''}`,
            `Imported from the saved result: ${outcome.artifactId ?? ''}`,
          ),
        )
      } else {
        setNote(outcome.error ?? 'Ошибка')
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setStepBusy(false)
      void refreshRunner()
      void load()
    }
  }

  const agentNow = async (cleanupNotes = false) => {
    setStepBusy(true)
    setNote(undefined)
    try {
      await onBeforeStart?.()
      const outcome = await onAgent(projectName, cleanupNotes ? false : runner?.autopilotEnabled || runner?.autopilotActive || autopilot, cleanupNotes)
      if (outcome.autopilot) {
        setNote(autopilotNote(outcome.autopilot))
      } else if (outcome.ok) {
        if (outcome.phase === 'TERMINAL') { setNote(cleanupNotes ? text('Пояснения удалены отдельным коммитом; проверки и аудит обновлены. Документы сохранены в артефактах.', 'Notes removed in a separate commit; checks and audit refreshed. Documents remain in the artifacts.') : text('Ветка с коммитами подготовлена; проверки и аудит обновлены.', 'The branch with commits is prepared; checks and audit are refreshed.')); return }
        const changed = (outcome.changedFiles ?? []).length
        setNote(
          text(
            `Агент исправил ${changed} файлов — продолжаю проверку.`,
            `Agent repaired ${changed} file(s) — continuing the verification.`,
          ),
        )
        // Continuous migration: after the repair is accepted, the supervisor
        // verifies the exact candidate and moves to the next cohort on its own.
        await driveNow()
      } else {
        setNote(outcome.error ?? 'Ошибка')
      }
    } catch (error) {
      setNote(error instanceof Error ? error.message : String(error))
    } finally {
      setStepBusy(false)
      void refreshRunner()
      void load()
    }
  }

  const toggleLog = async () => {
    if (showLog || logAttemptId !== attempt?.attemptId || attemptLog === undefined) {
      try {
        const result = await onAttempt(projectName)
        if (result.attempt && canApplyAttemptRead(currentAttemptRef.current, result.attempt, projectName, workspaceId)) {
          setLogAttemptId(result.attempt.attemptId)
          setAttemptLog(result.attemptLog ?? '')
        }
      } catch {
        // diagnostics are best-effort
      }
    }
    setShowLog((current) => !current)
  }

  const snapshot = state.phase === 'ready' ? state.snapshot : undefined
  const task = snapshot?.task
  const insufficient = Boolean(snapshot && !snapshot.present && snapshot.missing.length > 0)

  // Machine-readable blocker (e.g. uninitialized submodule) surfaced ONCE.
  const blocker = parseIterativeFailure(attempt?.lastError) ?? parseIterativeFailure(attempt?.reason)
  const validationChanged = !runner?.present && validationDraft !== undefined && !validationProfilesEqual(validationDraft, runner?.validationProfile)
  const mainAction = deriveMainAction({
    inFlight: childAlive,
    attempt: validationChanged ? undefined : attempt,
    runner,
    taskPresent: Boolean(task),
    noTargets: validationChanged ? false : noTargetsStep,
    checked: !validationChanged && runner?.checked?.ok === true,
  })
  const repairMutationBlocked = mainAction.state === 'agent' && (runner?.repairPause?.exhausted === true || (attempt?.status === 'failed' && /^(?:FORBIDDEN_MUTATION|AGENT_REPAIR_BUDGET_EXHAUSTED):/.test(attempt.lastError ?? '')))
  const retryIsDiscoverySearch = blocker?.code === 'DISCOVERY_UNSETTLED'
  const mainLabel = repairMutationBlocked ? text('Подобрать другой набор', 'Try another package set') : mainAction.state === 'agent' && attempt?.status === 'done' && attempt.lastStep === 'agent' ? text('Продолжить исправление', 'Continue repair') : mainAction.state === 'retry-check' && retryIsDiscoverySearch
    ? text('Повторить поиск обновлений', 'Retry the update search')
    : text(...MAIN_LABEL[mainAction.state])
  const mainDescription = repairMutationBlocked
    ? text('Эту попытку нельзя продолжить: исчерпан лимит ремонта или нарушена защита файлов. Продолжим подбор от последнего проверенного результата; непроверенные правки не будут приняты.', 'This attempt cannot continue: its repair budget is exhausted or protected files changed. Continue planning from the last verified result; unverified edits will not be accepted.')
    : retryIsDiscoverySearch
    ? text(
        'Registry-данные не получены или бюджет поиска исчерпан — результат не засчитан. Повторите поиск или настройте состав обновления.',
        'Registry data was not obtained or the search budget was exhausted — not counted. Retry the search or configure the update scope.',
      )
    : mainAction.state === 'blocked' && (runner?.progressSummary?.accepted ?? 0) > 0
    ? text('Принятые обновления проверены и сохранены. Финальное подтверждение результата заблокировано; откройте результат и данные аудита.', 'Accepted updates are verified and saved. Final confirmation is blocked; open the result and audit evidence.')
    : mainAction.state === 'agent' && attempt?.status === 'done' && attempt.lastStep === 'agent'
    ? (attempt.reason || text('Правки сохранены отдельно. Продолжение возобновит ремонт; принятие требует проверки.', 'Trial edits are preserved. Continue resumes repair; acceptance requires verification.'))
    : mainAction.state === 'agent-waiting'
    ? (attempt?.reason || text(...MAIN_DESCRIPTION[mainAction.state]))
    : text(...MAIN_DESCRIPTION[mainAction.state])

  const elapsedMs = attemptActiveElapsed(attempt, attemptAlive && childAlive, nowTick)
  const progress = attempt?.packageProgress
  const currentAttemptLog = logAttemptId === attempt?.attemptId ? attemptLog : undefined
  const showLogTail = showLog && currentAttemptLog
  const activity = iterativeActivity(attempt, childAlive, language)
  const attemptFailure = attempt && attempt.status === 'failed'
    ? (attempt.lastError || attempt.reason || undefined)
    : undefined
  const decisionText = runner?.decision ? `${runner.decision.step ?? '—'} · ${runner.decision.reason}` : undefined

  const copyDiagnostics = () => {
    // Prefer the raw technical detail (lastError) over the short UI reason so
    // the copied diagnostic actually explains WHY (the short reason alone, e.g.
    // "check failed", is useless for triage).
    const reason = attempt?.lastError || attempt?.reason || mainAction.reasonShort || '—'
    const diagnostic =
      `DepLoom ${appVersion ?? '?'}\n` +
      `Проект: ${projectName}\n` +
      `Этап: ${attempt?.stage ?? '—'}${attempt?.phase ? ` (${attempt.phase})` : ''}\n` +
      `Попытка: ${attempt?.attemptId ?? '—'}\n` +
      `Причина: ${reason}\n` +
      `Решение: ${decisionText ?? '—'}\n` +
      `Статус журнала: ${attempt?.status ?? '—'}`
    const textarea = document.createElement('textarea')
    textarea.value = diagnostic
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    try {
      document.execCommand('copy')
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } finally {
      document.body.removeChild(textarea)
    }
  }

  const runMainAction = () => {
    switch (mainAction.state) {
      case 'check':
      case 'retry-check':
        // P1#4: "Проверить проект" / "Повторить проверку" run the REAL separate
        // check (`--check-only`): preflight + current-dependencies control, no
        // run, no discovery. Discovery is only (re)attempted for the specific
        // DISCOVERY_UNSETTLED blocker that it can actually clear.
        void beginNow(retryIsDiscoverySearch ? { mode: 'auto' } : { mode: 'none', checkOnly: true })
        break
      case 'repair-current':
        // P2 (#2): a red current-state control reaches the repair agent through
        // the durable BOOTSTRAP_REPAIR track. With no run yet we open one via
        // the repair-only begin (the red C0 lands in BOOTSTRAP_REPAIR); with a
        // red run we materialize the isolated trial up to the agent gate. Never
        // a dead-end re-check and never blocked by missing roadmap targets.
        if (!runner?.present) void beginNow({ mode: 'none', repair: true })
        else void driveNow()
        break
      case 'no-targets':
        void beginNow({ mode: 'auto' })
        break
      case 'ready':
        // Use the configured plan, or discover targets when none were saved.
        // A successful check must never lead to a no-op launch.
        void beginNow({ mode: 'auto' })
        break
      case 'repair-done':
        // P2 (#1): the repair of the current state is done and confirmed.
        // Starting the real migration is a separate, explicit action — the begin
        // handler archives the finished repair-only run (verdict preserved) and
        // opens a fresh migration run.
        void beginNow({ mode: 'none' })
        break
      case 'start-run':
      case 'continue-run':
      case 'recovered-running':
        void driveNow()
        break
      case 'running':
        void cancelNow()
        break
      case 'agent-waiting':
        // The repair is parked by a provider outage; the user control is
        // canceling the wait (flow:iterative:cancel clears only the *waiting*
        // lease — no agent was running; no repair attempt is spent).
        void cancelNow()
        break
      case 'agent':
        if (repairMutationBlocked) void driveNow(true)
        else void agentNow()
        break
      case 'result':
      case 'partial':
      case 'budget-stop':
      case 'blocked':
      case 'no-upgrade':
        // P1#3 / P2 (#3): an unfinished OR blocked / no-upgrade result opens the
        // SAME honest surface — the result dialog + diagnostics — without
        // claiming it was fully verified.
        if (task) setShowDialog(true)
        else void run('export-view', () => onExport(projectName))
        break
    }
  }

  let validationError: string | undefined
  const selectedProfile = validationDraft ?? runner?.validationProfile
  if (!runner?.present && selectedProfile) {
    try { validateMigrationProfile({ ...selectedProfile, commands: selectedProfile.commands.map(c => c.trim()).filter(Boolean) }) }
    catch (error) {
      validationError = error instanceof Error && error.message === 'VITEST_COMMAND_MUST_BE_A_SINGLE_SELECTED_COMMAND'
        ? text('Укажите одну команду Vitest из обязательного списка, без цепочек команд и собственных аргументов JSON-отчёта.', 'Choose a single Vitest command from the required list, without command chains or custom JSON-report arguments.')
        : text('Укажите от 1 до 20 обязательных команд и корректное описание отложенных проверок.', 'Select 1–20 required commands and a valid description of deferred checks.')
    }
  }
  const busyLocked = Boolean(validationError) || runner?.audit?.running === true || stepBusy || busy !== undefined || disabledExternal === true || state.phase === 'loading' || (runner === undefined && !runnerError)

  // P1#1: the panel is the SINGLE owner of the scenario main action. Mirror it
  // outward so the enclosing workspace hero renders exactly the same control
  // (label/state/enabled) instead of running its own legacy FLOW action. When
  // the panel unmounts we emit nothing, so the hero must not keep a stale
  // control for a panel that no longer exists.
  const runMainActionRef = useRef<() => void>(runMainAction)
  runMainActionRef.current = runMainAction
  useEffect(() => {
    if (!onScenario) return
    onScenario({
      projectName,
      workspaceId,
      state: mainAction.state,
      label: mainLabel,
      description: mainDescription,
      running: childAlive,
      activity: { title: activity.title, detail: activity.detail, percent: activity.percent },
      elapsed: fmtElapsed(elapsedMs),
      attempt,
      attemptLog: currentAttemptLog,
      runDir: snapshot?.runDir,
      enabled: mainAction.state === 'running' ? true : !busyLocked,
      act: () => runMainActionRef.current(),
    })
  }, [onScenario, projectName, workspaceId, mainAction.state, mainLabel, mainDescription, busyLocked, repairMutationBlocked, childAlive, activity.title, activity.detail, activity.percent, elapsedMs, attempt, currentAttemptLog, snapshot?.runDir])
  useEffect(() => () => onScenario?.(undefined), [onScenario])

  return (
    <section className="roadmap-card" data-testid="iterative-task-panel">
      <header className="roadmap-card-header">
        <div>
          <strong>{text('Обновление проекта', 'Project update')}</strong>
          <span>
            {text(
              'Один последовательный процесс: проверка → подбор версий → применение и проверка изменений.',
              'One continuous process: check → find versions → apply and verify changes.',
            )}
          </span>
        </div>
        <button type="button" className="icon-button" aria-label={text('Обновить', 'Reload')} onClick={() => { void load(); void refreshRunner() }} disabled={busy === 'load'}><RefreshCw size={16} /></button>
      </header>

      {!onStatusView ? <AuditGoalSummary audit={runner?.audit} initial={runner?.initialAudit} onOpenPath={onOpenPath} /> : null}

      {/* User UX: the one-card pipeline. The migration is a linear flow the user
          reads at a glance — Проверка → Подбор версий → Обновление → Результат.
          Which stage is DONE and which is CURRENT comes from the same pure
          scenario mapping as the single main action, so the stepper and the
          button can never disagree about where the project is. */}
      {(() => {
        const pipeline = scenarioPipeline(mainAction.state, !validationChanged && (runner?.checked?.ok === true || (attempt?.status === 'done' && attempt.lastStep === 'checked')), { attempt, runner })
        return (
          <div className="iterative-pipeline" data-testid="iterative-pipeline">
            {PIPELINE_STAGES.map((stage, index) => {
              const done = pipeline.completed.includes(stage.stage)
              const active = stage.stage === pipeline.current
              return (
                <Fragment key={stage.stage}>
                  {index > 0 ? <span className={`iterative-pipeline-connector ${pipeline.completed.includes(PIPELINE_STAGES[index - 1].stage) ? 'on' : ''} ${childAlive && PIPELINE_STAGES[index - 1].stage === pipeline.current ? (activity.percent === undefined ? 'working indeterminate' : 'working') : ''}`} aria-hidden="true"><span style={!childAlive || PIPELINE_STAGES[index - 1].stage !== pipeline.current || activity.percent === undefined ? undefined : { width: `${activity.percent}%` }} /></span> : null}
                <div data-stage={stage.stage} aria-current={active ? 'step' : undefined} className={`iterative-pipeline-step ${done ? 'done' : ''} ${active ? 'active' : ''}`}>
                  <span className="iterative-pipeline-dot" aria-hidden="true">{done ? <Check size={12} /> : index + 1}</span>
                  <span className="iterative-pipeline-name">{language === 'ru' ? stage.ru : stage.en}</span>
                </div>
                </Fragment>
              )
            })}
          </div>
        )
      })()}

      {runner?.phase === 'TERMINAL' && runner.canResumeTerminal === true && ['blocked', 'partial', 'budget-stop', 'no-upgrade'].includes(mainAction.state) ? (
        <div className="resume-notice" data-testid="iterative-resume-result">
          <span>{runner.stopDetail ? text(`Исправление группы из ${runner.stopDetail.packageCount} пакетов остановлено по таймауту. Новых принятых обновлений нет. Правки сохранены отдельно. ${runner.stopDetail.recoverable ? 'Продолжение возобновит ремонт.' : 'Нужен другой вариант обновления.'} Итоговый аудит: ${runner.stopDetail.auditStatus}.`, `Repair of ${runner.stopDetail.packageCount} packages stopped on timeout. No new updates accepted. Trial edits are preserved. ${runner.stopDetail.recoverable ? 'Continue resumes the repair.' : 'A different update proposal is needed.'} Final audit: ${runner.stopDetail.auditStatus}.`) : text('Принятые обновления сохранены. Продолжение сохраняет ограничения и бюджет; непринятые изменения требуют проверки.', 'Accepted updates are saved. Continuing preserves limits and budget; unaccepted changes still need verification.')}</span>
        </div>
      ) : null}

      <MigrationChecksPanel key={`${workspaceId}:${projectName}`} profile={validationDraft ? { ...runner?.validationProfile, ...validationDraft } : runner?.validationProfile} locked={Boolean(runner?.present || childAlive || stepBusy || disabledExternal)} scope={validationChanged ? undefined : runner?.validationScope ?? runner?.checked?.validationScope} onChange={setValidationDraft} />

      {/* L1: durable attempt strip — visible from the very first click, after a
          restart, and during a long check/discovery/cohort. User-facing wording;
          raw status/stage/steps live in the diagnostics below. */}
      {attempt ? (
        <div className={`attempt-strip ${attempt.status === 'done' && attempt.lastStep === 'agent' ? 'paused' : attempt.status}`} data-testid="attempt-strip">
          <div className="attempt-strip-row">
            <strong className={`attempt-status ${attempt.status === 'done' && attempt.lastStep === 'agent' ? 'paused' : attempt.status}`}>{childAlive || attemptAlive ? activity.title : activityText(attempt, text)}</strong>
            <span className="attempt-meta">
              · {text('прошло', 'elapsed')} {fmtElapsed(elapsedMs)}
              {childAlive && attempt.lastHeartbeatAt ? ` · ${text('последний сигнал', 'latest signal')}: ${fmtElapsed(Math.max(0, Date.now() - attempt.lastHeartbeatAt))} ${text('назад', 'ago')}` : ''}
              {attempt.targetSource === 'roadmap'
                ? ` · ${text('версии: по составу', 'versions: from scope')}`
                : attempt.targetSource === 'discovery'
                  ? ` · ${text('версии: авто-поиск', 'versions: auto')}`
                  : attempt.targetSource === 'none'
                    ? ` · ${text('версии: не заданы', 'versions: none')}`
                    : ''}
            </span>
          </div>
          <div className="attempt-strip-row">
            {progress && progress.total > 0 && (activity.percent !== undefined || attempt.discoveryCompleted) ? (
              <span className="attempt-meta">
                {attempt.discoveryCompleted ? text('Подбор версий завершён', 'Version search finished') : text('Подбор версий', 'Finding versions')}: {progress.processed}/{progress.total}{attempt.discoverySkipped ? text(` · не просмотрено по бюджету: ${attempt.discoverySkipped}`, ` · not examined within budget: ${attempt.discoverySkipped}`) : ''}
                <span className={`attempt-progress ${attempt.discoveryCompleted ? 'done' : ''}`}><span style={{ width: `${Math.min(100, Math.round((progress.processed / progress.total) * 100))}%` }} /></span>
              </span>
            ) : null}
            {attempt.status === 'running' && !childAlive ? (
              <span className="attempt-meta attempt-reason">
                {runner?.present
                  ? text('Работа была прервана перезапуском; продолжите с сохранённого состояния.', 'Work was interrupted by a restart; continue from the saved state.')
                  : text('Проверка была прервана до создания прогона. Повторите проверку.', 'The check was interrupted before a run was created. Re-check the project.')}
              </span>
            ) : null}
            {attempt.reason && attempt.status !== 'done' && attempt.stage !== 'drive' ? (
              <span className="attempt-meta attempt-reason">{attempt.reason}</span>
            ) : null}
            {attempt ? (
              <button type="button" className="linklike" onClick={() => void toggleLog()}>
                {showLog ? text('скрыть вывод', 'hide output') : text('показать вывод', 'show output')}
              </button>
            ) : null}
          </div>
          {showLogTail ? (
            <div className="iterative-log-messages">{iterativeLogMessages(currentAttemptLog ?? '').slice(-8).map((message, index) => <pre className="iterative-log-message" key={index}>{message}</pre>)}</div>
          ) : null}
        </div>
      ) : null}

      {runner?.workingCheckout ? (
        <div className="resume-notice" data-testid="iterative-working-checkout">
          <strong>{runner.workingCheckout.kind === 'trial'
            ? text('Изолированная рабочая копия', 'Isolated working checkout')
            : runner.workingCheckout.kind === 'checkpoint'
              ? text('Сохранённый проверенный результат', 'Saved verified result')
              : text('Исходная рабочая копия', 'Original checkout')}{runner.workingCheckout.checkpointId ? ` · ${runner.workingCheckout.checkpointId}` : ''}</strong>
          <span style={{ overflowWrap: 'anywhere' }}>{runner.workingCheckout.path}</span>
          <span>{text('Результат сохраняется отдельно. Этот прогон не пушит основную ветку и не переносит изменения в исходную копию автоматически.', 'The result is saved separately. This run does not push the main branch or automatically transfer changes into the original checkout.')}</span>
          <button type="button" className="button secondary" onClick={() => void onOpenPath(runner.workingCheckout!.path)}>
            <ExternalLink size={16} />{text('Открыть папку', 'Open folder')}
          </button>
        </div>
      ) : null}

      {runner?.runDirectory ? <div className="human-flow-utility iterative-folder-links" data-testid="iteration-folder-links">
        <button className="button secondary" onClick={() => void onOpenPath(runner.artifactsDirectory || runner.runDirectory)}><FileText size={16} />{text('Артефакты прогона', 'Run artifacts')}</button>
        {runner.iterationDirectory ? <button className="button secondary" onClick={() => void onOpenPath(runner.iterationDirectory)}><ExternalLink size={16} />{text('Рабочая директория итерации', 'Iteration working directory')}</button> : null}
      </div> : null}
      {runner?.phase === 'TERMINAL' && runner.workingCheckout?.kind === 'checkpoint' ? <div className="resume-notice" data-testid="delivery-result">
        {runner.residualRepair?.reason ? <p className="warning-text">{text('Оставшиеся цели требуют внимания', 'Remaining goals need attention')} · {runner.residualRepair.attempt}: {runner.residualRepair.reason}</p> : null}
        <strong>{runner.delivery?.status === 'done' ? text(`Коммиты подготовлены · ${runner.delivery.branch}`, `Commits prepared · ${runner.delivery.branch}`) : text('Подготовить результат в Git', 'Prepare the result in Git')}</strong>
        <span>{text('Агент разделит принятые изменения на смысловые коммиты в ветке из настроек. Связанные обновления остаются одной когортой. Затем повторим проверки и аудит.', 'An agent groups accepted changes into semantic commits on the configured branch. Related updates remain one cohort. Checks and audit run again afterwards.')}</span>
        {runner.delivery?.branchResolution === 'configured-branch-preserved' ? <span>{text(`Ветка ${runner.delivery.requestedBranch} сохранена. Результат подготовим в ${runner.delivery.branch}.`, `Branch ${runner.delivery.requestedBranch} was preserved. The result uses ${runner.delivery.branch}.`)}</span> : null}
        {runner.delivery?.commits?.map(commit => <code key={commit}>{commit}</code>)}
        {runner.delivery?.status === 'done' && runner.delivery.cleanup?.status !== 'done' ? <>
          <span>{text('После просмотра можно удалить два документа и только помеченные пояснения этого прогона. Копии документов останутся в артефактах; агент сделает отдельный коммит, затем повторим проверки и аудит.', 'After review, remove the two documents and only this run’s marked explanations. Copies remain in the artifacts; an agent creates a separate commit, followed by checks and audit.')}</span>
          <button className="button secondary" disabled={busyLocked || childAlive} onClick={() => void agentNow(true)}>{runner.delivery.cleanup ? text('Продолжить удаление пояснений', 'Resume note cleanup') : text('Удалить доки и миграционные комментарии', 'Remove docs and migration comments')}</button>
        </> : null}
        {runner.delivery?.cleanup?.archiveRoot ? <button className="button secondary" onClick={() => void onOpenPath(runner.delivery!.cleanup!.archiveRoot)}><FileText size={16} />{text('Сохранённые документы', 'Archived documents')}</button> : null}
        {runner.delivery?.workspaceRoot ? <button className="button secondary" onClick={() => void onOpenPath(runner.delivery!.workspaceRoot)}><ExternalLink size={16} />{text('Открыть ветку результата', 'Open the result branch')}</button> : null}
        {runner.delivery?.status !== 'done' ? <button className="button secondary" disabled={busyLocked || childAlive} onClick={() => void agentNow()}><Rocket size={16} />{runner.delivery ? text('Продолжить подготовку коммитов', 'Resume commit preparation') : text('Устранить остаток и подготовить коммиты', 'Repair remaining goals and prepare commits')}</button> : null}
      </div> : null}

      {runner?.scopeExpansionIssues?.length ? (
        <div className="resume-notice warning" role="status" data-testid="scope-expansion-issues">
          <strong>{text('Для части обновлений нужен уточнённый вариант', 'Some updates need a more specific proposal')}</strong>
          <span>{text('Проверенный результат сохранён. Заблокированные группы остаются в списке незавершённых обновлений; другие группы можно попробовать отдельно.', 'The verified result is preserved. Blocked groups remain in the pending updates; other groups can be tried separately.')}</span>
          {runner.scopeExpansionIssues.map((issue, index) => <span key={index}>
            {issue.reason === 'COHORT_ATOM_EXCEEDS_CAP'
              ? text(`Обязательная группа: ${issue.packageCount} пакетов при лимите ${issue.maxPackages}. Нужен больший лимит или уточнённое предложение companion-пакетов.`, `Required group: ${issue.packageCount} packages, cap ${issue.maxPackages}. Increase the cap or provide a narrower companion proposal.`)
              : `${issue.packages.join(', ')} · ${issue.proposals.join('; ')} · ${issue.nextAction}`}
          </span>)}
        </div>
      ) : null}
      {validationError ? <div className="resume-notice warning" role="alert">{validationError}</div> : null}
      {note ? <div className="resume-notice"><span>{note}</span></div> : null}

      {/* Single error surface: a short reason + next step up front, the full
          technical reason in the expandable diagnostics, and a copy button that
          includes the app version / stage / attempt id / technical reason.
          The no-targets and running states are DECISIONS, not errors — no red
          card, the main action explains itself. */}
      {mainAction.state !== 'no-targets' && (attemptFailure || runnerError || blocker) ? (
        <div className={`resume-notice ${attempt?.status === 'canceled' ? '' : 'danger'}`}>
          <strong>{attempt?.status === 'canceled' ? text('Работа остановлена', 'Work was stopped') : text('Работа не завершилась', 'Work did not finish')}</strong>
          <span>
            {blocker?.summary || (attemptFailure && !blocker ? attemptFailure : undefined) || runnerError || text('Неизвестная причина.', 'Unknown cause.')}
          </span>
          {blocker?.command ? (
            <pre className="attempt-log" style={{ margin: '6px 0 0' }}>{blocker.command}</pre>
          ) : null}
          <footer className="baseline-intent-actions">
            <button type="button" className="linklike" onClick={() => setShowDiag((value) => !value)}>
              {showDiag ? <ChevronUp size={15} /> : <ChevronDown size={15} />}{text('Подробности', 'Details')}
            </button>
            <button type="button" className="linklike" onClick={copyDiagnostics}>
              {copied ? <Check size={15} /> : <Clipboard size={15} />}{copied ? text('Скопировано', 'Copied') : text('Скопировать диагностику', 'Copy diagnostics')}
            </button>
          </footer>
          {showDiag ? (
            <pre className="attempt-log">{attemptFailure}{attempt?.attemptId ? `\nattemptId: ${attempt.attemptId}` : ''}{attempt?.stage ? `\nstage: ${attempt.stage}` : ''}{decisionText ? `\ndecision: ${decisionText}` : ''}</pre>
          ) : null}
        </div>
      ) : null}

      {state.phase === 'loading' ? (
        <div className="resume-notice"><span>{text('Загружаю…', 'Loading…')}</span></div>
      ) : state.phase === 'error' ? (
        <div className="resume-notice danger">
          <strong>{text('Не удалось прочитать задание', 'Could not read the assignment')}</strong>
          <span>{state.message}</span>
        </div>
      ) : (
        <>
          {/* THE single main action for the current scenario state. */}
          <div className="resume-notice">
            <span>{mainDescription}</span>
            <label className="iterative-autopilot-option"><input type="checkbox" checked={runner?.autopilotEnabled || runner?.autopilotActive || autopilot} disabled={disabledExternal === true} onChange={event => { const enabled = event.target.checked; setAutopilot(enabled); void window.dependencyFlow?.setIterativeAutopilot({ workspaceId, projectName, enabled }).then(() => refreshRunner()).catch(error => { setAutopilot(!enabled); setNote(String(error)) }) }} />{text('Автопилот: продолжать автоматически, включая исправления агентом', 'Autopilot: continue automatically, including agent repairs')}</label>
            <small>{runner?.phase === 'TERMINAL'
              ? runner.canResumeTerminal
                ? text('Включите автопилот для автоматического продолжения или продолжите вручную кнопкой ниже.', 'Enable autopilot to continue automatically, or use the button below to continue manually.')
                : text('Прогон завершён. Для продолжения нужен новый вариант обновления; проверенный результат сохранён.', 'The run has ended. Continuing requires a new update proposal; the verified result is preserved.')
              : text('Можно включить во время работы. Автопилот продолжит после текущего шага; выключение остановит автоматические переходы.', 'Enable while running to continue after the current step; disabling stops automatic transitions.')}</small>
            <footer className="baseline-intent-actions">
              <button
                type="button"
                className={`button ${runner?.phase === 'TERMINAL' && (runner.progressSummary?.accepted ?? 0) > 0 && mainAction.state !== 'result' ? 'secondary' : 'primary'}`}
                data-testid="iterative-main-action"
                // P1#2: Stop must stay AVAILABLE while work is running — the
                // busy lock disables everything EXCEPT the in-flight Stop.
                disabled={mainAction.state === 'running' ? false : busyLocked || Boolean(validationError)}
                onClick={() => void runMainAction()}
              >
                {mainAction.state === 'running' ? <X size={16} /> : mainAction.state === 'agent' || mainAction.state === 'repair-current' ? <Wrench size={16} /> : mainAction.state === 'result' || mainAction.state === 'partial' || mainAction.state === 'budget-stop' || mainAction.state === 'blocked' || mainAction.state === 'no-upgrade' ? <FileText size={16} /> : <Rocket size={16} />}
                {mainLabel}
              </button>
              {runner?.phase === 'TERMINAL' && runner.canResumeTerminal === true && ['blocked', 'partial', 'budget-stop', 'no-upgrade'].includes(mainAction.state) ? (
                <button type="button" className="button primary" data-testid="iterative-resume-action"
                  disabled={busyLocked || childAlive} onClick={() => { void driveNow(false, true) }}>
                  <Rocket size={16} />{text('Продолжить с сохранённого результата', 'Continue from saved result')}
                </button>
              ) : null}
              {(attempt || runner?.present) && mainAction.state !== 'running' ? (
                <button type="button" className="button secondary" data-testid="iterative-restart"
                  disabled={busyLocked}
                  title={text('Прежний прогон, снимки и результаты сохранятся в истории. Проверим текущие файлы проекта заново.', 'The previous run, snapshots and results stay in history. Re-check the current project files.')}
                  onClick={() => void beginNow({ mode: 'none', checkOnly: true, restart: true })}>
                  <RefreshCw size={16} />{text('Начать заново', 'Start over')}
                </button>
              ) : null}
              {mainAction.state === 'no-targets' ? (
                <button type="button" className="button secondary" disabled={busyLocked} onClick={() => onConfigureScope?.()}>
                  <Wrench size={16} />{text('Настроить обновление', 'Configure the update')}
                </button>
              ) : null}
              {!busyLocked && !runner?.present && !noTargetsStep && runner?.legacyPlanPresent ? (
                <>
                  <button type="button" className="button secondary" title={text('Использует последний сохранённый план Baseline. Обновления не применяются; проверки нужно выполнить заново.', 'Uses the last saved Baseline plan. No updates are applied; checks must run again.')} onClick={() => void exportLegacyNow()}>
                    <FileText size={16} />{text('Создать ТЗ из прежнего плана', 'Create an assignment from an earlier plan')}
                  </button>
                </>
              ) : null}
            </footer>
            {runtimeLine(runner) ? <span className="attempt-meta">{runtimeLine(runner)}</span> : null}
          </div>

          {!runner?.present ? (
            <div className="resume-notice">
              <strong>{text('Перед запуском', 'Before you start')}</strong>
              <span>
                {text(
                  'Draft предлагает план обновления; запуск применяет и проверяет изменения. Состав и политика настраиваются кнопкой «Настроить состав / Draft», агент, модель и Node — в настройках запуска выше.',
                  'The Draft proposes an update plan; the launch applies and verifies the changes. Use Configure scope / Draft for scope and policy; set the agent, model and Node in Run settings above.',
                )}
              </span>
            </div>
          ) : null}

          {runner?.progressSummary ? <>
            <div className="roadmap-summary-grid">
              <div><strong>{runner.progressSummary.remaining}</strong><span>{text('до предложенных версий', 'below proposed versions')}</span></div>
              <div><strong>{runner.progressSummary.accepted}</strong><span>{text('принято обновлений', 'accepted updates')}</span></div>
              <div><strong>{runner.progressSummary.deferred}</strong><span>{text('отложено', 'deferred')}</span></div>
            </div>
            <p className="attempt-meta">{runner.progressSummary.checkpointId} · {text('всего целей', 'total goals')}: {runner.progressSummary.denominator} · {text('цели с версиями', 'versioned targets')}: {runner.progressSummary.targetCount} · {text('без подобранной версии', 'without a selected version')}: {runner.progressSummary.unresolvedGoals}</p>
          </> : null}

          {!task ? (
            <div className="resume-notice">
              <strong>{text('Задание ещё не сформировано', 'The assignment is not ready yet')}</strong>
              {insufficient ? (
                <span>
                  {text('Здесь появятся подтверждённые обновления и итоговое ТЗ. Начните обновление после проверки проекта.', 'Verified updates and the final assignment will appear here. Start the update after checking the project.')}
                </span>
              ) : (
                <span>{text('После проверки и обновлений задание появится здесь — его можно посмотреть и скопировать.', 'After the check and updates, the assignment appears here — you can view and copy it.')}</span>
              )}
            </div>
          ) : (
            <>
              {snapshot?.stale ? (
                <div className="resume-notice warning">
                  <strong>{text('ТЗ устарело', 'The assignment is stale')}</strong>
                  <span>{snapshot.staleReason}</span>
                </div>
              ) : null}

              <footer className="baseline-intent-actions">
                {!['result', 'partial', 'budget-stop', 'blocked', 'no-upgrade'].includes(mainAction.state) ? (
                  <button type="button" className="button secondary" disabled={busyLocked} onClick={() => setShowDialog(true)}>
                    <FileText size={16} />{text('Посмотреть задание', 'View assignment')}
                  </button>
                ) : null}
                <button type="button" className="button secondary" disabled={busyLocked} onClick={() => void run('copy', () => onCopy(projectName, language))}>
                  {copied && !showDiag ? <Check size={16} /> : <Clipboard size={16} />}{text('Скопировать задание', 'Copy the assignment')}
                </button>
                <button type="button" className="button secondary" disabled={busyLocked} onClick={() => void run('save', () => onSave(projectName, language))}>
                  <Save size={16} />{text('Сохранить…', 'Save…')}
                </button>
                <button type="button" className="button secondary" disabled={busyLocked} onClick={() => void run('export', () => onExport(projectName))}>
                  <RefreshCw size={16} />{text('Пересформировать', 'Rebuild')}
                </button>
              </footer>
            </>
          )}
        </>
      )}

      {showDialog && task ? (
        <div className="baseline-intent-backdrop" role="presentation" onClick={() => setShowDialog(false)}>
          <section className="baseline-intent-dialog" role="dialog" aria-modal="true" aria-labelledby="iterative-task-title" onClick={(event) => event.stopPropagation()}>
            <header className="baseline-intent-header">
              <div>
                <FileText size={18} />
                <div>
                  <strong id="iterative-task-title">{text('Задание на адаптацию зависимостей', 'Dependency adaptation assignment')}</strong>
                  <span>{task.runId} · {task.targetCheckpointId} · {task.artifactId}</span>
                </div>
              </div>
              <button type="button" className="icon-button" aria-label={text('Закрыть', 'Close')} onClick={() => setShowDialog(false)}><X size={17} /></button>
            </header>
            <pre data-testid="iterative-task-preview" style={{ margin: 0, maxHeight: '56vh', overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', padding: '16px', borderRadius: '10px', border: '1px solid var(--border, rgba(127, 127, 127, .3))', background: 'var(--panel, rgba(0, 0, 0, .08))', fontSize: '12px', lineHeight: 1.55 }}>{task.content}</pre>
            <footer className="baseline-intent-actions">
              {snapshot.runDir ? (
                <button type="button" className="button secondary" onClick={() => void onOpenPath(snapshot.runDir)}><ExternalLink size={16} />{text('Открыть папку задания', 'Open task folder')}</button>
              ) : null}
              <button type="button" className="button primary" disabled={busy !== undefined} onClick={() => void run('copy', () => onCopy(projectName, language))}>
                {copied ? <Check size={16} /> : <Clipboard size={16} />}{copied ? text('Скопировано', 'Copied') : text('Скопировать задание', 'Copy assignment')}
              </button>
            </footer>
          </section>
        </div>
      ) : null}
    </section>
  )
}
