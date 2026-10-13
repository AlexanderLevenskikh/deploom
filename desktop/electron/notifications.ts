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


const ITERATIVE_STEP_LABELS: Record<string, string> = {
  'plan-next': 'Подбор группы зависимостей', materialize: 'Подготовка проверочного окружения',
  verify: 'Проверка варианта', 'apply-feedback': 'Обработка результата проверки',
  finish: 'Завершение расчёта', delivery: 'Подготовка результата в Git',
}
const ITERATIVE_STAGE_LABELS: Record<IterativeAttemptRecord['stage'], string> = {
  preflight: 'Проверка окружения', begin: 'Исходная проверка', drive: 'Расчёт обновлений',
  agent: 'Работа агента', none: 'Проверка проекта',
}

/** Observe journal transitions, never poll results or interpret process output as success. */
export function createIterativeNotificationObserver() {
  const seen = new Map<string, { attemptId: string; terminal?: string; steps: string[] }>()
  return (attempt: IterativeAttemptRecord): FlowNotificationEvent[] => {
    const scope = JSON.stringify([attempt.workspaceId, attempt.projectName])
    const steps = Array.isArray(attempt.stepsDone) ? attempt.stepsDone.filter(step => typeof step === 'string') : []
    let previous = seen.get(scope)
    if (!previous || previous.attemptId !== attempt.attemptId) {
      previous = { attemptId: attempt.attemptId, steps: [...steps] }
      seen.delete(scope)
      seen.set(scope, previous)
      if (seen.size > 128) seen.delete(seen.keys().next().value!)
    }
    const events: FlowNotificationEvent[] = []
    const stage = attempt.lastStep === 'checked' || attempt.lastStep === 'check'
      ? 'Проверка проекта' : ITERATIVE_STAGE_LABELS[attempt.stage]
    if (attempt.cancelRequested || attempt.status === 'canceled') {
      previous.steps = [...steps]
      return events
    }
    if (attempt.status === 'starting' || attempt.status === 'running') previous.terminal = undefined
    const prefix = previous.steps.every((step, index) => steps[index] === step)
    for (const step of steps.slice(prefix ? previous.steps.length : 0)) {
      if (ITERATIVE_STEP_LABELS[step]) events.push({ kind: 'iterative-stage-complete',
        projectName: attempt.projectName, stage: ITERATIVE_STEP_LABELS[step] })
    }
    previous.steps = [...steps]
    if (attempt.status !== 'failed' && attempt.status !== 'done') return events
    const terminal = JSON.stringify([attempt.status, attempt.stage, attempt.lastStep])
    if (previous.terminal === terminal) return events
    previous.terminal = terminal
    if (attempt.status === 'failed') {
      events.push({ kind: attempt.lastError ? 'stage-failed' : 'stage-attention', projectName: attempt.projectName, stage })
    } else if (attempt.lastStep === 'cohort-review') {
      events.push({ kind: 'stage-attention', projectName: attempt.projectName, stage: 'Согласование группы зависимостей' })
    } else if (attempt.stage !== 'drive') {
      events.push({ kind: 'iterative-stage-complete', projectName: attempt.projectName,
        stage: ITERATIVE_STEP_LABELS[attempt.lastStep || ''] || stage })
    }
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
