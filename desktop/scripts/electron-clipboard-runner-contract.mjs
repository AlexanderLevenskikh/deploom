import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { evaluateClipboardProbe, runClipboardProbe } from "./electron-clipboard-runner.mjs";
export async function verifyClipboardRunnerContract() {
  const marker = "marker кириллица";
  const bytes = Buffer.byteLength(marker, "utf8");
  const success = { status: 0, stdout: "CLIPBOARD_PROBE " + JSON.stringify({ ok: true, markerEqual: true, markerBytes: bytes, readbackBytes: bytes }) + "\n", stderr: "" };
  assert.equal(evaluateClipboardProbe(success, marker).ok, true);
  for (const run of [
    { ...success, status: 2 }, { ...success, status: 0xc0000005 }, { ...success, signal: "SIGTERM" },
    { ...success, error: { code: "ETIMEDOUT" } }, { status: 0, stdout: "" },
    { status: 0, stdout: "CLIPBOARD_PROBE {" },
    { ...success, stdout: success.stdout + success.stdout },
    { status: 0, stdout: 'CLIPBOARD_PROBE {"ok":true,"markerBytes":1}' },
  ]) {
    let calls = 0;
    assert.equal(runClipboardProbe(() => { calls++; return run; }, marker).ok, false);
    assert.equal(calls, 1, "unknown errors and mismatches must not retry");
  }
  const readyTimeout = { status: 4, stdout: 'CLIPBOARD_PROBE {"ok":false,"error":"APP_READY_TIMEOUT"}' };
  let calls = 0;
  const retry = runClipboardProbe(() => ++calls === 1 ? readyTimeout : success, marker);
  assert.equal(retry.ok, true); assert.equal(retry.attempts, 2);
  calls = 0;
  assert.equal(runClipboardProbe(() => { calls++; return readyTimeout; }, marker).ok, false);
  assert.equal(calls, 2, "ready timeout retry must be bounded");
  const code = readFileSync(new URL("./electron-clipboard-probe.cjs", import.meta.url), "utf8");
  const writes = [], exits = [], quits = [], scheduled = [];
  vm.runInNewContext(code, { require: () => ({ app: { whenReady: () => Promise.resolve(), exit: value => exits.push(value), quit: () => quits.push(true) }, clipboard: { writeText: () => {}, readText: () => marker } }), process: { argv: ["electron", "probe", marker], stdout: { write: (text, done) => writes.push({ text, done }) } }, Buffer, setImmediate: fn => scheduled.push(fn), setTimeout: () => 1, clearTimeout: () => {} });
  await Promise.resolve();
  assert.equal(writes.length, 1); assert.deepEqual(exits, [], "app.exit must await stdout flush");
  writes[0].done(); assert.deepEqual(exits, []); assert.deepEqual(quits, []);
  assert.equal(scheduled.length, 1, "teardown must leave the stdout completion callback");
  scheduled[0](); assert.deepEqual(quits, [true]); assert.deepEqual(exits, [], "success must use graceful shutdown");
  console.log("check-electron-clipboard-runner: OK (exit/protocol, bounded ready retry, stdout flush)");
}
