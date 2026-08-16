import { AlertCircle, AlertTriangle, Check, ChevronDown, Circle, CircleHelp, ExternalLink, FileText, Info, LoaderCircle, Pause, Play, RotateCcw, Send, ShieldCheck } from 'lucide-react'

import { useEffect, useMemo, useState } from 'react'
import { ACTION_ORDER, FLOW_STAGES } from '../data/flow'
import { useLanguage } from '../i18n'
import { BranchFailureModal } from './BranchFailureModal'
import { GoalDetailsModal } from './GoalDetailsModal'
import type { ActionInput, AgentProvider, FlowAction, MigrationBranchProgress, ProjectSpec, TargetLevel, WorkspaceDetails } from '../types'

const AUTOPILOT_HELP = {
  ru: '«Продолжить» автономно доводит текущий этап: Supervisor, retry и recovery работают без ручных перезапусков. Автопилот делает то же самое и дополнительно сам переходит между этапами FLOW, возвращаясь к migration после недостигнутой цели.',
  en: 'Continue autonomously completes the current stage: Supervisor, retry and recovery work without manual restarts. Autopilot also advances between FLOW stages and returns to migration when the target is still unmet.',
} as const

type Props = {
  details: WorkspaceDetails
  project: ProjectSpec
  activeAction?: FlowAction
  autopilotActive?: boolean
  onRun: (input: ActionInput) => Promise<void>
  onSendAgentNote: (note: string, branch?: string) => Promise<boolean>
  onStartAutopilot: (input: { workspaceId: string; projectName: string; target: TargetLevel; releaseBranch?: string }) => Promise<void>
  onStopAutopilot: () => Promise<void>
  onRecoverWithAgent: (input: { workspaceId?: string; projectName: string; note: string }) => Promise<void>
  onOpenDashboard: () => void
  onOpenPath: (path?: string) => Promise<void>
  onChoosePrompt: (projectName: string) => Promise<void>
  onUpdateWorkspace: (patch: { id: string; agent?: AgentProvider; agentModel?: string }) => Promise<void>
  onUpdateProjectBranches: (input: { workspaceId?: string; projectName: string; branchBase?: string; push?: boolean }) => Promise<void>
  onListAgentModels: (agentProvider: AgentProvider, cwd?: string) => Promise<string[]>
}

