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
import { useLanguage } from './i18n'

type WorkspaceTab = 'flow' | 'dashboard'

function App() {
  const flow = useDependencyFlow()
  const { language, setLanguage, t } = useLanguage()
  const [tab, setTab] = useState<WorkspaceTab>('flow')
  const [setupBusy, setSetupBusy] = useState(false)
  const [showAddProject, setShowAddProject] = useState(false)
  const payload = flow.payload
  const details = payload?.details

  if (flow.loading || !payload) {
    return <div className="splash"><Workflow size={28} /><strong>DepLoom</strong><span>{t('app.loading')}</span></div>
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
    ? t('app.update.restart', { version: flow.updateStatus.version ?? t('app.update.newVersion') })
    : flow.updateStatus.state === 'downloading'
      ? t('app.update.downloading', { percent: flow.updateStatus.percent ?? 0 })
      : flow.updateStatus.state === 'checking' || flow.updateStatus.state === 'available'
        ? t('app.update.checking')
        : flow.updateStatus.state === 'error' ? t('app.update.unavailable') : t('app.update.none')
  const updateTitle = updateReady
    ? t('app.update.installTitle', { version: flow.updateStatus.version ?? '' }).trim()
    : flow.updateStatus.message || t('app.update.waitTitle')

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="app-brand"><div className="brand-mark small"><GitFork size={18} /></div><strong>DepLoom</strong></div>
        <label className="workspace-select">Workspace<select value={details.workspace.id} onChange={(event) => void flow.selectWorkspace(event.target.value)}>{payload.state.workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}</select></label>
        <div className="header-actions">
          <div className="language-switch" role="group" aria-label={t('app.interfaceLanguage')}>
            <button className={language === 'ru' ? 'active' : ''} aria-pressed={language === 'ru'} onClick={() => setLanguage('ru')}>RU</button>
            <button className={language === 'en' ? 'active' : ''} aria-pressed={language === 'en'} onClick={() => setLanguage('en')}>EN</button>
          </div>
          <label className="notification-toggle" title={t('app.notificationsTitle')}><input type="checkbox" checked={payload.notificationsEnabled} onChange={(event) => void flow.setNotificationsEnabled(event.target.checked)} /><Bell size={15} /><span>{t('app.notifications')}</span></label>
          <button className={`button ${updateReady ? 'primary' : 'secondary'}`} disabled={!updateReady} title={updateTitle} onClick={() => void flow.installUpdate()}>{updateReady ? <RotateCcw size={16} /> : <RefreshCw className={flow.updateStatus.state === 'checking' || flow.updateStatus.state === 'downloading' ? 'spin' : ''} size={16} />}{updateLabel}</button>
          <button className="button secondary" disabled={!details.dashboardExists} onClick={openDashboard}><ExternalLink size={16} /> Dashboard</button>
          {flow.activeJobId ? <button className="button secondary" onClick={() => void flow.cancelJob()}><Pause size={16} /> {t('app.stop')}</button> : <button className="button secondary" disabled={!project} onClick={() => project && void flow.runAction({ action: 'preflight', workspaceId: details.workspace.id, projectName: project.name })}><Play size={16} /> {t('app.check')}</button>}
          <button className="icon-button" title={t('app.settingsTitle')} onClick={() => void flow.openPath(`${details.workspace.path}/${details.workspace.settingsPath}`)}><Settings size={18} /></button>
        </div>
      </header>

      {flow.error ? <div className="error-banner" role="alert"><span>{flow.error}</span><button onClick={() => flow.setError(undefined)}>{t('app.closeError')}</button></div> : null}

      <div className="app-grid">
        <ProjectRail details={details} selected={project} active={flow.workspaceBusy} onRefreshAll={() => void flow.runAction({ action: 'generate-all', workspaceId: details.workspace.id, label: 'DepLoom: all projects' })} onSelectProject={(name) => void flow.selectProject(name)} onAddProject={() => setShowAddProject(true)} onAddWorkspace={() => void flow.registerExisting()} />

        <main className="main-workspace">
          <div className="workspace-titlebar">
            <div><Boxes size={19} /><h1>{project?.name || t('app.noProject')}</h1></div>
            <div className="tab-switch" role="tablist"><button role="tab" aria-selected={tab === 'flow'} className={tab === 'flow' ? 'active' : ''} onClick={() => setTab('flow')}>FLOW</button><button role="tab" aria-selected={tab === 'dashboard'} className={tab === 'dashboard' ? 'active' : ''} onClick={() => setTab('dashboard')}>Dashboard</button></div>
          </div>
          {tab === 'flow' && project ? <FlowWorkspace details={details} project={project} activeAction={flow.activeWorkspaceId === details.workspace.id && flow.activeProjectName === project.name ? flow.activeAction : undefined} autopilotActive={flow.autopilotActive && flow.autopilotProjectName === project.name} onRun={flow.runAction} onSendAgentNote={flow.sendAgentNote} onStartAutopilot={flow.startAutopilot} onStopAutopilot={flow.stopAutopilot} onRecoverWithAgent={flow.recoverWithAgent} onOpenDashboard={openDashboard} onOpenPath={flow.openPath} onChoosePrompt={flow.choosePrompt} onUpdateWorkspace={flow.updateWorkspace} onUpdateProjectBranches={flow.updateProjectBranches} onListAgentModels={flow.listAgentModels} /> : null}
          {tab === 'dashboard' ? <DashboardWorkspace details={details} onRefresh={flow.refresh} onOpenExternal={() => flow.openPath(details.dashboardPath)} /> : null}
        </main>

        <LogPanel logs={flow.logs} knownSources={knownLogSources} environment={payload.environment} active={Boolean(flow.activeJobId)} activeJobId={flow.activeJobId} activeAction={flow.activeAction} runStartedAt={flow.activeRunStartedAt} migrationProgress={details.migrationProgress} onSendAgentNote={flow.sendAgentNote} onCancel={() => void flow.cancelJob()} onClear={flow.clearLogs} />
      </div>

      {showAddProject ? <AddProjectDialog workspaceId={details.workspace.id} onClose={() => setShowAddProject(false)} onPickDirectory={flow.pickDirectory} onSubmit={flow.addProject} /> : null}

      <footer className="status-bar">
        <span>DepLoom {payload.appVersion}</span><span>Template: {details.git.branch}</span><span>Tool: {payload.environment.python.available ? t('app.footer.toolReady') : t('app.footer.noPython')}</span><span className="status-spacer" /><span><i className={`status-dot ${payload.environment[details.workspace.agent]?.available ? 'success' : 'danger'}`} />{details.workspace.agent}: {payload.environment[details.workspace.agent]?.available ? t('app.footer.available') : t('app.footer.notFound')}</span><span>{details.git.dirty ? t('app.footer.workspaceChanged') : t('app.footer.workspaceClean')}</span>
      </footer>
    </div>
  )
}

export default App
