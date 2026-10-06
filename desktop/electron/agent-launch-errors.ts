// Classification of repair-agent LAUNCH failures (opencode / claude / codex).
//
// The user-critical rule this module enforces: a provider that is temporarily
// unavailable (rate limit, reset window, transient network/server errors) must
// NOT end the migration. It parks the repair in a durable wait and retries
// after the provider's reset time or a bounded exponential backoff, WITHOUT
// consuming a repair attempt and without turning the outage into an
// incompatibility verdict.
//
// Classification is deliberately CONSERVATIVE. The four buckets map 1:1 to the
// desired handling:
//   - rate-limited  -> wait and re-try, honoring Retry-After/reset when given,
//                      else bounded expoential backoff with jitter; no budget.
//   - temporary     -> finite manual budget; autonomous repair keeps waiting.
//   - auth-config   -> permanent: fail with a clear, actionable message.
//   - permanent     -> permanent: fail with a clear, actionable message.
//   - unknown       -> no evidence of anything specific: re-try with a SMALL
//                      FINITE budget, then fail with the raw diagnostics.
//
// An empty stderr is NEVER taken as evidence of a rate limit or of anything
// else specific: exit-with-no-output classifies as `unknown`, not as a limit.
//
// The module is PURE (no fs / Electron / React): a behavior check script
// invokes it directly with fixtures instead of grepping the sources.
//
// Diagnostics are bounded (tails, not full streams) and never carry
// environment variables, auth tokens or command arguments — only the exit
// code, the timeout flag and the tail of the provider's own output.

export type AgentLaunchFailureKind = 'rate-limited' | 'temporary' | 'auth-config' | 'permanent' | 'unknown'

export type AgentLaunchDiagnostics = {
  /** Process exit code; provider error envelopes can also accompany zero. */
  code: number
  timedOut: boolean
  /** Bounded tail of stdout (no secrets; env/args are never captured). */
  stdout: string
  stderr: string
  kind: AgentLaunchFailureKind
  /** One-line human cause; bounded length. */
  detail: string
  /** Retry-After / reset seconds reported by the provider (rate-limited only). */
  retryAfterSeconds?: number
}

// A confirmed rate limit has NO budget: it keeps re-waiting as long as the
// provider keeps reporting it (each wait honors Retry-After/reset; a reset
// without a time uses bounded backoff). Temporary and unknown outages get a
// FINITE manual budget so an unconfirmed / never-resolving outage eventually stops
// with a readable diagnostic instead of retrying forever. auth-config and
// permanent are 0: retrying cannot fix them. Autopilot can keep retrying
// confirmed temporary failures; unknown failures retain their finite budget.
export const AGENT_LAUNCH_RETRY_BUDGET: Record<AgentLaunchFailureKind, number> = {
  'rate-limited': Number.POSITIVE_INFINITY,
  temporary: 5,
  'auth-config': 0,
  permanent: 0,
  unknown: 3,
}

// Maximum timer duration. Provider windows longer than one hour are honored;
// a parked lease does not expire during a provider-reported waiting window.
export const AGENT_LAUNCH_MAX_RETRY_AFTER_MS = 2_147_000_000
export const AGENT_LAUNCH_MIN_RETRY_DELAY_MS = 1000
// Bounded exponential backoff cap (120 s) prevents tight polling during a wait.
export const AGENT_LAUNCH_BACKOFF_CAP_MS = 120_000

const MAX_DETAIL_LENGTH = 1000
const MAX_TAIL_LENGTH = 1200

export function redactAgentDiagnostics(text: string): string {
  return text
    .replace(/(authorization\s*["']?\s*[:=]\s*["']?)(?:Bearer\s+)?[^\s,"'}]+/gi, '$1[redacted]')
    .replace(/((?:api[-_ ]?key|access[-_ ]?token|auth[-_ ]?token)\s*["']?\s*[:=]\s*["']?)[^"'\s,}]+/gi, '$1[redacted]')
}

function tail(text: string | undefined, max = MAX_TAIL_LENGTH): string {
  if (!text) return ''
  const value = redactAgentDiagnostics(text).trim()
  return value.length <= max ? value : `…${value.slice(-max)}`
}

