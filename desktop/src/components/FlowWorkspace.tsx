import { FileText, LoaderCircle, Play, RotateCcw, ShieldCheck } from 'lucide-react'

import { useEffect, useMemo, useRef, useState } from 'react'
import { ACTION_ORDER, FLOW_STAGES } from '../data/flow'
import { useLanguage } from '../i18n'
import { BranchFailureModal } from './BranchFailureModal'
import { QuickSelect } from './QuickSelect'
import { ModelPicker } from './ModelPicker'
import { BaselineIntentDialog } from './BaselineIntentDialog'
import { LogPanel } from './LogPanel'
import { PromptPreviewDialog } from './PromptPreviewDialog'
import { IterativeTaskPanel } from './IterativeTaskPanel'
import { AuditGoalSummary } from './AuditGoalSummary'
import { IterativeRunDiagnostics } from './IterativeRunDiagnostics'
import { normalizeBaselineIntentPlan } from '../data/baselineIntent'
import type { ScenarioSignal } from '../../electron/iterative-scenario'
import type { DraftProgressPayload } from '../hooks/useDependencyFlow'
import type { ActionInput, AgentProvider, BaselineDecision, BaselineIntent, BaselineIntentPlan, DraftResultSnapshot, EnvironmentInfo, FlowAction, IterativeAgentOutcome, IterativeAttemptView, IterativeBeginOutcome, IterativeDriveOutcome, IterativeStatusOutcome, IterativeTaskActionOutcome, IterativeTaskSnapshot, JobOutput, MigrationBranchProgress, ProjectPromptPreview, ProjectSpec, TargetLevel, WorkspaceDetails } from '../types'


type Props = {
  details: WorkspaceDetails
  project: ProjectSpec
  appVersion?: string
  activeAction?: FlowAction
  activeRunId?: string
  activeRunStartedAt?: number
  activeDraftProgress?: DraftProgressPayload
  draftLaunch?: { workspaceId?: string; projectName: string; runId?: string; autoOpen: boolean; proofMode?: 'DRAFT' | 'VERIFIED'; acknowledgedRunId?: string; at: number }
  onMarkDraftLaunched: (workspaceId: string | undefined, projectName: string, runId?: string, autoOpen?: boolean, proofMode?: 'DRAFT' | 'VERIFIED') => void
  onResetDraftLaunch: (workspaceId: string | undefined, projectName: string, autoOpen?: boolean, proofMode?: 'DRAFT' | 'VERIFIED') => void
  onAcknowledgeDraftRun: (workspaceId: string | undefined, projectName: string, runId?: string) => void
  autopilotActive?: boolean
  baselineDecision?: BaselineDecision
  onClearBaselineDecision: () => void
  onGetBaselineIntentPlan: (projectName: string) => Promise<BaselineIntentPlan>
  onGetCurrentDraftResult: (input: { workspaceId?: string; projectName: string; runId?: string }) => Promise<{ result: DraftResultSnapshot; prompt?: ProjectPromptPreview; plan?: string } | undefined>
  onRun: (input: ActionInput) => Promise<{ jobId: string; runId?: string } | undefined>
  onSendAgentNote: (note: string, branch?: string) => Promise<boolean>
  onStartAutopilot: (input: { workspaceId: string; projectName: string; target: TargetLevel; releaseBranch?: string }) => Promise<void>
  onStopAutopilot: () => Promise<void>
  onRecoverWithAgent: (input: { workspaceId?: string; projectName: string; note: string }) => Promise<void>
  onOpenDashboard: () => void
  onOpenPath: (path?: string) => Promise<void>
  onChoosePrompt: (projectName: string) => Promise<void>
  onUpdateWorkspace: (patch: { id: string; agent?: AgentProvider; agentModel?: string }) => Promise<void>
  onUpdateProjectBranches: (input: { workspaceId?: string; projectName: string; branchBase?: string; push?: boolean }) => Promise<void>
  onUpdateProjectNode: (input: { workspaceId?: string; projectName: string; nodeVersion?: string }) => Promise<void>
  onListNodeVersions: () => Promise<string[]>
  onListAgentModels: (agentProvider: AgentProvider, cwd?: string) => Promise<string[]>
  onGetIterativeTask: (projectName: string) => Promise<IterativeTaskSnapshot>
  onExportIterativeTask: (projectName: string) => Promise<IterativeTaskActionOutcome>
  onExportLegacyIterativeTask: (projectName: string) => Promise<IterativeTaskActionOutcome>
  onCopyIterativeTask: (projectName: string, language?: string) => Promise<IterativeTaskActionOutcome>
  onSaveIterativeTask: (projectName: string, language?: string) => Promise<IterativeTaskActionOutcome>
  onIterativeStatus: (projectName: string) => Promise<IterativeStatusOutcome>
  onIterativeDrive: (projectName: string, autopilot?: boolean, retryInfra?: boolean, discardCandidate?: boolean, resumeTerminal?: boolean) => Promise<IterativeDriveOutcome>
  onIterativeBegin: (projectName: string, discovery?: { autopilot?: boolean; mode: 'auto' | 'none'; timeoutSeconds?: number; parallelism?: number; maxPackages?: number; validationProfile?: { commands: string[]; unitCommand?: string; compareExistingFailures?: boolean; deferredChecks?: string }; checkOnly?: boolean; repair?: boolean; restart?: boolean }) => Promise<IterativeBeginOutcome>
  onIterativeAgent: (projectName: string, autopilot?: boolean, cleanupNotes?: boolean) => Promise<IterativeAgentOutcome>
  onIterativeAttempt: (projectName: string, includeRunLog?: boolean) => Promise<{ ok: boolean; present: boolean; attempt?: IterativeAttemptView; attemptLog?: string; runLog?: string; error?: string }>
  onIterativeCancel: (projectName: string) => Promise<{ ok: boolean }>
  liveIterativeAttempt?: IterativeAttemptView
  // Live log window (activity bubbles) for the current run, rendered below all
  // the flow content so failures can be inspected in place.
  logs?: JobOutput[]
  onCancelJob?: () => Promise<void>
  onClearLogs?: () => void
}

