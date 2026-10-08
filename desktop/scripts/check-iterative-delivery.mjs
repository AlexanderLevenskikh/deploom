import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { auditLevel, readAuditView, runDeliveryWorkflow, workflowPrompt } from '../dist-electron/iterative-delivery.js'
import { decideNextStep } from '../dist-electron/iterative-runner.js'
import { createIterativeAutopilot } from '../dist-electron/iterative-autopilot.js'

const root = mkdtempSync(join(tmpdir(), 'deploom-delivery-contract-'))
const write = (name, value) => writeFileSync(join(root, name), JSON.stringify(value))
const audit = { status: 'FAIL', auditComplete: true, generatedAt: new Date().toISOString(), packageTotals: { critical: 1, high: 7 }, lagOkPct: 92.7 }
try {
  write('current-audit.json', audit)
  assert.equal(auditLevel(readAuditView(root)).status, 'red')
  write('current-audit-pending.json', { error: 'registry unavailable' })
  assert.equal(auditLevel(readAuditView(root)), undefined)
  assert.equal(readAuditView(root).error, 'registry unavailable')
  rmSync(join(root, 'current-audit-pending.json'))
  mkdirSync(join(root, 'checkpoints'))
  write('run.json', { runId: 'audit-fixture', activeCheckpointId: 'C4' })
  write('checkpoints/C4.json', { checkpointId: 'C4', audit: { status: 'UNKNOWN' } })
  assert.equal(readAuditView(root).stale, true)
  assert.equal(auditLevel(readAuditView(root)), undefined, 'C0 must not be presented as current C4')
  write('checkpoints/C4.json', { checkpointId: 'C4', sourceSnapshotKey: 'source-fixture', audit })
  assert.equal(auditLevel(readAuditView(root)).status, 'red')
  write('delivery-state.json', { runId: 'audit-fixture', sourceSnapshotKey: 'source-fixture', checkpointId: 'C4', status: 'done', audit: { ...audit, status: 'PASS', packageTotals: { critical: 0, high: 0 }, lagOkPct: 100 } })
  assert.equal(auditLevel(readAuditView(root)).status, 'green')
  const delivered = JSON.parse(readFileSync(join(root, 'delivery-state.json'), 'utf8'))
  write('delivery-state.json', { ...delivered, runId: 'another-run' })
  assert.equal(auditLevel(readAuditView(root)).status, 'red', 'a foreign delivery cannot replace current checkpoint evidence')
  assert.equal(auditLevel({ ...audit, generatedAt: '2000-01-01', stale: true }), undefined)
  write('targets.json', { auto: '9.0.0' })
  const ready = { run: { phase: 'READY' }, activeCheckpoint: { checkpointId: 'C0', fullAssignment: { auto: '1.0.0' } } }
  assert.equal(decideNextStep(root, ready).step, 'audit')
  assert.equal(decideNextStep(root, { ...ready, activeCheckpoint: { ...ready.activeCheckpoint, audit: { status: 'UNKNOWN', generatedAt: new Date().toISOString() } } }).step, 'finish', 'unavailable audit cannot loop forever')
  assert.equal(decideNextStep(root, { ...ready, goalSatisfied: true, activeCheckpoint: { ...ready.activeCheckpoint, audit: { status: 'PASS' } } }).step, 'finish', 'chosen policy can succeed without all auto proposals')
  assert.equal(decideNextStep(root, { ...ready, activeCheckpoint: { ...ready.activeCheckpoint, checkpointId: 'C1', audit: { status: 'UNKNOWN' } } }).step, 'plan-next', 'an unaudited new cohort is not a failed audit')

  write('run-config.json', { projectName: 'fixture', packagePolicies: { frozen: 'keep-current', required: 'required' }, targets: { required: '2.0.0' }, verifyConfig: { commands: ['node check.cjs'] } })
  rmSync(join(root, 'delivery-state.json'))
  let paidLaunches = 0, verifyCalls = 0
  const deliveryState = { status: 'ready', runId: 'fixture', checkpointId: 'C4', branch: 'codex/delivery', workspaceRoot: root, sourceHead: 'base' }
  const deps = {
    canceled: () => false, processAlive: () => false, progress: () => {}, provider: 'codex',
    command: async step => {
      if (step === 'security-prepare') return { status: 'not-needed' }
      if (step === 'delivery-prepare') { write('delivery-state.json', deliveryState); return deliveryState }
      if (step === 'delivery-verify') { verifyCalls++; if (verifyCalls === 1) throw new Error('AUDIT_NETWORK_UNAVAILABLE'); return { status: 'done' } }
      throw new Error(step)
    },
    launch: async context => {
      paidLaunches++
      context.onSpawn(123); context.onSession('saved-session')
      const durable = JSON.parse(readFileSync(join(root, 'delivery-agent.json'), 'utf8'))
      assert.equal(durable.sessionId, 'saved-session', 'session must be durable before agent exit')
      assert.equal(context.provider, 'codex')
      assert.ok(context.prompt.includes('mutually dependent cohorts'))
      assert.ok(context.prompt.includes('@skbkontur/react-icons'))
    },
  }
  assert.equal((await runDeliveryWorkflow(root, deps)).ok, false)
  assert.equal((await runDeliveryWorkflow(root, deps)).ok, true)
  assert.equal(paidLaunches, 1, 'restart after verification failure must not replay a finished paid agent')
  assert.equal(verifyCalls, 2)

  write('delivery-agent.json', { identity: 'delivery:fixture:C4:0', status: 'running', childPid: 55, sessionId: 'saved-session', provider: 'codex' })
  assert.equal((await runDeliveryWorkflow(root, { ...deps, processAlive: () => true })).error, 'DELIVERY_AGENT_IN_PROGRESS')
  assert.equal(paidLaunches, 1)
  let resumed
  await runDeliveryWorkflow(root, { ...deps, launch: async context => { resumed = context.sessionId } })
  assert.equal(resumed, 'saved-session')
  assert.ok(workflowPrompt('security', { ...deliveryState, baseCheckpointId: 'C4' }, { packagePolicies: { frozen: 'keep-current' } }, root).includes('shell-quote'))

  const calls = []
  const pilot = createIterativeAutopilot(() => 'fixture')
  pilot.register('drive', async () => { calls.push('drive'); return { ok: true, stopped: 'finished', needsDelivery: true } })
  pilot.register('agent', async () => { calls.push('agent'); return { ok: true, phase: 'TERMINAL' } })
  const begin = pilot.register('begin', async () => ({ ok: true }))
  const result = await begin(null, { autopilot: true })
  assert.equal(result.autopilot.stopped, 'finished')
  assert.deepEqual(calls, ['drive', 'agent'])
  console.log('check-iterative-delivery: OK (audit freshness, selected goals, restart/session and final autopilot)')
} finally { rmSync(root, { recursive: true, force: true }) }
