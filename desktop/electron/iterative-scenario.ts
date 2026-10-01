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
  /** P1#4: a real standalone "Проверить проект" passed (durable, non-run). */
  checked: boolean
}

export type ScenarioMainActionState =
  | "check" // проект ещё не проверен → Проверить проект (реальный preflight+контроль)
  | "retry-check" // проверка не дала результата / исправимый локальный блокер → Повторить проверку
  | "repair-current" // P2 (#2): контроль текущих зависимостей красный → Исправить проект агентом
  | "no-targets" // сохранённых целей нет → предложить авто-поиск / настройку
  | "ready" // P1#4: проверка пройдена, миграция ещё не начата → Начать обновление
  | "start-run" // проект готов, ничего ещё не гонялось → Начать обновление
  | "continue-run" // работа приостановлена → Продолжить обновление
  | "running" // реальный процесс выполняется → Остановить
  | "recovered-running" // журнал running, но живого процесса нет → Продолжить
  | "agent" // требуется ремонт агентом → Исправить агентом
  | "result" // результат ПОЛНОСТЬЮ проверен и принят → Посмотреть результат
  | "partial" // P1#3: завершено, но не все цели выполнены → Посмотреть результат (честно)
  | "budget-stop" // P1#3: остановлено бюджетом → Посмотреть результат (честно)
  | "repair-done" // P2 (#1): ремонт текущего состояния завершён и подтверждён → Начать обновление
  | "blocked" // P2 (#3): терминал-блокер без подтверждающего результата → Посмотреть результат (честно)
  | "no-upgrade" // P2 (#3): проверенных обновлений не найдено — НЕ «частичное обновление» → Посмотреть результат

export type ScenarioMainAction = {
  state: ScenarioMainActionState
  /** fixable local blocker surfaced once (e.g. an uninitialized submodule). */
  blocker?: IterativeFailureInfo
  /** true when this is the first drive of a freshly captured run. */
  firstRun: boolean
  /** user-facing one-line reason + a diagnostics payload for copy. */
  reasonShort: string
}

/** P1#1: the single scenario action, mirrorable by the enclosing workspace hero
 * so the legacy FLOW button and the panel stop competing: ONE control, ONE
 * state machine. `act` is the panel's own handler. */
export type ScenarioSignal = {
  state: ScenarioMainActionState
  label: string
  description: string
  running: boolean
  enabled: boolean
  act: () => void
}

const FIXABLE_CODES = new Set(["SOURCE_SUBMODULE_INCOMPLETE", "SOURCE_SUBMODULE_STATUS_FAILED"])

