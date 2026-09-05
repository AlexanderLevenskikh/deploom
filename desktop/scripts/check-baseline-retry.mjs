import assert from 'node:assert/strict'
import { isDeterministicToolFailure } from '../dist-electron/baseline-retry.js'
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
console.log('Baseline deterministic failure / transient retry checks passed')
