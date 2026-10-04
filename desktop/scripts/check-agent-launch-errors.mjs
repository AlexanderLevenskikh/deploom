import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import {
  AGENT_LAUNCH_BACKOFF_CAP_MS,
  AGENT_LAUNCH_MAX_RETRY_AFTER_MS,
  AGENT_LAUNCH_MIN_RETRY_DELAY_MS,
  AGENT_LAUNCH_RETRY_BUDGET,
  agentLaunchProviderError,
  agentLaunchRetryDelayMs,
  agentLaunchTimeoutMs,
  canRetryAgentLaunch,
  classifyAgentLaunchFailure,
  extractRetryAfterSeconds,
  isRetryableAgentLaunchFailure,
  redactAgentDiagnostics,
} from "../dist-electron/agent-launch-errors.js";

const DESKTOP = resolve(dirname(fileURLToPath(import.meta.url)), "..");

const launch = (stderr, code = 1, stdout = "", timedOut = false) =>
  classifyAgentLaunchFailure({ code, stdout, stderr, timedOut });

// 1. A confirmed rate limit is classified FIRST (even alongside auth text) and
// carries the provider reset time when one is reported.
const rateLimited = launch("Error: 429 Too Many Requests — Rate limit exceeded. Retry-After: 30");
assert.equal(rateLimited.kind, "rate-limited");
assert.equal(rateLimited.retryAfterSeconds, 30);
const rateLimitedReset = launch("x-ratelimit-reset: 45\nOpenAI API error 429");
assert.equal(rateLimitedReset.kind, "rate-limited");
assert.equal(rateLimitedReset.retryAfterSeconds, 45);
const overQuota = launch("quota exceeded for model requests");
assert.equal(overQuota.kind, "rate-limited");
assert.equal(overQuota.retryAfterSeconds, undefined, "A rate limit without a reset time stays retryable via backoff");

// 2. Empty stderr is NEVER evidence of a rate limit (or of anything specific):
// exit-with-no-output is an honest `unknown`, not an inferred limit.
const emptyStderr = launch("");
assert.equal(emptyStderr.kind, "unknown", "Empty stderr must not be inferred as a rate limit");
assert.ok(emptyStderr.detail.includes("no output"), "The unknown detail must describe the missing evidence");

// 3. Authentication / configuration problems are permanent-by-handling.
for (const stderr of [
  "Error 401 Unauthorized",
  "HTTP 403 Forbidden",
  "authentication failed: invalid api key",
  "API key not configured; run `opencode auth login`",
]) {
  assert.equal(launch(stderr).kind, "auth-config", `Expected auth-config for: ${stderr}`);
}

// 4. Permanent, non-retryable causes.
for (const stderr of [
  "ProviderModelNotFoundError: Model not found: test-provider/obsolete",
  "Error: model not found: gpt-4-xyz",
  "unknown command \"opencode-typo\"",
  "Error: invalid argument --nope",
]) {
  assert.equal(launch(stderr).kind, "permanent", `Expected permanent for: ${stderr}`);
}
// A success is never a launch failure (left to the outcome/feedback parsers).
assert.equal(launch("", 0).kind, "permanent");

// 5. Timeouts and server-side / network blips are temporary.
assert.equal(launch("", 0, "", true).kind, "temporary", "A timed-out launch is a temporary outage");
assert.equal(launch("HTTP 500 Internal Server Error").kind, "temporary");
assert.equal(launch("ECONNREFUSED connect failed").kind, "temporary");
assert.equal(launch("the service is temporarily unavailable, try again later").kind, "temporary");

// 6. Retry budgets: a confirmed rate limit has none; temporary/unknown are
// finite; auth-config/permanent are 0 (retrying cannot fix them).
assert.ok(Number.isFinite(AGENT_LAUNCH_RETRY_BUDGET["rate-limited"]) === false, "Rate limit must be unbounded");
assert.equal(AGENT_LAUNCH_RETRY_BUDGET.temporary, 5);
assert.equal(AGENT_LAUNCH_RETRY_BUDGET.unknown, 3);
assert.equal(AGENT_LAUNCH_RETRY_BUDGET["auth-config"], 0);
assert.equal(AGENT_LAUNCH_RETRY_BUDGET.permanent, 0);
assert.equal(isRetryableAgentLaunchFailure("rate-limited"), true);
assert.equal(isRetryableAgentLaunchFailure("auth-config"), false);
assert.equal(isRetryableAgentLaunchFailure("permanent"), false);
assert.equal(canRetryAgentLaunch("temporary", 4), true);
assert.equal(canRetryAgentLaunch("temporary", 5), false);
assert.equal(canRetryAgentLaunch("temporary", 6), false, "Attempt beyond the temporary budget must stop");
assert.equal(canRetryAgentLaunch("unknown", 2), true);
assert.equal(canRetryAgentLaunch("unknown", 3), false);
assert.equal(canRetryAgentLaunch("unknown", 4), false);
assert.equal(canRetryAgentLaunch("rate-limited", 10_000), true);