export function deriveMainAction(input: ScenarioInput): ScenarioMainAction {
  const attempt = input.attempt
  const runner = input.runner
  const blocker = parseIterativeFailure(attempt?.lastError) ?? parseIterativeFailure(attempt?.reason)
  const attemptLive = attempt?.status === "running" || attempt?.status === "starting"
  const attemptFailed = attempt?.status === "failed" || attempt?.status === "canceled"
  const decisionStep = runner?.decision?.step ?? null
  const decisionSatisfied = Boolean(runner?.decision?.satisfied)
  const decisionReason = runner?.decision?.reason ?? ""

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
    // P2 (#2): a RED current-state control (PROJECT_CONTROL_FAILED) must reach
    // the repair agent, not a dead-end re-check: clicking this opens the durable
    // run whose red C0 puts the coordinator on the BOOTSTRAP_REPAIR track
    // (isolated checkout → agent → authoritative re-verify). A fresh error
    // beats an OLD "no targets" explanation (P2#5).
    if (attemptFailed && blocker?.code === "PROJECT_CONTROL_FAILED") {
      return {
        state: "repair-current",
        blocker,
        firstRun: false,
        reasonShort:
          "Контроль текущих зависимостей не пройден. Агент исправит проект в изолированном окружении, затем проверка повторится авторитетно.",
      }
    }
    // P2#5: a fresh fixable blocker (uninitialized submodule / unsettled
    // discovery) takes priority over an OLD "no roadmap targets" decision — a
    // new error must never hide behind the previous state.
    if (attemptFailed && blocker && (blocker.fixable || FIXABLE_CODES.has(blocker.code))) {
      return {
        state: "retry-check",
        blocker,
        firstRun: false,
        reasonShort: blocker.summary || "Проверка нашла исправимую проблему подготовки проекта.",
      }
    }
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
    if (attemptFailed) {
      return {
        state: "check",
        blocker,
        firstRun: false,
        reasonShort: attempt?.lastError ?? attempt?.reason ?? "Проверка не завершилась. Повторите проверку.",
      }
    }
    // P1#4: a real "Проверить проект" already passed (durable verdict) and the
    // migration has not started — STARTING is a separate, explicit action.
    if (input.checked) {
      return {
        state: "ready",
        blocker,
        firstRun: false,
        reasonShort: "Проект проверен — можно начать обновление.",
      }
    }
    return {
      state: "check",
      blocker,
      firstRun: false,
      reasonShort: "Проект ещё не проверен: проверим подготовку и текущие зависимости. Обновление не запускается.",
    }
  }

  // 4. A durable run exists.
  if (decisionStep === "agent") {
    return { state: "agent", blocker, firstRun: false, reasonShort: "Нужны исправления — выпустите агента в изолированный trial." }
  }
  // P2 (#2): a red run (BOOTSTRAP_REPAIR) first materializes the
  // version-neutral trial; the action stays the repair flow, never
  // "Начать обновление".
  if (decisionStep === "bootstrap-materialize") {
    return {
      state: "repair-current",
      blocker,
      firstRun: false,
      reasonShort:
        "Исправим текущий проект в изолированном окружении: сперва подготовим trial, затем агент и повторная авторитетная проверка.",
    }
  }
  // P1#3: never call an unfinished result "проверено". The run is fully done
  // ONLY when the decision explicitly says the policy is satisfied AND the
  // finish step was reached (independent audit recorded). A terminal without
  // satisfaction is a PARTIAL result (blocked / not-all-goals / no-upgrade)
  // or a BUDGET stop — both are surfaced honestly, never as a verified success.
  if (decisionSatisfied && decisionStep === "finish") {
    return { state: "result", blocker, firstRun: false, reasonShort: "Обновления проверены — откройте результат." }
  }
  if (decisionStep === "finish" || runner?.phase === "TERMINAL") {
    // P2 (#3): the terminal reason is the durable DURABLE verdict keyword from
    // the authoritative layer (COMPLETE / REPAIR_VERIFIED / BLOCKED_BASELINE /
    // PARTIAL_VERIFIED / NO_VERIFIED_UPGRADE / NO_ACTIONABLE / SCOPE_EXHAUSTED).
    // Each gets its own HONEST state — a repair success is NOT a partial
    // migration, and "no verified upgrade" is NOT "часть обновлений применена".
    if (decisionReason === "REPAIR_VERIFIED") {
      return {
        state: "repair-done",
        blocker,
        firstRun: false,
        reasonShort: "Ремонт завершён: текущее состояние подтверждено. Начните обновление.",
      }
    }
    if (decisionReason === "BLOCKED_BASELINE") {
      return {
        state: "blocked",
        blocker,
        firstRun: false,
        reasonShort: "Обновление заблокировано — авторитетная проверка не подтвердила результат.",
      }
    }
    if (decisionReason === "NO_VERIFIED_UPGRADE" || decisionReason === "NO_ACTIONABLE" || decisionReason === "SCOPE_EXHAUSTED") {
      return {
        state: "no-upgrade",
        blocker,
        firstRun: false,
        reasonShort: "Проверенных обновлений не найдено — обновление не выполнено (это не частичный результат).",
      }
    }
    if (/budget|исчерпан бюджет|бюджет/i.test(decisionReason)) {
      return {
        state: "budget-stop",
        blocker,
        firstRun: false,
        reasonShort: "Работа остановилась по бюджету: применённое и проверенное сохранено, но не все цели выполнены.",
      }
    }
    return {
      state: "partial",
      blocker,
      firstRun: false,
      reasonShort: "Частичный результат: часть обновлений применена и проверена, остальные цели не выполнены.",
    }
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

// User UX (one-card pipeline): the migration is a LINEAR flow the user should be
// able to read at a glance — Проверка → Подбор версий → Обновление → Результат.
// `scenarioPipeline(state, checked)` maps the single scenario action to which
// stage is CURRENT and which stages are already DONE, so the interface always
// shows progress and never a dead end. PURE — the panel renders it, the check
// script pins it.
export type PipelineStage = "check" | "plan" | "upgrade" | "result"

export const PIPELINE_STAGES: ReadonlyArray<{ stage: PipelineStage; ru: string; en: string }> = [
  { stage: "check", ru: "Проверка", en: "Check" },
  { stage: "plan", ru: "Подбор версий", en: "Plan" },
  { stage: "upgrade", ru: "Обновление", en: "Update" },
  { stage: "result", ru: "Результат", en: "Result" },
]

export function scenarioPipeline(state: ScenarioMainActionState, checked: boolean): { current: PipelineStage; completed: PipelineStage[] } {
  switch (state) {
    case "result":
      // a finished, independently-audited, policy-satisfied migration.
      return { current: "result", completed: ["check", "plan", "upgrade"] }
    case "partial":
    case "budget-stop":
      // verified work was applied, but the goal was not reached — the flow is
      // at the result, honestly labelled partial.
      return { current: "result", completed: ["check", "plan", "upgrade"] }
    case "blocked":
    case "no-upgrade":
      // the flow ended WITHOUT a confirmed upgrade — result, honestly.
      return { current: "result", completed: ["check", "plan"] }
    case "repair-done":
      // the CURRENT state was fixed and verified — the real update starts next.
      return { current: "plan", completed: ["check"] }
    case "ready":
      // checked, migration not started yet — next is starting the update.
      return { current: "plan", completed: ["check"] }
    case "no-targets":
      // choosing how to find versions; the check may or may not have run yet.
      return { current: "plan", completed: checked ? ["check"] : [] }
    case "start-run":
    case "continue-run":
    case "running":
    case "recovered-running":
    case "agent":
      return { current: "upgrade", completed: ["check", "plan"] }
    default:
      // check / retry-check / repair-current / fresh — the project is being
      // checked (repair is part of making that check pass).
      return { current: "check", completed: [] }
  }
}
