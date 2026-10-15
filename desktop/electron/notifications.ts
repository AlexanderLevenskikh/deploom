import type { FlowAction } from './flow-state.js'
import type { IterativeAttemptRecord } from './iterative-attempt.js'

export type FlowNotificationEvent =
  | { kind: 'stage-complete'; projectName: string; action: FlowAction }
  | { kind: 'iterative-stage-complete'; projectName: string; stage: string }
  | { kind: 'stage-failed'; projectName: string; stage: string }
  | { kind: 'stage-attention'; projectName: string; stage: string }
  | { kind: 'iterative-run-complete'; projectName: string }
  | { kind: 'group-complete'; projectName: string; branch: string; index: number; total: number; packages: string[] }
  | { kind: 'autopilot-complete'; projectName: string; published: boolean }

const ACTION_LABELS: Record<FlowAction, string> = {
  preflight: 'Проверка окружения',
  'sync-tool': 'Синхронизация инструмента',
  baseline: 'Baseline',
  generate: 'Построение roadmap',
  'generate-all': 'Построение roadmap для всех проектов',
  audit: 'Независимый аудит',
  agent: 'Миграция',
  recover: 'Автовосстановление',
  release: 'Release-ветка',
  'commit-state': 'Коммит состояния',
  'push-workspace': 'Публикация workspace',
}

export function flowNotificationContent(event: FlowNotificationEvent): { title: string; body: string } {
  if (event.kind === 'stage-failed' || event.kind === 'stage-attention') {
    return { title: event.kind === 'stage-failed' ? 'DepLoom · Ошибка этапа' : 'DepLoom · Требуется внимание',
      body: event.projectName + ': ' + event.stage + '. Откройте журнал DepLoom.' }
  }
  if (event.kind === 'iterative-stage-complete') {
    return { title: 'DepLoom · Этап завершён', body: event.projectName + ': ' + event.stage }
  }
  if (event.kind === 'iterative-run-complete') {
    return { title: 'DepLoom · Прогон завершён', body: event.projectName + ': результат и выполненные проверки доступны в DepLoom' }
  }
  if (event.kind === 'stage-complete') {
    return { title: 'DepLoom · Этап завершён', body: event.projectName + ': ' + ACTION_LABELS[event.action] }
  }
  if (event.kind === 'group-complete') {
    const packageText = event.packages.length
      ? ' · ' + event.packages.slice(0, 3).join(', ') + (event.packages.length > 3 ? ' +' + (event.packages.length - 3) : '')
      : ''
    return {
      title: 'DepLoom · Группа мигрирована',
      body: event.projectName + ': группа ' + event.index + ' из ' + event.total + ' (' + event.branch + ')' + packageText,
    }
  }
  return {
    title: 'DepLoom · Автопилот завершён',
    body: event.projectName + (event.published ? ': FLOW завершён, изменения опубликованы' : ': доступный FLOW завершён'),
  }
}


const ITERATIVE_STAGE_LABELS: Record<IterativeAttemptRecord['stage'], string> = {
  preflight: 'Проверка окружения', begin: 'Исходная проверка', drive: 'Расчёт обновлений',
  agent: 'Работа агента', none: 'Проверка проекта',
}

/** Observe journal transitions, never poll results or interpret process output as success. */
export function createIterativeNotificationObserver() {
  const seen = new Map<string, { attemptId: string; terminal?: string; stage: number }>()
  return (attempt: IterativeAttemptRecord, options: { autopilot?: boolean; stopped?: boolean } = {}): FlowNotificationEvent[] => {
    const scope = JSON.stringify([attempt.workspaceId, attempt.projectName])
    // Cohort steps may repeat dozens of times. Notifications follow only the
    // same four major stages shown by the UI; never infer project success.
    const pipeline = !options.autopilot && attempt.status === 'done' && attempt.lastStep === 'delivery' && attempt.phase === 'delivery' ? 4
      : attempt.stage === 'drive' || attempt.stage === 'agent' && attempt.runCreated !== false ? 3
      : attempt.lastStep === 'checked' || /^begin\.discovery/.test(attempt.phase ?? '') || attempt.discoveryCompleted ? 2 : 1
    let previous = seen.get(scope)
    if (!previous || previous.attemptId !== attempt.attemptId) {
      previous = { attemptId: attempt.attemptId, stage: pipeline }
      seen.delete(scope)
      seen.set(scope, previous)
      if (seen.size > 128) seen.delete(seen.keys().next().value!)
    }
    const events: FlowNotificationEvent[] = []
    if (attempt.cancelRequested || attempt.status === 'canceled') return events
    if (attempt.status === 'starting' || attempt.status === 'running') previous.terminal = undefined
    if (attempt.status !== 'failed' && pipeline > previous.stage) {
      const labels: Record<number, string> = { 2: 'Проверка → Подбор версий', 3: 'Подбор версий → Обновление', 4: 'Обновление → Результат' }
      events.push({ kind: 'iterative-stage-complete', projectName: attempt.projectName, stage: labels[pipeline] })
      previous.stage = pipeline
    }
    if (attempt.status !== 'failed') return events
    // An Autopilot handler can fail while the supervisor is already selecting
    // another cohort or retrying infrastructure. Only its stop callback owns
    // the final failure notification.
    if (options.autopilot && !options.stopped) return events
    const terminal = JSON.stringify([attempt.status, attempt.stage, attempt.lastStep])
    if (previous.terminal === terminal) return events
    previous.terminal = terminal
    events.push({ kind: 'stage-failed', projectName: attempt.projectName,
      stage: attempt.lastStep === 'checked' || attempt.lastStep === 'check' ? 'Проверка проекта' : ITERATIVE_STAGE_LABELS[attempt.stage] })
    return events
  }
}

type NativeNotification = {
  once(event: string, listener: (...args: unknown[]) => void): unknown
  show(): void
}

/** Notification delivery must never change a completed job's outcome. */
export function createFlowNotificationSender(io: {
  enabled(): boolean; supported(): boolean
  create(content: { title: string; body: string }): NativeNotification
  onFailure(): void; onClick(): void
}) {
  const active = new Set<NativeNotification>()
  return (event: FlowNotificationEvent): void => {
    try {
      if (!io.enabled() || !io.supported()) return
      const notification = io.create(flowNotificationContent(event))
      active.add(notification)
      const release = () => active.delete(notification)
      notification.once('failed', () => { release(); io.onFailure() })
      notification.once('close', release)
      notification.once('click', () => { release(); io.onClick() })
      if (active.size > 64) active.delete(active.values().next().value!)
      try { notification.show() } catch { release(); io.onFailure() }
    } catch { io.onFailure() }
  }
}