export function FlowWorkspace({ details, project, activeAction, autopilotActive, onRun, onSendAgentNote, onStartAutopilot, onStopAutopilot, onRecoverWithAgent, onOpenDashboard, onOpenPath, onChoosePrompt, onUpdateWorkspace, onUpdateProjectBranches, onListAgentModels }: Props) {
  const { language, text, t } = useLanguage()
  const [target, setTarget] = useState<TargetLevel>('yellow')
  const [label, setLabel] = useState('')
  const [releaseBranch, setReleaseBranch] = useState(project.git?.releaseBranch || 'libs-release')
  const [gateCommand, setGateCommand] = useState('')
  const [agentNote, setAgentNote] = useState('')
  const [noteSendState, setNoteSendState] = useState<'idle' | 'sending' | 'sent'>('idle')
  const [goalDetailsOpen, setGoalDetailsOpen] = useState(false)
  const [selectedBranchFailure, setSelectedBranchFailure] = useState<MigrationBranchProgress | null>(null)
  const [selectedStageIndex, setSelectedStageIndex] = useState<number | null>(null)
  const run = details.teamState?.projects[project.name]
  const recovery = run?.recovery
  const agentRecoveryAvailable = recovery?.kind === 'agent'
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
  const isActionCompleted = (action: FlowAction) => completed.has(action) && action !== activeAction
  const completedStageCount = ACTION_ORDER.filter(isActionCompleted).length
  const promptReady = Boolean(details.projectPromptPath && details.migrationProgress?.project === project.name)
  // A branch already created/ready/merged means the plan is mid-flight even
  // when there's no interrupted agent CLI session left to resume (e.g. the
  // orchestrator's own merge step stopped on a conflict and the user just
  // fixed it by hand) -- the primary button must read as "continue", not
  // "start", or it looks identical to the destructive "Начать заново" action.
  const hasMigrationProgress = Boolean(promptReady && details.migrationProgress?.branches.some((branch) => branch.status !== 'waiting'))
  const dirtyBlocksMigration = Boolean(details.migrationProgress?.dirty && (details.migrationProgress.currentBranch === details.migrationProgress.mergedBranch || details.migrationProgress.branches.some((branch) => branch.branch === details.migrationProgress?.currentBranch)))
  const planReady = details.dashboardExists && promptReady
  const runningIndex = activeAction ? FLOW_STAGES.findIndex((stage) => stage.action === activeAction) : -1
  const currentIndex = runningIndex >= 0 ? runningIndex : FLOW_STAGES.findIndex((stage) => stage.action ? !completed.has(stage.action) : !planReady)
  const currentLevel = details.projectLevels[project.name]
  const levelRefreshing = ['baseline', 'generate', 'generate-all'].includes(activeAction ?? '')
  const measuredDate = currentLevel?.measuredAt ? new Date(currentLevel.measuredAt) : undefined
  const measuredLabel = measuredDate && !Number.isNaN(measuredDate.getTime()) ? measuredDate.toLocaleString(language === 'ru' ? 'ru-RU' : 'en-US', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : undefined
  const runTarget: TargetLevel = run?.target === 'green' ? 'green' : 'yellow'
  const statusRank = { red: 0, yellow: 1, green: 2 } as const
  const remainingForTarget = runTarget === 'yellow' ? currentLevel?.remainingYellow : currentLevel?.remainingGreen
  const targetReached = Boolean(currentLevel && statusRank[currentLevel.status] >= statusRank[runTarget] && (remainingForTarget ?? 0) === 0)
  const actionsComplete = !activeAction && currentIndex < 0
  const goalMissed = completed.has('generate') && !targetReached
  const flowComplete = actionsComplete && targetReached
  const activeIndex = currentIndex < 0 ? FLOW_STAGES.length - 1 : currentIndex
  const displayedIndex = selectedStageIndex ?? activeIndex
  const active = Boolean(activeAction)
  const displayedAction = FLOW_STAGES[displayedIndex].action
  const bestEffortReleaseEligible = Boolean(details.targetClosure?.bestEffortReleaseEligible)
  const goalBlocked = goalMissed && displayedAction === 'release' && !bestEffortReleaseEligible
  const configuredBranch = project.git?.baseBranch || project.git?.branchPrefix || 'libs'
  const [branchBase, setBranchBase] = useState(configuredBranch)
  const [pushEnabled, setPushEnabled] = useState(Boolean(project.git?.push))
  const [agentModel, setAgentModel] = useState(details.workspace.agentModel ?? '')
  const [modelSuggestions, setModelSuggestions] = useState<string[]>([])
  const levelLabels = { red: t('levels.red'), yellow: t('levels.yellow'), green: t('levels.green') } as const
  const levelTones = { red: 'danger', yellow: 'warning', green: 'success' } as const
  const migrationStatusLabels = {
    waiting: t('flow.status.waiting'), created: t('flow.status.created'), partial: t('flow.status.partial'),
    changes: t('flow.status.changes'), ready: t('flow.status.ready'), integrated: t('flow.status.integrated'), merged: t('flow.status.merged'),
  } as const
  const migrationRuntimeLabels = {
    planning: t('flow.runtime.planning'), queued: t('flow.runtime.queued'), starting: t('flow.runtime.starting'),
    running: t('flow.runtime.running'), bootstrapping: t('flow.runtime.bootstrapping'), verifying: t('flow.runtime.verifying'),
    repairing: t('flow.runtime.repairing'), failed: t('flow.runtime.failed'), ready: t('flow.runtime.ready'),
    merging: t('flow.runtime.merging'), 'integration-verifying': t('flow.runtime.integration'),
  } as const
  const migrationStatusLabel = (branch: MigrationBranchProgress) => branch.runtime ? migrationRuntimeLabels[branch.runtime.phase] : migrationStatusLabels[branch.status]
  const migrationFailureTone = (branch: MigrationBranchProgress) => /USER_ACTION_REQUIRED|APPROVAL_REQUIRED|SAFETY_STOP|MANUAL_INTERVENTION_REQUIRED/i.test(branch.runtime?.detail ?? '') ? 'error' : 'warning'
  const migrationBranchProgressText = (branch: MigrationBranchProgress) => {
    if (!branch.runtime && !['created', 'partial', 'changes', 'ready'].includes(branch.status)) return t('flow.branch.dependencies', { count: branch.packages.length })
    const progressText = t('flow.branch.targets', { met: branch.metPackages, total: branch.packages.length })
    if (branch.runtime?.phase === 'failed') return `${progressText} · ${t('flow.branch.failureHint')}`
    return `${progressText}${branch.runtime?.detail ? ` · ${branch.runtime.detail}` : ''}`
  }
  const migrationBranchActive = (branch: MigrationBranchProgress) => Boolean(branch.runtime && !['planning', 'queued', 'failed', 'ready'].includes(branch.runtime.phase))
  const planningMigrationBranches = details.migrationProgress?.branches.filter((branch) => branch.runtime?.phase === 'planning').length ?? 0
  const queuedMigrationBranches = details.migrationProgress?.branches.filter((branch) => branch.runtime?.phase === 'queued').length ?? 0
  const failedMigrationBranches = details.migrationProgress?.branches.filter((branch) => branch.runtime?.phase === 'failed').length ?? 0

  useEffect(() => {
    setBranchBase(configuredBranch)
    setPushEnabled(Boolean(project.git?.push))
  }, [configuredBranch, project.git?.push, project.name])

  useEffect(() => { setAgentModel(details.workspace.agentModel ?? '') }, [details.workspace.agentModel, details.workspace.id])
  useEffect(() => { if (run?.target === 'yellow' || run?.target === 'green') setTarget(run.target) }, [run?.target])
  useEffect(() => { setReleaseBranch(run?.releaseBranch || project.git?.releaseBranch || 'libs-release') }, [project.git?.releaseBranch, project.name, run?.releaseBranch])
  // Resetting on `currentIndex` moved the detail panel -- and the primary
  // button with it -- out from under the user whenever a background refresh
  // shifted the computed stage, so a click could run a different stage than
  // the one on screen. Only switching projects invalidates the selection.
  useEffect(() => { setSelectedStageIndex(null) }, [project.name])

  useEffect(() => {
    let cancelled = false
    setModelSuggestions([])
    void onListAgentModels(details.workspace.agent, project.path).then((models) => { if (!cancelled) setModelSuggestions(models) })
    return () => { cancelled = true }
  }, [details.workspace.agent, project.path, onListAgentModels])

  const execute = async (stageIndex: number, resumeOverride?: boolean, restartMigration?: boolean) => {
    const stage = FLOW_STAGES[stageIndex]
    if (!stage.action) { onOpenDashboard(); return }
    const resumeAgent = stage.action === 'agent' && (resumeOverride ?? canResumeAgent)
    if (stage.confirmationKey && !window.confirm(t(stage.confirmationKey))) return
    const noteToSend = stage.action === 'agent' && !restartMigration ? agentNote.trim() || undefined : undefined
    await onRun({ action: stage.action, workspaceId: details.workspace.id, projectName: project.name, target, label, releaseBranch, gateCommand, resumeAgent, restartMigration, agentNote: noteToSend, commitMessage: `chore(deps): save ${project.name} roadmap state` })
    if (noteToSend) setAgentNote('')
  }

  const sendLiveNote = async () => {
    const trimmed = agentNote.trim()
    if (!trimmed) return
    setNoteSendState('sending')
    await onSendAgentNote(trimmed)
    setAgentNote('')
    setNoteSendState('sent')
    window.setTimeout(() => setNoteSendState((current) => current === 'sent' ? 'idle' : current), 2500)
  }

  const startRecovery = async () => {
    const trimmed = agentNote.trim()
    if (!trimmed || !agentRecoveryAvailable) return
    setNoteSendState('sending')
    await onRecoverWithAgent({ workspaceId: details.workspace.id, projectName: project.name, note: trimmed })
    setAgentNote('')
    setNoteSendState('sent')
    window.setTimeout(() => setNoteSendState((current) => current === 'sent' ? 'idle' : current), 2500)
  }

  const persistAgentModel = async (value = agentModel) => {
    const trimmed = value.trim()
    setAgentModel(trimmed)
    if (trimmed === (details.workspace.agentModel ?? '')) return
    try {
      await onUpdateWorkspace({ id: details.workspace.id, agentModel: trimmed })
    } catch (error) {
      setAgentModel(details.workspace.agentModel ?? '')
      window.alert(error instanceof Error ? error.message : String(error))
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

  return (
    <section className="flow-workspace">
      <div className="project-facts">
        <div><span>{t('flow.projectPath')}</span><strong title={project.path}>{project.path}</strong></div>
        <div><span title={currentLevel?.measuredAt ? t('flow.lastMeasured', { value: currentLevel.measuredAt }) : undefined}>{t('flow.currentLevel')}{levelRefreshing ? ` · ${t('flow.recalculating')}` : measuredLabel ? ` · ${measuredLabel}` : ''}</span><strong className="level-label"><i className={`status-dot ${currentLevel ? levelTones[currentLevel.status] : 'muted'}`} />{currentLevel ? levelLabels[currentLevel.status] : t('levels.unknown')}{typeof currentLevel?.lagOkPct === 'number' ? ` · ${currentLevel.lagOkPct.toFixed(1)}%` : ''}</strong></div>
        <fieldset className="target-field"><legend>{t('flow.targetLevel')}</legend><label><input type="radio" checked={target === 'yellow'} onChange={() => setTarget('yellow')} /><span className="target-dot yellow" />{t('levels.yellow')}</label><label><input type="radio" checked={target === 'green'} onChange={() => setTarget('green')} /><span className="target-dot green" />{t('levels.green')}</label></fieldset>
        <div><span>{t('common.branch')}</span><div className="git-plan-control"><input aria-label={t('flow.updateBranch')} value={branchBase} onChange={(event) => setBranchBase(event.target.value)} onBlur={() => void persistGitSettings()} onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }} placeholder="libs" /><label className="push-toggle" title={t('flow.pushTitle')}><input type="checkbox" checked={pushEnabled} onChange={(event) => void persistGitSettings(branchBase, event.target.checked)} />Push</label></div></div>
        <div><span>{t('flow.workspace')}</span><strong className={details.git.dirty ? 'warning-text' : 'success-text'}>{details.git.dirty ? t('flow.workspaceDirty', { count: details.git.summary.length }) : t('flow.workspaceClean')}</strong></div>
        <div><span>{t('flow.agent')}</span><select value={details.workspace.agent} onChange={(event) => void onUpdateWorkspace({ id: details.workspace.id, agent: event.target.value as AgentProvider })}><option value="codex">Codex</option><option value="opencode">OpenCode</option><option value="claude">Claude</option></select></div>
        <div><span>{t('flow.model')}</span><input aria-label={t('flow.modelAria')} list="agent-model-suggestions" value={agentModel} onChange={(event) => setAgentModel(event.target.value)} onBlur={() => void persistAgentModel()} onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }} placeholder={t('flow.modelDefault')} title={t('flow.modelTitle')} /><datalist id="agent-model-suggestions">{modelSuggestions.map((option) => <option value={option} key={option} />)}</datalist></div>
      </div>

      <div className="run-progress">
        <span>{t('flow.runProgress')}</span><div className="progress-track"><div style={{ width: `${Math.round((completedStageCount / ACTION_ORDER.length) * 100)}%` }} /></div><strong>{t('flow.commandsCompleted', { done: completedStageCount, total: ACTION_ORDER.length })}</strong>
        <label className="run-label">{t('flow.label')}<input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="deps-2026-q3" /></label>
      </div>

      <div className="flow-heading"><div><h2>{t('flow.currentRun')}</h2><p>{t('flow.currentRunHint')}</p></div></div>

      <div className="flow-layout">
        <ol className="stage-list">
          {FLOW_STAGES.map((stage, index) => {
            const isCurrent = !flowComplete && index === activeIndex
            const isSelected = index === displayedIndex && !(flowComplete && selectedStageIndex === null)
            const done = stage.action ? isActionCompleted(stage.action) : planReady
            return (
              <li key={stage.id} className={`${done ? 'done' : ''} ${isCurrent ? 'current' : ''} ${isSelected ? 'selected' : ''}`}>
                <button onClick={() => setSelectedStageIndex(index)} disabled={active} aria-pressed={isSelected}>
                  <span className="stage-marker">{done ? <Check size={14} /> : active && isCurrent ? <LoaderCircle className="spin" size={14} /> : stage.id}</span>
                  <span className="stage-copy"><strong>{t(stage.titleKey)}</strong><small>{done ? t('flow.stage.completed') : isCurrent ? t('flow.stage.current') : t('flow.stage.waiting')}</small></span>
                  {isSelected ? <ChevronDown size={15} /> : null}
                </button>
              </li>
            )
          })}
        </ol>

        <div className="stage-detail">
          {actionsComplete && goalMissed && selectedStageIndex === null ? <div className="flow-complete goal-missed">
            <span className="flow-complete-icon"><AlertTriangle size={24} /></span>
            <h3>{run?.bestEffortRelease ? text('FLOW завершён в best-effort режиме', 'FLOW completed in best-effort mode') : text('Цель не достигнута', 'Target not reached')}</h3>
            <p>{run?.bestEffortRelease ? text(
              'Все исполнимые этапы завершены и release прошёл обычные project gates/hooks, но выбранный health-level остался недостижим текущим безопасным планом. Оставшиеся blockers сохранены для следующей итерации или другого агента.',
              'All executable stages completed and release passed the normal project gates/hooks, but the selected health level is still unreachable by the current safe plan. Remaining blockers are preserved for the next iteration or another agent.',
            ) : bestEffortReleaseEligible ? text(
              'Текущий исполнимый план исчерпан. Можно продолжить audit/release/state в best-effort режиме: проверки и repository hooks останутся обязательными.',
              'The current executable plan is exhausted. Audit/release/state may continue in best-effort mode; verification and repository hooks remain mandatory.',
            ) : text(
              'В текущем плане ещё есть исполнимые действия или критичный blocker. Supervisor продолжит/перепланирует миграцию; release пока заблокирован.',
              'The current plan still contains executable actions or a critical blocker. Supervisor will continue/replan migration; release remains blocked.',
            )}</p>
            <strong>{currentLevel ? `${levelLabels[currentLevel.status]}${typeof currentLevel.lagOkPct === 'number' ? ` · ${currentLevel.lagOkPct.toFixed(1)}%` : ''}; ${text('цель', 'target')} — ${runTarget === 'yellow' ? `${t('levels.yellow')} (≥ 80%)` : t('levels.green')}` : text('Итоговый уровень не рассчитан', 'Final level is not calculated')}</strong>
            <div className="stage-actions">
              {details.targetClosure ? <button className="button secondary" onClick={() => setGoalDetailsOpen(true)}><Info size={16} /> {text('Почему не достигнута', 'Why it was not reached')}</button> : null}
              {run?.bestEffortRelease?.handoffPath ? <button className="button secondary" onClick={() => void onOpenPath(run.bestEffortRelease?.handoffPath)}><FileText size={16} /> {text('Открыть handoff', 'Open handoff')}</button> : null}
              <button className="button primary" onClick={onOpenDashboard}><ExternalLink size={16} /> {text('Открыть свежий dashboard', 'Open fresh Dashboard')}</button>
            </div>
            <div className="autopilot-actions">
              {autopilotActive
                ? <button className="button secondary" onClick={() => void onStopAutopilot()}><Pause size={16} /> {t('flow.autopilot.stop')}</button>
                : <button className="button secondary" disabled={active} onClick={() => {
                    if (window.confirm(`Автопилот самостоятельно пройдёт оставшиеся этапы FLOW для ${project.name}, будет чинить recoverable-ошибки и использовать best-effort release только при исчерпанном безопасном плане. Публикация ${project.git?.push ? 'разрешена настройкой git.push' : 'НЕ выполняется: git.push выключен'}. Запустить?`)) {
                      void onStartAutopilot({ workspaceId: details.workspace.id, projectName: project.name, target, releaseBranch })
                    }
                  }}><Play size={16} /> {t('flow.autopilot.start')}</button>}
              <span className="autopilot-help" tabIndex={0} title={AUTOPILOT_HELP[language]} aria-label={AUTOPILOT_HELP[language]}><CircleHelp size={15} /></span>
            </div>
          </div> : flowComplete && selectedStageIndex === null ? <div className="flow-complete">
            <span className="flow-complete-icon"><Check size={24} /></span>
            <h3>{text('Прогон завершён', 'Run completed')}</h3>
            <p>{text('Все этапы FLOW выполнены. Финальный roadmap сохранён и готов к просмотру.', 'All FLOW stages completed. The final roadmap is saved and ready for review.')}</p>
            <strong>{currentLevel ? `${text('Итоговый уровень', 'Final level')}: ${levelLabels[currentLevel.status]}${typeof currentLevel.lagOkPct === 'number' ? ` · ${currentLevel.lagOkPct.toFixed(1)}%` : ''}` : text('Итоговый уровень не рассчитан', 'Final level is not calculated')}</strong>
            <button className="button primary" onClick={onOpenDashboard}><ExternalLink size={16} /> {text('Открыть финальный dashboard', 'Open final Dashboard')}</button>
          </div> : <>
          <div className="stage-detail-title"><div><h3>{t(FLOW_STAGES[displayedIndex].titleKey)}</h3><p>{t(FLOW_STAGES[displayedIndex].descriptionKey)}</p></div><span className="step-number">{t('flow.step', { step: displayedIndex + 1 })}</span></div>
          <div className="check-table">
            <div><Check className="success-text" size={17} /><span>{t('flow.check.workspaceSettings')}</span><strong>{details.settingsExists ? 'OK' : t('flow.check.notFound')}</strong><small>{details.workspace.settingsPath}</small></div>
            <div><Check className={details.git.dirty ? 'warning-text' : 'success-text'} size={17} /><span>{t('flow.check.git')}</span><strong>{details.git.dirty ? t('flow.check.attention') : 'OK'}</strong><small>{details.git.branch}</small></div>
            <div>{details.dashboardExists ? <Check className="success-text" size={17} /> : <Circle size={17} />}<span>{t('flow.check.dashboard')}</span><strong>{details.dashboardExists ? t('common.ready') : t('common.waiting')}</strong><small>{details.dashboardExists ? (details.dashboardPath || '—') : t('flow.check.dashboardMissing')}</small></div>
            <div>{promptReady ? <Check className="success-text" size={17} /> : <Circle size={17} />}<span>{t('flow.check.agentPrompt')}</span><strong>{promptReady ? t('common.ready') : t('common.waiting')}</strong><small>{details.projectPromptPath || t('flow.check.exportPrompt')}</small></div>
          </div>
          {goalMissed ? <div className="confirmation"><AlertTriangle size={18} /><span>{text('Цель', 'Target')} «{runTarget === 'yellow' ? t('levels.yellow') : t('levels.green')}» {text('ещё не достигнута', 'is not reached yet')}{typeof currentLevel?.lagOkPct === 'number' ? `: ${currentLevel.lagOkPct.toFixed(1)}%` : ''}{typeof details.targetClosure?.neededForYellow === 'number' && details.targetClosure.neededForYellow > 0 ? `, ${text('не хватает', 'missing')} ${details.targetClosure.neededForYellow}` : ''}. {bestEffortReleaseEligible ? text('Исполнимый plan исчерпан: audit/release/state можно довести автоматически в best-effort режиме; проверки не ослабляются.', 'The executable plan is exhausted: audit/release/state may continue automatically in best-effort mode; verification is not weakened.') : text('Release пока заблокирован, но audit/state можно сохранять — Supervisor должен сначала исчерпать безопасные варианты migration/replan.', 'Release is still blocked, but audit/state may be saved. Supervisor must first exhaust safe migration/replan options.')}</span>{details.targetClosure ? <button className="button secondary" onClick={() => setGoalDetailsOpen(true)}><Info size={16} /> {t('common.details')}</button> : null}</div> : null}
          {details.migrationProgress ? <section className="migration-progress" aria-label={t('flow.migrationProgressAria')}>
            <div className="migration-progress-heading"><div><strong>{t('flow.migrationGroups')}</strong><span>{text(
              `${details.migrationProgress.completedBranches} в merged · ${details.migrationProgress.readyBranches} готовы · ${details.migrationProgress.activeBranches} выполняются${details.migrationProgress.activeDependencies ? ` (${details.migrationProgress.activeDependencies} целей)` : ''}${planningMigrationBranches ? ` · ${t('flow.supervisorReplans')}` : ''}${queuedMigrationBranches ? ` · ${queuedMigrationBranches} ${t('flow.queued')}` : ''}${failedMigrationBranches ? ` · ${failedMigrationBranches} ${t('flow.waitSupervisor')}` : ''} · ${t('flow.total')} ${details.migrationProgress.totalBranches}`,
              `${details.migrationProgress.completedBranches} merged · ${details.migrationProgress.readyBranches} ready · ${details.migrationProgress.activeBranches} running${details.migrationProgress.activeDependencies ? ` (${details.migrationProgress.activeDependencies} targets)` : ''}${planningMigrationBranches ? ` · ${t('flow.supervisorReplans')}` : ''}${queuedMigrationBranches ? ` · ${queuedMigrationBranches} ${t('flow.queued')}` : ''}${failedMigrationBranches ? ` · ${failedMigrationBranches} ${t('flow.waitSupervisor')}` : ''} · ${t('flow.total')} ${details.migrationProgress.totalBranches}`,
            )}</span></div><b>{t('flow.targetsConfirmed', { done: details.migrationProgress.completedDependencies, total: details.migrationProgress.totalDependencies, ref: details.migrationProgress.factsRef || text('рабочем дереве', 'working tree') })}{details.migrationProgress.readyDependencies ? ` · ${t('flow.moreReady', { count: details.migrationProgress.readyDependencies })}` : ''}</b></div>
            {!details.migrationProgress.trustworthy ? <div className="migration-worktree-warning"><AlertTriangle size={14} /><span>{t('flow.gitIncomplete')}</span></div> : null}
            {details.migrationProgress.dirty ? <div className="migration-worktree-warning"><AlertTriangle size={14} /><span>{dirtyBlocksMigration ? text(
              `На ветке миграции осталось ${details.migrationProgress.dirtyChanges} незакоммиченных изменений — они блокируют завершение миграции.`,
              `${details.migrationProgress.dirtyChanges} uncommitted changes remain on the migration branch and block migration completion.`,
            ) : text(
              `На ${details.migrationProgress.currentBranch} осталось ${details.migrationProgress.dirtyChanges} незакоммиченных изменений, но эта ветка не входит в Branch plan. Завершённая миграция не откатывается; разберите состояние на соответствующем этапе${agentRecoveryAvailable ? ' через Recovery ниже' : ''}.`,
              `${details.migrationProgress.dirtyChanges} uncommitted changes remain on ${details.migrationProgress.currentBranch}, but this branch is outside the Branch plan. Completed migration is preserved; resolve this state at the relevant stage${agentRecoveryAvailable ? ' using Recovery below' : ''}.`,
            )}</span></div> : null}
            {details.migrationProgress.unmetPackages.length ? <div className="migration-worktree-warning"><AlertTriangle size={14} /><span>{text('В', 'In')} {details.migrationProgress.factsRef || text('рабочем дереве', 'working tree')} {text('не выполнено', 'there are')} {details.migrationProgress.unmetPackages.length} {text('целей scope', 'unmet scope targets')}: {details.migrationProgress.unmetPackages.slice(0, 8).join(', ')}{details.migrationProgress.unmetPackages.length > 8 ? '…' : ''}.</span></div> : null}
            <div className="migration-branches">
              {details.migrationProgress.branches.map((branch) => <div className={`migration-branch ${branch.status}${branch.runtime ? ` runtime-${branch.runtime.phase}` : ''}${branch.runtime?.phase === 'failed' ? ` failure-${migrationFailureTone(branch)}` : ''}${branch.checkedOut ? ' checked-out' : ''}`} key={branch.branch}>
                {branch.runtime?.phase === 'failed' ? <button type="button" className="migration-error-indicator" data-tone={migrationFailureTone(branch)} title={branch.runtime.detail || t('flow.workerBlocker')} aria-label={t('flow.failureReasonAria', { label: branch.label })} onClick={() => setSelectedBranchFailure(branch)}><AlertCircle size={16} /></button> : branch.status === 'merged' && !branch.runtime ? <Check size={15} /> : migrationBranchActive(branch) ? <LoaderCircle className="spin" size={15} /> : <Circle size={15} />}
                <span><strong>{branch.label}</strong><code title={branch.branch}>{branch.branch}</code></span>
                <small title={branch.runtime?.updatedAt}>{migrationBranchProgressText(branch)}</small><b title={branch.worktreeDirtyChanges ? `${branch.worktreeDirtyChanges} незакоммиченных изменений в ${branch.worktreePath}` : branch.integratedInto ? `Фактически находится в ${branch.integratedInto}, но не в ${details.migrationProgress?.mergedBranch}` : undefined}>{migrationStatusLabel(branch)}</b>
              </div>)}
            </div>
          </section> : null}
          {FLOW_STAGES[displayedIndex].action === 'release' ? <div className="release-fields"><label>{t('flow.releaseBranch')}<input value={releaseBranch} onChange={(event) => setReleaseBranch(event.target.value)} /></label><label>{t('flow.finalGate')} <span>{t('flow.finalGateHint')}</span><input value={gateCommand} onChange={(event) => setGateCommand(event.target.value)} placeholder="yarn test && yarn build" /></label></div> : null}
          {FLOW_STAGES[displayedIndex].action === 'agent' && details.promptStale ? <div className="resume-notice warning"><strong>{t('flow.promptStale.title')}</strong><span>{t('flow.promptStale.body')}</span></div> : null}
          {FLOW_STAGES[displayedIndex].action === 'agent' && interruptedSession ? <div className={`resume-notice ${canResumeAgent ? '' : 'warning'}`}><strong>{canResumeAgent ? text('Сессия сохранена', 'Session saved') : `${text('Выберите', 'Select')} ${interruptedSession.provider}`}</strong><span>{canResumeAgent ? (run?.activeAgentBranch ? text(
            `Сохранено прерывание группы ${run.activeAgentBranch}. Сессия продолжится только если fingerprint текущего prompt/scope совпадёт; после нового baseline, target или prompt будет создан свежий контекст.`,
            `The interruption for group ${run.activeAgentBranch} is saved. The session resumes only if the current prompt/scope fingerprint matches; a new baseline, target or prompt starts a fresh context.`,
          ) : text(
            'Сессия продолжится только при точном совпадении fingerprint текущего prompt/scope; устаревший контекст автоматически не используется.',
            'The session resumes only when the current prompt/scope fingerprint matches exactly; stale context is never reused automatically.',
          )) : text('Остановленная сессия принадлежит другому агенту.', 'The stopped session belongs to another agent.')}</span></div> : null}
          {FLOW_STAGES[displayedIndex].action === 'agent' && !interruptedSession && hasMigrationProgress ? <div className="resume-notice"><strong>{t('flow.planPartial.title')}</strong><span>{t('flow.planPartial.body')}</span></div> : null}
          {recovery ? autopilotActive && recovery.kind === 'agent'
            ? <div className="resume-notice"><strong>{text('Автопилот устраняет', 'Autopilot is resolving')} · {recovery.code}</strong><span>{text('Это внутреннее recoverable-состояние. Supervisor/Executor уже получили ошибку как рабочий контекст; ввод пользователя не требуется. Карточка исчезнет после повторной deterministic verification.', 'This is an internal recoverable state. Supervisor/Executor already received the failure as working context; no user input is required. The card disappears after deterministic verification passes again.')}</span></div>
            : <div className={`resume-notice ${recovery.kind === 'agent' ? '' : recovery.kind === 'hard' ? 'danger' : 'warning'}`}><strong>{recovery.kind === 'agent' ? `${text('Recovery доступен', 'Recovery available')} · ${recovery.code}` : `Safety stop · ${recovery.code}`}</strong><span>{recovery.kind === 'agent' ? text('Git-состояние сохранено. Если Автопилот выключен, можно дать recovery-агенту дополнительный контекст вручную; scope/Git safety gates останутся обязательными.', 'Git state is preserved. If Autopilot is off, you may provide extra context to the recovery agent manually; scope/Git safety gates remain mandatory.') : recovery.kind === 'infrastructure' ? text('До этого состояния Автопилот уже выполняет bounded infrastructure retry. Если карточка осталась, повторяемый сбой не удалось устранить автоматически.', 'Autopilot already performs bounded infrastructure retries before this state. If this card remains, the repeated failure could not be resolved automatically.') : text('Это редкий safety stop: продолжение могло бы нарушить согласованный scope/Git-инвариант.', 'This is a rare safety stop: continuing could violate the agreed scope/Git invariant.')}</span>{recovery.kind === 'agent' ? <><textarea value={agentNote} onChange={(event) => setAgentNote(event.target.value)} placeholder={text('Например: разберись с оставшимися изменениями, исправь причину падения hook и доведи release до чистого состояния.', 'For example: inspect the remaining changes, fix the failing hook and bring release to a clean state.')} rows={3} />{activeAction === 'recover' ? <button type="button" className="button secondary" disabled={!agentNote.trim() || noteSendState === 'sending'} onClick={() => void sendLiveNote()}><Send size={14} /> {text('Отправить recovery-агенту', 'Send to recovery agent')}</button> : <button type="button" className="button secondary" disabled={active || !agentNote.trim() || noteSendState === 'sending'} onClick={() => void startRecovery()}><Send size={14} /> {text('Разобраться с ошибкой', 'Resolve failure')}</button>}</> : null}</div> : null}
          {autopilotActive ? <div className="resume-notice"><strong>{t('flow.autopilot.running')}</strong><span>{t('flow.autopilot.runningBody')}</span></div> : null}
          <div className="documents-contract">
            <FileText size={18} /><div><strong>{t('flow.documents.title')}</strong><p>{t('flow.documents.description')}</p></div>
          </div>
          {FLOW_STAGES[displayedIndex].confirmationKey ? <div className="confirmation"><AlertTriangle size={18} /><span>{t(FLOW_STAGES[displayedIndex].confirmationKey)}</span></div> : null}
          <div className="stage-actions">
            {FLOW_STAGES[displayedIndex].action === 'agent' && !canResumeAgent ? <button className="button secondary" disabled={active} title={text('Необязательно: Desktop сам построит актуальный prompt. Используйте только чтобы явно подменить его файлом.', 'Optional: Desktop builds the current prompt automatically. Use this only to explicitly replace it with a file.')} onClick={() => void onChoosePrompt(project.name)}><FileText size={16} /> {t('flow.customPrompt')}</button> : null}
            {FLOW_STAGES[displayedIndex].action === 'agent' ? <button className="button secondary" disabled={active} onClick={() => { if (window.confirm(text('Текущие изменения сохранятся в safety stash. Ветки Branch plan (work-ветки и merged) для этого проекта будут удалены локально, сохранённая сессия агента забудется. Начать миграцию заново?', 'Current changes will be saved to a safety stash. Branch-plan work and merged branches for this project will be removed locally and the saved agent session will be forgotten. Start migration over?'))) void execute(displayedIndex, false, true) }}><RotateCcw size={16} /> {t('flow.restartMigration')}</button> : null}
            <button className="button primary" disabled={active || goalBlocked} title={goalBlocked ? t('flow.release.blockedTitle') : bestEffortReleaseEligible && displayedAction === 'release' ? t('flow.release.bestEffortTitle') : undefined} onClick={() => void execute(displayedIndex)}>{active ? <LoaderCircle className="spin" size={17} /> : displayedIndex === 2 ? <ExternalLink size={17} /> : displayedIndex === 5 ? <ShieldCheck size={17} /> : <Play size={17} />}{FLOW_STAGES[displayedIndex].action === 'agent' && canResumeAgent ? t('flow.continueAgent') : FLOW_STAGES[displayedIndex].action === 'agent' && hasMigrationProgress ? t('flow.continueMigration') : FLOW_STAGES[displayedIndex].action === 'release' && goalMissed && bestEffortReleaseEligible ? t('flow.bestEffortRelease') : t(FLOW_STAGES[displayedIndex].buttonKey)}</button>
          </div>
          <div className="autopilot-actions">
            {autopilotActive
              ? <button className="button secondary" onClick={() => void onStopAutopilot()}><Pause size={16} /> {t('flow.autopilot.stop')}</button>
              : <button className="button secondary" disabled={active} onClick={() => {
                  if (window.confirm(`Автопилот самостоятельно пройдёт оставшиеся этапы FLOW для ${project.name}, будет чинить recoverable-ошибки и использовать best-effort release только при исчерпанном безопасном плане. Публикация ${project.git?.push ? 'разрешена настройкой git.push' : 'НЕ выполняется: git.push выключен'}. Запустить?`)) {
                    void onStartAutopilot({ workspaceId: details.workspace.id, projectName: project.name, target, releaseBranch })
                  }
                }}><Play size={16} /> {t('flow.autopilot.start')}</button>}
            <span className="autopilot-help" tabIndex={0} title={AUTOPILOT_HELP[language]} aria-label={AUTOPILOT_HELP[language]}><CircleHelp size={15} /></span>
          </div>
          </>}
        </div>
      </div>
      {goalDetailsOpen && details.targetClosure ? <GoalDetailsModal closure={details.targetClosure} projectName={project.name} onClose={() => setGoalDetailsOpen(false)} /> : null}
      {selectedBranchFailure?.runtime?.phase === 'failed' ? <BranchFailureModal branch={selectedBranchFailure} onClose={() => setSelectedBranchFailure(null)} /> : null}
    </section>
  )
}