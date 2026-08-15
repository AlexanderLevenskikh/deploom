import { AlertCircle, AlertTriangle, Check, ChevronDown, Circle, CircleHelp, ExternalLink, FileText, Info, LoaderCircle, Pause, Play, RotateCcw, Send, ShieldCheck } from 'lucide-react'

import { useEffect, useMemo, useState } from 'react'
import { ACTION_ORDER, FLOW_STAGES } from '../data/flow'
import { BranchFailureModal } from './BranchFailureModal'
import { GoalDetailsModal } from './GoalDetailsModal'
import type { ActionInput, AgentProvider, FlowAction, MigrationBranchProgress, ProjectSpec, TargetLevel, WorkspaceDetails } from '../types'

const AUTOPILOT_HELP = '«Продолжить» автономно доводит текущий этап: Supervisor, retry и recovery работают без ручных перезапусков. Автопилот делает то же самое и дополнительно сам переходит между этапами FLOW, возвращаясь к migration после недостигнутой цели.'

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
  const measuredLabel = measuredDate && !Number.isNaN(measuredDate.getTime()) ? measuredDate.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : undefined
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
  const levelLabels = { red: 'Красный', yellow: 'Жёлтый', green: 'Зелёный' } as const
  const levelTones = { red: 'danger', yellow: 'warning', green: 'success' } as const
  const migrationStatusLabels = { waiting: 'Ожидает', created: 'Ветка создана', partial: 'Частично выполнена', changes: 'Есть незавершённые изменения', ready: 'Готова к merge', integrated: 'Не в merged', merged: 'В merged' } as const
  const migrationRuntimeLabels = { planning: 'Ожидает новый план', queued: 'В очереди', starting: 'Слот открыт', running: 'Агент работает', bootstrapping: 'Подготовка окружения', verifying: 'Проверка группы', repairing: 'Исправление', failed: 'Ожидает Supervisor', ready: 'Готова к merge', merging: 'Выполняется merge', 'integration-verifying': 'Проверка merged' } as const
  const migrationStatusLabel = (branch: MigrationBranchProgress) => branch.runtime ? migrationRuntimeLabels[branch.runtime.phase] : migrationStatusLabels[branch.status]
  const migrationFailureTone = (branch: MigrationBranchProgress) => /USER_ACTION_REQUIRED|APPROVAL_REQUIRED|SAFETY_STOP|MANUAL_INTERVENTION_REQUIRED/i.test(branch.runtime?.detail ?? '') ? 'error' : 'warning'
  const migrationBranchProgressText = (branch: MigrationBranchProgress) => {
    if (!branch.runtime && !['created', 'partial', 'changes', 'ready'].includes(branch.status)) return `${branch.packages.length} зависимостей`
    const progressText = `${branch.metPackages} из ${branch.packages.length} целей`
    if (branch.runtime?.phase === 'failed') return `${progressText} · Причина доступна по значку слева`
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
    if (stage.confirmation && !window.confirm(stage.confirmation)) return
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
        <div><span>Путь к проекту</span><strong title={project.path}>{project.path}</strong></div>
        <div><span title={currentLevel?.measuredAt ? `Последнее измерение: ${currentLevel.measuredAt}` : undefined}>Текущий уровень{levelRefreshing ? ' · пересчёт…' : measuredLabel ? ` · ${measuredLabel}` : ''}</span><strong className="level-label"><i className={`status-dot ${currentLevel ? levelTones[currentLevel.status] : 'muted'}`} />{currentLevel ? levelLabels[currentLevel.status] : 'Не рассчитан'}{typeof currentLevel?.lagOkPct === 'number' ? ` · ${currentLevel.lagOkPct.toFixed(1)}%` : ''}</strong></div>
        <fieldset className="target-field"><legend>Целевой уровень</legend><label><input type="radio" checked={target === 'yellow'} onChange={() => setTarget('yellow')} /><span className="target-dot yellow" />Жёлтый</label><label><input type="radio" checked={target === 'green'} onChange={() => setTarget('green')} /><span className="target-dot green" />Зелёный</label></fieldset>
        <div><span>Ветка</span><div className="git-plan-control"><input aria-label="Ветка обновления" value={branchBase} onChange={(event) => setBranchBase(event.target.value)} onBlur={() => void persistGitSettings()} onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }} placeholder="libs" /><label className="push-toggle" title="Отправлять созданные ветки в remote"><input type="checkbox" checked={pushEnabled} onChange={(event) => void persistGitSettings(branchBase, event.target.checked)} />Push</label></div></div>
        <div><span>Workspace</span><strong className={details.git.dirty ? 'warning-text' : 'success-text'}>{details.git.dirty ? `Есть изменения (${details.git.summary.length})` : 'Ветка чистая'}</strong></div>
        <div><span>Агент</span><select value={details.workspace.agent} onChange={(event) => void onUpdateWorkspace({ id: details.workspace.id, agent: event.target.value as AgentProvider })}><option value="codex">Codex</option><option value="opencode">OpenCode</option><option value="claude">Claude</option></select></div>
        <div><span>Модель</span><input aria-label="Модель агента" list="agent-model-suggestions" value={agentModel} onChange={(event) => setAgentModel(event.target.value)} onBlur={() => void persistAgentModel()} onKeyDown={(event) => { if (event.key === 'Enter') event.currentTarget.blur() }} placeholder="по умолчанию агента" title="Передаётся агенту как --model; пусто — агент выбирает модель сам" /><datalist id="agent-model-suggestions">{modelSuggestions.map((option) => <option value={option} key={option} />)}</datalist></div>
      </div>

      <div className="run-progress">
        <span>Прогресс запуска</span><div className="progress-track"><div style={{ width: `${Math.round((completedStageCount / ACTION_ORDER.length) * 100)}%` }} /></div><strong>{completedStageCount} из {ACTION_ORDER.length} команд завершено</strong>
        <label className="run-label">Метка<input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="deps-2026-q3" /></label>
      </div>

      <div className="flow-heading"><div><h2>Текущий запуск</h2><p>Один проект и одна группа на итерацию.</p></div></div>

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
                  <span className="stage-copy"><strong>{stage.title}</strong><small>{done ? 'Завершено' : isCurrent ? 'Текущий шаг' : 'Ожидает'}</small></span>
                  {isSelected ? <ChevronDown size={15} /> : null}
                </button>
              </li>
            )
          })}
        </ol>

        <div className="stage-detail">
          {actionsComplete && goalMissed && selectedStageIndex === null ? <div className="flow-complete goal-missed">
            <span className="flow-complete-icon"><AlertTriangle size={24} /></span>
            <h3>{run?.bestEffortRelease ? 'FLOW завершён в best-effort режиме' : 'Цель не достигнута'}</h3>
            <p>{run?.bestEffortRelease ? 'Все исполнимые этапы завершены и release прошёл обычные project gates/hooks, но выбранный health-level остался недостижим текущим безопасным планом. Оставшиеся blockers сохранены для следующей итерации или другого агента.' : bestEffortReleaseEligible ? 'Текущий исполнимый план исчерпан. Можно продолжить audit/release/state в best-effort режиме: проверки и repository hooks останутся обязательными.' : 'В текущем плане ещё есть исполнимые действия или критичный blocker. Supervisor продолжит/перепланирует миграцию; release пока заблокирован.'}</p>
            <strong>{currentLevel ? `${levelLabels[currentLevel.status]}${typeof currentLevel.lagOkPct === 'number' ? ` · ${currentLevel.lagOkPct.toFixed(1)}%` : ''}; цель — ${runTarget === 'yellow' ? 'Жёлтый (не менее 80%)' : 'Зелёный'}` : 'Итоговый уровень не рассчитан'}</strong>
            <div className="stage-actions">
              {details.targetClosure ? <button className="button secondary" onClick={() => setGoalDetailsOpen(true)}><Info size={16} /> Почему не достигнута</button> : null}
              {run?.bestEffortRelease?.handoffPath ? <button className="button secondary" onClick={() => void onOpenPath(run.bestEffortRelease?.handoffPath)}><FileText size={16} /> Открыть handoff</button> : null}
              <button className="button primary" onClick={onOpenDashboard}><ExternalLink size={16} /> Открыть свежий dashboard</button>
            </div>
            <div className="autopilot-actions">
              {autopilotActive
                ? <button className="button secondary" onClick={() => void onStopAutopilot()}><Pause size={16} /> Остановить автопилот</button>
                : <button className="button secondary" disabled={active} onClick={() => {
                    if (window.confirm(`Автопилот самостоятельно пройдёт оставшиеся этапы FLOW для ${project.name}, будет чинить recoverable-ошибки и использовать best-effort release только при исчерпанном безопасном плане. Публикация ${project.git?.push ? 'разрешена настройкой git.push' : 'НЕ выполняется: git.push выключен'}. Запустить?`)) {
                      void onStartAutopilot({ workspaceId: details.workspace.id, projectName: project.name, target, releaseBranch })
                    }
                  }}><Play size={16} /> Автопилот до результата</button>}
              <span className="autopilot-help" tabIndex={0} title={AUTOPILOT_HELP} aria-label={AUTOPILOT_HELP}><CircleHelp size={15} /></span>
            </div>
          </div> : flowComplete && selectedStageIndex === null ? <div className="flow-complete">
            <span className="flow-complete-icon"><Check size={24} /></span>
            <h3>Прогон завершён</h3>
            <p>Все этапы FLOW выполнены. Финальный roadmap сохранён и готов к просмотру.</p>
            <strong>{currentLevel ? `Итоговый уровень: ${levelLabels[currentLevel.status]}${typeof currentLevel.lagOkPct === 'number' ? ` · ${currentLevel.lagOkPct.toFixed(1)}%` : ''}` : 'Итоговый уровень не рассчитан'}</strong>
            <button className="button primary" onClick={onOpenDashboard}><ExternalLink size={16} /> Открыть финальный dashboard</button>
          </div> : <>
          <div className="stage-detail-title"><div><h3>{FLOW_STAGES[displayedIndex].title}</h3><p>{FLOW_STAGES[displayedIndex].description}</p></div><span className="step-number">Шаг {displayedIndex + 1}</span></div>
          <div className="check-table">
            <div><Check className="success-text" size={17} /><span>Настройки workspace</span><strong>{details.settingsExists ? 'OK' : 'Не найдены'}</strong><small>{details.workspace.settingsPath}</small></div>
            <div><Check className={details.git.dirty ? 'warning-text' : 'success-text'} size={17} /><span>Состояние Git</span><strong>{details.git.dirty ? 'Внимание' : 'OK'}</strong><small>{details.git.branch}</small></div>
            <div>{details.dashboardExists ? <Check className="success-text" size={17} /> : <Circle size={17} />}<span>Dashboard</span><strong>{details.dashboardExists ? 'Готов' : 'Ожидает'}</strong><small>{details.dashboardExists ? (details.dashboardPath || '—') : 'Для этого проекта ещё не построен'}</small></div>
            <div>{promptReady ? <Check className="success-text" size={17} /> : <Circle size={17} />}<span>Agent prompt</span><strong>{promptReady ? 'Готов' : 'Ожидает'}</strong><small>{details.projectPromptPath || 'Выгрузите из dashboard этого проекта'}</small></div>
          </div>
          {goalMissed ? <div className="confirmation"><AlertTriangle size={18} /><span>Цель «{runTarget === 'yellow' ? 'Жёлтый' : 'Зелёный'}» ещё не достигнута{typeof currentLevel?.lagOkPct === 'number' ? `: ${currentLevel.lagOkPct.toFixed(1)}%` : ''}{typeof details.targetClosure?.neededForYellow === 'number' && details.targetClosure.neededForYellow > 0 ? `, не хватает ${details.targetClosure.neededForYellow}` : ''}. {bestEffortReleaseEligible ? 'Исполнимый plan исчерпан: audit/release/state можно довести автоматически в best-effort режиме; проверки не ослабляются.' : 'Release пока заблокирован, но audit/state можно сохранять — Supervisor должен сначала исчерпать безопасные варианты migration/replan.'}</span>{details.targetClosure ? <button className="button secondary" onClick={() => setGoalDetailsOpen(true)}><Info size={16} /> Подробности</button> : null}</div> : null}
          {details.migrationProgress ? <section className="migration-progress" aria-label="Прогресс миграции">
            <div className="migration-progress-heading"><div><strong>Группы миграции</strong><span>{details.migrationProgress.completedBranches} в merged · {details.migrationProgress.readyBranches} готовы · {details.migrationProgress.activeBranches} выполняются{details.migrationProgress.activeDependencies ? ` (${details.migrationProgress.activeDependencies} целей)` : ''}{planningMigrationBranches ? ' · Supervisor пересчитывает общий план' : ''}{queuedMigrationBranches ? ` · ${queuedMigrationBranches} в очереди` : ''}{failedMigrationBranches ? ` · ${failedMigrationBranches} ожидают Supervisor` : ''} · всего {details.migrationProgress.totalBranches}</span></div><b>{details.migrationProgress.completedDependencies} из {details.migrationProgress.totalDependencies} целей подтверждено в {details.migrationProgress.factsRef || 'рабочем дереве'}{details.migrationProgress.readyDependencies ? ` · ещё ${details.migrationProgress.readyDependencies} готовы к merge` : ''}</b></div>
            {!details.migrationProgress.trustworthy ? <div className="migration-worktree-warning"><AlertTriangle size={14} /><span>Часть запросов к Git не ответила, картина может быть неполной. Прогресс этапов не изменяется до успешного чтения.</span></div> : null}
            {details.migrationProgress.dirty ? <div className="migration-worktree-warning"><AlertTriangle size={14} /><span>{dirtyBlocksMigration ? `На ветке миграции осталось ${details.migrationProgress.dirtyChanges} незакоммиченных изменений — они блокируют завершение миграции.` : `На ${details.migrationProgress.currentBranch} осталось ${details.migrationProgress.dirtyChanges} незакоммиченных изменений, но эта ветка не входит в Branch plan. Завершённая миграция не откатывается; разберите состояние на соответствующем этапе${agentRecoveryAvailable ? ' через Recovery ниже' : ''}.`}</span></div> : null}
            {details.migrationProgress.unmetPackages.length ? <div className="migration-worktree-warning"><AlertTriangle size={14} /><span>В {details.migrationProgress.factsRef || 'рабочем дереве'} не выполнено {details.migrationProgress.unmetPackages.length} целей scope: {details.migrationProgress.unmetPackages.slice(0, 8).join(', ')}{details.migrationProgress.unmetPackages.length > 8 ? '…' : ''}.</span></div> : null}
            <div className="migration-branches">
              {details.migrationProgress.branches.map((branch) => <div className={`migration-branch ${branch.status}${branch.runtime ? ` runtime-${branch.runtime.phase}` : ''}${branch.runtime?.phase === 'failed' ? ` failure-${migrationFailureTone(branch)}` : ''}${branch.checkedOut ? ' checked-out' : ''}`} key={branch.branch}>
                {branch.runtime?.phase === 'failed' ? <button type="button" className="migration-error-indicator" data-tone={migrationFailureTone(branch)} title={branch.runtime.detail || 'Worker завершился с blocker'} aria-label={`Причина ошибки ${branch.label}`} onClick={() => setSelectedBranchFailure(branch)}><AlertCircle size={16} /></button> : branch.status === 'merged' && !branch.runtime ? <Check size={15} /> : migrationBranchActive(branch) ? <LoaderCircle className="spin" size={15} /> : <Circle size={15} />}
                <span><strong>{branch.label}</strong><code title={branch.branch}>{branch.branch}</code></span>
                <small title={branch.runtime?.updatedAt}>{migrationBranchProgressText(branch)}</small><b title={branch.worktreeDirtyChanges ? `${branch.worktreeDirtyChanges} незакоммиченных изменений в ${branch.worktreePath}` : branch.integratedInto ? `Фактически находится в ${branch.integratedInto}, но не в ${details.migrationProgress?.mergedBranch}` : undefined}>{migrationStatusLabel(branch)}</b>
              </div>)}
            </div>
          </section> : null}
          {FLOW_STAGES[displayedIndex].action === 'release' ? <div className="release-fields"><label>Release branch<input value={releaseBranch} onChange={(event) => setReleaseBranch(event.target.value)} /></label><label>Дополнительный final gate <span>необязательно; release.finalGateCommands из настроек запускаются автоматически</span><input value={gateCommand} onChange={(event) => setGateCommand(event.target.value)} placeholder="yarn test && yarn build" /></label></div> : null}
          {FLOW_STAGES[displayedIndex].action === 'agent' && details.promptStale ? <div className="resume-notice warning"><strong>Prompt устарел</strong><span>Dashboard новее сохранённого prompt. При следующем запуске Executor Desktop сам пересоберёт prompt из актуального Dashboard; вручную экспортировать его больше не обязательно.</span></div> : null}
          {FLOW_STAGES[displayedIndex].action === 'agent' && interruptedSession ? <div className={`resume-notice ${canResumeAgent ? '' : 'warning'}`}><strong>{canResumeAgent ? 'Сессия сохранена' : `Выберите ${interruptedSession.provider}`}</strong><span>{canResumeAgent ? (run?.activeAgentBranch ? `Сохранено прерывание группы ${run.activeAgentBranch}. Сессия продолжится только если fingerprint текущего prompt/scope совпадёт; после нового baseline, target или prompt будет создан свежий контекст.` : 'Сессия продолжится только при точном совпадении fingerprint текущего prompt/scope; устаревший контекст автоматически не используется.') : 'Остановленная сессия принадлежит другому агенту.'}</span></div> : null}
          {FLOW_STAGES[displayedIndex].action === 'agent' && !interruptedSession && hasMigrationProgress ? <div className="resume-notice"><strong>План уже частично выполнен</strong><span>Агентской CLI-сессии для продолжения нет (например, оркестратор сам остановился на конфликте merge), но уже созданные и смерженные ветки не будут тронуты — план продолжится с текущего состояния Git. Чтобы удалить их и начать заново, используйте отдельную кнопку «Начать заново».</span></div> : null}
          {recovery ? autopilotActive && recovery.kind === 'agent'
            ? <div className="resume-notice"><strong>Автопилот устраняет · {recovery.code}</strong><span>Это внутреннее recoverable-состояние. Supervisor/Executor уже получили ошибку как рабочий контекст; ввод пользователя не требуется. Карточка исчезнет после повторной deterministic verification.</span></div>
            : <div className={`resume-notice ${recovery.kind === 'agent' ? '' : recovery.kind === 'hard' ? 'danger' : 'warning'}`}><strong>{recovery.kind === 'agent' ? `Recovery доступен · ${recovery.code}` : `Safety stop · ${recovery.code}`}</strong><span>{recovery.kind === 'agent' ? 'Git-состояние сохранено. Если Автопилот выключен, можно дать recovery-агенту дополнительный контекст вручную; scope/Git safety gates останутся обязательными.' : recovery.kind === 'infrastructure' ? 'До этого состояния Автопилот уже выполняет bounded infrastructure retry. Если карточка осталась, повторяемый сбой не удалось устранить автоматически.' : 'Это редкий safety stop: продолжение могло бы нарушить согласованный scope/Git-инвариант.'}</span>{recovery.kind === 'agent' ? <><textarea value={agentNote} onChange={(event) => setAgentNote(event.target.value)} placeholder="Например: разберись с оставшимися изменениями, исправь причину падения hook и доведи release до чистого состояния." rows={3} />{activeAction === 'recover' ? <button type="button" className="button secondary" disabled={!agentNote.trim() || noteSendState === 'sending'} onClick={() => void sendLiveNote()}><Send size={14} /> Отправить recovery-агенту</button> : <button type="button" className="button secondary" disabled={active || !agentNote.trim() || noteSendState === 'sending'} onClick={() => void startRecovery()}><Send size={14} /> Разобраться с ошибкой</button>}</> : null}</div> : null}
          {autopilotActive ? <div className="resume-notice"><strong>Автопилот работает</strong><span>FLOW сам запускает следующий этап, перехватывает recoverable-ошибки встроенным агентом, повторяет transient infrastructure failures и остановится только на доказанном safety boundary или повторяющемся blocker без прогресса.</span></div> : null}
          <div className="documents-contract">
            <FileText size={18} /><div><strong>Три итоговых документа</strong><p>Технический changelog, человеческий итог с реальной пользой и полная карта изменений для ревью.</p></div>
          </div>
          {FLOW_STAGES[displayedIndex].confirmation ? <div className="confirmation"><AlertTriangle size={18} /><span>{FLOW_STAGES[displayedIndex].confirmation}</span></div> : null}
          <div className="stage-actions">
            {FLOW_STAGES[displayedIndex].action === 'agent' && !canResumeAgent ? <button className="button secondary" disabled={active} title="Необязательно: Desktop сам построит актуальный prompt. Используйте только чтобы явно подменить его файлом." onClick={() => void onChoosePrompt(project.name)}><FileText size={16} /> Свой prompt</button> : null}
            {FLOW_STAGES[displayedIndex].action === 'agent' ? <button className="button secondary" disabled={active} onClick={() => { if (window.confirm('Текущие изменения сохранятся в safety stash. Ветки Branch plan (work-ветки и merged) для этого проекта будут удалены локально, сохранённая сессия агента забудется. Начать миграцию заново?')) void execute(displayedIndex, false, true) }}><RotateCcw size={16} /> Начать заново</button> : null}
            <button className="button primary" disabled={active || goalBlocked} title={goalBlocked ? 'Supervisor ещё видит исполнимые действия/критичный blocker; сначала завершите migration plan' : bestEffortReleaseEligible && displayedAction === 'release' ? 'Best-effort release: health-цель не достигнута, но final gates/hooks остаются обязательными' : undefined} onClick={() => void execute(displayedIndex)}>{active ? <LoaderCircle className="spin" size={17} /> : displayedIndex === 2 ? <ExternalLink size={17} /> : displayedIndex === 5 ? <ShieldCheck size={17} /> : <Play size={17} />}{FLOW_STAGES[displayedIndex].action === 'agent' && canResumeAgent ? 'Продолжить агента' : FLOW_STAGES[displayedIndex].action === 'agent' && hasMigrationProgress ? 'Продолжить миграцию' : FLOW_STAGES[displayedIndex].action === 'release' && goalMissed && bestEffortReleaseEligible ? 'Создать best-effort release' : FLOW_STAGES[displayedIndex].button}</button>
          </div>
          <div className="autopilot-actions">
            {autopilotActive
              ? <button className="button secondary" onClick={() => void onStopAutopilot()}><Pause size={16} /> Остановить автопилот</button>
              : <button className="button secondary" disabled={active} onClick={() => {
                  if (window.confirm(`Автопилот самостоятельно пройдёт оставшиеся этапы FLOW для ${project.name}, будет чинить recoverable-ошибки и использовать best-effort release только при исчерпанном безопасном плане. Публикация ${project.git?.push ? 'разрешена настройкой git.push' : 'НЕ выполняется: git.push выключен'}. Запустить?`)) {
                    void onStartAutopilot({ workspaceId: details.workspace.id, projectName: project.name, target, releaseBranch })
                  }
                }}><Play size={16} /> Автопилот до результата</button>}
            <span className="autopilot-help" tabIndex={0} title={AUTOPILOT_HELP} aria-label={AUTOPILOT_HELP}><CircleHelp size={15} /></span>
          </div>
          </>}
        </div>
      </div>
      {goalDetailsOpen && details.targetClosure ? <GoalDetailsModal closure={details.targetClosure} projectName={project.name} onClose={() => setGoalDetailsOpen(false)} /> : null}
      {selectedBranchFailure?.runtime?.phase === 'failed' ? <BranchFailureModal branch={selectedBranchFailure} onClose={() => setSelectedBranchFailure(null)} /> : null}
    </section>
  )
}