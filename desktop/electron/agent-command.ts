const OPENCODE_MIGRATION_MESSAGE = 'Выполни приложенное задание миграции полностью. Branch plan исчерпывающий и неизменяемый: запрещено создавать группы, ветки или package targets вне него. Для package-manager install и регенерации lockfile сразу задавай timeout не менее 10 минут, а для полного набора проверок — не менее 15 минут; не трать первую попытку на заведомо короткий лимит.'

// The desktop app never picks a model on the user's behalf beyond the agent's
// own default: an explicit model is only appended when the workspace sets one.
function modelArgs(model?: string): string[] {
  return model ? ['--model', model] : []
}

function attachArgs(serverUrl?: string): string[] {
  return serverUrl ? ['--attach', serverUrl] : []
}

// A one-off note the user typed in the desktop app after an unexpected stop
// -- e.g. "conflict resolved by hand, don't touch git again" or "skip this,
// target is known REFACTOR_REQUIRED". Kept clearly attributed and separate
// from any git-derived feedback so the agent doesn't conflate an
// instruction from a human with its own state reading; still bound by the
// immutable Branch plan/manifest like everything else in these messages.
function userNoteSuffix(userNote?: string): string {
  return userNote ? ` Сообщение от пользователя: ${userNote}. Учти это как приоритетный контекст, но не как повод выйти за рамки неизменяемого Branch plan/manifest.` : ''
}


function orchestratorFeedbackSuffix(gateFeedback?: string): string {
  return gateFeedback ? ` Фактическое состояние по данным оркестратора: ${gateFeedback}. Не повторяй уже завершённую работу; доведи только оставшийся scope и повтори проверки.` : ''
}

// A resumed session only gets this short nudge, never the full prompt as an
// attachment again (see buildOpenCodeResumeArgs below). Resume is reserved for
// a real external interruption with an exact scope fingerprint; ordinary
// autonomous retries start a fresh session. Re-reading the saved batch prompt
// here is therefore a recovery guard, not a repeated auto-continue cost. The
// interrupted agent's conversation history should still have the original scope manifest and
// Branch plan in context. It isn't always: OpenCode can silently compact its
// own session mid-run ("conversation was compacted and media files were
// removed from context"), and once that happens the agent has no ground
// truth left and starts reconstructing the task from git log / live registry
// lookups -- which has been observed inventing extra, never-planned groups
// (group-6, group-7, ...) with arbitrary "latest" targets instead of the
// vetted ones. Pointing back at the saved prompt file gives it a way to
// re-ground itself instead of improvising.
function resumeMigrationMessage(promptPath?: string, gateFeedback?: string, userNote?: string): string {
  const base = 'Продолжи выполнение миграции с места остановки. Сохраняй исходный scope и не повторяй уже завершённую работу.'
  const prompt = promptPath ? ` Перед любыми следующими действиями заново прочитай файл ${promptPath}. Branch plan в нём исчерпывающий и неизменяемый: не создавай группы, ветки или package targets вне плана. Если Git уже содержит ветку вне текущей ревизии плана, не merge/delete/reset её и не изобретай новые targets: сохрани её как evidence и сообщи Supervisor'у для автономного adoption/quarantine; это не повод звать пользователя.` : ''
  // The feedback carries the orchestrator's own reading of git: where HEAD is,
  // what is already in the merged branch, and what genuinely remains. Handing
  // over only a failure list invited the agent to re-migrate groups that were
  // already merged, because from inside a work branch that work looks undone.
  const feedback = gateFeedback ? ` Фактическое состояние по данным Git: ${gateFeedback}. Уже влитую работу не повторяй и не откатывай; доведи только перечисленное как незавершённое, оставаясь внутри исходного Branch plan, затем повтори проверки.` : ''
  return `${base}${prompt}${feedback}${userNoteSuffix(userNote)}`
}

// codex runs with --sandbox workspace-write and claude with
// --permission-mode acceptEdits so neither blocks on an interactive approval
// that nothing is present to answer in an unattended `run`. opencode has no
// such default — without --auto it silently auto-*rejects* every permission
// request instead (including reads of files outside --dir, e.g. the
// AGENT_RUNBOOK/roadmap JSON the prompt itself requires), which is why a run
// can look "stuck" ignoring the branch plan: it was denied the reads/writes
// needed to follow it in the first place.
export function buildOpenCodeAgentArgs(projectPath: string, promptPath: string, model?: string, userNote?: string, serverUrl?: string, gateFeedback?: string): string[] {
  return [
    'run',
    `${OPENCODE_MIGRATION_MESSAGE}${orchestratorFeedbackSuffix(gateFeedback)}${userNoteSuffix(userNote)}`,
    '--format',
    'json',
    '--dir',
    projectPath,
    `--file=${promptPath}`,
    '--auto',
    ...attachArgs(serverUrl),
    ...modelArgs(model),
  ]
}
export function buildOpenCodeResumeArgs(projectPath: string, sessionId: string, model?: string, promptPath?: string, gateFeedback?: string, userNote?: string, serverUrl?: string): string[] {
  return ['run', resumeMigrationMessage(promptPath, gateFeedback, userNote), '--session', sessionId, '--format', 'json', '--dir', projectPath, '--auto', ...attachArgs(serverUrl), ...modelArgs(model)]
}

export function buildCodexAgentArgs(projectPath: string, model?: string): string[] {
  return ['exec', '--json', '--sandbox', 'workspace-write', '-C', projectPath, ...modelArgs(model), '-']
}
export function buildCodexResumeArgs(sessionId: string, model?: string, promptPath?: string, gateFeedback?: string, userNote?: string): string[] {
  return ['exec', 'resume', '--json', sessionId, resumeMigrationMessage(promptPath, gateFeedback, userNote), ...modelArgs(model)]
}

const CLAUDE_MIGRATION_ARGS = ['--print', '--output-format', 'stream-json', '--verbose', '--permission-mode', 'acceptEdits']

export function buildClaudeAgentArgs(model?: string): string[] {
  return [...CLAUDE_MIGRATION_ARGS, ...modelArgs(model)]
}
export function buildClaudeResumeArgs(sessionId: string, model?: string, promptPath?: string, gateFeedback?: string, userNote?: string): string[] {
  return [...CLAUDE_MIGRATION_ARGS, '--resume', sessionId, resumeMigrationMessage(promptPath, gateFeedback, userNote), ...modelArgs(model)]
}

// `opencode models` prints one `provider/model` id per line — the exact
// format its own `--model` flag expects (a bare model id without the
// provider prefix is rejected).
export function parseOpencodeModelsOutput(stdout: string): string[] {
  return stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
}
