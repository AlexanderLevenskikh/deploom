import { contextBridge, ipcRenderer } from 'electron'

const api = {
  bootstrap: () => ipcRenderer.invoke('flow:bootstrap'),
  pickDirectory: () => ipcRenderer.invoke('flow:pick-directory'),
  pickFile: (filters?: Array<{ name: string; extensions: string[] }>) => ipcRenderer.invoke('flow:pick-file', filters),
  copyText: (text: string) => ipcRenderer.invoke('flow:copy-text', text),
  listAgentModels: (agentProvider: string, cwd?: string) => ipcRenderer.invoke('flow:list-agent-models', agentProvider, cwd),
  registerWorkspace: (input: unknown) => ipcRenderer.invoke('flow:register-workspace', input),
  cloneWorkspace: (input: unknown) => ipcRenderer.invoke('flow:clone-workspace', input),
  addProject: (input: unknown) => ipcRenderer.invoke('flow:add-project', input),
  removeProject: (input: unknown) => ipcRenderer.invoke('flow:remove-project', input),
  selectWorkspace: (workspaceId: string) => ipcRenderer.invoke('flow:select-workspace', workspaceId),
  updateWorkspace: (input: unknown) => ipcRenderer.invoke('flow:update-workspace', input),
  listProjectBranches: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:list-project-branches', input),
  updateProjectBranches: (input: unknown) => ipcRenderer.invoke('flow:update-project-branches', input),
  listNodeVersions: () => ipcRenderer.invoke('flow:list-node-versions'),
  updateProjectNode: (input: unknown) => ipcRenderer.invoke('flow:update-project-node', input),
  refreshWorkspace: () => ipcRenderer.invoke('flow:refresh-workspace'),
  getBaselineIntentPlan: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:baseline-intent-plan', input),
  getCurrentProjectPromptPreview: () => ipcRenderer.invoke('flow:current-project-prompt-preview'),
  getCurrentDraftResult: (input: { workspaceId?: string; projectName: string; runId?: string }) => ipcRenderer.invoke('flow:get-current-draft-result', input),
  getDependencyGraphSnapshot: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:dependency-graph-snapshot', input),
  getIterativeTask: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:iterative:task', input),
  exportIterativeTask: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:iterative:export', input),
exportLegacyIterativeTask: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:iterative:export-legacy', input),
  copyIterativeTask: (input: { workspaceId?: string; projectName: string; language?: string }) => ipcRenderer.invoke('flow:iterative:copy-task', input),
  saveIterativeTask: (input: { workspaceId?: string; projectName: string; language?: string }) => ipcRenderer.invoke('flow:iterative:save-task', input),
  setIterativeAutopilot: (input: { workspaceId?: string; projectName: string; enabled: boolean }) => ipcRenderer.invoke('flow:iterative:autopilot', input),
  iterativeStatus: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:iterative:status', input),
  reviewIterativeCohort: (input: { workspaceId?: string; projectName: string; enabled?: boolean; candidateId?: string; selected?: string[] }) => ipcRenderer.invoke('flow:iterative:cohort-review', input),
  iterativeDrive: (input: { workspaceId?: string; projectName: string; autopilot?: boolean; retryInfra?: boolean; discardCandidate?: boolean; resumeTerminal?: boolean }) => ipcRenderer.invoke('flow:iterative:drive', input),
  iterativeBegin: (input: { workspaceId?: string; projectName: string; autopilot?: boolean; discovery?: { mode: 'auto' | 'none'; timeoutSeconds?: number; parallelism?: number; maxPackages?: number }; validationProfile?: { commands: string[]; unitCommand?: string; compareExistingFailures?: boolean; deferredChecks?: string }; checkOnly?: boolean; repair?: boolean; restart?: boolean }) => ipcRenderer.invoke('flow:iterative:begin', input),
  iterativeAgent: (input: { workspaceId?: string; projectName: string; autopilot?: boolean; cleanupNotes?: boolean }) => ipcRenderer.invoke('flow:iterative:agent', input),
  iterativeAttempt: (input: { workspaceId?: string; projectName: string; includeRunLog?: boolean }) => ipcRenderer.invoke('flow:iterative:attempt', input),
  iterativeCancel: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:iterative:cancel', input),
  runAction: (input: unknown) => ipcRenderer.invoke('flow:run-action', input),
  cancelJob: (jobId: string) => ipcRenderer.invoke('flow:cancel-job', jobId),
  pauseJob: (jobId: string) => ipcRenderer.invoke('flow:pause-job', jobId),
  sendAgentNote: (input: { jobId: string; note: string; branch?: string }) => ipcRenderer.invoke('flow:send-agent-note', input),
  recoverWithAgent: (input: { workspaceId?: string; projectName: string; note: string }) => ipcRenderer.invoke('flow:recover-with-agent', input),
  openPath: (targetPath: string) => ipcRenderer.invoke('flow:open-path', targetPath),
  checkForUpdates: () => ipcRenderer.invoke('flow:check-for-updates'),
  setNotificationsEnabled: (enabled: boolean) => ipcRenderer.invoke('flow:set-notifications-enabled', enabled),
  notifyAutopilotComplete: (input: { projectName: string; published: boolean }) => ipcRenderer.invoke('flow:notify-autopilot-complete', input),
  installUpdate: () => ipcRenderer.invoke('flow:install-update'),
  getHardwareSnapshot: () => ipcRenderer.invoke('flow:get-hardware-snapshot'),
  storageMaintenance: (action: 'inspect' | 'clean', operationId: string) => ipcRenderer.invoke('flow:storage-maintenance', action, operationId),
  storageStatus: () => ipcRenderer.invoke('flow:storage-status'),
  onStorageStatus: (handler: (event: unknown) => void) => {
    const listener = (_event: unknown, status: unknown) => handler(status)
    ipcRenderer.on('flow:storage-status', listener)
    return () => ipcRenderer.removeListener('flow:storage-status', listener)
  },
  onStorageProgress: (handler: (event: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => handler(payload)
    ipcRenderer.on('flow:storage-progress', listener)
    return () => ipcRenderer.removeListener('flow:storage-progress', listener)
  },
  getThemePreference: () => ipcRenderer.invoke('flow:get-theme-preference'),
  setThemePreference: (preference: string) => ipcRenderer.invoke('flow:set-theme-preference', preference),
  onUpdateStatus: (handler: (event: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => handler(payload)
    ipcRenderer.on('flow:update-status', listener)
    return () => ipcRenderer.removeListener('flow:update-status', listener)
  },
  onJobOutput: (handler: (event: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => handler(payload)
    ipcRenderer.on('flow:job-output', listener)
    return () => ipcRenderer.removeListener('flow:job-output', listener)
  },
  onMigrationProgressChanged: (handler: (event: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => handler(payload)
    ipcRenderer.on('flow:migration-progress-changed', listener)
    return () => ipcRenderer.removeListener('flow:migration-progress-changed', listener)
  },
  onJobFinished: (handler: (event: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => handler(payload)
    ipcRenderer.on('flow:job-finished', listener)
    return () => ipcRenderer.removeListener('flow:job-finished', listener)
  },
  onDownloadSaved: (handler: (event: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => handler(payload)
    ipcRenderer.on('flow:download-saved', listener)
    return () => ipcRenderer.removeListener('flow:download-saved', listener)
  },
  onIterativeAttempt: (handler: (payload: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => handler(payload)
    ipcRenderer.on('flow:iterative:attempt', listener)
    return () => ipcRenderer.removeListener('flow:iterative:attempt', listener)
  },
}

contextBridge.exposeInMainWorld('dependencyFlow', api)