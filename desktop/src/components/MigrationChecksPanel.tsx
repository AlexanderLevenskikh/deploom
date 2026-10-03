import { useLanguage } from '../i18n'
import type { MigrationValidationProfile } from '../../electron/migration-validation-profile'

type Props = {
  profile?: MigrationValidationProfile & { suggestedUnitCommand?: string }
  locked: boolean
  scope?: { mode?: string; existingFailures?: number; total?: number; skipped?: number }
  onChange: (profile: MigrationValidationProfile) => void
}

export function MigrationChecksPanel({ profile, locked, scope, onChange }: Props) {
  const { text } = useLanguage()
  if (!profile) return null
  return <details className="resume-notice migration-checks" open={!locked} data-testid="migration-checks">
    <summary><strong>{text('Проверки миграции', 'Migration checks')}</strong> · {profile.commands.filter(c => c.trim()).length} {text('команд', 'commands')}{profile.deferredChecks ? ` · ${text('есть неподтверждённые проверки', 'some checks are not verified')}` : ''}{scope?.mode === 'test-nonregression' ? ` · ${text('сравнение с исходным; сбоев:', 'compare with initial; failures:')} ${scope.existingFailures}/${scope.total}; skipped: ${scope.skipped ?? 0} (${text('не полностью зелёный проект', 'not a fully green project')})` : ''}</summary>
    <p>{text('Эти команды обязательны при начальной проверке и после обновлений. Успех относится только к выбранному набору проверок. Изменить его можно до запуска; для текущего прогона набор зафиксирован.', 'These commands are required for the initial control and after updates. Success applies only to the selected checks. Configure them before starting; an existing run keeps its pinned selection.')}</p>
    <label>{text('Обязательные команды — по одной в строке', 'Required commands — one per line')}
      <textarea aria-label={text('Обязательные команды', 'Required commands')} rows={5} disabled={locked} value={profile.commands.join('\n')} onChange={e => onChange({ ...profile, commands: e.target.value.split('\n') })} />
    </label>
    {profile.suggestedUnitCommand && !locked ? <button type="button" className="button secondary" onClick={() => {
      const suggestion = profile.suggestedUnitCommand!
      const script = suggestion.split(' --')[0]
      onChange({ ...profile, unitCommand: suggestion, commands: [...profile.commands.filter(c => c !== script && c !== suggestion), suggestion] })
    }}>{text('Выбрать Vitest по src (без E2E вне src)', 'Select Vitest in src (no E2E outside src)')}</button> : null}
    <label><input type="checkbox" disabled={locked} checked={profile.compareExistingFailures === true} onChange={e => onChange({ ...profile, compareExistingFailures: e.target.checked })} />{text('Сравнивать исходные сбои Vitest: новые сбои и потеря покрытия запрещены', 'Compare existing Vitest failures: no new failures or lost coverage')}</label>
    {profile.compareExistingFailures ? <label>{text('Команда Vitest из обязательного списка (для npm добавьте -- перед аргументами)', 'Vitest command from the required list (npm needs -- before arguments)')}
      <input aria-label={text('Команда сравнения Vitest', 'Vitest comparison command')} disabled={locked} value={profile.unitCommand || ''} onChange={e => onChange({ ...profile, unitCommand: e.target.value })} />
      <p>{text('JSON-отчёт добавит DepLoom. Ошибки сборки тестов, инфраструктуры и неизвестные результаты остаются блокерами. Проект с прежними сбоями не считается полностью зелёным.', 'DepLoom adds the JSON report. Collection, infrastructure and unknown results remain blockers. A project with existing failures is not considered fully green.')}</p>
    </label> : null}
    <label>{text('Отложенные проверки и причина (например, E2E требуют стенд)', 'Deferred checks and reason (for example, E2E require a server)')}
      <textarea aria-label={text('Отложенные проверки', 'Deferred checks')} rows={3} disabled={locked} value={profile.deferredChecks || ''} onChange={e => onChange({ ...profile, deferredChecks: e.target.value })} />
    </label>
    {profile.deferredChecks ? <p>{text('Не подтверждено:', 'Not verified:')} {profile.deferredChecks}</p> : null}
    {scope?.mode === 'test-nonregression' ? <p role="status">{text('Проверка отсутствия новых сбоев. Исходно падающих тестов:', 'Nonregression check. Initially failing tests:')} {scope.existingFailures} / {scope.total}. {text('Это не полностью зелёный проект.', 'This is not a fully green project.')}</p> : null}
  </details>
}