// 7. Delay honors Retry-After (bounded by the timer range), never below
// the 1 s floor, and bounded exponential backoff with ±20 % jitter caps at
// 120 s so concurrent agents retry with spread, not in lockstep.
const retryAfter = agentLaunchRetryDelayMs({ kind: "rate-limited", retryAfterSeconds: 30 }, 1);
assert.ok(retryAfter >= 30_000 && retryAfter <= 30_000 * 1.0, `Retry-After must be honored (got ${retryAfter})`);
assert.equal(agentLaunchRetryDelayMs({ kind: "rate-limited", retryAfterSeconds: 7200 }, 1), 7200_000, "A two-hour provider window must not retry early");
assert.equal(agentLaunchRetryDelayMs({ kind: "rate-limited", retryAfterSeconds: 1e12 }, 1), AGENT_LAUNCH_MAX_RETRY_AFTER_MS);
{
  const absentReset = agentLaunchRetryDelayMs({ kind: "rate-limited", retryAfterSeconds: 0 }, 1);
  assert.ok(absentReset >= 4000 && absentReset <= 6000, `Zero/absent reset must fall back to the 5 s backoff (got ${absentReset})`);
  assert.ok(agentLaunchRetryDelayMs({ kind: "rate-limited", retryAfterSeconds: undefined }, 1) >= AGENT_LAUNCH_MIN_RETRY_DELAY_MS, "Absent reset must respect the 1 s floor");
}
for (let attempt = 1; attempt <= 12; attempt++) {
  for (const kind of ["temporary", "unknown", "rate-limited"]) {
    const delay = agentLaunchRetryDelayMs({ kind, retryAfterSeconds: undefined }, attempt);
    assert.ok(delay >= AGENT_LAUNCH_MIN_RETRY_DELAY_MS, `Delay must never be below the floor (${kind} #${attempt})`);
    assert.ok(delay <= AGENT_LAUNCH_BACKOFF_CAP_MS, `Delay must never exceed the 120 s cap (${kind} #${attempt})`);
  }
}

// 8. Diagnostics are bounded: never full streams that could carry tokens.
const huge = launch(`${"Auth failure noise ".repeat(5000)}Error model not found: provider/typo`);
assert.ok(huge.detail.length < 1200, `Detail must be a bounded tail (${huge.detail.length})`);
assert.ok(!huge.detail.includes("Auth failure noise".repeat(200)), "Detail must not contain the full repeated noise");

// 9. Retry-After extraction covers the common provider forms.
assert.equal(extractRetryAfterSeconds("Retry-After: 60"), 60);
assert.equal(extractRetryAfterSeconds("x-ratelimit-reset: 120"), 120);
assert.equal(extractRetryAfterSeconds('{"error":{"retry_after":75}}'), 75);
assert.equal(extractRetryAfterSeconds("no timing info"), undefined);

const now = Date.parse("2026-10-05T00:00:00Z");
assert.equal(extractRetryAfterSeconds(`x-ratelimit-reset: ${now / 1000 + 30}`, now), 30);
assert.equal(extractRetryAfterSeconds(`x-ratelimit-reset: ${now + 30_000}`, now), 30);
assert.equal(extractRetryAfterSeconds(`Retry-After: ${new Date(now + 60_000).toUTCString()}`, now), 60);
assert.equal(extractRetryAfterSeconds("x-ratelimit-reset-requests: 2m", now), 120);
for (const message of ["insufficient_quota: no credits", "HTTP 429 insufficient_quota", "billing limit exceeded", "payment required"]) {
  assert.equal(launch(message).kind, "permanent", message);
}
const jsonLimit = JSON.stringify({type: "error", error: {data: {message: "429 Too Many Requests", headers: {"retry-after": "30"}}}});
assert.ok(agentLaunchProviderError(jsonLimit));
assert.equal(launch("", 0, jsonLimit).kind, "rate-limited");
assert.equal(launch("", 0, jsonLimit).retryAfterSeconds, 30);
assert.equal(agentLaunchProviderError(JSON.stringify({type: "text", part: {text: "check error 429"}})), undefined);
assert.equal(launch("", 0, "ordinary agent text mentions 429").kind, "permanent");
assert.equal(extractRetryAfterSeconds('x-ratelimit-reset-tokens: 1m12s', now), 72);
assert.equal(extractRetryAfterSeconds('{"retry_after_ms":30000}', now), 30);
assert.equal(redactAgentDiagnostics('Authorization: Bearer sample-value'), 'Authorization: [redacted]');
assert.equal(redactAgentDiagnostics('{"apiKey":"sample-value"}'), '{"apiKey":"[redacted]"}');
assert.equal(launch('APITimeoutError: request timed out').kind, 'temporary');
assert.equal(canRetryAgentLaunch('temporary', 1000, true), true);
for (const kind of ['permanent','auth-config','unknown']) assert.equal(canRetryAgentLaunch(kind, 1000, true), false);
for (let n = 1; n < 20; n++) {
  const delay = agentLaunchRetryDelayMs({kind:'temporary'},n,true);
  assert.ok(delay >= Math.min(30_000 * 2 ** (n-1),1800_000)*0.8);
  assert.ok(delay <= 1800_000);
}
assert.equal(agentLaunchRetryDelayMs({kind:'temporary',retryAfterSeconds:7200},7,true),7200_000);
assert.deepEqual([0,1,2,100].map(n=>agentLaunchTimeoutMs(n,true)),[900_000,1800_000,3600_000,3600_000]);
assert.equal(agentLaunchTimeoutMs(100,false),900_000);
console.log(`check-agent-launch-errors: OK (${DESKTOP})`);
