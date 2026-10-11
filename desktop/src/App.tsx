import { BarChart3, Bell, Boxes, Download, GitFork, Network, Pause, Play, ScrollText, Settings, Trash2, Workflow } from 'lucide-react'
import { lazy, Suspense, useEffect, useState } from 'react'
import './App.css'
import { AddProjectDialog } from './components/AddProjectDialog'
import { WorkspaceDialog } from './components/WorkspaceDialog'
import { FlowWorkspace } from './components/FlowWorkspace'
import { LogPanel } from './components/LogPanel'
import { ProjectRail } from './components/ProjectRail'
import { QuickSelect } from './components/QuickSelect'
import { SetupScreen } from './components/SetupScreen'
import { WorkspaceOverview } from './components/WorkspaceOverview'
import { useDependencyFlow } from './hooks/useDependencyFlow'
import { useLanguage } from './i18n'
import { StorageMaintenanceDialog } from './components/StorageMaintenanceDialog'
import { StorageBackgroundStatus } from './components/StorageBackgroundStatus'

// Heavy, non-first-screen views are loaded on demand so the first screen and
// the first tab switch stay cheap; the named export is preserved for the
// contract check.
const DashboardWorkspace = lazy(() => import('./components/DashboardWorkspace').then((module) => ({ default: module.DashboardWorkspace })))
const DependencyGraphWorkspace = lazy(() => import('./components/DependencyGraphWorkspace').then((module) => ({ default: module.DependencyGraphWorkspace })))

import type { StorageStatus } from './types'

type WorkspaceTab = 'overview' | 'dependencies' | 'report' | 'journal'