export function FlowWorkspace({ details, project, appVersion, activeAction, activeRunId, activeRunStartedAt, activeDraftProgress, draftLaunch, onMarkDraftLaunched, onResetDraftLaunch, onAcknowledgeDraftRun, baselineDecision, onClearBaselineDecision, onGetBaselineIntentPlan, onGetCurrentDraftResult, onRun, onSendAgentNote, onOpenDashboard, onOpenPath, onUpdateWorkspace, onUpdateProjectBranches, onUpdateProjectNode, onListNodeVersions, onListAgentModels, onGetIterativeTask, onExportIterativeTask, onExportLegacyIterativeTask, onCopyIterativeTask, onSaveIterativeTask, onIterativeStatus, onIterativeDrive, onIterativeBegin, onIterativeAgent, onIterativeAttempt, onIterativeCancel, liveIterativeAttempt, logs, onCancelJob, onClearLogs }: Props) {
  const { language, text, t } = useLanguage()
  // The persisted goal drives stage actions and autopilot: a green target set
  // in the Baseline dialog must survive into generate/release instead of
  // being reset to a hard-coded yellow on the next step (F1).
  const target: TargetLevel = details.baselineIntent?.targetLevel === 'green' ? 'green' : 'yellow'
  const [label] = useState('')
  const [releaseBranch, setReleaseBranch] = useState(project.git?.releaseBranch || 'libs-release')
  const [gateCommand] = useState('')
  const [agentNote, setAgentNote] = useState('')
  const [selectedBranchFailure, setSelectedBranchFailure] = useState<MigrationBranchProgress | null>(null)
  const [, setSelectedStageIndex] = useState<number | null>(null)
  const [baselineIntentDialog, setBaselineIntentDialog] = useState<{ mode: 'prepare' | 'decision'; resume: 'auto' | 'continue' | 'restart'; plan: BaselineIntentPlan; decision?: BaselineDecision }>()
  const [draftPromptPreview, setDraftPromptPreview] = useState<ProjectPromptPreview>()
  const [iterativeView, setIterativeView] = useState<IterativeStatusOutcome>()
  useEffect(() => setIterativeView(undefined), [details.workspace.id, project.name])
  // Draft completion is resolved by runId from workspace state, not by watching
  // file mtime/size. The launch marker lives in the flow hook (it survives
  // FlowWorkspace remounts across tabs), and the ack map keys by workspace AND
  // project so two workspaces sharing a project name never hide each other's
  // result. The completion banner is fresh only for the exact run the user
  // launched (marker.runId match), never for a leftover earlier result; the
  // acknowledge (dismiss / auto-open) is recorded in the hook too, so a remount
  // or a fresh details object can never re-open an already consumed preview.
  // After acknowledge the persistent "последний Draft" card stays reachable.
  const draftResult = details.draftResult
  const draftLaunchActive = activeAction === 'baseline' && draftLaunch?.proofMode === 'DRAFT' && (draftLaunch.runId === undefined || draftLaunch.runId === activeRunId)
  const draftResultFresh = Boolean(draftResult && draftLaunch?.runId && draftLaunch.acknowledgedRunId !== draftResult.runId && draftResult.runId === draftLaunch.runId)
  const acknowledgeDraftResult = () => { if (draftResult) onAcknowledgeDraftRun(details.workspace.id, project.name, draftResult.runId) }
  // Returns true only when the prompt content was confirmed delivered to the
  // dialog; the caller (auto-open) uses that to consume the run's one-shot
  // freshness, never before receipt.
  const openDraftPrompt = async (): Promise<boolean> => {
    try {
      const loaded = await onGetCurrentDraftResult({ workspaceId: details.workspace.id, projectName: project.name, runId: draftResult?.runId })
      if (loaded?.prompt) {
        setDraftPromptPreview(loaded.prompt)
        return true
      }
      window.alert(language === 'ru' ? 'Draft завершился, но prompt artifact не найден. Проверьте артефакты запуска.' : 'Draft finished, but the prompt artifact was not found. Check the run artifacts.')
      return false
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error))
      return false
    }
  }
  const openDraftPromptRef = useRef(openDraftPrompt)
  openDraftPromptRef.current = openDraftPrompt
  // Auto-open the prompt of the run the user explicitly asked to show
  // ("Создать Draft и показать промпт"), and only for that exact run's
  // completion. Until the launch marker is bound to a concrete runId (the job
  // started), an older leftover result must never be auto-opened. F5: the
  // auto-open consumes the run's freshness by runId only AFTER the prompt
  // content was confirmed delivered (openDraftPrompt resolved true), so a
  // remount, a new details object or a Dashboard→FLOW switch can never re-open
  // an already shown run; the manual "Принято" button and the persistent
  // "последний Draft" card remain reachable.
  useEffect(() => {
    if (!draftResult || !draftLaunch || !draftResultFresh) return
    if (!draftLaunch.autoOpen || !draftLaunch.runId) return
    if (draftResult.runId !== draftLaunch.runId) return
    void openDraftPromptRef.current().then((confirmed) => {
      if (confirmed) onAcknowledgeDraftRun(details.workspace.id, project.name, draftResult.runId)
    })
  }, [draftLaunch, draftResult, draftResultFresh])
  const [baselineDecisionDismissed, setBaselineDecisionDismissed] = useState(false)
  const run = details.teamState?.projects[project.name]
  const recovery = run?.recovery
  const baselineRestartRequired = recovery?.action === 'baseline' && (recovery.code === 'BASELINE_RECOVERY_CONTINUE_UNAVAILABLE' || recovery.message.includes('BASELINE_RECOVERY_CONTINUE_UNAVAILABLE'))
  // Legacy whole-migration session, or (once the per-branch-group loop has
  // run for this project) whichever branch it was last working on.
  const activeGroupSession = run?.activeAgentBranch ? run.agentSessions?.[run.activeAgentBranch] : undefined
  const interruptedSession = run?.agentSession?.interrupted
    ? run.agentSession
    : activeGroupSession?.interrupted ? activeGroupSession : undefined
  const canResumeAgent = interruptedSession?.provider === details.workspace.agent
  const completed = useMemo(() => new Set(run?.completedActions ?? (run?.status === 'passed' && run.lastAction ? ACTION_ORDER.slice(0, ACTION_ORDER.indexOf(run.lastAction as never) + 1) : [])), [run])
  // Only the stage being re-run loses its check mark. Clearing everything from
  // the running stage onwards made the progress bar walk backwards during a
  // run and flipped finished stages to "Ожидает"; the stored state already
  // drops the genuinely invalidated downstream stages when a stage restarts.
  const promptReady = Boolean(details.projectPromptPath && details.migrationProgress?.project === project.name)
  // A branch already created/ready/merged means the plan is mid-flight even
  // when there's no interrupted agent CLI session left to resume (e.g. the
  // orchestrator's own merge step stopped on a conflict and the user just
  // fixed it by hand) -- the primary button must read as "continue", not
  // "start", or it looks identical to the destructive "Начать заново" action.
  const planReady = details.dashboardExists && promptReady
  const runningIndex = activeAction ? FLOW_STAGES.findIndex((stage) => stage.action === activeAction) : -1
  const currentIndex = runningIndex >= 0 ? runningIndex : FLOW_STAGES.findIndex((stage) => stage.action ? !completed.has(stage.action) : !planReady)
  const currentLevel = details.projectLevels[project.name]
  const levelRefreshing = ['baseline', 'generate', 'generate-all'].includes(activeAction ?? '')
  const measuredDate = currentLevel?.measuredAt ? new Date(currentLevel.measuredAt) : undefined
  const measuredLabel = measuredDate && !Number.isNaN(measuredDate.getTime()) ? measuredDate.toLocaleString(language === 'ru' ? 'ru-RU' : 'en-US', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : undefined
  const acceptance = details.acceptanceVerdict
  const acceptanceAccepted = acceptance?.status === 'ACCEPTED'
  const acceptanceNeedsRemediation = acceptance?.status === 'REMEDIATION_REQUIRED'
  const acceptanceUnknown = !acceptance || acceptance.status === 'UNKNOWN'
  // Publication is an explicit opt-in. An accepted, release/state-complete run
  // is a completed product result even when git.push is intentionally disabled.
  const requiredCompletionActions = project.git?.push ? ACTION_ORDER : ACTION_ORDER.filter((action) => action !== 'push-workspace')
  const actionsComplete = !activeAction && requiredCompletionActions.every((action) => completed.has(action))
  const flowComplete = actionsComplete && acceptanceAccepted
  const activeIndex = currentIndex < 0 ? FLOW_STAGES.length - 1 : currentIndex
  const active = Boolean(activeAction)
  // P1#1: the deterministic iterative scenario owns the ONE main action. The
  // panel mirrors it here; while a signal is present, the hero never renders
  // its own competing legacy FLOW primary.
  const [reportedScenario, setScenarioSignal] = useState<ScenarioSignal | undefined>(undefined)
  const scenarioSignal = reportedScenario?.projectName === project.name && reportedScenario.workspaceId === details.workspace.id ? reportedScenario : undefined
  const scenarioOnScenario = setScenarioSignal
  const acceptanceTone = acceptanceAccepted ? 'success' : acceptanceNeedsRemediation ? 'danger' : 'muted'
  const acceptanceLabel = acceptanceAccepted ? 'ACCEPTED' : acceptanceNeedsRemediation ? 'REMEDIATION_REQUIRED' : 'UNKNOWN'
  const configuredBranch = project.git?.baseBranch || project.git?.branchPrefix || 'libs'
  const [branchBase, setBranchBase] = useState(configuredBranch)
  const [pushEnabled, setPushEnabled] = useState(Boolean(project.git?.push))
  const configuredNodeVersion = project.nodeVersion ?? ''
  const [nodeVersionDraft, setNodeVersionDraft] = useState(configuredNodeVersion)
  const [availableNodeVersions, setAvailableNodeVersions] = useState<string[]>([])
  useEffect(() => {
    void onListNodeVersions().then((versions) => setAvailableNodeVersions(versions)).catch(() => setAvailableNodeVersions([]))
  }, [onListNodeVersions])
  const [agentModel, setAgentModel] = useState(details.workspace.agentModel ?? '')
  const [modelSuggestions, setModelSuggestions] = useState<string[]>([])
  const [modelListError, setModelListError] = useState<string>()
  const [modelListLoaded, setModelListLoaded] = useState(false)

  useEffect(() => {
    setBranchBase(configuredBranch)
    setPushEnabled(Boolean(project.git?.push))
  }, [configuredBranch, project.git?.push, project.name])

  useEffect(() => { setAgentModel(details.workspace.agentModel ?? '') }, [details.workspace.agentModel, details.workspace.id])
  useEffect(() => { setReleaseBranch(run?.releaseBranch || project.git?.releaseBranch || 'libs-release') }, [project.git?.releaseBranch, project.name, run?.releaseBranch])
  // Resetting on `currentIndex` moved the detail panel -- and the primary
  // button with it -- out from under the user whenever a background refresh
  // shifted the computed stage, so a click could run a different stage than
  // the one on screen. Only switching projects invalidates the selection.
  useEffect(() => { setSelectedStageIndex(null) }, [project.name])

  useEffect(() => {
    let cancelled = false
    setModelSuggestions([])
    setModelListError(undefined)
    setModelListLoaded(false)
    void onListAgentModels(details.workspace.agent, project.path).then((models) => { if (!cancelled) { setModelSuggestions(models); setModelListLoaded(true) } }).catch(error => { if (!cancelled) setModelListError(String(error)) })
    return () => { cancelled = true }
  }, [details.workspace.agent, project.path, onListAgentModels])

  // BLOCK_W_P0_P1_TYPES_NESTED_FIX_V1
  const openBaselineIntentDialog = async (mode: 'prepare' | 'decision', resume: 'auto' | 'continue' | 'restart', decision?: BaselineDecision) => {
    try {
      const loaded = normalizeBaselineIntentPlan(await onGetBaselineIntentPlan(project.name))
      // A new Baseline retries package scope/deferred cohorts from a clean
      // slate; every product-level preference (target level, lag %, lag
      // window, productMode, control/budget, M/L caps) stays as saved.
      const plan = mode === 'prepare' ? { ...loaded, intent: {
        ...loaded.intent,
        policies: {},
        deferredCohorts: [],
        cohortAction: undefined,
        autoOpenPrompt: undefined,
      } } : loaded
      setBaselineIntentDialog({ mode, resume, plan, decision })
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error))
    }
  }

  const openDeferredImprovementDialog = async () => {
    try {
      const loaded = normalizeBaselineIntentPlan(await onGetBaselineIntentPlan(project.name))
      // Improvement preserves the deferred queue/user keep-current policy.
      setBaselineIntentDialog({ mode: 'prepare', resume: 'restart', plan: loaded })
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error))
    }
  }

  const baselinePolicyIdentity = (intent: BaselineIntent) =>
    JSON.stringify([
      Object.entries(intent.policies ?? {}).sort(([left], [right]) => left.localeCompare(right)),
      // T2: the goal criteria are part of the intent identity. Changing the
      // target level or the numeric lag gate must invalidate a pending
      // "continue" resume just like a policy change, or the resumed run would
      // silently keep the old goal.
      intent.targetLevel ?? 'yellow',
      intent.minLagOkPct ?? 80,
      intent.acceptancePolicy ?? null,
    ])


  const runBaselineIntent = async (intent: BaselineIntent, autoOpenPrompt = false) => {
    const pending = baselineIntentDialog
    if (!pending) return
    const policyChanged =
      pending.mode === 'decision' &&
      baselinePolicyIdentity(pending.plan.intent) !== baselinePolicyIdentity(intent)
    const effectiveBaselineResume = policyChanged ? 'restart' : pending.resume
    const effectiveTarget: TargetLevel = intent.targetLevel === 'green' ? 'green' : 'yellow'
    // Any new baseline start invalidates the previous launch marker: while
    // this job runs, an older Draft's banner must not stay "fresh", and a
    // Verified run must never inherit the Draft banner/progress strip. For a
    // Draft launch the marker is then re-armed with autoOpen and bound to the
    // exact runId only after the job actually started (ack).
    onResetDraftLaunch(details.workspace.id, project.name, intent.proofMode === 'DRAFT' ? autoOpenPrompt : false, intent.proofMode)
    const started = await onRun({ action: 'baseline', workspaceId: details.workspace.id, projectName: project.name, target: effectiveTarget, label, releaseBranch, gateCommand, baselineResume: effectiveBaselineResume, baselineIntent: intent, commitMessage: `chore(deps): save ${project.name} roadmap state` })
    // A failed start must not arm the completion banner or close the intent
    // dialog: runAction already surfaced the error to the user, and the marker
    // stays unbound (runId undefined) so no old result can match it later.
    if (!started) return
    // Bind the launch marker to the exact runId only after the job starts; the
    // fresh banner and auto-open then match that run, never an older leftover.
    if (intent.proofMode === 'DRAFT') {
      onMarkDraftLaunched(details.workspace.id, project.name, started.runId, autoOpenPrompt, 'DRAFT')
    }
    // Completion is delivered through the runId-scoped draft result in
    // workspace details (main.ts imports artifacts/runs/<runId>/draft/*
    // after the planner exits); no file mtime/size watching.
    setBaselineIntentDialog(undefined)
    if (pending.mode === 'decision') { setBaselineDecisionDismissed(false); onClearBaselineDecision() }
  }

  useEffect(() => {
    if (!baselineDecision || baselineDecisionDismissed || baselineDecision.project !== project.name || baselineIntentDialog?.mode === 'decision') return
    let cancelled = false
    void onGetBaselineIntentPlan(project.name).then((rawPlan) => {
      if (cancelled) return
      setBaselineIntentDialog({
        mode: 'decision',
        resume: 'continue',
        plan: normalizeBaselineIntentPlan(rawPlan),
        decision: baselineDecision,
      })
    })
    return () => { cancelled = true }
  }, [baselineDecision, baselineDecisionDismissed, baselineIntentDialog?.mode, onGetBaselineIntentPlan, project.name])

  useEffect(() => { setBaselineDecisionDismissed(false) }, [baselineDecision])

  const execute = async (stageIndex: number, resumeOverride?: boolean, restartMigration?: boolean, baselineResume?: 'auto' | 'continue' | 'restart') => {
    const stage = FLOW_STAGES[stageIndex]
    if (!stage.action) { onOpenDashboard(); return }
    const resumeAgent = stage.action === 'agent' && (resumeOverride ?? canResumeAgent)
    const skipBaselineModeConfirmation = stage.action === 'baseline'
    if (stage.confirmationKey && !skipBaselineModeConfirmation && !window.confirm(t(stage.confirmationKey))) return
    // N7: an explicit "start over" must bypass the decision branch, or the
    // dialog would open on the pending decision and "restart" would stay dead.
    if (stage.action === 'baseline' && baselineResume === 'restart') {
      await openBaselineIntentDialog('prepare', 'restart')
      return
    }
    if (stage.action === 'baseline' && baselineDecision) {
      setBaselineDecisionDismissed(false)
      await openBaselineIntentDialog('decision', 'continue', baselineDecision)
      return
    }
    if (stage.action === 'baseline' && baselineResume !== 'continue') {
      await openBaselineIntentDialog('prepare', baselineResume ?? 'auto')
      return
    }
    const noteToSend = stage.action === 'agent' && !restartMigration ? agentNote.trim() || undefined : undefined
    await persistAgentModel()
    await onRun({ action: stage.action, workspaceId: details.workspace.id, projectName: project.name, target, label, releaseBranch, gateCommand, resumeAgent, restartMigration, baselineResume: stage.action === 'baseline' ? (baselineResume ?? 'auto') : undefined, agentNote: noteToSend, commitMessage: `chore(deps): save ${project.name} roadmap state` })
    if (noteToSend) setAgentNote('')
  }

  const restartBaseline = (stageIndex: number) => {
    if (window.confirm(text('Начать Baseline заново? Оркестрационный checkpoint будет сброшен, но exact proof/artifact cache с совпадающей identity останется доступен.', 'Restart Baseline? The orchestration checkpoint will be reset, while exact proof/artifact cache with matching identity remains reusable.'))) {
      void execute(stageIndex, undefined, undefined, 'restart')
    }
  }



  const modelValidationError = modelListError || (details.workspace.agent === 'opencode' && agentModel.trim() && modelListLoaded && !modelSuggestions.includes(agentModel.trim()) ? text('Модель недоступна у выбранного провайдера. Выберите её из списка.', 'Model unavailable for the selected provider. Choose one from the list.') : undefined)
  const validateAgentSelection = async () => {
    if (details.workspace.agent !== 'opencode' || !agentModel.trim()) return
    const available = await onListAgentModels(details.workspace.agent, project.path)
    setModelSuggestions(available); setModelListLoaded(true)
    if (!available.includes(agentModel.trim())) throw new Error(text(`Модель ${agentModel.trim()} недоступна. Выберите доступную модель выше.`, `Model ${agentModel.trim()} unavailable. Choose an available model above.`))
  }

  const persistAgentModel = async (value = agentModel) => {
    const trimmed = value.trim()
    setAgentModel(trimmed)
    if (trimmed === (details.workspace.agentModel ?? '')) return
    try {
      await onUpdateWorkspace({ id: details.workspace.id, agentModel: trimmed })
    } catch (error) {
      setAgentModel(details.workspace.agentModel ?? '')
      throw error
    }
  }


  const persistGitSettings = async (nextBranch = branchBase, nextPush = pushEnabled) => {
    const normalizedBranch = nextBranch.trim() || 'libs'
    setBranchBase(normalizedBranch)
    setPushEnabled(nextPush)
    try {
      await onUpdateProjectBranches({
        workspaceId: details.workspace.id,
        projectName: project.name,
        branchBase: normalizedBranch,
        push: nextPush,
      })
    } catch (error) {
      setBranchBase(configuredBranch)
      setPushEnabled(Boolean(project.git?.push))
      window.alert(error instanceof Error ? error.message : String(error))
    }
  }

  const persistNodeVersion = async (nextVersion: string) => {
    const normalized = nextVersion.trim()
    setNodeVersionDraft(normalized)
    try {
      await onUpdateProjectNode({
        workspaceId: details.workspace.id,
        projectName: project.name,
        nodeVersion: normalized || undefined,
      })
    } catch (error) {
      setNodeVersionDraft(configuredNodeVersion)
      throw error
    }
  }


  // BLOCK_PROGRESSIVE_HUMAN_FLOW_V1
  const humanVerifiedUpdates = details.migrationProgress?.completedDependencies
    ?? baselineDecision?.bestIncumbent?.changed_dependency_count
    ?? 0
  const humanActiveUpdates = details.migrationProgress
    ? details.migrationProgress.activeDependencies
    : undefined
  const humanDeferredUpdates = baselineDecision?.bestIncumbent?.deferred_targets?.length
  const humanRemainingUpdates = details.migrationProgress?.unmetPackages.length
  const humanFreshness = typeof currentLevel?.lagOkPct === 'number' ? `${currentLevel.lagOkPct.toFixed(1)}%` : '—'
  const currentScenario = !active ? scenarioSignal : undefined
  const humanStatusTitle = currentScenario
    ? (currentScenario.running || currentScenario.attempt?.status === 'failed' || currentScenario.attempt?.status === 'canceled' || currentScenario.state === 'recovered-running' ? currentScenario.activity.title : currentScenario.state === 'ready' ? text('Проект готов к обновлению', 'Project ready to update') : currentScenario.label)
    : flowComplete
    ? text('Полезный результат готов', 'Useful result is ready')
    : draftLaunchActive
      ? text('Готовим черновой план', 'Preparing a draft plan')
      : activeAction === 'baseline'
        ? text('Ищем первый проверенный результат', 'Finding the first verified result')
        : active
          ? text('DepLoom продолжает работу', 'DepLoom is working')
          : baselineDecision?.bestIncumbent
            ? text('Проверенный результат сохранён', 'Verified result is saved')
            : acceptanceNeedsRemediation
              ? text('Улучшаем безопасность', 'Improving security')
              : acceptanceUnknown && completed.has('audit')
                ? text('Нужен свежий аудит', 'A fresh audit is required')
                : text('Готовы к следующему шагу', 'Ready for the next step')
  const humanStatusBody = currentScenario
    ? [currentScenario.running ? currentScenario.activity.detail : currentScenario.description, currentScenario.attempt ? `${text('прошло', 'elapsed')} ${currentScenario.elapsed}` : ''].filter(Boolean).join(' · ')
    : flowComplete
    ? text('Все обязательные проверки пройдены. Можно остановиться здесь или позже вернуться к оставшимся обновлениям.', 'All required checks passed. You can stop here or return to the remaining upgrades later.')
    : draftLaunchActive
      ? text('Черновой план строится без установки и physical-проверок: инвентаризация → обогащение → публикация промпта. Проект не изменяется.', 'The draft plan is built without installing or physical checks: inventory → enrichment → prompt publication. The project is left untouched.')
      : activeAction === 'baseline'
        ? text('Пробуем полезные наборы обновлений и физически проверяем проект. Неудачная следующая попытка не должна уничтожать последний доказанный результат.', 'Trying useful upgrade sets and physically verifying the project. A failed next attempt must not destroy the last proven result.')
        : baselineDecision?.bestIncumbent
        ? text('Последняя рабочая версия сохранена. Можно продолжать искать улучшение без потери уже проверенного результата.', 'The last working result is preserved. Search can continue without losing what is already verified.')
        : acceptanceNeedsRemediation
          ? text('Текущий результат ещё нельзя принять из-за security policy. DepLoom сосредоточится на минимальном необходимом исправлении.', 'The current result cannot be accepted yet because of security policy. DepLoom will focus on the smallest required remediation.')
          : acceptanceUnknown && completed.has('audit')
            ? text('Данные безопасности неполные или устарели. Нужен новый независимый аудит перед принятием результата.', 'Security evidence is incomplete or stale. A fresh independent audit is required before accepting the result.')
            : text('DepLoom покажет здесь только полезное состояние работы. Подробные стадии и диагностика спрятаны ниже.', 'DepLoom shows only useful work state here. Detailed stages and diagnostics are kept below.')
  const humanPrimaryStage = FLOW_STAGES[activeIndex]
  const artifactsPath = currentScenario?.attempt ? (currentScenario.runDir ?? `${details.workspace.path}/.dependency-roadmap/iterative`) : `${details.workspace.path}/.dependency-roadmap/artifacts/runs`

  return (
    <section className="flow-workspace">

      <section className="human-flow-card" aria-label={text('Состояние обновления зависимостей', 'Dependency upgrade status')}>
        <div className="human-flow-hero">
          <span className={`human-flow-icon ${currentScenario ? (currentScenario.running ? 'active' : currentScenario.attempt?.status === 'failed' ? 'warning' : '') : flowComplete || baselineDecision?.bestIncumbent ? 'success' : active ? 'active' : acceptanceNeedsRemediation ? 'warning' : ''}`}>
            {active || currentScenario?.running ? <LoaderCircle className="spin" size={22} /> : <ShieldCheck size={22} />}
          </span>
          <div><h2>{humanStatusTitle}</h2><p>{humanStatusBody}</p></div>
        </div>
        {/* User UX: with the iterative panel active, the hero is a COMPACT status
            header — the panel owns the pipeline, the metrics and the ONE main
            action. This slim utility row keeps the functional fallbacks always
            on screen (Draft creation first-class, history, artifacts, deferred
            improvements) without duplicating the primary action or the metrics. */}
        {scenarioSignal ? (
          <div className="human-flow-utility">
            <button className="button secondary" disabled={active || scenarioSignal.running} onClick={() => void openBaselineIntentDialog('prepare', 'auto')}><FileText size={16} />{text('Настроить состав / Draft', 'Configure scope / Draft')}</button>
            {draftResult ? <button className="button secondary" onClick={() => void openDraftPrompt()}><FileText size={16} />{text('Последний Draft', 'Last Draft')}</button> : null}
            <button className="button secondary" disabled={!iterativeView?.artifactsDirectory && !run && !activeAction && !scenarioSignal?.attempt} onClick={() => void onOpenPath(iterativeView?.artifactsDirectory || artifactsPath)}><FileText size={16} />{text('Открыть артефакты', 'Open artifacts')}</button>
            {iterativeView?.iterationDirectory ? <button className="button secondary" onClick={() => void onOpenPath(iterativeView.iterationDirectory)}><FileText size={16} />{text('Рабочая директория итерации', 'Iteration working directory')}</button> : null}
            {flowComplete && acceptanceAccepted ? <button className="button secondary" disabled={active} onClick={() => void openDeferredImprovementDialog()}><RotateCcw size={16} />{text('Продолжить улучшение', 'Continue improving')}</button> : null}
          </div>
        ) : (
          <>
        <div className="human-flow-metrics">
          <div><span>{text('Проверено', 'Verified')}</span><strong>{humanVerifiedUpdates}</strong><small>{text('обновлений', 'updates')}</small></div>
          <div><span>{text('Сейчас пробуем', 'Trying now')}</span><strong>{typeof humanActiveUpdates === 'number' ? humanActiveUpdates : activeAction === 'baseline' ? '…' : '—'}</strong><small>{activeAction === 'baseline' && humanActiveUpdates === undefined ? text('идёт проверка', 'verification running') : text('обновлений', 'updates')}</small></div>
          <div><span>{text('Отложено', 'Deferred')}</span><strong>{typeof humanDeferredUpdates === 'number' ? humanDeferredUpdates : '—'}</strong><small>{typeof humanDeferredUpdates === 'number' ? text('на потом', 'for later') : text('пока нет данных', 'not known yet')}</small></div>
          <div><span>{text('Актуальность библиотек', 'Library freshness')}</span><strong>{humanFreshness}</strong><small>{text('это метрика, не цель', 'metric, not a goal')}</small></div>
        </div>
        <div className={`human-flow-safety ${acceptanceAccepted ? 'success' : acceptanceNeedsRemediation ? 'warning' : ''}`}>
          <ShieldCheck size={17} />
          <div><strong>{acceptanceAccepted ? text('Безопасность проверена', 'Security verified') : acceptanceNeedsRemediation ? text('Нужно исправить security findings', 'Security findings need remediation') : text('Безопасность ещё проверяется', 'Security is not verified yet')}</strong>
          <span>{typeof acceptance?.critical === 'number' || typeof acceptance?.high === 'number' ? `Critical ${acceptance?.critical ?? '?'} · High ${acceptance?.high ?? '?'}` : text('Critical всегда должен быть 0. Для принятия нужен свежий полный аудит.', 'Critical must always be 0. Acceptance requires a fresh complete audit.')}</span></div>
        </div>
        {typeof humanRemainingUpdates === 'number' && humanRemainingUpdates > 0 ? <p className="human-flow-remaining">{text(`Осталось разобрать: ${humanRemainingUpdates}. Они не обнуляют уже проверенный результат.`, `Remaining to address: ${humanRemainingUpdates}. They do not invalidate the already verified result.`)}</p> : null}
        {draftLaunchActive ? <DraftLiveProgress startedAt={activeRunStartedAt} progress={activeDraftProgress} runId={activeRunId} /> : null}
        <div className="human-flow-actions">
          {/* P1#1 / User UX: when the iterative panel is active the compact hero
              (above) is just a status header with the fallback utility row; the
              panel owns the ONE main action. This legacy action row only runs on
              the no-panel path (scenarioSignal absent), where the legacy FLOW
              primary applies. */}
          {!flowComplete && !active && humanPrimaryStage.action ? <button className="button primary" disabled={humanPrimaryStage.action === 'release' && !acceptanceAccepted} onClick={() => baselineRestartRequired && humanPrimaryStage.action === 'baseline' ? restartBaseline(activeIndex) : void execute(activeIndex, undefined, undefined, humanPrimaryStage.action === 'baseline' ? (details.baselineRecovery?.available ? 'continue' : 'auto') : undefined)}><Play size={16} />{baselineRestartRequired && humanPrimaryStage.action === 'baseline' ? text('Начать новый поиск', 'Start a new search') : humanPrimaryStage.action === 'baseline' && details.baselineRecovery?.available ? text('Продолжить', 'Continue') : text('Продолжить работу', 'Continue')}</button> : null}
          {!flowComplete && !active && humanPrimaryStage.action === 'baseline' && (details.baselineRecovery?.available || run?.lastAction === 'baseline') ? <button className="button secondary" onClick={() => restartBaseline(activeIndex)}><RotateCcw size={16} />{text('Начать заново', 'Start over')}</button> : null}
          {/* L3: Draft/scope stays reachable right after project selection — on
              Preflight and after a failed C0 — not only when the FLOW stage is
              Baseline. The planner still guards what a Draft may do. */}
          {!active ? <button className="button secondary" onClick={() => void openBaselineIntentDialog('prepare', 'auto')}><FileText size={16} />{text('Настроить состав / Draft', 'Configure scope / Draft')}</button> : null}
          {flowComplete && acceptanceAccepted ? <button className="button secondary" disabled={active} onClick={() => void openDeferredImprovementDialog()}><RotateCcw size={16} />{text('Продолжить улучшение', 'Continue improving')}</button> : null}
          {draftResult ? <button className="button secondary" onClick={() => void openDraftPrompt()}><FileText size={16} />{text('Последний Draft', 'Last Draft')}</button> : null}
          <button className="button secondary" disabled={!run && !activeAction} onClick={() => void onOpenPath(artifactsPath)}><FileText size={16} />{text('Открыть артефакты', 'Open artifacts')}</button>
        </div>
        {draftResult ? <div className={`draft-result-card${draftResultFresh ? ' fresh' : ''}`}>
          <div className="draft-result-heading">
            <div>
              <strong>{draftResultFresh ? (draftResult.status === 'DRAFT_READY' ? text('Draft готов', 'Draft is ready') : text('Draft частичный', 'Draft is partial')) : text(`Последний Draft · ${draftResult.status}`, `Last Draft · ${draftResult.status}`)}</strong>
              {draftResultFresh ? <span>{draftResult.summary}</span> : <span>{draftResult.summary || text('Сохранённый результат Draft этого проекта.', 'Saved Draft result for this project.')}</span>}
              <span className="draft-result-meta">run <code>{draftResult.runId}</code> · {draftResult.elapsedMs}ms{typeof draftResult.deadlineSeconds === 'number' ? ` · ${text('deadline', 'deadline')} ${draftResult.deadlineSeconds}s` : ''} · {draftResult.verificationStatus} / {draftResult.authority} / {draftResult.compatibility}</span>
              {draftResult.status === 'DRAFT_PARTIAL' && typeof draftResult.metadata.processed === 'number' ? <span className="draft-result-meta">{
                (() => {
                  const parts = [
                    text(`обработано ${draftResult.metadata.processed}/${draftResult.metadata.processedTotal ?? draftResult.metadata.total}`, `processed ${draftResult.metadata.processed}/${draftResult.metadata.processedTotal ?? draftResult.metadata.total}`),
                    typeof draftResult.metadata.metadataKnown === 'number' ? text(`метаданные ${draftResult.metadata.metadataKnown}/${draftResult.metadata.metadataTotal ?? draftResult.metadata.total}`, `metadata ${draftResult.metadata.metadataKnown}/${draftResult.metadata.metadataTotal ?? draftResult.metadata.total}`) : '',
                    typeof draftResult.metadata.pending === 'number' && draftResult.metadata.pending > 0 ? text(`не начаты ${draftResult.metadata.pending}`, `not started ${draftResult.metadata.pending}`) : '',
                    typeof draftResult.metadata.interrupted === 'number' && draftResult.metadata.interrupted > 0 ? text(`прерваны deadline ${draftResult.metadata.interrupted}`, `interrupted ${draftResult.metadata.interrupted}`) : '',
                    typeof draftResult.metadata.registryFailed === 'number' && draftResult.metadata.registryFailed > 0 ? text(`registry failed ${draftResult.metadata.registryFailed}`, `registry failed ${draftResult.metadata.registryFailed}`) : '',
                    typeof draftResult.metadata.osvUnknown === 'number' && draftResult.metadata.osvUnknown > 0 ? text(`OSV недоступен ${draftResult.metadata.osvUnknown}`, `OSV unavailable ${draftResult.metadata.osvUnknown}`) : '',
                  ].filter(Boolean).join(' · ')
                  return parts || text('частичный результат', 'partial result')
                })()
              }</span> : null}
              {(() => {
                // R12/F2: honest goal chips. The denominator is the WHOLE
                // active scope (74/76), never ok/(ok+shortfall) which would
                // render 74/74. F1 separates the policy gate shortfall from the
                // +5 p.p. reserve; F3 shows projected C/H on the exact chosen
                // versions + goal feasibility instead of implying a plan fixes
                // the findings. F4 keeps a truncation caveat visible without
                // presenting actionable proposed rows as blockers.
                const meta = draftResult.metadata
                const lagRu: string[] = []
                const lagEn: string[] = []
                if (typeof meta.postPlanLagOk === 'number') {
                  const denomScope = (typeof meta.postPlanScopeTotal === 'number' && meta.postPlanScopeTotal > 0) ? meta.postPlanScopeTotal : undefined
                  const denom = denomScope !== undefined
                    ? `${meta.postPlanLagOk}/${denomScope}`
                    : (typeof meta.postPlanShortfall === 'number' ? `${meta.postPlanLagOk}/${meta.postPlanLagOk + meta.postPlanShortfall}` : `${meta.postPlanLagOk}`)
                  const pct = typeof meta.postPlanLagOkPct === 'number' ? ` (${meta.postPlanLagOkPct}%)` : ''
                  const tailRu: string[] = []
                  const tailEn: string[] = []
                  if (typeof meta.postPlanPolicyShortfall === 'number' && meta.postPlanPolicyShortfall > 0) {
                    tailRu.push(`shortfall по политике ${meta.postPlanPolicyShortfall}`)
                    tailEn.push(`policy shortfall ${meta.postPlanPolicyShortfall}`)
                  }
                  if (typeof meta.postPlanReserveShortfall === 'number' && meta.postPlanReserveShortfall > 0) {
                    tailRu.push(`по запасу ${meta.postPlanReserveShortfall}`)
                    tailEn.push(`reserve ${meta.postPlanReserveShortfall}`)
                  }
                  lagRu.push(`projected lag-OK ${denom}${pct}` + (tailRu.length ? ' · ' + tailRu.join(' · ') : ''))
                  lagEn.push(`projected lag-OK ${denom}${pct}` + (tailEn.length ? ' · ' + tailEn.join(' · ') : ''))
                }
                if (typeof meta.postPlanCritical === 'number' || typeof meta.postPlanHigh === 'number') {
                  lagRu.push(`projected C/H на целях: ${meta.postPlanCritical ?? 0}/${meta.postPlanHigh ?? 0}`)
                  lagEn.push(`projected C/H on targets: ${meta.postPlanCritical ?? 0}/${meta.postPlanHigh ?? 0}`)
                }
                if (typeof meta.postPlanSecurityUnknown === 'number' && meta.postPlanSecurityUnknown > 0) {
                  lagRu.push(`целей без OSV: ${meta.postPlanSecurityUnknown}`)
                  lagEn.push(`targets without OSV: ${meta.postPlanSecurityUnknown}`)
                }
                if (meta.postPlanGoal) {
                  const level = meta.postPlanTargetLevel ? ` (${meta.postPlanTargetLevel})` : ''
                  const goalRu = ({ feasible: 'цель достижима', unknown: 'цель не подтверждена (OSV)', blocked: 'цель НЕ достижима этим планом' } as Record<string, string>)[meta.postPlanGoal]
                  const goalEn = ({ feasible: 'goal reachable', unknown: 'goal not confirmed (OSV)', blocked: 'goal NOT reachable by this plan' } as Record<string, string>)[meta.postPlanGoal]
                  lagRu.push((goalRu ?? meta.postPlanGoal) + level)
                  lagEn.push((goalEn ?? meta.postPlanGoal) + level)
                }
                const parts = [
                  lagRu.length || lagEn.length ? text(lagRu.join(' · '), lagEn.join(' · ')) : '',
                  typeof meta.candidateTruncated === 'number' && meta.candidateTruncated > 0 ? text(
                    `поиск кандидатов усечён ${meta.candidateTruncated}${typeof meta.candidateTruncatedTargetless === 'number' && meta.candidateTruncatedTargetless > 0 ? ` (без target: ${meta.candidateTruncatedTargetless})` : ''}`,
                    `candidate search truncated ${meta.candidateTruncated}${typeof meta.candidateTruncatedTargetless === 'number' && meta.candidateTruncatedTargetless > 0 ? ` (targetless: ${meta.candidateTruncatedTargetless})` : ''}`,
                  ) : '',
                  typeof meta.noTarget === 'number' && meta.noTarget > 0 ? text(`без безопасного target ${meta.noTarget}`, `no safe target ${meta.noTarget}`) : '',
                  typeof meta.blocked === 'number' && meta.blocked > 0 ? text(`target заблокирован ${meta.blocked}`, `target blocked ${meta.blocked}`) : '',
                ].filter(Boolean).join(' · ')
                return parts ? <span className="draft-result-meta">{parts}</span> : null
              })()}
            </div>
          </div>
          {/* G3: the stale warning is shown regardless of fresh/acknowledged —
              a result that no longer matches today's inputs must never be
              presented as current, even right after generation. */}
          {draftResult.stale ? <div className="resume-notice warning"><strong>{text('Draft устарел', 'Draft is stale')}</strong><span>{draftResult.staleReason || text('Входные файлы или acceptance-политика изменились с момента генерации.', 'Input files or the acceptance policy changed since generation.')}</span></div> : null}
          <div className="human-flow-actions">
            <button className="button primary" onClick={() => void openDraftPrompt()}><FileText size={16} />{draftResultFresh ? text('Показать draft prompt', 'Show draft prompt') : text('Открыть промпт', 'Open prompt')}</button>
            {draftResult.artifacts.plan ? <button className="button secondary" onClick={() => void onOpenPath(draftResult.artifacts.plan)}><FileText size={16} />{text('План', 'Plan')}</button> : null}
            {draftResult.artifacts.manifest ? <button className="button secondary" onClick={() => void onOpenPath(draftResult.artifacts.manifest)}><FileText size={16} />{text('Manifest', 'Manifest')}</button> : null}
            {draftResultFresh ? <button className="button secondary" onClick={acknowledgeDraftResult}>{text('Принято', 'Acknowledge')}</button> : null}
          </div>
        </div> : null}
          </>
        )}
      </section>

      <AuditGoalSummary audit={iterativeView?.audit} initial={iterativeView?.initialAudit} onOpenPath={onOpenPath} />

      <section className="roadmap-card migration-launch-settings"><header className="roadmap-card-header"><div><strong>{text('Настройки запуска', 'Run settings')}</strong><span>{text('Агент и модель используются для исправлений. Выберите их до запуска; работа агента может расходовать токены.', 'Agent and model are used for repairs. Choose them before starting; agent work may consume tokens.')}</span></div></header>
      <fieldset className="project-facts" disabled={active || scenarioSignal?.running}>
        <div><span>{t('flow.projectPath')}</span><strong title={project.path}>{project.path}</strong></div>
        <div><span>{t('common.branch')}</span><div className="git-plan-control"><input aria-label={t('flow.updateBranch')} value={branchBase} onChange={(event) => setBranchBase(event.target.value)} onBlur={() => void persistGitSettings()} onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }} placeholder="libs" /><label className="push-toggle" title={t('flow.pushTitle')}><input type="checkbox" checked={pushEnabled} onChange={(event) => void persistGitSettings(branchBase, event.target.checked)} />Push</label></div></div>
        <div><span>{t('flow.workspace')}</span><strong className={details.git.dirty ? 'warning-text' : 'success-text'}>{details.git.dirty ? t('flow.workspaceDirty', { count: details.git.summary.length }) : t('flow.workspaceClean')}</strong></div>
        <div><span>{t('flow.agent')}</span><QuickSelect value={details.workspace.agent} options={[{ value: 'codex', label: 'Codex' }, { value: 'opencode', label: 'OpenCode' }, { value: 'claude', label: 'Claude' }]} onChange={(value) => void onUpdateWorkspace({ id: details.workspace.id, agent: value as AgentProvider })} ariaLabel={t('flow.agent')} /></div>
        <div><span>{t('flow.model')}</span><ModelPicker value={agentModel} options={modelSuggestions} onChange={setAgentModel} onCommit={value => persistAgentModel(value).catch(error => window.alert(error instanceof Error ? error.message : String(error)))} placeholder={t('flow.modelDefault')} ariaLabel={t('flow.modelAria')} title={t('flow.modelTitle')} />{modelValidationError ? <span className="error-text" role="alert">{modelValidationError}</span> : null}</div>
        <div><span title={text('Явная версия Node.js проекта/CI. Пусто = среда не зафиксирована (host runtime, без гарантии совместимости с CI). Проверки и верификация будут выполняться установленным Node.', 'Explicit project/CI Node.js version. Empty = no fixed runtime (host ambient Node, no CI-compatibility claim). Verification and checks run on the installed Node.')}>{text('Node.js', 'Node.js')}</span><ModelPicker value={nodeVersionDraft} options={availableNodeVersions} onChange={setNodeVersionDraft} onCommit={value => persistNodeVersion(value).catch(error => window.alert(error instanceof Error ? error.message : String(error)))} placeholder={text('не задано', 'not set')} ariaLabel={text('Node.js для проекта/CI', 'Node.js for project/CI')} title={text('Node.js для проекта/CI', 'Node.js for project/CI')} />
          {project.nodeHints?.length ? <span className="node-hints" title={text('Закрепления Node в репозитории (engines.node / .nvmrc / .node-version). Подсказка: выбирать отсюда не обязательно — пустое значение означает среду хоста.', 'Node pins in this repository (engines.node / .nvmrc / .node-version). Suggestion only — leaving it empty keeps the host runtime.')}>{text('В репо закреплён Node', 'Repo pins Node')}: {project.nodeHints.join(', ')}</span> : null}
        </div>
      </fieldset>
      </section>

      <IterativeTaskPanel
        key={`${details.workspace.id}:${project.name}`}
        workspaceId={details.workspace.id}
        projectName={project.name}
        appVersion={appVersion}
        refreshKey={activeRunId}
        onGet={onGetIterativeTask}
        onExport={onExportIterativeTask}
        onExportLegacy={onExportLegacyIterativeTask}
        onCopy={onCopyIterativeTask}
        onSave={onSaveIterativeTask}
        onStatus={onIterativeStatus}
        onDrive={onIterativeDrive}
        onBegin={onIterativeBegin}
        onAgent={onIterativeAgent}
        onAttempt={onIterativeAttempt}
        onCancel={onIterativeCancel}
        liveAttempt={liveIterativeAttempt}
        onBeforeStart={async () => { await validateAgentSelection(); await persistAgentModel(); await persistNodeVersion(nodeVersionDraft) }}
        onConfigureScope={() => void openBaselineIntentDialog('prepare', 'auto')}
        onOpenPath={onOpenPath}
        // P1#1: the panel mirrors the one main action to the hero and must not
        // allow a start while a legacy activeAction owns the checkout.
        onScenario={scenarioOnScenario}
        onStatusView={setIterativeView}
        disabledExternal={active}
      />

      <details className="flow-technical-details">
        <summary>{text('Технические детали', 'Technical details')}</summary>
        <div className="flow-technical-details-body">
          {(acceptance || currentLevel) ? <section className="previous-audit-metrics"><strong>{text('Метрики последнего отдельного аудита', 'Metrics from the last separate audit')}</strong><p>{text('Это сохранённые метрики отдельного аудита; они не являются статусом текущего итеративного запуска.', 'These are saved metrics from a separate audit, not the status of the current iterative run.')}</p><div className="project-facts">
        <div><span>{text('Acceptance', 'Acceptance')}</span><strong className="level-label" title={acceptance?.reasons.join(' · ')}><i className={`status-dot ${acceptanceTone}`} />{acceptanceLabel}{typeof acceptance?.critical === 'number' && typeof acceptance?.high === 'number' ? ` · C${acceptance.critical}/H${acceptance.high}` : ''}</strong></div>
        <div><span title={currentLevel?.measuredAt ? t('flow.lastMeasured', { value: currentLevel.measuredAt }) : undefined}>{text('Freshness', 'Freshness')}{levelRefreshing ? ` · ${t('flow.recalculating')}` : measuredLabel ? ` · ${measuredLabel}` : ''}</span><strong>{typeof currentLevel?.lagOkPct === 'number' ? `${currentLevel.lagOkPct.toFixed(1)}%` : text('не рассчитана', 'not calculated')}</strong></div>
          </div></section> : null}
          <p className="flow-artifact-note">{text('Текущая попытка сохраняет stdout/stderr и служебные события в attempt.log в каталоге итеративного запуска. Журналы прежнего FLOW / Draft находятся в .dependency-roadmap/artifacts/runs. Логи нужны для диагностики и не определяют результат.', 'The current attempt saves stdout/stderr and service events to attempt.log in the iterative run directory. Previous FLOW / Draft logs are under .dependency-roadmap/artifacts/runs. Logs are diagnostic only and do not determine the result.')}</p>


      {currentScenario ? <IterativeRunDiagnostics signal={currentScenario} /> : null}

        </div>
      </details>
      <details className="flow-technical-details flow-logs-details">
        <summary>{text('Логи запуска', 'Run logs')}</summary>
        {currentScenario ? <IterativeRunDiagnostics signal={currentScenario} logs onReadLog={() => onIterativeAttempt(project.name, true)} onOpenLog={() => onOpenPath(`${currentScenario.runDir}/run.log`)} /> : null}
        {logs?.length || active ? <details className="flow-legacy-history" open={active ? true : undefined}>
          <summary>{active ? text('Журнал текущего FLOW / Draft', 'Current FLOW / Draft log') : text('Журнал прежнего FLOW / Draft (отдельный запуск)', 'Previous FLOW / Draft log (separate run)')}</summary>
          <LogPanel logs={logs ?? []} environment={{} as EnvironmentInfo} showEnvironment={false} showRunMonitor={active} active={active} activeJobId={activeRunId} activeAction={activeAction} runStartedAt={activeRunStartedAt} migrationProgress={details.migrationProgress} onSendAgentNote={onSendAgentNote} onCancel={onCancelJob ?? (async () => {})} onClear={onClearLogs ?? (() => {})} />
        </details> : null}
      </details>
      {selectedBranchFailure?.runtime?.phase === 'failed' ? <BranchFailureModal branch={selectedBranchFailure} onClose={() => setSelectedBranchFailure(null)} /> : null}
      {baselineIntentDialog ? <BaselineIntentDialog mode={baselineIntentDialog.mode} resume={baselineIntentDialog.resume} plan={baselineIntentDialog.plan} decision={baselineIntentDialog.decision} onCancel={() => { setBaselineIntentDialog(undefined); if (baselineIntentDialog.mode === 'decision') setBaselineDecisionDismissed(true) }} onSubmit={runBaselineIntent} /> : null}
      {draftPromptPreview ? <PromptPreviewDialog preview={draftPromptPreview} onClose={() => setDraftPromptPreview(undefined)} onOpenPath={onOpenPath} /> : null}

    </section>
  )
}

