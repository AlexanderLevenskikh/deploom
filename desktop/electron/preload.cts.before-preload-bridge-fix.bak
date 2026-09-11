import { contextBridge, ipcRenderer } from 'electron'
import { readFile, stat } from 'node:fs/promises'

const MAX_PROMPT_PREVIEW_BYTES = 2 * 1024 * 1024

async function getCurrentProjectPromptPreview() {
  // The renderer never supplies a filesystem path here. Resolve the current
  // project prompt through the existing trusted main-process workspace state,
  // then read exactly that file from the privileged preload context.
  const refreshed = await ipcRenderer.invoke('flow:refresh-workspace') as {
    details?: {
      workspace?: { selectedProject?: string }
      projectPromptPath?: string
      promptStale?: boolean
    }
  }
  const details = refreshed?.details
  const targetPath = details?.projectPromptPath
  if (!targetPath) return undefined

  const info = await stat(targetPath)
  if (!info.isFile()) throw new Error('Current project prompt is not a file')
  if (info.size > MAX_PROMPT_PREVIEW_BYTES) throw new Error('Current project prompt is too large to preview')

  return {
    path: targetPath,
    content: await readFile(targetPath, 'utf8'),
    stale: Boolean(details?.promptStale),
    mtimeMs: info.mtimeMs,
    size: info.size,
    projectName: details?.workspace?.selectedProject,
  }
}

const api = {
  bootstrap: () => ipcRenderer.invoke('flow:bootstrap'),
  pickDirectory: () => ipcRenderer.invoke('flow:pick-directory'),
  pickFile: (filters?: Array<{ name: string; extensions: string[] }>) => ipcRenderer.invoke('flow:pick-file', filters),
  listAgentModels: (agentProvider: string, cwd?: string) => ipcRenderer.invoke('flow:list-agent-models', agentProvider, cwd),
  registerWorkspace: (input: unknown) => ipcRenderer.invoke('flow:register-workspace', input),
  cloneWorkspace: (input: unknown) => ipcRenderer.invoke('flow:clone-workspace', input),
  addProject: (input: unknown) => ipcRenderer.invoke('flow:add-project', input),
  removeProject: (input: unknown) => ipcRenderer.invoke('flow:remove-project', input),
  selectWorkspace: (workspaceId: string) => ipcRenderer.invoke('flow:select-workspace', workspaceId),
  updateWorkspace: (input: unknown) => ipcRenderer.invoke('flow:update-workspace', input),
  updateProjectBranches: (input: unknown) => ipcRenderer.invoke('flow:update-project-branches', input),
  refreshWorkspace: () => ipcRenderer.invoke('flow:refresh-workspace'),
  getBaselineIntentPlan: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:baseline-intent-plan', input),
  getCurrentProjectPromptPreview,
  getDependencyGraphSnapshot: (input: { workspaceId?: string; projectName: string }) => ipcRenderer.invoke('flow:dependency-graph-snapshot', input),
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