// Renderer clipboard helper: writes text through the Electron main-process
// clipboard (UI -> preload -> IPC), because navigator.clipboard can be
// unavailable or silently fail in the Electron web context on some platforms.
// Falls back to the web clipboard and reports failure honestly instead of
// pretending the copy happened.
export async function copyTextToClipboard(text: string): Promise<boolean> {
  const api = window.dependencyFlow
  if (api && typeof api.copyText === 'function') {
    try {
      const result = await api.copyText(text)
      if (result && result.ok) return true
    } catch {
      // fall through to the web clipboard
    }
  }
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}
