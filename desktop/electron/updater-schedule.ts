export const INITIAL_UPDATE_CHECK_DELAY_MS = 10_000
export const UPDATE_CHECK_INTERVAL_MS = 30 * 60 * 1_000

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
