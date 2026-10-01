// ONE migration scenario, ONE main action (P-## review, "восстановить понятный
// UX миграции").
//
// Cornerstone of the acceptance: the primary button on the panel must say the
// concrete next user action for the current state, the user-facing wording must
// not leak internal step names (C0, begin, roadmap targets, verified
// checkpoint), and liveness must come from Electron (inFlight), never from a
// locally remounted React component.
//
// This module is PURE (no fs, no Electron, no React): it maps durable/Electron
// signals to one MainAction, and a behavioral check script invokes the built
// module with fixtures instead of grepping the sources.
export type IterativeFailureInfo = {
  code: string
  summary: string
  command: string
  fixable: boolean
}

/** Parse the machine-readable Python failure envelope
 * (`ITERATIVE_MIGRATION_FAILURE_V1 {...}`, printed on ONE line) from a journal
 * error/reason that may wrap it in surrounding text. */
export function parseIterativeFailure(text: string | undefined): IterativeFailureInfo | undefined {
  if (!text) return undefined
  const marker = "ITERATIVE_MIGRATION_FAILURE_V1 "
  const at = text.indexOf(marker)
  const candidate = at >= 0 ? text.slice(at + marker.length) : text
  const line = candidate.split(/\r?\n/, 1)[0]
  const open = line.indexOf("{")
  const close = line.lastIndexOf("}")
  if (open < 0 || close <= open) return undefined
  try {
    const parsed = JSON.parse(line.slice(open, close + 1)) as Record<string, unknown>
    if (parsed && typeof parsed.code === "string") {
      return {
        code: parsed.code,
        summary: String(parsed.summary ?? ""),
        command: String(parsed.command ?? ""),
        fixable: parsed.fixable === true,
      }
    }
  } catch {
    // not a failure envelope — fall through
  }
  return undefined
}

export type ScenarioAttemptShape = {
  status?: string
  stage?: string
  lastError?: string
  reason?: string
  lastStep?: string
  targetSource?: string
  stepsDone?: string[]
  runCreated?: boolean
} | undefined

export type ScenarioRunnerShape = {
  present: boolean
  phase?: string
  decision?: { step: string | null; satisfied?: boolean; reason?: string } | undefined
} | undefined

export type ScenarioInput = {
  /** Electron-authoritative: a real begin/drive child is alive for this project. */
  inFlight: boolean
  attempt?: ScenarioAttemptShape
  runner?: ScenarioRunnerShape
  taskPresent: boolean
  /** journal lastStep 'no-targets' / roadmap-empty path */
  noTargets: boolean
}

export type ScenarioMainActionState =
  | "check" // проект ещё не проверен → Проверить проект
  | "retry-check" // проверка выявила исправимый локальный блокер → Повторить проверку
  | "no-targets" // сохранённых целей нет → предложить авто-поиск / настройку
  | "start-run" // проект готов, ничего ещё не гонялось → Начать обновление
  | "continue-run" // работа приостановлена → Продолжить обновление
  | "running" // реальный процесс выполняется → Остановить
  | "recovered-running" // журнал running, но живого процесса нет → Продолжить
  | "agent" // требуется ремонт агентом → Исправить агентом
  | "result" // результат проверен → Посмотреть результат

export type ScenarioMainAction = {
  state: ScenarioMainActionState
  /** fixable local blocker surfaced once (e.g. an uninitialized submodule). */
  blocker?: IterativeFailureInfo
  /** true when this is the first drive of a freshly captured run. */
  firstRun: boolean
  /** user-facing one-line reason + a diagnostics payload for copy. */
  reasonShort: string
}

const FIXABLE_CODES = new Set(["SOURCE_SUBMODULE_INCOMPLETE", "SOURCE_SUBMODULE_STATUS_FAILED"])

export function deriveMainAction(input: ScenarioInput): ScenarioMainAction {
  const attempt = input.attempt
  const runner = input.runner
  const blocker = parseIterativeFailure(attempt?.lastError) ?? parseIterativeFailure(attempt?.reason)
  const attemptLive = attempt?.status === "running" || attempt?.status === "starting"
  const attemptFailed = attempt?.status === "failed" || attempt?.status === "canceled"
  const decisionStep = runner?.decision?.step ?? null

  // 1. A REAL child is running → the only sensible action is Stop. This state
  //    overrides everything (including a fresh remount), because it comes from
  //    Electron, not from a local busy flag.
  if (input.inFlight) {
    return { state: "running", blocker, firstRun: false, reasonShort: activityReason(attempt) }
  }

  // 2. The durable journal says "running" but there is NO live child → the app
  //    (or the tab) restarted; the run can only be resumed by driving it.
  if (attemptLive) {
    return {
      state: "recovered-running",
      blocker,
      firstRun: Boolean(attempt?.runCreated),
      reasonShort: "Журнал говорит, что работа шла; процесс не запущен — продолжите, чтобы возобновить её.",
    }
  }

  // 3. No durable run yet: the project has never been verified.
  if (!runner?.present) {
    // The roadmap has no concrete targets yet — offer the understandable choice
    // (auto-discovery / configure scope) BEFORE any run is claimed.
    if (input.noTargets || attempt?.lastStep === "no-targets") {
      return {
        state: "no-targets",
        blocker,
        firstRun: false,
        reasonShort: "Сохранённых целей обновления нет — выберите, как их подобрать.",
      }
    }
    // A fixable LOCAL readiness blocker (e.g. dangling git submodule) found by
    // the cheap preflight → offer to re-check AFTER the user fixes it; the
    // exact command is shown in the diagnostics, never auto-run.
    if (attemptFailed && blocker && (blocker.fixable || FIXABLE_CODES.has(blocker.code))) {
      return {
        state: "retry-check",
        blocker,
        firstRun: false,
        reasonShort: blocker.summary || "Проверка нашла исправимую проблему подготовки проекта.",
      }
    }
    if (attemptFailed) {
      return {
        state: "check",
        blocker,
        firstRun: false,
        reasonShort: attempt?.lastError ?? attempt?.reason ?? "Проверка не завершилась. Повторите проверку.",
      }
    }
    return {
      state: "check",
      blocker,
      firstRun: false,
      reasonShort: "Проект ещё не проверен: зафиксируем текущее состояние и найдём доступные обновления.",
    }
  }

  // 4. A durable run exists.
  if (decisionStep === "agent") {
    return { state: "agent", blocker, firstRun: false, reasonShort: "Нужны исправления — выпустите агента в изолированный trial." }
  }
  const satisfied = decisionStep === "finish" || Boolean(runner.decision?.satisfied) || runner.phase === "TERMINAL"
  if (satisfied) {
    return { state: "result", blocker, firstRun: false, reasonShort: "Обновления проверены — откройте результат." }
  }
  const firstRun = Boolean(attempt?.runCreated) && !(attempt?.stepsDone?.length)
  return {
    state: firstRun ? "start-run" : "continue-run",
    blocker,
    firstRun,
    reasonShort: "Работа приостановлена — следующий этап готов к запуску.",
  }
}

/** User-facing activity phrase for a live attempt (no internal step names). */
export function activityReason(attempt: ScenarioAttemptShape): string {
  const stage = attempt?.stage
  if (stage === "agent") return "Нужны исправления (агент работает в изолированном trial)"
  if (stage === "drive") return "Обновляем пакеты и проверяем результат"
  if (stage === "begin") {
    if (attempt?.lastStep === "prepare") return "Подготавливаем проект"
    return "Проверяем текущие зависимости"
  }
  if (stage === "preflight") return "Предварительная проверка проекта"
  return "Работа выполняется"
}