/** Extract a provider-reported reset/retry-after in SECONDS from output text.
 *  Accepts the HTTP `Retry-After: <seconds>`, common `x-ratelimit-reset`, and
 *  `retry_after` JSON-like forms. Returns undefined when absent/unparseable. */
export function extractRetryAfterSeconds(text: string, now = Date.now()): number | undefined {
  if (!text) return undefined
  const milliseconds = text.match(/retry_after_ms\s*["']?\s*[:=]\s*["']?\s*(\d+)/i)
  if (milliseconds) return Number(milliseconds[1]) / 1000
  const duration = text.match(/x-ratelimit-reset(?:-requests|-tokens)?\s*["']?\s*[:=]\s*["']?\s*((?:\d+(?:\.\d+)?(?:ms|s|m|h))+)/i)
  if (duration) return [...duration[1].matchAll(/(\d+(?:\.\d+)?)(ms|s|m|h)/gi)].reduce((sum, part) => sum + Number(part[1]) * ({ ms: 0.001, s: 1, m: 60, h: 3600 }[part[2].toLowerCase()] ?? 1), 0)
  const after = text.match(/retry[-_ ]?after\s*["']?\s*[:=]?\s*["']?\s*(\d+(?:\.\d+)?)(?=["'\s,}]|$)/i)
  if (after) return Number(after[1])
  const date = text.match(/retry[-_ ]?after\s*["']?\s*[:=]\s*["']?\s*((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),[^\n"']+GMT)/i)
  if (date) {
    const at = Date.parse(date[1])
    if (Number.isFinite(at)) return Math.max(0, (at - now) / 1000)
  }
  const reset = text.match(/(?:x-ratelimit-reset(?:-requests|-tokens)?|rate[ _-]?limit[^]{0,40}?reset)\s*["']?\s*[:=]?\s*["']?\s*(\d+(?:\.\d+)?)(ms|s|m|h)?/i)
  if (!reset) return undefined
  const value = Number(reset[1])
  const unit = reset[2]?.toLowerCase()
  if (unit) return value * ({ ms: 0.001, s: 1, m: 60, h: 3600 }[unit] ?? 1)
  // Unix timestamps are absolute; small numeric reset values are durations.
  if (value >= 1e12) return Math.max(0, (value - now) / 1000)
  if (value >= 1e9) return Math.max(0, value - now / 1000)
  return value
}

/** Only provider error envelopes confer failure authority at exit code zero.
 * Ordinary assistant/tool text mentioning limits is not an outage. */
export function agentLaunchProviderError(output: string, pending?: string): string | undefined {
  for (const line of output.split(/\r?\n/)) {
    try {
      const event = JSON.parse(line)
      if (event.type === 'error' || event.error || (event.type === 'result' && event.is_error === true)) {
        pending = JSON.stringify(event)
      } else if (event.type === 'step_finish' && event.part?.type === 'step-finish' &&
          event.part.reason === 'stop' && typeof event.part.messageID === 'string' &&
          typeof event.sessionID === 'string' && event.part.sessionID === event.sessionID) {
        // OpenCode can recover an earlier request error inside the same process.
        // Its terminal assistant completion supersedes that error, never a tool
        // result or prose. This is dispatch evidence; Python still verifies it.
        const previousSession = pending ? JSON.parse(pending).sessionID : undefined
        if (!previousSession || previousSession === event.sessionID) pending = undefined
      }
    } catch { /* plain output */ }
  }
  return pending
}

const HAS_RATE_LIMIT = [
  /\b429\b/,
  /\brate[ _-]?limit/i,
  /too many requests/i,
  /quota exceeded for (?:model )?requests/i,
  /limit.{0,24}?exceeded/i,
  /exceeded.{0,24}?limit/i,
  /request.{0,24}?limit/i,
  /over[ -]?rate/i,
]

const HAS_AUTH_OR_CONFIG = [
  /\b401\b/,
  /\b403\b/,
  /unauthori[sz]ed?/i,
  /authentication/i,
  /not authenticated/i,
  /api[ _-]?key/i,
  /invalid.{0,24}?(?:token|key|credential)/i,
  /credentials?/i,
  /provider.{0,32}?(?:not|never|did not).{0,32}?(?:configured|set|found)/i,
  /config.{0,24}?(?:invalid|missing|wrong|not found)/i,
]

const HAS_PERMANENT = [
  /insufficient[_ -]quota/i,
  /(?:no|insufficient|out of) (?:billing )?credits/i,
  /(?:billing|payment).{0,40}(?:required|exceeded|limit|disabled)/i,
  /(?:monthly|daily) (?:spend|budget).{0,30}(?:exceeded|limit)/i,
  /model not found/i,
  /ProviderModelNotFoundError/i,
  /unknown command/i,
  /command not found/i,
  /no such file/i,
  /invalid.{0,24}?(?:argument|option|flag)/i,
  /not.{0,24}?(?:supported|implemented)/i,
  /unknown flag/i,
  /AGENT_SESSION_PERSIST_FAILED/i,
]

const HAS_TEMPORARY = [
  /\b5\d\d\b/,
  /temporar/i,
  /unavailable/i,
  /try again later/i,
  /overloaded/i,
  /ETIMEDOUT/i,
  /timeout|timed[ -]?out/i,
  /ECONNRESET/i,
  /ECONNREFUSED/i,
  /EAI_AGAIN/i,
  /ENETUNREACH/i,
  /EHOSTUNREACH/i,
  /server error/i,
  /maintenance/i,
  /connection (?:reset|refused|closed)/i,
]

function findMatch(patterns: RegExp[], text: string): string | undefined {
  for (const pattern of patterns) {
    const match = text.match(pattern)
    if (match) return match[0]
  }
  return undefined
}

/** Classify a failed agent launch. `code` is the child exit code; a `timedOut`
 *  child reports the timeout as its own class even when it exited 0. */
export function classifyAgentLaunchFailure(input: {
  code: number
  stdout: string
  stderr: string
  timedOut?: boolean
  providerError?: string
}): AgentLaunchDiagnostics {
  const timedOut = input.timedOut === true
  const code = timedOut && input.code === 0 ? 124 : input.code
  const stdout = tail(input.stdout)
  const stderr = tail(input.stderr)
  const providerError = input.providerError ?? agentLaunchProviderError(input.stdout)
  const source = providerError ? redactAgentDiagnostics(providerError) : `${stdout}\n${stderr}`
  // A successful process is never a launch failure; it is left to the
  // provider-error/outcome parsers. We only classify failed launches.
  if (!timedOut && code === 0 && !providerError) {
    return { code, timedOut, stdout, stderr, kind: 'permanent', detail: 'agent process exited 0 — not a launch failure' }
  }
  const retryAfterSeconds = extractRetryAfterSeconds(source)
  // Order matters: rate-limit evidence is checked first so a 429 is never
  // downgraded to a generic temporary/unknown because other text is present.
  if (!HAS_PERMANENT.some((re) => re.test(source)) && HAS_RATE_LIMIT.some((re) => re.test(source))) {
    const hint = findMatch(HAS_RATE_LIMIT, source)
    const reset = retryAfterSeconds !== undefined ? `; retry after ${retryAfterSeconds}s` : ''
    return { code, timedOut, stdout, stderr, kind: 'rate-limited', retryAfterSeconds, detail: `${hint ?? 'rate limit'} (exit ${code})${reset}`.slice(0, MAX_DETAIL_LENGTH) }
  }
  // Permanent is checked BEFORE auth-config: "ProviderModelNotFoundError: Model
  // not found: …" also matches the broad "provider … not … found" config hint,
  // but a wrong model id is a definite misconfiguration that retrying cannot
  // fix and must surface as MODEL-not-found, never be downgraded to a generic
  // auth "provider not found" reading.
  if (HAS_PERMANENT.some((re) => re.test(source))) {
    const hint = findMatch(HAS_PERMANENT, source)
    return { code, timedOut, stdout, stderr, kind: 'permanent', detail: `${hint ?? 'permanent error'} (exit ${code})`.slice(0, MAX_DETAIL_LENGTH) }
  }
  if (HAS_AUTH_OR_CONFIG.some((re) => re.test(source))) {
    const hint = findMatch(HAS_AUTH_OR_CONFIG, source)
    return { code, timedOut, stdout, stderr, kind: 'auth-config', detail: `${hint ?? 'authentication/configuration error'} (exit ${code})`.slice(0, MAX_DETAIL_LENGTH) }
  }
  if (timedOut || HAS_TEMPORARY.some((re) => re.test(source))) {
    const hint = timedOut ? 'launch timed out' : findMatch(HAS_TEMPORARY, source)
    return { code, timedOut, stdout, stderr, kind: 'temporary', detail: `${hint ?? 'transient failure'} (exit ${code})`.slice(0, MAX_DETAIL_LENGTH) }
  }
  // No recognized evidence — explicitly NOT a rate limit and NOT anything
  // specific. Empty stderr lands here: honest `unknown`, not an inferred limit.
  const bare = code !== 0 && !stdout && !stderr
  return {
    code,
    timedOut,
    stdout,
    stderr,
    kind: 'unknown',
    detail: bare
      ? `agent exited ${code} with no output; cause unknown`
      : `${providerError ? redactAgentDiagnostics(providerError) : stdout || stderr || `exit ${code}`}`.slice(0, MAX_DETAIL_LENGTH),
  }
}

/** Grow the agent watchdog after provider outages, while retaining a deadline. */
export function agentLaunchTimeoutMs(launchAttempts: number, autopilot = false): number {
  return 15 * 60_000 * (autopilot ? 2 ** Math.max(0, Math.min(launchAttempts, 2)) : 1)
}

export function isRetryableAgentLaunchFailure(kind: AgentLaunchFailureKind): boolean {
  return AGENT_LAUNCH_RETRY_BUDGET[kind] !== 0
}

/** True when this launch attempt may be auto-retried (budget still available
 *  and the kind is retryable). `attempt` is 1-based within the wait episode. */
export function canRetryAgentLaunch(kind: AgentLaunchFailureKind, attempt: number, autopilot = false): boolean {
  if (autopilot && kind === 'temporary') return true
  if (!isRetryableAgentLaunchFailure(kind)) return false
  const budget = AGENT_LAUNCH_RETRY_BUDGET[kind]
  if (!Number.isFinite(budget)) return true
  return attempt < budget
}

/** Delay before the NEXT launch attempt, in ms. Honors the provider's
 *  Retry-After/reset (capped), otherwise a bounded exponential backoff with
 *  ±20% jitter so concurrent agents do not retry in lockstep. The caps apply
 *  to the FINAL value: jitter may never push the delay below the 1 s floor or
 *  above the manual 120 s or autonomous 30 min backoff cap. */
export function agentLaunchRetryDelayMs(diag: Pick<AgentLaunchDiagnostics, 'kind' | 'retryAfterSeconds'>, attempt: number, autopilot = false): number {
  if (diag.retryAfterSeconds !== undefined && Number.isFinite(diag.retryAfterSeconds) && diag.retryAfterSeconds > 0) {
    return Math.min(Math.max(diag.retryAfterSeconds * 1000, AGENT_LAUNCH_MIN_RETRY_DELAY_MS), AGENT_LAUNCH_MAX_RETRY_AFTER_MS)
  }
  const extended = autopilot && (diag.kind === 'temporary' || diag.kind === 'rate-limited')
  const cap = extended ? 30 * 60_000 : AGENT_LAUNCH_BACKOFF_CAP_MS
  const base = extended ? 30_000 : diag.kind === 'unknown' ? 10_000 : 5_000
  const exponent = Math.max(0, Math.min(attempt - 1, 8))
  const planned = Math.min(base * 2 ** exponent, cap)
  const jitter = 0.8 + (Math.random() * 0.4)
  return Math.max(AGENT_LAUNCH_MIN_RETRY_DELAY_MS, Math.min(Math.round(planned * jitter), cap))
}
