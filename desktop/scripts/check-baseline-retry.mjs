import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { isDeterministicToolFailure, isDeterministicSourcePreflightFailure, formatSourceCheckoutDirtyFailure } from '../dist-electron/baseline-retry.js'
const envelope = JSON.stringify({ schemaVersion: 'DEPLOOM_FAILURE_V2', category: 'TOOL_INTERNAL_ERROR', code: 'TypeError', retryability: 'report-defect', summary: "emit() got multiple values for keyword argument 'event'" })
for (const stream of ['stdout', 'stderr']) {
  const result = { code: 4, stderr: 'old network timeout', stdout: '', [stream]: envelope }
  assert.equal(isDeterministicToolFailure(result), true)
}
assert.equal(isDeterministicToolFailure({ code: 4, stdout: 'Baseline stopped safely: TOOL_INTERNAL_ERROR', stderr: '' }), true)
assert.equal(isDeterministicToolFailure({ code: 1, stdout: '', stderr: 'Traceback (most recent call last):\nTypeError: duplicate event' }), true)
for (const text of ['ETIMEDOUT registry request', '{broken json', 'package compilation TypeError: example', JSON.stringify({ schemaVersion: 'DEPLOOM_FAILURE_V2', category: 'NETWORK_TRANSIENT' })]) {
  assert.equal(isDeterministicToolFailure({ code: 1, stdout: text, stderr: '' }), false)
}
assert.equal(isDeterministicToolFailure({ code: 0, stdout: envelope, stderr: '' }), false)

// Deterministic source checkout preflight failures must classify separately
// from the programming-defect category: they describe the project's Git state
// and never become transient by re-running.
const dirty = '[error] SOURCE_CHECKOUT_DIRTY: Demo: commit/stash/remove changes before generation: M package.json; M yarn.lock'
assert.equal(isDeterministicSourcePreflightFailure({ code: 2, stderr: dirty, stdout: '' }), true)
assert.equal(isDeterministicSourcePreflightFailure({ code: 2, stdout: dirty, stderr: '' }), true)
assert.equal(isDeterministicSourcePreflightFailure({ code: 1, stderr: 'SOURCE_BRANCH_DIVERGED: work @ a1b2 differs from origin/work @ c3d4', stdout: '' }), true)
assert.equal(isDeterministicSourcePreflightFailure({ code: 4, stderr: 'SOURCE_FETCH_FAILED: origin/timeout', stdout: '' }), true)
assert.equal(isDeterministicSourcePreflightFailure({ code: 4, stderr: 'GIT_NOT_FOUND: git binary missing', stdout: '' }), true)
for (const text of ['ETIMEDOUT registry request', 'BASELINE_VERIFICATION_PLATEAU: no further closure progress'] ) {
  assert.equal(isDeterministicSourcePreflightFailure({ code: 1, stderr: text, stdout: '' }), false)
}
assert.equal(isDeterministicSourcePreflightFailure({ code: 0, stderr: dirty, stdout: '' }), false)

// formatSourceCheckoutDirtyFailure extracts the dirty tracked files while
// stripping the git short status prefix and handling multi-file rows.
assert.deepEqual(formatSourceCheckoutDirtyFailure(dirty), { files: ['package.json', 'yarn.lock'] })
assert.deepEqual(formatSourceCheckoutDirtyFailure('LOG line\n[error] SOURCE_CHECKOUT_DIRTY: X: commit/stash/remove changes before generation:  M src/app.tsx\n').files, ['src/app.tsx'])
assert.deepEqual(formatSourceCheckoutDirtyFailure('[error] SOURCE_CHECKOUT_DIRTY: X: commit/stash/remove changes before generation: ?? untracked.txt').files, ['untracked.txt'])
assert.equal(formatSourceCheckoutDirtyFailure('[error] SOURCE_CHECKOUT_DIRTY: X: checkout mismatch'), undefined)
assert.equal(formatSourceCheckoutDirtyFailure('no source lines here'), undefined)
assert.equal(formatSourceCheckoutDirtyFailure(''), undefined)

// main.ts must apply the preflight classification as a NON-retryable
// deterministic failure so the real cause is preserved instead of worker
// retirement noise, and the user-facing message must not imply that the
// baseline search hit a budget.
const mainSource = readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
assert.match(mainSource, /function isWorkerLifecycleNoise/)
assert.match(mainSource, /if \(isDeterministicSourcePreflightFailure\(result\)\) return true/)
assert.match(mainSource, /BASELINE_WORKER_RETIRE_AWAIT_TIMEOUT/)
assert.match(mainSource, /sourcePreflightCommandFailureMessage\(job, result\)/)
const dirtyAction = mainSource.match(/Анализ незакоммиченных изменений возможен только через отдельный явный контракт продукта со снимком source snapshot, а не как обход этого отказа\./)
if (!dirtyAction) throw new Error('dirty-checkout user message contract is missing from main.ts')
if (/budget/i.test(dirtyAction[0])) throw new Error('dirty-checkout message must not mention a budget')

console.log('Baseline deterministic failure / source-preflight non-retryable + dirty checkout checks passed')
