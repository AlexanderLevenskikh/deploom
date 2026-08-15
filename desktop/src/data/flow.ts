import type { FlowAction } from '../types'

export type FlowStage = {
  id: number
  action?: FlowAction
  title: string
  description: string
  button: string
  confirmation?: string
}

export const FLOW_STAGES: FlowStage[] = [
  { id: 1, action: 'preflight', title: 'Подготовка', description: 'Git, конфиги и окружение', button: 'Проверить проект' },
  { id: 2, action: 'baseline', title: 'Снять baseline', description: 'Снимок исходного состояния текущего цикла', button: 'Снять baseline', confirmation: 'Baseline начнёт новый цикл планирования от текущего состояния. Во время уже запущенной миграции переснимайте его только осознанно: цели и сравнение будут рассчитаны заново.' },
  { id: 3, title: 'План обновления', description: 'Цель, группы и scope в dashboard', button: 'Открыть dashboard' },
  { id: 4, action: 'agent', title: 'Миграция агентом', description: 'Codex или OpenCode по выгруженному scope', button: 'Запустить агента' },
  { id: 5, action: 'generate', title: 'Верификация', description: 'Свежий roadmap и достижение целевого уровня', button: 'Перестроить отчёт' },
  { id: 6, action: 'audit', title: 'Независимый аудит', description: 'Уязвимости и lag-policy', button: 'Запустить аудит' },
  { id: 7, action: 'release', title: 'Release-ветка', description: 'Чистый squash из проверенного merged', button: 'Создать release', confirmation: 'Release создаётся от pinned source commit и не должен содержать audit workspace.' },
  { id: 8, action: 'commit-state', title: 'Сохранить state', description: 'Конфиги, история и артефакты команды', button: 'Сделать state-коммит', confirmation: 'В коммит попадут только state-каталоги рабочего набора и gitlink tool.' },
  { id: 9, action: 'push-workspace', title: 'Опубликовать', description: 'Release-ветка проекта и командный state', button: 'Опубликовать результат', confirmation: 'Будут отправлены сохранённая release-ветка проекта и текущая ветка командного workspace. Проверьте оба remote.' },
]

export const ACTION_ORDER = FLOW_STAGES.flatMap((stage) => stage.action ? [stage.action] : [])
