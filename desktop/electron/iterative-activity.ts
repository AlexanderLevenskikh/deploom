/** Display-only projection. Counters are evidence, never a fabricated ETA. */
export type ActivityAttempt = {
  attemptId: string
  status: string
  stage: string
  phase?: string
  packageProgress?: { processed: number; total: number }
}
type AttemptReadIdentity = { attemptId: string; projectName: string; workspaceId?: string; lastHeartbeatAt: number; status: string }

/** Delayed IPC reads may not replace a newer attempt or a terminal event. */
export function canApplyAttemptRead(current: AttemptReadIdentity | undefined, incoming: AttemptReadIdentity | undefined, projectName: string, workspaceId?: string): boolean {
  if (!incoming || incoming.projectName !== projectName || (workspaceId !== undefined && incoming.workspaceId !== undefined && incoming.workspaceId !== workspaceId)) return false
  if (!current) return true
  if (current.attemptId !== incoming.attemptId || current.lastHeartbeatAt > incoming.lastHeartbeatAt) return false
  const terminal = ['done', 'failed', 'canceled'].includes(current.status)
  return !(terminal && ['running', 'starting'].includes(incoming.status) && current.lastHeartbeatAt === incoming.lastHeartbeatAt)
}

export type IterativeActivity = { title: string; detail: string; percent?: number }

export function iterativeActivity(attempt: ActivityAttempt | undefined, live: boolean, language: 'ru' | 'en'): IterativeActivity {
  const text = (ru: string, en: string) => language === 'ru' ? ru : en
  if (!attempt) return { title: text('Подготовка запуска', 'Preparing the run'), detail: '' }
  if (!live) return {
    title: attempt.status === 'failed' ? text('Попытка завершилась ошибкой', 'The attempt failed')
      : attempt.status === 'canceled' ? text('Работа остановлена', 'Work stopped')
      : attempt.status === 'done' ? text('Этап завершён', 'Stage completed')
      : text('Работа прервана — можно продолжить', 'Work interrupted — ready to resume'),
    detail: '',
  }
  const phase = attempt.phase ?? ''
  let title = attempt.stage === 'agent' ? text('Агент исправляет проект', 'The agent is repairing the project')
    : attempt.stage === 'drive' ? text('Обновляем пакеты и проверяем результат', 'Updating packages and verifying the result')
    : text('Проверяем текущие зависимости', 'Checking current dependencies')
  if (/source-materialization|source capture|source snapshot|sealed source snapshot/i.test(phase)) title = text('Готовим изолированную копию проекта', 'Preparing an isolated project copy')
  else if (/resolver-install|resolver install/i.test(phase)) title = text('Устанавливаем зависимости в проверочной копии', 'Installing dependencies in the verification copy')
  else if (/discovery/i.test(phase)) title = text('Подбираем доступные версии пакетов', 'Finding available package versions')
  else if (/audit/i.test(phase)) title = text('Проверяем уязвимости зависимостей', 'Auditing dependency vulnerabilities')
  else if (/lifecycle|project.check|command/i.test(phase) && !/begin.check-progress/.test(phase)) title = text('Выполняем проверки проекта', 'Running project checks')
  else if (/cache configure/i.test(phase)) title = text('Готовим кэш проверок', 'Preparing the verification cache')
  const facts: string[] = []
  const files = phase.match(/files=(\d+)/)?.[1]
  const bytes = phase.match(/bytes=(\d+)/)?.[1]
  if (files) facts.push(`${text('файлов', 'files')}: ${Number(files).toLocaleString(language)}`)
  if (bytes) facts.push(`${(Number(bytes) / 1024 ** 3).toLocaleString(language, { maximumFractionDigits: 2 })} ${text('ГиБ', 'GiB')}`)
  const counter = attempt.packageProgress
  const validCounter = /discovery/i.test(phase) && counter && Number.isFinite(counter.processed) && Number.isFinite(counter.total) && counter.total > 0 && counter.processed >= 0 && counter.processed <= counter.total
  if (validCounter) facts.push(`${text('пакетов', 'packages')}: ${counter.processed}/${counter.total}`)
  return { title, detail: facts.join(' · '), percent: validCounter ? Math.round(counter.processed / counter.total * 100) : undefined }
}
