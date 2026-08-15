import { Bell, Boxes, ExternalLink, GitFork, Pause, Play, RefreshCw, RotateCcw, Settings, Workflow } from 'lucide-react'
import { useState } from 'react'
import './App.css'
import { AddProjectDialog } from './components/AddProjectDialog'
import { DashboardWorkspace } from './components/DashboardWorkspace'
import { FlowWorkspace } from './components/FlowWorkspace'
import { LogPanel } from './components/LogPanel'
import { ProjectRail } from './components/ProjectRail'
import { SetupScreen } from './components/SetupScreen'
import { useDependencyFlow } from './hooks/useDependencyFlow'

type WorkspaceTab = 'flow' | 'dashboard'

function App() {
  const flow = useDependencyFlow()
  const [tab, setTab] = useState<WorkspaceTab>('flow')
  const [setupBusy, setSetupBusy] = useState(false)
  const [showAddProject, setShowAddProject] = useState(false)
  const payload = flow.payload
  const details = payload?.details

  if (flow.loading || !payload) {
    return <div className="splash"><Workflow size={28} /><strong>Dependency Flow</strong><span>Загрузка рабочего состояния…</span></div>
  }

  if (!details) {
    return <SetupScreen busy={setupBusy} onSelectExisting={flow.registerExisting} onPickParent={flow.pickDirectory} onCreate={async (input) => { setSetupBusy(true); try { await flow.cloneWorkspace(input) } catch (error) { flow.setError(error instanceof Error ? error.message : String(error)) } finally { setSetupBusy(false) } }} />
  }

  const project = flow.selectedProject
  const run = project ? details.teamState?.projects[project.name] : undefined
  const sessionBranches = new Set(Object.keys(run?.agentSessions ?? {}))
  if (run?.activeAgentBranch) sessionBranches.add(run.activeAgentBranch)
  for (const branch of details.migrationProgress?.branches ?? []) sessionBranches.add(branch.branch)
  const branchLabels = new Map((details.migrationProgress?.branches ?? []).map((branch) => [branch.branch, branch.label]))
  const knownLogSources = [...sessionBranches].map((branch) => ({ kind: 'group' as const, id: branch, label: branchLabels.get(branch) || branch }))
  const openDashboard = () => setTab('dashboard')
  const updateReady = flow.updateStatus.state === 'ready'
  const updateLabel = flow.updateStatus.state === 'ready'
    ? `Перезапустить и обновить до ${flow.updateStatus.version ?? 'новой версии'}`
    : flow.updateStatus.state === 'downloading'
      ? `Скачивание ${flow.updateStatus.percent ?? 0}%`
      : flow.updateStatus.state === 'checking' || flow.updateStatus.state === 'available'
        ? 'Проверка обновлений…'
        : flow.updateStatus.state === 'error' ? 'Обновление недоступно' : 'Обновлений нет'
  const updateTitle = updateReady
    ? `Установить Dependency Flow ${flow.updateStatus.version ?? ''}`.trim()
    : flow.updateStatus.message || 'Кнопка станет доступна после автоматического скачивания новой версии.'

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-brand"><div className="brand-mark small"><GitFork size={18} /></div><strong>Dependency Flow</strong></div>
        <label className="workspace-select">Workspace<select value={details.workspace.id} onChange={(event) => void flow.selectWorkspace(event.target.value)}>{payload.state.workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}</select></label>
        <div className="header-actions">
          <label className="notification-toggle" title="Показывать системные Windows-уведомления после каждого завершённого этапа FLOW и каждой мигрированной группы."><input type="checkbox" checked={payload.notificationsEnabled} onChange={(event) => void flow.setNotificationsEnabled(event.target.checked)} /><Bell size={15} /><span>Уведомления</span></label>
          <button className={`button ${updateReady ? 'primary' : 'secondary'}`} disabled={!updateReady} title={updateTitle} onClick={() => void flow.installUpdate()}>{updateReady ? <RotateCcw size={16} /> : <RefreshCw className={flow.updateStatus.state === 'checking' || flow.updateStatus.state === 'downloading' ? 'spin' : ''} size={16} />}{updateLabel}</button>
          <button className="button secondary" disabled={!details.dashboardExists} onClick={openDashboard}><ExternalLink size={16} /> Dashboard</button>
          {flow.activeJobId ? <button className="button secondary" onClick={() => void flow.cancelJob()}><Pause size={16} /> Остановить</button> : <button className="button secondary" disabled={!project} onClick={() => project && void flow.runAction({ action: 'preflight', workspaceId: details.workspace.id, projectName: project.name })}><Play size={16} /> Проверить</button>}
          <button className="icon-button" title="Открыть settings.project.json" onClick={() => void flow.openPath(`${details.workspace.path}/${details.workspace.settingsPath}`)}><Settings size={18} /></button>
        </div>
      </header>

      {flow.error ? <div className="error-banner" role="alert"><span>{flow.error}</span><button onClick={() => flow.setError(undefined)}>Закрыть</button></div> : null}

      <div className="app-grid">
        <ProjectRail details={details} selected={project} active={flow.workspaceBusy} onRefreshAll={() => void flow.runAction({ action: 'generate-all', workspaceId: details.workspace.id, label: 'Dependency Flow: все проекты' })} onSelectProject={(name) => void flow.selectProject(name)} onAddProject={() => setShowAddProject(true)} onAddWorkspace={() => void flow.registerExisting()} />

        <main className="main-workspace">
          <div className="workspace-titlebar">
            <div><Boxes size={19} /><h1>{project?.name || 'Проект не выбран'}</h1></div>
            <div className="tab-switch" role="tablist"><button role="tab" aria-selected={tab === 'flow'} className={tab === 'flow' ? 'active' : ''} onClick={() => setTab('flow')}>FLOW</button><button role="tab" aria-selected={tab === 'dashboard'} className={tab === 'dashboard' ? 'active' : ''} onClick={() => setTab('dashboard')}>Dashboard</button></div>
          </div>
          {tab === 'flow' && project ? <FlowWorkspace details={details} project={project} activeAction={flow.activeWorkspaceId === details.workspace.id && flow.activeProjectName === project.name ? flow.activeAction : undefined} autopilotActive={flow.autopilotActive && flow.autopilotProjectName === project.name} onRun={flow.runAction} onSendAgentNote={flow.sendAgentNote} onStartAutopilot={flow.startAutopilot} onStopAutopilot={flow.stopAutopilot} onRecoverWithAgent={flow.recoverWithAgent} onOpenDashboard={openDashboard} onOpenPath={flow.openPath} onChoosePrompt={flow.choosePrompt} onUpdateWorkspace={flow.updateWorkspace} onUpdateProjectBranches={flow.updateProjectBranches} onListAgentModels={flow.listAgentModels} /> : null}
          {tab === 'dashboard' ? <DashboardWorkspace details={details} onRefresh={flow.refresh} onOpenExternal={() => flow.openPath(details.dashboardPath)} /> : null}
        </main>

        <LogPanel logs={flow.logs} knownSources={knownLogSources} environment={payload.environment} active={Boolean(flow.activeJobId)} onSendAgentNote={flow.sendAgentNote} onCancel={() => void flow.cancelJob()} onClear={flow.clearLogs} />
      </div>

      {showAddProject ? <AddProjectDialog workspaceId={details.workspace.id} onClose={() => setShowAddProject(false)} onPickDirectory={flow.pickDirectory} onSubmit={flow.addProject} /> : null}


      <footer className="status-bar">
        <span>Dependency Flow {payload.appVersion}</span><span>Template: {details.git.branch}</span><span>Tool: {payload.environment.python.available ? 'готов' : 'нет Python'}</span><span className="status-spacer" /><span><i className={`status-dot ${payload.environment[details.workspace.agent]?.available ? 'success' : 'danger'}`} />{details.workspace.agent}: {payload.environment[details.workspace.agent]?.available ? 'доступен' : 'не найден'}</span><span>{details.git.dirty ? 'Workspace изменён' : 'Workspace чистый'}</span>
      </footer>
    </div>
  )
}

export default App
