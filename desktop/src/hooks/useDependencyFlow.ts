import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ActionInput, AgentProvider, BootstrapPayload, DownloadSaved, FlowAction, JobFinished, JobOutput, ProjectSpec, TargetLevel, UpdateStatus, WorkspaceDetails, WorkspaceRecord } from '../types'
import { goalSeekingStopReason, nextAutopilotAction, type AutopilotPolicyState } from '../autopilot-policy'

const DEMO_WORKSPACE: WorkspaceRecord = {
  id: 'demo',
  name: 'Командный workspace',
  path: 'C:/work/frontend-deps-workspace',
  templateRemote: '',
  toolRemote: '',
  settingsPath: '.dependency-roadmap/settings.project.json',
  agent: 'codex',
  selectedProject: 'Admin.App',
  latestPromptPath: 'C:/work/frontend-deps-workspace/.dependency-roadmap/artifacts/agent-prompt.md',
  latestPromptPaths: { 'Admin.App': 'C:/work/frontend-deps-workspace/.dependency-roadmap/artifacts/agent-prompt.md' },
}

const DEMO_DETAILS: WorkspaceDetails = {
  workspace: DEMO_WORKSPACE,
  projects: [
    { name: 'Admin.App', path: 'C:/work/Admin.App', git: { sourceBranch: 'master', baseBranch: 'deps-demo', mergedBranch: 'deps-demo-merged' } },
    { name: 'Demo.App', path: 'C:/work/Demo.App', git: { sourceBranch: 'master', baseBranch: 'deps-baseline', mergedBranch: 'deps-baseline-merged' } },
    { name: 'Legacy.App', path: 'C:/work/Legacy.App', git: { sourceBranch: 'master', baseBranch: 'libs', mergedBranch: 'libs-merged' } },
  ],
  settingsExists: true,
  dashboardExists: true,
  promptStale: false,
  dashboardPath: 'C:/work/frontend-deps-workspace/.dependency-roadmap/artifacts/local-dependency-roadmap.html',
  projectPromptPath: 'C:/work/frontend-deps-workspace/.dependency-roadmap/artifacts/agent-prompt.md',
  git: { branch: 'master', dirty: false, summary: [] },
  teamState: { schemaVersion: 1, updatedAt: new Date().toISOString(), projects: { 'Admin.App': { lastAction: 'preflight', status: 'passed', updatedAt: new Date().toISOString(), completedActions: ['preflight', 'baseline'] } } },
  projectLevels: { 'Admin.App': { status: 'red', lagOkPct: 42.5 }, 'Demo.App': { status: 'yellow', lagOkPct: 82.1 }, 'Legacy.App': { status: 'green', lagOkPct: 100 } },
  migrationProgress: {
    project: 'Admin.App', mergedBranch: 'deps-demo-merged', currentBranch: 'deps-demo-group-2',
    createdBranches: 2, totalBranches: 3, completedDependencies: 7, readyDependencies: 4, activeDependencies: 4, completedBranches: 1, readyBranches: 1, activeBranches: 1, totalDependencies: 14, unmetPackages: ['h'], dirty: true, dirtyChanges: 3,
    unexpectedBranches: [], factsRef: 'deps-demo-merged', trustworthy: true,
    branches: [
      { branch: 'deps-demo-group-1', label: 'Критичные', packages: ['a', 'b', 'c', 'd', 'e', 'f', 'g'], status: 'merged', checkedOut: false, metPackages: 7 },
      { branch: 'deps-demo-group-2', label: 'Высокие', packages: ['h', 'i', 'j', 'k'], status: 'ready', checkedOut: true, metPackages: 4 },
      { branch: 'deps-demo-group-3', label: 'Средние', packages: ['l', 'm', 'n'], status: 'waiting', checkedOut: false, metPackages: 0 },
    ],
  },
}

