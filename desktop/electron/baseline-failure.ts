type BaselineFailureEnvelope = {
  code: string
  category: string
  summary: string
  diagnosticArtifact: string
}

/** The generator emits one structured terminal envelope among ordinary log lines. */
export function extractBaselineFailureEnvelope(output: string): BaselineFailureEnvelope | undefined {
  let latest: BaselineFailureEnvelope | undefined
  for (const line of output.split(/\r?\n/)) {
    const start = line.indexOf('{')
    if (start < 0) continue
    try {
      const value: unknown = JSON.parse(line.slice(start))
      if (!value || typeof value !== 'object') continue
      const record = value as Record<string, unknown>
      if (record.schemaVersion !== 'DEPLOOM_FAILURE_V2') continue
      const code = typeof record.code === 'string' ? record.code : ''
      if (!/^[A-Z][A-Z0-9_]{3,}$/.test(code)) continue
      latest = {
        code,
        category: typeof record.category === 'string' ? record.category : '',
        summary: typeof record.summary === 'string' ? record.summary.slice(0, 600) : '',
        diagnosticArtifact: typeof record.diagnosticArtifact === 'string' ? record.diagnosticArtifact : '',
      }
    } catch { /* Progress and project command output are not failure envelopes. */ }
  }
  return latest
}

/** First line is short and actionable; the technical cause stays available below it. */
export function baselineFailureMessage(result: { code: number; stderr: string; stdout: string }): string | undefined {
  if (result.code === 0) return undefined
  const failure = extractBaselineFailureEnvelope(`${result.stderr}\n${result.stdout}`)
  if (!failure) return undefined

  let explanation: string
  if (failure.code === 'BASELINE_RECOVERY_CONTINUE_UNAVAILABLE') {
    const changed = failure.summary.includes('reason=identity-mismatch')
    explanation = changed
      ? 'Сохранённый checkpoint относится к другому состоянию проекта или настройкам. Продолжить его нельзя. Нажмите «Начать заново» в Baseline, чтобы начать новый поиск.'
      : 'Сохранённый checkpoint недоступен для продолжения. Нажмите «Начать заново» в Baseline, чтобы начать новый поиск.'
  } else if (failure.category === 'BUDGET_EXHAUSTED' || failure.code === 'BASELINE_BUDGET_EXHAUSTED') {
    explanation = 'Время поиска истекло до получения нового проверенного результата. Это не означает, что решения нет. Посмотрите последний блокирующий check и задайте больший бюджет для продолжения.'
  } else if (failure.category === 'SEARCH_LIMIT' || failure.code === 'BASELINE_VERIFICATION_PLATEAU') {
    explanation = 'Поиск достиг лимита попыток без улучшения. Повторять с теми же настройками бесполезно: измените состав, попробуйте более глубокий поиск или проверьте блокирующие команды проекта.'
  } else if (failure.category === 'PROJECT_INCOMPATIBLE') {
    explanation = 'Выбранные версии не прошли проверки проекта. Откройте логи Baseline и найдите упавшую команду; исправьте проект или измените состав обновлений.'
  } else if (failure.category === 'ENVIRONMENT' || failure.category === 'SOURCE_INTEGRITY') {
    explanation = 'Исходный проект не удалось безопасно подготовить или подтвердить. Убедитесь, что фоновые изменения завершены, и проверьте состояние Git и выбранную ветку.'
  } else if (failure.category === 'AUTH' || failure.category === 'NETWORK' || failure.category === 'REGISTRY') {
    explanation = 'Не удалось получить нужные данные из registry. Проверьте доступ, учётные данные и нужные версии пакетов; после исправления повторите запуск.'
  } else if (failure.category === 'TOOL_INTERNAL_ERROR') {
    explanation = 'В DepLoom возникла внутренняя ошибка. Сохраните диагностический файл и передайте его разработчикам; повторный запуск того же кода может не помочь.'
  } else {
    explanation = 'Baseline остановился без нового проверенного результата. Откройте техническую причину и диагностический файл ниже, чтобы выбрать следующий шаг.'
  }

  return `${failure.code}: ${explanation}${failure.summary ? `\n\nТехническая причина: ${failure.summary}` : ''}${failure.diagnosticArtifact ? `\nДиагностика: ${failure.diagnosticArtifact}` : ''}`
}
