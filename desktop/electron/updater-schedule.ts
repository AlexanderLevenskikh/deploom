export const INITIAL_UPDATE_CHECK_DELAY_MS = 10_000
export const UPDATE_CHECK_INTERVAL_MS = 30 * 60 * 1_000

// Retry only transport/service failures. Authentication, invalid feeds and
// package integrity failures require an explicit error, not a retry loop.
export async function checkUpdateFeed<T>(check: () => Promise<T>, wait: (delay: number) => Promise<void> = delay => new Promise(resolve => setTimeout(resolve, delay))): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    try { return await check() }
    catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      const code = error && typeof error === 'object' && 'code' in error ? String(error.code) : ''
      // GitHubProvider can wrap HttpError and retain its status only in the
      // nested stack. Prefer that status to incidental response-body words.
      const nestedStatus = /\bHttpError:\s*(\d{3})\b/i.exec(message)
      const status = error && typeof error === 'object' && 'statusCode' in error
        ? Number(error.statusCode) : nestedStatus ? Number(nestedStatus[1]) : undefined
      const transient = status !== undefined ? [429, 500, 502, 503, 504].includes(status) : /\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|ENETUNREACH|ERR_NAME_NOT_RESOLVED|ERR_TIMED_OUT|ERR_NETWORK_CHANGED|ERR_INTERNET_DISCONNECTED|ERR_CONNECTION_RESET|ERR_CONNECTION_TIMED_OUT)\b/.test(`${code} ${message}`)
        || /^HTTP_ERROR_(?:429|500|502|503|504)$/.test(code)
        || /(?:HTTP(?: error)?[: ]+|status(?: code)?[: =]+)(?:429|500|502|503|504)\b/i.test(message)
      if (!transient || attempt >= 2) throw error
      await wait(attempt === 0 ? 2000 : 8000)
    }
  }
}

type TimerHandle = ReturnType<typeof setTimeout>

type UpdateScheduler = {
  setTimeout: (callback: () => void, delay: number) => TimerHandle
  setInterval: (callback: () => void, delay: number) => TimerHandle
  clearTimeout: (handle: TimerHandle) => void
  clearInterval: (handle: TimerHandle) => void
}

export function scheduleUpdateChecks(check: () => void, scheduler: UpdateScheduler = globalThis): () => void {
  const initial = scheduler.setTimeout(check, INITIAL_UPDATE_CHECK_DELAY_MS)
  const interval = scheduler.setInterval(check, UPDATE_CHECK_INTERVAL_MS)
  return () => {
    scheduler.clearTimeout(initial)
    scheduler.clearInterval(interval)
  }
}