const DEMO: BootstrapPayload = {
  appVersion: '0.1.9',
  state: { schemaVersion: 1, selectedWorkspaceId: 'demo', workspaces: [DEMO_WORKSPACE] },
  details: DEMO_DETAILS,
  environment: {
    git: { available: true, version: 'git version 2.51' },
    python: { available: true, version: 'Python 3.14' },
    codex: { available: true, version: 'codex-cli 0.144' },
    opencode: { available: true, version: '1.17' },
  },
  defaults: { templateRemote: DEMO_WORKSPACE.templateRemote, toolRemote: DEMO_WORKSPACE.toolRemote },
  updateStatus: { state: 'ready', version: '0.1.7' },
  notificationsEnabled: true,
}

type AutopilotState = AutopilotPolicyState & {
  workspaceId: string
  releaseBranch?: string
  recoveryCounts: Record<string, number>
  recoveryCycles: number
  infrastructureCounts: Record<string, number>
}

const MAX_AUTOPILOT_RECOVERY_CYCLES = 8
const MAX_AUTOPILOT_INFRA_RETRIES = 3
function autopilotActionInput(details: WorkspaceDetails, state: AutopilotState, action: FlowAction): ActionInput {
  const project = details.projects.find((item) => item.name === state.projectName)
  return {
    action,
    workspaceId: state.workspaceId,
    projectName: state.projectName,
    target: state.target,
    label: action === 'baseline' ? `Dependency Flow: autopilot ${new Date().toISOString().slice(0, 10)}` : 'Dependency Flow: autopilot',
    releaseBranch: state.releaseBranch || project?.git?.releaseBranch || `${project?.git?.baseBranch || project?.git?.branchPrefix || 'libs'}-release`,
    commitMessage: `chore(deps): save ${state.projectName} roadmap state`,
    autopilot: true,
  }
}

