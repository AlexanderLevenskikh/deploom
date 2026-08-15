import { BrowserWindow } from 'electron'

const PROMPT_HARNESS_TIMEOUT_MS = 45_000

async function withPromptHarnessTimeout<T>(operation: Promise<T>, phase: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      operation,
      new Promise<T>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error('PROMPT_HARNESS_TIMEOUT: ' + phase + ' exceeded ' + PROMPT_HARNESS_TIMEOUT_MS + 'ms')), PROMPT_HARNESS_TIMEOUT_MS)
      }),
    ])
  } finally {
    if (timer) clearTimeout(timer)
  }
}


export async function buildProjectPrompt(dashboardUrl: string, project: string, targetMode: 'default' | 'yellow' | 'green'): Promise<string> {
  const win = new BrowserWindow({
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  })
  try {
    await withPromptHarnessTimeout(win.loadURL(dashboardUrl), 'dashboard load')
    const result = await withPromptHarnessTimeout(win.webContents.executeJavaScript(`(() => {
      if (typeof buildCompactPromptFromCurrentView !== 'function') return { error: 'STALE_DASHBOARD' };
      try {
        const projectFilter = document.getElementById('projectFilter');
        const target = document.getElementById('targetMode');
        const scope = document.getElementById('promptScope');
        if (!projectFilter || !target) return { error: 'dashboard filters are missing' };
        projectFilter.value = ${JSON.stringify(project)};
        target.value = ${JSON.stringify(targetMode)};
        if (scope) scope.value = 'project-target';
        return { prompt: buildCompactPromptFromCurrentView() };
      } catch (error) {
        return { error: String((error && error.message) || error) };
      }
    })()`), 'dashboard prompt export') as { prompt?: unknown; error?: string }
    if (result?.error) throw new Error(`Не удалось автоматически экспортировать project prompt: ${result.error}`)
    if (typeof result?.prompt !== 'string' || !result.prompt.trim()) throw new Error('Автоматический экспорт вернул пустой project prompt')
    return result.prompt
  } finally {
    win.destroy()
  }
}

// buildGroupScopedCompactPrompt (in the bundled generator's dashboard HTML)
// reads live filter/table DOM state and calls the same manifest/branch-plan/
// dossier builders and hash function the canonical validator trusts -- it is
// not a pure function portable to a plain Node context. A hidden window
// loading the already-generated dashboard is the same mechanism the app
// already uses for the visible "Dashboard" tab, just off-screen; the
// generator embeds all of its data into the page at generation time (no
// runtime fetch/XHR), so this is deterministic and needs no network access.
export async function buildGroupScopedPrompt(dashboardUrl: string, project: string, branch: string, targetMode: 'default' | 'yellow' | 'green', packages?: string[]): Promise<string> {
  const win = new BrowserWindow({
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  })
  try {
    await withPromptHarnessTimeout(win.loadURL(dashboardUrl), 'dashboard load')
    // A thrown Error crossing the executeJavaScript boundary can surface on
    // the Node side as Electron's own generic "Script failed to execute,
    // this normally means an error was thrown" -- observed for real, with no
    // trace of the actual message. Catching inside the page and returning a
    // plain, structured-cloneable {error} object instead sidesteps that
    // entirely: whatever went wrong comes back as real text.
    const result = await withPromptHarnessTimeout(win.webContents.executeJavaScript(`(() => {
      if (typeof buildGroupScopedCompactPrompt !== 'function') {
        return { error: 'STALE_DASHBOARD' };
      }
      try {
        const projectFilter = document.getElementById('projectFilter');
        const target = document.getElementById('targetMode');
        if (!target) return { error: 'dashboard target filter is missing' };
        if (projectFilter) projectFilter.value = ${JSON.stringify(project)};
        target.value = ${JSON.stringify(targetMode)};
        return { prompt: buildGroupScopedCompactPrompt(${JSON.stringify(project)}, ${JSON.stringify(branch)}, ${JSON.stringify(packages ?? null)}) };
      } catch (error) {
        return { error: String((error && error.message) || error) };
      }
    })()`), 'dashboard prompt export') as { prompt?: unknown; error?: string }
    if (result?.error === 'STALE_DASHBOARD') {
      // Observed for real: the desktop app updates its bundled tool, but the
      // dashboard HTML on disk is a static artifact only the generator
      // rewrites -- it keeps whatever functions existed when it was last
      // built until the user reruns "Верификация", even after the app that
      // reads it is long past that version.
      throw new Error(`Dashboard-отчёт устарел и не содержит функцию генерации промпта по группе. Перестройте отчёт («Верификация») и повторите запуск агента.`)
    }
    if (result?.error) throw new Error(`Не удалось построить промпт для ветки ${branch}: ${result.error}`)
    const prompt = result?.prompt
    if (typeof prompt !== 'string' || !prompt.trim()) throw new Error(`Пустой prompt для ветки ${branch}`)
    return prompt
  } finally {
    win.destroy()
  }
}
