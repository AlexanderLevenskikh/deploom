import { contextBridge, ipcRenderer } from 'electron'

const api = {
  bootstrap: () => ipcRenderer.invoke('flow:bootstrap'),
  pickDirectory: () => ipcRenderer.invoke('flow:pick-directory'),
  pickFile: (filters?: Array<{ name: string; extensions: string[] }>) => ipcRenderer.invoke('flow:pick-file', filters),
  listAgentModels: (agentProvider: string, cwd?: string) => ipcRenderer.invoke('flow:list-agent-models', agentProvider, cwd),
  registerWorkspace: (input: unknown) => ipcRenderer.invoke('flow:register-workspace', input),
  cloneWorkspace: (input: unknown) => ipcRenderer.invoke('flow:clone-workspace', input),
  addProject: (input: unknown) => ipcRenderer.invoke('flow:add-project', input),
  selectWorkspace: (workspaceId: string) => ipcRenderer.invoke('flow:select-workspace', workspaceId),
  updateWorkspace: (input: unknown) => ipcRenderer.invoke('flow:update-workspace', input),
  updateProjectBranches: (input: unknown) => ipcRenderer.invoke('flow:update-project-branches', input),
  refreshWorkspace: () => ipcRenderer.invoke('flow:refresh-workspace'),
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
}

contextBridge.exposeInMainWorld('dependencyFlow', api)