export function useDependencyFlow() {
  const [payload, setPayload] = useState<BootstrapPayload>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>()
  type ActiveRun = { jobId: string; action: ActionInput['action']; workspaceId?: string; projectName?: string }
  const [activeRuns, setActiveRuns] = useState<Record<string, ActiveRun>>({})
  const [logs, setLogs] = useState<JobOutput[]>([])
  const viewEpochRef = useRef(0)
  const selectedWorkspaceId = payload?.details?.workspace.id
  const selectedProjectName = payload?.details?.workspace.selectedProject ?? payload?.details?.projects[0]?.name
  const selectedContextRef = useRef<{ workspaceId?: string; projectName?: string }>({})
  selectedContextRef.current = { workspaceId: selectedWorkspaceId, projectName: selectedProjectName }
  const contextKey = (workspaceId?: string, projectName?: string) => `${workspaceId ?? ''}::${projectName ?? ''}`
  const contextIsVisible = (workspaceId?: string, projectName?: string) => {
    const selected = selectedContextRef.current
    return selected.workspaceId === workspaceId && selected.projectName === projectName
  }
  const setContextError = (workspaceId: string | undefined, projectName: string | undefined, message: string | undefined) => {
    if (contextIsVisible(workspaceId, projectName)) setError(message)
  }
  const selectedActiveRun = activeRuns[contextKey(selectedWorkspaceId, selectedProjectName)]
  const activeJobId = selectedActiveRun?.jobId
  const anyActiveJob = Object.keys(activeRuns).length > 0
  const rememberActiveRun = (run: ActiveRun) => setActiveRuns((current) => ({ ...current, [contextKey(run.workspaceId, run.projectName)]: run }))
  const forgetActiveJob = (jobId: string) => setActiveRuns((current) => Object.fromEntries(Object.entries(current).filter(([, run]) => run.jobId !== jobId)) as Record<string, ActiveRun>)
  const [lastDownload, setLastDownload] = useState<DownloadSaved>()
  const [pendingRoadmapRecalc, setPendingRoadmapRecalc] = useState<Array<{ workspaceId: string; projectName: string }>>()
  const [updateStatus, setUpdateStatus] = useState<UpdateStatus>({ state: 'idle' })
  const [autopilotActive, setAutopilotActive] = useState(false)
  const autopilotRef = useRef<AutopilotState | undefined>(undefined)
  const api = window.dependencyFlow

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const nextPayload = api ? await api.bootstrap() : DEMO
      setPayload(nextPayload)
      setUpdateStatus(nextPayload.updateStatus ?? { state: 'idle' })
      setError(undefined)
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError))
    } finally {
      setLoading(false)
    }
  }, [api])

  useEffect(() => { void load() }, [load])

  // A refresh walks the project's git history, so it can outlast the poll
  // interval on a busy machine. Without this guard the polls pile up, multiply
  // the git load that made them slow, and land out of order -- an older
  // snapshot overwriting a newer one is what made stage markers flicker.
  const refreshInFlight = useRef<Promise<void> | undefined>(undefined)
  const refreshQueued = useRef(false)
  const refresh = useCallback(async () => {
    if (!api) return
    refreshQueued.current = true
    if (refreshInFlight.current) return refreshInFlight.current
    const pending = (async () => {
      do {
        refreshQueued.current = false
        const epoch = viewEpochRef.current
        const result = await api.refreshWorkspace()
        if (viewEpochRef.current === epoch) {
          setPayload((current) => current ? { ...current, state: result.state, details: result.details } : current)
        }
      } while (refreshQueued.current)
    })().finally(() => { refreshInFlight.current = undefined })
    refreshInFlight.current = pending
    return pending
  }, [api])

  useEffect(() => {
    if (!api) return
    const intervalMs = anyActiveJob ? 3_000 : 10_000
    const timer = window.setInterval(() => { void refresh() }, intervalMs)
    return () => window.clearInterval(timer)
  }, [anyActiveJob, api, refresh])

  useEffect(() => {
    if (!api) return
    const removeOutput = api.onJobOutput((event) => {
      setLogs((current) => [...current.slice(-999), event])
    })
    const removeMigrationProgress = api.onMigrationProgressChanged((event) => {
      if (event.workspaceId === selectedWorkspaceId && event.projectName === selectedProjectName) void refresh()
    })
    const removeFinished = api.onJobFinished((event: JobFinished) => {
      void (async () => {
        const epoch = viewEpochRef.current
        const result = await api.refreshWorkspace()
        if (viewEpochRef.current === epoch) {
          setPayload((current) => current ? { ...current, state: result.state, details: result.details } : current)
        }
        forgetActiveJob(event.jobId)
        const eventVisible = contextIsVisible(event.workspaceId, event.projectName)
        const autopilot = autopilotRef.current
        if (!autopilot || event.projectName !== autopilot.projectName) {
          if (event.error && eventVisible) setContextError(event.workspaceId, event.projectName, event.error)
          return
        }

        if (event.error) {
          const recovery = result.details.teamState?.projects[autopilot.projectName]?.recovery
          if (recovery?.kind === 'infrastructure') {
            const infrastructureKey = `${recovery.code}:${recovery.message.replace(/\s+/g, ' ').slice(0, 600)}`
            const infrastructureCount = (autopilot.infrastructureCounts[infrastructureKey] ?? 0) + 1
            autopilot.infrastructureCounts[infrastructureKey] = infrastructureCount
            if (infrastructureCount <= MAX_AUTOPILOT_INFRA_RETRIES) {
              const retryAction = (recovery.action && recovery.action !== 'recover' ? recovery.action : event.action === 'recover' ? 'agent' : event.action) as FlowAction
              setContextError(autopilot.workspaceId, autopilot.projectName, undefined)
              setLogs((current) => [...current.slice(-999), { jobId: 'autopilot', stream: 'system', workspaceId: autopilot.workspaceId, projectName: autopilot.projectName, line: `Автопилот: инфраструктурная ошибка ${recovery.code}, автоматический retry ${infrastructureCount}/${MAX_AUTOPILOT_INFRA_RETRIES} этапа ${retryAction}. Пользовательское вмешательство пока не требуется.` }])
              await new Promise((resolve) => window.setTimeout(resolve, Math.min(4000, 750 * infrastructureCount)))
              try {
                const started = await api.runAction(autopilotActionInput(result.details, autopilot, retryAction))
                rememberActiveRun({ jobId: started.jobId, action: retryAction, workspaceId: autopilot.workspaceId, projectName: autopilot.projectName })
                return
              } catch (retryError) {
                // A transient IPC/process-spawn failure can happen before a
                // job exists and therefore cannot produce another finished
                // event. Give the start itself one bounded retry as well.
                await new Promise((resolve) => window.setTimeout(resolve, 1000))
                try {
                  const started = await api.runAction(autopilotActionInput(result.details, autopilot, retryAction))
                  rememberActiveRun({ jobId: started.jobId, action: retryAction, workspaceId: autopilot.workspaceId, projectName: autopilot.projectName })
                  return
                } catch (startRetryError) {
                  autopilotRef.current = undefined
                  setAutopilotActive(false)
                  setContextError(autopilot.workspaceId, autopilot.projectName, `Не удалось запустить infrastructure retry для ${recovery.code}: ${startRetryError instanceof Error ? startRetryError.message : String(startRetryError)}. Исходная ошибка: ${retryError instanceof Error ? retryError.message : String(retryError)}`)
                  return
                }
              }
            }
            autopilotRef.current = undefined
            setAutopilotActive(false)
            setContextError(autopilot.workspaceId, autopilot.projectName, `Инфраструктурная ошибка ${recovery.code} повторилась после ${MAX_AUTOPILOT_INFRA_RETRIES} автоматических retry. ${recovery.message}`)
            return
          }
          if (recovery?.kind === 'agent') {
            const recoveryKey = `${recovery.code}:${recovery.message.replace(/\s+/g, ' ').slice(0, 600)}`
            const recoveryCount = (autopilot.recoveryCounts[recoveryKey] ?? 0) + 1
            autopilot.recoveryCounts[recoveryKey] = recoveryCount
            autopilot.recoveryCycles += 1
            if (recoveryCount > 1 || autopilot.recoveryCycles > MAX_AUTOPILOT_RECOVERY_CYCLES) {
              autopilotRef.current = undefined
              setAutopilotActive(false)
              const reason = recoveryCount > 1
                ? `одинаковый recovery ${recovery.code} уже выполнялся и не устранил исходную причину`
                : `исчерпан общий budget ${MAX_AUTOPILOT_RECOVERY_CYCLES} recovery-циклов`
              setContextError(autopilot.workspaceId, autopilot.projectName, undefined)
              setLogs((current) => [...current.slice(-999), { jobId: 'autopilot', stream: 'system', workspaceId: autopilot.workspaceId, projectName: autopilot.projectName, line: `Автопилот завершил автономные попытки без красного аварийного статуса: ${reason}. Лучшее Git/state сохранено для handoff. ${recovery.message}` }])
              return
            }
            setLogs((current) => [...current.slice(-999), { jobId: 'autopilot', stream: 'system', workspaceId: autopilot.workspaceId, projectName: autopilot.projectName, line: `Автопилот перехватил ${recovery.code}: запускаю встроенный recovery-agent без ручного баннера.` }])
            try {
              const recovered = await api.recoverWithAgent({
                workspaceId: autopilot.workspaceId,
                projectName: autopilot.projectName,
                note: 'Autopilot recovery: самостоятельно разберись с сохранённой ошибкой, сохрани все safety-инварианты, доведи текущий этап максимально до зелёного состояния и верни управление оркестратору.',
              })
              rememberActiveRun({ jobId: recovered.jobId, action: 'recover', workspaceId: autopilot.workspaceId, projectName: autopilot.projectName })
              return
            } catch (recoveryError) {
              autopilotRef.current = undefined
              setAutopilotActive(false)
              setContextError(autopilot.workspaceId, autopilot.projectName, recoveryError instanceof Error ? recoveryError.message : String(recoveryError))
              return
            }
          }
          autopilotRef.current = undefined
          setAutopilotActive(false)
          setContextError(autopilot.workspaceId, autopilot.projectName, event.error)
          return
        }

        // Do not clear recovery history merely because a recovery-agent process
        // exited successfully. The original stage must prove the repair by
        // passing next; otherwise the same failure would be allowed to loop.
        const goalStop = goalSeekingStopReason(result.details, autopilot)
        if (goalStop) {
          autopilotRef.current = undefined
          setAutopilotActive(false)
          setContextError(autopilot.workspaceId, autopilot.projectName, undefined)
          setLogs((current) => [...current.slice(-999), { jobId: 'autopilot', stream: 'system', workspaceId: autopilot.workspaceId, projectName: autopilot.projectName, line: `Автопилот исчерпал повторяющийся путь без аварии: ${goalStop}. Лучшее Git/state сохранено; повторять тот же цикл автоматически не буду.` }])
          return
        }
        const next = nextAutopilotAction(result.details, autopilot)
        if (!next) {
          autopilotRef.current = undefined
          setAutopilotActive(false)
          setContextError(autopilot.workspaceId, autopilot.projectName, undefined)
          setLogs((current) => [...current.slice(-999), { jobId: 'autopilot', stream: 'system', workspaceId: autopilot.workspaceId, projectName: autopilot.projectName, line: `Автопилот завершил доступный FLOW для ${autopilot.projectName}${autopilot.publish ? ' включая публикацию' : '; публикация отключена настройкой git.push'}.` }])
          await api.notifyAutopilotComplete({ projectName: autopilot.projectName, published: autopilot.publish })
          return
        }
        try {
          const input = autopilotActionInput(result.details, autopilot, next)
          setLogs((current) => [...current.slice(-999), { jobId: 'autopilot', stream: 'system', workspaceId: autopilot.workspaceId, projectName: autopilot.projectName, line: `Автопилот → ${next}` }])
          const started = await api.runAction(input)
          rememberActiveRun({ jobId: started.jobId, action: next, workspaceId: autopilot.workspaceId, projectName: autopilot.projectName })
        } catch (nextError) {
          autopilotRef.current = undefined
          setAutopilotActive(false)
          setContextError(autopilot.workspaceId, autopilot.projectName, nextError instanceof Error ? nextError.message : String(nextError))
        }
      })().catch((finishError) => {
        const failedWorkspaceId = event.workspaceId
        const failedProjectName = event.projectName
        autopilotRef.current = undefined
        setAutopilotActive(false)
        setContextError(failedWorkspaceId, failedProjectName, finishError instanceof Error ? finishError.message : String(finishError))
      })
    })
    const removeUpdateStatus = api.onUpdateStatus(setUpdateStatus)
    const removeDownload = api.onDownloadSaved((event) => {
      setLastDownload(event)
      setLogs((current) => [...current.slice(-999), { jobId: 'download', stream: 'system', workspaceId: event.workspaceId, projectName: event.projectName, line: `Сохранено: ${event.path}` }])
      // The dashboard covers every project at once, so the edited project is
      // not necessarily the selected one; main.ts reports which ones actually
      // changed and each is recalculated in turn.
      const projects = event.recalculateProjects?.length ? event.recalculateProjects : (event.projectName ? [event.projectName] : [])
      if (event.recalculate && event.workspaceId && projects.length) {
        const workspaceId = event.workspaceId
        setPendingRoadmapRecalc((current) => {
          const queued = current ?? []
          const missing = projects.filter((project) => !queued.some((entry) => entry.workspaceId === workspaceId && entry.projectName === project))
          return [...queued, ...missing.map((projectName) => ({ workspaceId, projectName }))]
        })
        setLogs((current) => [...current.slice(-999), { jobId: 'system', stream: 'system', workspaceId: event.workspaceId, projectName: event.projectName, line: `Scope изменён (${projects.join(', ')}): roadmap и targets будут пересчитаны автоматически.` }])
      }
      void refresh()
    })
    return () => { removeOutput(); removeMigrationProgress(); removeFinished(); removeDownload(); removeUpdateStatus() }
  }, [api, refresh, selectedProjectName, selectedWorkspaceId])


  // One project per pass: only one job may run per workspace, so the queue is
  // drained as each recalculation finishes (activeJobId is a dependency, so
  // this re-fires on completion).
  useEffect(() => {
    const next = pendingRoadmapRecalc?.[0]
    if (!api || !next || anyActiveJob) return
    let cancelled = false
    const input: ActionInput = {
      action: 'generate',
      workspaceId: next.workspaceId,
      projectName: next.projectName,
      label: 'Dependency Flow: автоматический пересчёт после изменения scope',
    }
    setPendingRoadmapRecalc((current) => {
      const rest = (current ?? []).slice(1)
      return rest.length ? rest : undefined
    })
    void api.runAction(input).then((result) => {
      if (!cancelled) rememberActiveRun({ jobId: result.jobId, action: input.action, workspaceId: input.workspaceId, projectName: input.projectName })
    }).catch((recalcError) => {
      if (!cancelled) setContextError(input.workspaceId, input.projectName, recalcError instanceof Error ? recalcError.message : String(recalcError))
    })
    return () => { cancelled = true }
  }, [anyActiveJob, api, pendingRoadmapRecalc])

  const applyWorkspaceResult = useCallback((result: { state: BootstrapPayload['state']; details: WorkspaceDetails }, expectedEpoch = viewEpochRef.current) => {
    if (viewEpochRef.current !== expectedEpoch) return
    setPayload((current) => current ? { ...current, state: result.state, details: result.details } : current)
  }, [])

  const pickDirectory = useCallback(async () => api?.pickDirectory(), [api])

  const registerExisting = useCallback(async () => {
    if (!api) return
    const path = await api.pickDirectory()
    if (!path) return
    applyWorkspaceResult(await api.registerWorkspace({ path }))
  }, [api, applyWorkspaceResult])

  const cloneWorkspace = useCallback(async (input: { parentPath: string; folderName: string; teamRemote?: string; templateRemote?: string }) => {
    if (!api) throw new Error('Клонирование доступно в desktop-приложении.')
    applyWorkspaceResult(await api.cloneWorkspace(input))
  }, [api, applyWorkspaceResult])

  const addProject = useCallback(async (input: { workspaceId?: string; name: string; path: string; sourceBranch?: string; baseBranch?: string; mergedBranch?: string }) => {
    if (!api) throw new Error('Добавление проекта доступно в desktop-приложении.')
    applyWorkspaceResult(await api.addProject(input))
  }, [api, applyWorkspaceResult])

  const selectWorkspace = useCallback(async (workspaceId: string) => {
    if (!api) return
    setError(undefined)
    const epoch = ++viewEpochRef.current
    applyWorkspaceResult(await api.selectWorkspace(workspaceId), epoch)
  }, [api, applyWorkspaceResult])

  const selectProject = useCallback(async (projectName: string) => {
    if (!payload?.details) return
    const workspaceId = payload.details.workspace.id
    setError(undefined)
    const epoch = ++viewEpochRef.current
    // Change the visible context immediately, but clear derived selected-project
    // facts until main returns a snapshot computed for this exact project.
    setPayload((current) => current?.details && current.details.workspace.id === workspaceId ? {
      ...current,
      details: {
        ...current.details,
        workspace: { ...current.details.workspace, selectedProject: projectName },
        dashboardExists: false,
        projectPromptPath: undefined,
        promptStale: false,
        targetClosure: undefined,
        migrationProgress: undefined,
      },
    } : current)
    if (!api) return
    const result = await api.updateWorkspace({ id: workspaceId, selectedProject: projectName })
    applyWorkspaceResult(result, epoch)
  }, [api, applyWorkspaceResult, payload?.details])

  const updateWorkspace = useCallback(async (patch: Partial<WorkspaceRecord> & { id: string }) => {
    if (!api) {
      setPayload((current) => current?.details ? { ...current, details: { ...current.details, workspace: { ...current.details.workspace, ...patch } } } : current)
      return
    }
    applyWorkspaceResult(await api.updateWorkspace(patch))
  }, [api, applyWorkspaceResult])

  const updateProjectBranches = useCallback(async (input: { workspaceId?: string; projectName: string; branchBase?: string; push?: boolean }) => {
    if (!api) {
      setPayload((current) => {
        if (!current?.details) return current
        const branchBase = input.branchBase?.trim() || 'libs'
        const projects = current.details.projects.map((project) => project.name === input.projectName ? {
          ...project,
          git: { ...project.git, baseBranch: branchBase, branchPrefix: branchBase, mergedBranch: `${branchBase}-merged`, ...(typeof input.push === 'boolean' ? { push: input.push } : {}) },
        } : project)
        return { ...current, details: { ...current.details, projects } }
      })
      return
    }
    applyWorkspaceResult(await api.updateProjectBranches(input))
  }, [api, applyWorkspaceResult])
  const runAction = useCallback(async (input: ActionInput) => {
    setContextError(input.workspaceId, input.projectName, undefined)
    const logContext = { workspaceId: input.workspaceId, projectName: input.projectName }
    setLogs((current) => [...current, { jobId: 'system', stream: 'system', ...logContext, line: `Запуск: ${input.action}` }])
    if (!api) {
      const demoId = `demo-${Date.now()}`
      rememberActiveRun({ jobId: demoId, action: input.action, workspaceId: input.workspaceId, projectName: input.projectName })
      window.setTimeout(() => {
        setLogs((current) => [...current, { jobId: demoId, stream: 'stdout', workspaceId: input.workspaceId, projectName: input.projectName, line: 'Демонстрационный режим: команда успешно завершена.' }])
        forgetActiveJob(demoId)
      }, 700)
      return
    }
    try {
      const result = await api.runAction(input)
      rememberActiveRun({ jobId: result.jobId, action: input.action, workspaceId: input.workspaceId, projectName: input.projectName })
    } catch (runError) {
      const message = runError instanceof Error ? runError.message : String(runError)
      setLogs((current) => [...current.slice(-999), { jobId: 'system', stream: 'stderr', ...logContext, line: message }])
      setContextError(input.workspaceId, input.projectName, message)
    }
  }, [api])

  const startAutopilot = useCallback(async (input: { workspaceId: string; projectName: string; target: TargetLevel; releaseBranch?: string }) => {
    if (!api) return
    let firstAction: FlowAction | undefined
    try {
      if (anyActiveJob) throw new Error('Сначала завершите или остановите текущую изменяющую команду.')
      setContextError(input.workspaceId, input.projectName, undefined)
      const epoch = viewEpochRef.current
      const refreshed = await api.refreshWorkspace()
      if (viewEpochRef.current === epoch) {
        setPayload((current) => current ? { ...current, state: refreshed.state, details: refreshed.details } : current)
      }
      const project = refreshed.details.projects.find((item) => item.name === input.projectName)
      if (!project) throw new Error('Проект ' + input.projectName + ' не найден.')

      const state: AutopilotState = {
        workspaceId: input.workspaceId, projectName: input.projectName, target: input.target, releaseBranch: input.releaseBranch,
        publish: Boolean(project.git?.push), recoveryCounts: {}, recoveryCycles: 0, infrastructureCounts: {}, goalSignatures: {}, goalCycles: 0,
      }
      autopilotRef.current = state
      setAutopilotActive(true)
      const initialGoalStop = goalSeekingStopReason(refreshed.details, state)
      if (initialGoalStop) throw new Error('Автопилот не может продолжить без повторения доказанно безрезультатного goal-цикла: ' + initialGoalStop)
      firstAction = nextAutopilotAction(refreshed.details, state)
      if (!firstAction) {
        autopilotRef.current = undefined
        setAutopilotActive(false)
        return
      }
      const actionInput = autopilotActionInput(refreshed.details, state, firstAction)
      setLogs((current) => [...current.slice(-999), { jobId: 'autopilot', stream: 'system', workspaceId: input.workspaceId, projectName: input.projectName, line: 'Автопилот запущен для ' + input.projectName + ': цель ' + input.target + ', первый этап ' + firstAction + '.' }])
      const started = await api.runAction(actionInput)
      rememberActiveRun({ jobId: started.jobId, action: firstAction, workspaceId: input.workspaceId, projectName: input.projectName })
    } catch (startError) {
      const message = startError instanceof Error ? startError.message : String(startError)
      autopilotRef.current = undefined
      setAutopilotActive(false)
      setContextError(input.workspaceId, input.projectName, message)
      setLogs((current) => [...current.slice(-999), { jobId: 'autopilot', stream: 'stderr', workspaceId: input.workspaceId, projectName: input.projectName, line: 'Автопилот не смог запуститься' + (firstAction ? ' на этапе ' + firstAction : '') + ': ' + message }])
    }
  }, [anyActiveJob, api])
  const stopAutopilot = useCallback(async () => {
    autopilotRef.current = undefined
    setAutopilotActive(false)
    if (api && activeJobId) await api.cancelJob(activeJobId)
  }, [activeJobId, api])

  const cancelJob = useCallback(async () => {
    if (api && activeJobId) await api.cancelJob(activeJobId)
  }, [activeJobId, api])

  // OpenCode uses a local attached server and can receive this as a real
  // asynchronous message in the current session. Codex/Claude still use
  // one-shot CLI invocations, so main.ts queues their message for the next
  // autonomous attempt rather than dropping it.
  const sendAgentNote = useCallback(async (note: string, branch?: string): Promise<boolean> => {
    if (!api || !activeJobId) return false
    return api.sendAgentNote({ jobId: activeJobId, note, branch })
  }, [activeJobId, api])

  const recoverWithAgent = useCallback(async (input: { workspaceId?: string; projectName: string; note: string }) => {
    if (!api) return
    setContextError(input.workspaceId, input.projectName, undefined)
    const result = await api.recoverWithAgent(input)
    rememberActiveRun({ jobId: result.jobId, action: 'recover', workspaceId: input.workspaceId, projectName: input.projectName })
  }, [api])

  const choosePrompt = useCallback(async (projectName: string) => {
    if (!api || !payload?.details) return
    const promptPath = await api.pickFile([{ name: 'Agent prompt', extensions: ['txt', 'md'] }])
    if (promptPath) await updateWorkspace({
      id: payload.details.workspace.id,
      latestPromptPaths: { [projectName]: promptPath },
    })
  }, [api, payload?.details, updateWorkspace])

  const openPath = useCallback(async (path?: string) => {
    if (api && path) await api.openPath(path)
  }, [api])

  const listAgentModels = useCallback(async (agentProvider: AgentProvider, cwd?: string): Promise<string[]> => {
    if (!api) return agentProvider === 'claude' ? ['opus', 'sonnet', 'haiku'] : []
    try {
      return await api.listAgentModels(agentProvider, cwd)
    } catch {
      return []
    }
  }, [api])
  const checkForUpdates = useCallback(async () => {
    if (!api) return
    setUpdateStatus({ state: 'checking' })
    try {
      const result = await api.checkForUpdates()
      if (result.error) setUpdateStatus(result.error)
      else if (result.development) setUpdateStatus({ state: 'current', version: result.currentVersion })
    } catch (updateError) {
      setUpdateStatus({ state: 'error', message: updateError instanceof Error ? updateError.message : String(updateError) })
    }
  }, [api])

  const setNotificationsEnabled = useCallback(async (enabled: boolean) => {
    if (!api) {
      setPayload((current) => current ? { ...current, notificationsEnabled: enabled } : current)
      return
    }
    const result = await api.setNotificationsEnabled(enabled)
    setPayload((current) => current ? { ...current, notificationsEnabled: result.enabled } : current)
  }, [api])
  const installUpdate = useCallback(async () => {
    if (api) await api.installUpdate()
  }, [api])

  const selectedProject = useMemo<ProjectSpec | undefined>(() => {
    const details = payload?.details
    if (!details) return undefined
    return details.projects.find((project) => project.name === details.workspace.selectedProject) ?? details.projects[0]
  }, [payload?.details])
  const visibleLogs = useMemo(() => logs.filter((entry) =>
    entry.workspaceId === selectedWorkspaceId && entry.projectName === selectedProject?.name
  ), [logs, selectedProject?.name, selectedWorkspaceId])

  return {
    payload, loading, error, activeJobId, workspaceBusy: anyActiveJob, autopilotActive, autopilotProjectName: autopilotRef.current?.projectName, activeAction: selectedActiveRun?.action, activeWorkspaceId: selectedActiveRun?.workspaceId, activeProjectName: selectedActiveRun?.projectName, logs: visibleLogs, lastDownload, updateStatus, selectedProject,
    load, refresh, pickDirectory, registerExisting, cloneWorkspace, addProject, selectWorkspace, selectProject, updateWorkspace, updateProjectBranches,
    runAction, startAutopilot, stopAutopilot, cancelJob, sendAgentNote, recoverWithAgent, choosePrompt, openPath, listAgentModels, checkForUpdates, setNotificationsEnabled, installUpdate, clearLogs: () => setLogs((current) => current.filter((entry) => !(entry.workspaceId === selectedWorkspaceId && entry.projectName === selectedProject?.name))), setError,
  }
}
