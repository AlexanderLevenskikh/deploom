import type { FlowAction } from './flow-state.js'

export type FlowNotificationEvent =
  | { kind: 'stage-complete'; projectName: string; action: FlowAction }
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
  if (event.kind === 'stage-complete') {
    return { title: 'Dependency Flow · Этап завершён', body: event.projectName + ': ' + ACTION_LABELS[event.action] }
  }
  if (event.kind === 'group-complete') {
    const packageText = event.packages.length
      ? ' · ' + event.packages.slice(0, 3).join(', ') + (event.packages.length > 3 ? ' +' + (event.packages.length - 3) : '')
      : ''
    return {
      title: 'Dependency Flow · Группа мигрирована',
      body: event.projectName + ': группа ' + event.index + ' из ' + event.total + ' (' + event.branch + ')' + packageText,
    }
  }
  return {
    title: 'Dependency Flow · Автопилот завершён',
    body: event.projectName + (event.published ? ': FLOW завершён, изменения опубликованы' : ': доступный FLOW завершён'),
  }
}