function App() {
  const flow = useDependencyFlow()
  const { language, setLanguage, t } = useLanguage()
  const [tab, setTab] = useState<WorkspaceTab>('overview')
  const [setupBusy, setSetupBusy] = useState(false)
  const [showAddProject, setShowAddProject] = useState(false)
  const [showWorkspaceDialog, setShowWorkspaceDialog] = useState(false)
  const [showStorage, setShowStorage] = useState(false)
  const [storageStatus, setStorageStatus] = useState<StorageStatus>()
  useEffect(() => {
    const api = window.dependencyFlow
    if (!api?.onStorageStatus) return
    let live = true; let received = false
    const unsubscribe = api.onStorageStatus(status => { received = true; if (live) setStorageStatus(status) })
    void api.storageStatus().then(status => { if (live && !received) setStorageStatus(status) }).catch(() => {})
    return () => { live = false; unsubscribe() }
  }, [])
  const [graphFullscreen, setGraphFullscreen] = useState(false)
  const [graphLeftHidden, setGraphLeftHidden] = useState(false)
  const themePreference = flow.themePreference
  const payload = flow.payload
  const details = payload?.details

  if (flow.loading) {
    return <div className="splash"><Workflow size={28} /><strong>DepLoom</strong><span>{t('app.loading')}</span></div>
  }

  if (!payload) {
    return <div className="splash"><Workflow size={28} /><strong>DepLoom</strong><span>{flow.error || 'Desktop bridge is unavailable.'}</span></div>
  }

  if (!details) {
    return <SetupScreen busy={setupBusy} onSelectExisting={agent => flow.registerExisting(undefined, agent)} onPickParent={flow.pickDirectory} onCreate={async (input) => { setSetupBusy(true); try { await flow.cloneWorkspace(input) } catch (error) { flow.setError(error instanceof Error ? error.message : String(error)) } finally { setSetupBusy(false) } }} />
  }

  const project = flow.selectedProject
  const openDashboard = () => setTab('report')
  const graphActive = tab === 'dependencies'
  const graphHideLeft = graphActive && (graphFullscreen || graphLeftHidden)
  const updateReady = flow.updateStatus.state === 'ready'
  const updateDownloading = flow.updateStatus.state === 'checking' || flow.updateStatus.state === 'available' || flow.updateStatus.state === 'downloading'
  const updateTitle = updateReady
    ? t('app.update.installTitle', { version: flow.updateStatus.version ?? '' }).trim()
    : flow.updateStatus.message || (flow.updateStatus.state === 'downloading'
      ? t('app.update.downloading', { percent: flow.updateStatus.percent ?? 0 })
      : t('app.update.waitTitle'))

  return (
    <div className={`app-shell${graphActive && graphFullscreen ? ' graph-fullscreen' : ''}`}>
      <header className="app-header">
        <div className="app-brand"><div className="brand-mark small"><GitFork size={18} /></div><strong>DepLoom</strong></div>
        <div className="workspace-select"><span>Workspace</span><QuickSelect value={details.workspace.id} options={payload.state.workspaces.map((workspace) => ({ value: workspace.id, label: workspace.name }))} onChange={(value) => void flow.selectWorkspace(value)} ariaLabel="Workspace" /></div>
        <div className="header-actions">
          <div className="theme-switch" role="group" aria-label={t('app.theme.interface')}>
            <button className={themePreference === 'light' ? 'active' : ''} aria-pressed={themePreference === 'light'} title={t('app.theme.light')} onClick={() => void flow.setThemePreference('light')}>☀</button>
            <button className={themePreference === 'system' ? 'active' : ''} aria-pressed={themePreference === 'system'} title={t('app.theme.system')} onClick={() => void flow.setThemePreference('system')}>◐</button>
            <button className={themePreference === 'dark' ? 'active' : ''} aria-pressed={themePreference === 'dark'} title={t('app.theme.dark')} onClick={() => void flow.setThemePreference('dark')}>☾</button>
          </div>
          <div className="language-switch" role="group" aria-label={t('app.interfaceLanguage')}>
            <button className={language === 'ru' ? 'active' : ''} aria-pressed={language === 'ru'} onClick={() => setLanguage('ru')}>RU</button>
            <button className={language === 'en' ? 'active' : ''} aria-pressed={language === 'en'} onClick={() => setLanguage('en')}>EN</button>
          </div>
          <label className="notification-toggle" title={t('app.notificationsTitle')}><input type="checkbox" checked={payload.notificationsEnabled} onChange={(event) => void flow.setNotificationsEnabled(event.target.checked)} /><Bell size={15} /><span>{t('app.notifications')}</span></label>
          <button className={`icon-button update-download-button${updateReady ? ' ready' : ''}${updateDownloading ? ' downloading' : ''}`} disabled={!updateReady} aria-busy={updateDownloading} aria-label={updateTitle} title={updateTitle} onClick={() => void flow.installUpdate()}><Download size={17} /></button>
          {flow.activeJobId ? <button className="button secondary" onClick={() => void flow.cancelJob()}><Pause size={16} /> {t('app.stop')}</button> : <button className="button secondary" disabled={!project} onClick={() => project && void flow.runAction({ action: 'preflight', workspaceId: details.workspace.id, projectName: project.name })}><Play size={16} /> {t('app.check')}</button>}
          <button className="icon-button" title={t('app.storageCleanup')} aria-label={t('app.storageCleanup')} onClick={() => setShowStorage(true)}><Trash2 size={18} /></button>
          <button className="icon-button" title={t('app.settingsTitle')} onClick={() => void flow.openPath(`${details.workspace.path}/${details.workspace.settingsPath}`)}><Settings size={18} /></button>
        </div>
      </header>

      {/* BLOCK_PROGRESSIVE_HUMAN_FLOW_V1 */}
      {/* BLOCK_HUMAN_FLOW_GRAPH_MONITOR_CLEANUP_V1 */}
      {flow.error ? <div className="human-error-toast" role="alert"><div><strong>{t('app.error.title')}</strong><span>{t('app.error.preservedResult')}</span><span className="error-summary">{flow.error.split('\n')[0]}</span><details><summary>{t('app.error.technicalDetails')}</summary><pre>{flow.error}</pre></details></div><button type="button" className="icon-button" aria-label={t('common.close')} onClick={() => flow.setError(undefined)}>×</button></div> : null}

      <div className={`app-grid${graphHideLeft ? ' graph-hide-project-rail' : ''}`}>
        {!graphHideLeft ? <ProjectRail details={details} selected={project} active={flow.workspaceBusy} onRefreshAll={() => void flow.runAction({ action: 'generate-all', workspaceId: details.workspace.id, label: 'DepLoom: all projects' })} onSelectProject={(name) => void flow.selectProject(name)} onAddProject={() => setShowAddProject(true)} onAddWorkspace={() => setShowWorkspaceDialog(true)} onRemoveWorkspace={() => void flow.removeWorkspace().catch(error => flow.setError(error instanceof Error ? error.message : String(error)))} onRemoveProject={(name) => void flow.removeProject(name).catch((error) => flow.setError(error instanceof Error ? error.message : String(error)))} onOverview={() => { setTab('overview'); void flow.selectOverview() }} /> : null}

        <main className="main-workspace">
          <div className="workspace-titlebar">
            <div><Boxes size={19} /><h1>{project?.name || t('app.noProject')}</h1></div>
            <div className="tab-switch" role="tablist"><button role="tab" aria-selected={tab === 'overview'} className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}><Workflow size={14} />{t('app.tab.overview')}</button><button role="tab" aria-selected={tab === 'dependencies'} className={tab === 'dependencies' ? 'active' : ''} onClick={() => setTab('dependencies')}><Network size={14} />{t('app.tab.dependencies')}</button><button role="tab" aria-selected={tab === 'report'} className={tab === 'report' ? 'active' : ''} onClick={() => setTab('report')}><BarChart3 size={14} />{t('app.tab.report')}</button><button role="tab" aria-selected={tab === 'journal'} className={tab === 'journal' ? 'active' : ''} onClick={() => setTab('journal')}><ScrollText size={14} />{t('app.tab.journal')}</button></div>
          </div>
          <Suspense fallback={<div className="main-workspace-suspense" aria-busy="true"><span>{t('app.loading')}</span></div>}>
            {!project ? <WorkspaceOverview details={details} environment={payload.environment} storageStatus={storageStatus} selectedName={undefined} onSelectProject={(name) => void flow.selectProject(name)} onAddProject={() => setShowAddProject(true)} onOpenStorage={() => setShowStorage(true)} /> : null}
          {tab === 'overview' && project ? <FlowWorkspace details={details} project={project} appVersion={payload.appVersion} activeAction={flow.activeWorkspaceId === details.workspace.id && flow.activeProjectName === project.name ? flow.activeAction : undefined} activeRunId={flow.activeRunId} activeRunStartedAt={flow.activeRunStartedAt} activeDraftProgress={flow.activeDraftProgress} draftLaunch={flow.draftLaunch} onMarkDraftLaunched={flow.markDraftLaunched} onResetDraftLaunch={flow.resetDraftLaunch} onAcknowledgeDraftRun={flow.acknowledgeDraftRun} autopilotActive={flow.autopilotActive && flow.autopilotProjectName === project.name} baselineDecision={flow.baselineDecision} onClearBaselineDecision={flow.clearBaselineDecision} onGetBaselineIntentPlan={flow.getBaselineIntentPlan} onGetCurrentDraftResult={flow.getCurrentDraftResult} onRun={flow.runAction} onStartAutopilot={flow.startAutopilot} onStopAutopilot={flow.stopAutopilot} onRecoverWithAgent={flow.recoverWithAgent} onOpenDashboard={openDashboard} onOpenPath={flow.openPath} onChoosePrompt={flow.choosePrompt} onUpdateWorkspace={flow.updateWorkspace} onUpdateProjectBranches={flow.updateProjectBranches} onUpdateProjectNode={flow.updateProjectNode} onListNodeVersions={flow.listNodeVersions} onListAgentModels={flow.listAgentModels} onGetIterativeTask={flow.getIterativeTask} onExportIterativeTask={flow.exportIterativeTask} onExportLegacyIterativeTask={flow.exportLegacyIterativeTask} onCopyIterativeTask={flow.copyIterativeTask} onSaveIterativeTask={flow.saveIterativeTask} onIterativeStatus={flow.iterativeStatus} onIterativeDrive={flow.iterativeDrive} onIterativeBegin={flow.iterativeBegin} onIterativeAgent={flow.iterativeAgent} onIterativeAttempt={flow.iterativeAttemptRead} onIterativeCancel={flow.iterativeCancel} liveIterativeAttempt={flow.iterativeAttempt} /> : null}
          {tab === 'dependencies' && project ? <DependencyGraphWorkspace details={details} project={project} baselineDecision={flow.baselineDecision} onGetSnapshot={flow.getDependencyGraphSnapshot} onOpenFlow={() => setTab('overview')} fullscreen={graphFullscreen} leftPaneHidden={graphLeftHidden} onToggleFullscreen={() => setGraphFullscreen((value) => !value)} onToggleLeftPane={() => setGraphLeftHidden((value) => !value)} /> : null}
          {tab === 'report' ? <DashboardWorkspace details={details} onRefresh={flow.refresh} onOpenExternal={() => flow.openPath(details.dashboardPath)} /> : null}
          {tab === 'journal' && project ? <LogPanel logs={flow.logs} environment={payload.environment} active={Boolean(flow.activeJobId)} activeJobId={flow.activeRunId} activeAction={flow.activeJobId ? flow.activeAction : undefined} runStartedAt={flow.activeRunStartedAt} migrationProgress={details.migrationProgress} refreshError={flow.refreshError} onSendAgentNote={flow.sendAgentNote} onCancel={() => flow.cancelJob()} onClear={flow.clearLogs} /> : null}
          </Suspense>
        </main>

      </div>

      {showAddProject ? <AddProjectDialog workspaceId={details.workspace.id} onClose={() => setShowAddProject(false)} onPickDirectory={flow.pickDirectory} onDiscoverProject={flow.discoverProject} onSubmit={flow.addProject} /> : null}

      {showStorage ? <StorageMaintenanceDialog status={storageStatus} onClose={() => setShowStorage(false)} onMaintenance={(action, operationId) => window.dependencyFlow!.storageMaintenance(action, operationId)} onProgress={window.dependencyFlow!.onStorageProgress} /> : null}
      {showWorkspaceDialog ? <WorkspaceDialog onClose={() => setShowWorkspaceDialog(false)} onPickDirectory={flow.pickDirectory} onConnectExisting={(path, agent) => flow.registerExisting(path, agent)} onCreate={flow.cloneWorkspace} /> : null}

      <footer className="status-bar">
        <StorageBackgroundStatus status={storageStatus} onOpen={() => setShowStorage(true)} />
        <span>DepLoom {payload.appVersion}</span><span>Template: {details.git.branch}</span><span>Tool: {payload.environment.python.available ? t('app.footer.toolReady') : t('app.footer.noPython')}</span><span className="status-spacer" /><span><i className={`status-dot ${payload.environment[details.workspace.agent]?.available ? 'success' : 'danger'}`} />{details.workspace.agent}: {payload.environment[details.workspace.agent]?.available ? t('app.footer.available') : t('app.footer.notFound')}</span><span>{details.git.dirty ? t('app.footer.workspaceChanged') : t('app.footer.workspaceClean')}</span>
      </footer>
    </div>
  )
}

export default App
