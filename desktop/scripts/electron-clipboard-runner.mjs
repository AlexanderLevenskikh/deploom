// Probe protocol and exit status jointly establish the real round-trip result.
export function evaluateClipboardProbe(run, marker) {
  const stdout = String(run.stdout ?? "");
  const stderr = String(run.stderr ?? "");
  const diagnostic = { status: run.status ?? null, signal: run.signal ?? null,
    spawnError: String(run.error?.code ?? ""), stdout: stdout.slice(-2000), stderr: stderr.slice(-2000) };
  if (run.error || run.signal) return { ok: false, reason: "spawn-or-signal", diagnostic };
  const lines = stdout.split(/\r?\n/).filter(line => line.startsWith("CLIPBOARD_PROBE "));
  if (lines.length !== 1) return { ok: false, reason: "missing-or-duplicate-result", diagnostic };
  let payload;
  try { payload = JSON.parse(lines[0].slice("CLIPBOARD_PROBE ".length)); }
  catch { return { ok: false, reason: "malformed-result", diagnostic }; }
  if (run.status === 4 && payload?.ok === false && payload.error === "APP_READY_TIMEOUT") {
    return { ok: false, reason: "app-ready-timeout", retryable: true, diagnostic };
  }
  const expected = Buffer.byteLength(marker, "utf8");
  if (run.status !== 0 || payload?.ok !== true || payload.markerEqual !== true ||
      expected <= 0 || payload.markerBytes !== expected || payload.readbackBytes !== expected) {
    return { ok: false, reason: "round-trip-or-exit-failed", diagnostic };
  }
  return { ok: true, markerBytes: expected };
}
export function runClipboardProbe(spawn, marker) {
  for (let attempts = 1; attempts <= 2; attempts++) {
    const result = evaluateClipboardProbe(spawn(), marker);
    if (result.ok || !result.retryable || attempts === 2) return { ...result, attempts };
  }
  throw new Error("unreachable clipboard retry state");
}