// Live Draft progress: a compact strip of the current planner operation driven
// by [draft-progress] events from the subprocess. F5 contracts:
//  - elapsed is computed MONOTONICALLY from the last backend event
//    (backend elapsedSec + local time since the event), and a 1s local timer
//    re-renders it even while the planner emits nothing, so a long blocked
//    network header or a CPU-heavy planning step does not freeze the clock;
//  - the live/stale indicator therefore flips predictably ~8s after the last
//    event, independent of unrelated rerenders;
//  - measured stage counters (completed/total/pct) are retained across events
//    OF THE SAME stage instead of being nulled by the next partial event; a
//    stage change resets them (the percent is explicitly a stage percent, and
//    a terminal 100 can only come from the backend finalize event);
//  - budget continuation ticks down from the last backend value between
//    events instead of freezing.
// It is planning-only by design and must never claim physical verification.
function DraftLiveProgress({ startedAt, progress, runId }: { startedAt?: number; progress?: DraftProgressPayload; runId?: string }) {
  const { text } = useLanguage()
  const [, setNowTick] = useState(0)
  // A fixed local cadence re-renders the strip while the backend is silent: the
  // monotonic clock, live/stale dot and budget continuation must not depend on
  // unrelated rerenders (F5).
  useEffect(() => {
    const timer = window.setInterval(() => setNowTick((value) => value + 1), 1000)
    return () => window.clearInterval(timer)
  }, [])
  // Retained measured stage counters: later events that carry only
  // operation/retry must not null the count (F5). A stage move resets them so
  // a scan's percent is never shown as the solve's.
  const [stageCounters, setStageCounters] = useState<{ step?: string; completed?: number; total?: number; pct?: number } | undefined>(undefined)
  useEffect(() => {
    if (!progress) return
    setStageCounters((current) => {
      const next = current && (typeof progress.step !== 'string' || current.step === progress.step) ? { ...current } : {}
      if (typeof progress.step === 'string') next.step = progress.step
      if (typeof progress.completed === 'number') next.completed = progress.completed
      if (typeof progress.total === 'number') next.total = progress.total
      if (typeof progress.pct === 'number') next.pct = progress.pct
      return Object.keys(next).length ? next : undefined
    })
  }, [progress])
  const stepLabels: Record<string, string> = {
    inventory: text('Локальная инвентаризация', 'Local inventory'),
    scan: text('Обогащение зависимостей', 'Dependency enrichment'),
    solve: text('Планирование targets', 'Targets planning'),
    finalize: text('Публикация Draft', 'Publishing the Draft'),
  }
  const stepLabel = progress?.step ? (stepLabels[progress.step] ?? progress.step) : text('Ожидаем первый результат планировщика…', 'Waiting for the planner…')
  // Monotonic elapsed, seconds: anchored to the backend measurement of the
  // last event and extended locally since it arrived (F5). Before any event the
  // process start time is the anchor; units are always seconds (the backend
  // sends seconds; the launch marker is a millisecond epoch).
  const anchorSec = typeof progress?.elapsedSec === 'number' ? Math.max(0, progress.elapsedSec) : 0
  const sinceLastEventMs = progress ? Math.max(0, Date.now() - progress.at) : 0
  const runningSec = progress
    ? anchorSec + sinceLastEventMs / 1000
    : typeof startedAt === 'number' && Number.isFinite(startedAt) ? Math.max(0, (Date.now() - startedAt) / 1000) : 0
  const shownSec = Math.floor(runningSec)
  const heartbeat = progress ? (Date.now() - progress.at > 8000 ? 'stale' : 'live') : 'wait'
  const staleNote = heartbeat === 'stale'
    ? text('планировщик молчит (нет событий — неизвестно, сколько ещё), поэтому время и бюджет могут быть неточными', 'planner is silent (no events — unknown how much is left), so time and budget may be inexact')
    : undefined
  const counters = stageCounters
  const stagePct = typeof counters?.pct === 'number' ? Math.max(0, Math.min(100, counters.pct)) : undefined
  // Budget continuation: the backend reports the remaining time at event time;
  // between events it ticks down locally so it is not frozen (F5).
  const budgetNow = typeof progress?.budgetRemainingSec === 'number'
    ? Math.max(0, progress.budgetRemainingSec - sinceLastEventMs / 1000)
    : undefined
  const progressParts = [
    progress?.package ? <code key="pkg">{progress.package}</code> : null,
    progress?.operation ? <span className="draft-live-op" key="op">{progress.operation}</span> : null,
    typeof progress?.retry === 'number' && progress.retry > 0 ? <span className="draft-live-op" key="retry">retry {progress.retry}</span> : null,
    typeof counters?.completed === 'number' && typeof counters?.total === 'number' && counters.total > 0
      ? <span className="draft-live-count" key="count">{counters.completed}/{counters.total}</span>
      : null,
    typeof budgetNow === 'number'
      ? <span className="draft-live-budget" key="budget" title={text('Остаток бюджета запуска (без резерва на публикацию)', 'Remaining run budget (net of the publication reserve)')}>{text('бюджет', 'budget')} {budgetNow.toFixed(0)}s</span>
      : null,
  ]
  return (
    <div className="draft-live-progress" aria-label={text('Ход работы Draft', 'Draft progress')}>
      <div className="draft-live-heading">
        <strong>{stepLabel}</strong>
        <span>{runId ? <code>{runId.slice(0, 12)}</code> : null}{text(` · ${shownSec}s`, ` · ${shownSec}s`)}</span>
      </div>
      <div className="draft-live-strip">
        <span className={`draft-live-dot ${heartbeat}`} />
        {progressParts}
      </div>
      {typeof stagePct === 'number' ? (
        <div className="draft-live-meter" aria-label={text(`Прогресс этапа ${stagePct}%`, `Stage progress ${stagePct}%`)}>
          <div className="draft-live-meter-fill" style={{ width: `${stagePct}%` }} />
          <span>{stagePct}%</span>
        </div>
      ) : null}
      {staleNote ? <small className="draft-live-stale">{staleNote}</small> : null}
      <small>{stagePct !== undefined && !progress?.status ? text('Проценты относятся только к текущему этапу; 100% завершения — только когда результат опубликован.', 'Percentages refer to the current stage only; 100% completion only when the result is published.') : ''}{text(' Planning-only: install/lifecycle/project checks не выполняются, проект не изменяется.', ' Planning-only: no install/lifecycle/project checks are run, the project is not modified.')}</small>
    </div>
  )
}