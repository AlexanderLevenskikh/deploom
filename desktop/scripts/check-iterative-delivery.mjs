import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { pendingCohortReview, saveCohortReviewEnabled } from '../dist-electron/iterative-cohort-review.js'
import { auditLevel, readAuditView, runDeliveryWorkflow, workflowPrompt, createDeliveryAgentWatchdog } from '../dist-electron/iterative-delivery.js'
import { decideNextStep } from '../dist-electron/iterative-runner.js'
import { createIterativeAutopilot } from '../dist-electron/iterative-autopilot.js'

const root = mkdtempSync(join(tmpdir(), 'deploom-delivery-contract-'))
const write = (name, value) => writeFileSync(join(root, name), JSON.stringify(value))
const audit = { status: 'FAIL', auditComplete: true, generatedAt: new Date().toISOString(), packageTotals: { critical: 1, high: 7 }, lagOkPct: 92.7 }
try {
  const cohort = { candidateId: 'review-1', baseCheckpointId: 'C1', policyHash: 'policy', stage: 'PLANNED', fullAssignment: { one: '2.0.0' }, delta: { changed: { one: '2.0.0' } } }
  const reviewPayload = { run: { activeCandidateId: 'review-1', activeCheckpointId: 'C1' }, candidate: cohort, activeCheckpoint: { fullAssignment: { one: '1.0.0' } } }
  assert.equal(pendingCohortReview(root, reviewPayload).packages[0].current, '1.0.0')
  write('cohort-approval.json', { ...cohort })
  assert.equal(pendingCohortReview(root, reviewPayload), undefined)
  assert.ok(pendingCohortReview(root, { ...reviewPayload, candidate: { ...cohort, fullAssignment: { one: '3.0.0' } } }), 'changed assignment invalidates approval')
  assert.ok(pendingCohortReview(root, { ...reviewPayload, activeCheckpoint: { ...reviewPayload.activeCheckpoint, sourceSnapshotKey: 'different-source' } }), 'source repair invalidates approval')
  saveCohortReviewEnabled(root, false)
  assert.equal(pendingCohortReview(root, reviewPayload), undefined)
  saveCohortReviewEnabled(root, true)
  assert.equal(auditLevel({ ...audit, status: 'UNKNOWN', auditComplete: false, securityComplete: true }).status, 'red', 'confirmed critical is visible despite incomplete lag')
  const auditPolicyReady = { run: { phase: 'READY', activeCandidateId: null }, activeCheckpoint: { checkpointId: 'C1', status: 'VERIFIED', fullAssignment: {}, audit: { status: 'UNKNOWN' } }, config: { goalMode: 'audit-policy' } }
  assert.equal(decideNextStep(root, auditPolicyReady).step, 'audit', 'every accepted cohort gets an audit before further planning')
  assert.equal(decideNextStep(root, { ...auditPolicyReady, goalSatisfied: true, activeCheckpoint: { ...auditPolicyReady.activeCheckpoint, audit: { status: 'PASS', generatedAt: new Date().toISOString() } } }).step, 'finish', 'goal attainment stops planning')
  assert.equal(decideNextStep(root, { ...auditPolicyReady, goalSatisfied: true, run: { phase: 'PLANNING', activeCandidateId: 'old-proposal' }, candidate: { candidateId: 'old-proposal', stage: 'PLANNED' }, activeCheckpoint: { ...auditPolicyReady.activeCheckpoint, audit: { status: 'PASS' } } }).step, 'finish', 'an old unused proposal cannot extend an achieved goal')
  let tick = 0
  const guard = createDeliveryAgentWatchdog(() => tick, 45, 180)
  tick = 40; guard.observe(JSON.stringify({type:'tool_use',part:{type:'tool',state:{status:'completed'}}}))
  tick = 45; assert.equal(guard.onDeadline(), 40, 'active work survives the previous absolute deadline')
  tick = 85; assert.equal(guard.onDeadline(), 0, 'inactivity must still stop')
  tick = 170; guard.observe(JSON.stringify({type:'text',part:{text:'working'}}))
  assert.equal(guard.onDeadline(), 10, 'activity cannot evade the total ceiling')
  tick = 180; assert.match(guard.failure({code:1,stdout:'',stderr:'',timedOut:true}), /TIME_BUDGET/)
  tick = 0
  const idle = createDeliveryAgentWatchdog(() => tick, 45, 180)
  tick = 44; idle.observe(JSON.stringify({type:'heartbeat'}))
  tick = 45; assert.equal(idle.onDeadline(), 0, 'heartbeats are not substantive work')
  assert.match(idle.failure({code:1,stdout:'',stderr:'',timedOut:true}), /IDLE_TIMEOUT/)
  assert.match(idle.failure({code:1,stdout:'',stderr:'',timedOut:false,canceled:true}), /CANCELED/)
  assert.match(idle.failure({code:0,stdout:JSON.stringify({type:'error',error:{message:'rate limited'}}),stderr:'',timedOut:false}), /rate-limited/)
  idle.observe(JSON.stringify({type:'step_finish',sessionID:'s',part:{type:'step-finish',reason:'stop',messageID:'m',sessionID:'s'}}))
  assert.equal(idle.failure({code:0,stdout:'',stderr:'',timedOut:false}), undefined)
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
  let paidLaunches = 0, verifyCalls = 0, preflightCalls = 0
  const deliveryState = { status: 'ready', runId: 'fixture', checkpointId: 'C4', branch: 'codex/delivery', workspaceRoot: root, sourceHead: 'base' }
  const deps = {
    canceled: () => false, processAlive: () => false, progress: () => {}, provider: 'codex',
    command: async step => {
      if (step === 'security-prepare') return { status: 'not-needed' }
      if (step === 'delivery-prepare') { write('delivery-state.json', deliveryState); return deliveryState }
      if (step === 'delivery-preflight') { preflightCalls++; return { status: 'ready', checkedFiles: 10, lineEndingChangeCount: 2 } }
      if (step === 'delivery-verify') { verifyCalls++; if (verifyCalls === 1) throw new Error('AUDIT_NETWORK_UNAVAILABLE'); return { status: 'done' } }
      throw new Error(step)
    },
    launch: async context => {
      assert.ok(preflightCalls > 0, 'check every delivery file before a paid semantic-commit agent')
      paidLaunches++
      context.onSpawn(123); context.onSession('saved-session')
      const durable = JSON.parse(readFileSync(join(root, 'delivery-agent.json'), 'utf8'))
      assert.equal(durable.sessionId, 'saved-session', 'session must be durable before agent exit')
      assert.equal(context.provider, 'codex')
      assert.ok(context.prompt.includes('mutually dependent cohorts'))
      assert.ok(context.prompt.includes('@skbkontur/react-icons'))
      assert.ok(context.prompt.includes('Never force-add or copy ignored inputs'))
      assert.ok(context.prompt.includes('checkpoint checks do not prove this new subject'))
      assert.ok(context.prompt.includes('ASCII-compatible legacy encodings such as Windows-1251'))
      assert.ok(context.prompt.includes('fresh checks and audit of the exact delivered bytes'))
    },
  }
  assert.equal((await runDeliveryWorkflow(root, deps)).ok, false)
  assert.equal((await runDeliveryWorkflow(root, deps)).ok, true)
  assert.equal(paidLaunches, 1, 'restart after verification failure must not replay a finished paid agent')
  assert.equal(verifyCalls, 2)
  const blockedPreflight = await runDeliveryWorkflow(root, { ...deps,
    command: async step => { if (step === 'delivery-preflight') throw new Error('DELIVERY_SOURCE_CHANGED: 2 blocked files; full report'); return deps.command(step) },
  })
  assert.equal(blockedPreflight.ok, false)
  assert.equal(paidLaunches, 1, 'complete preflight must reject content drift before launching an agent')
  assert.equal(verifyCalls, 2, 'preflight rejection must not run the expensive project verifier')
  write('delivery-agent.json', { identity: 'delivery:fixture:C4:0', status: 'needs-agent', sessionId: 'saved-session', provider: 'codex', verificationError: 'DELIVERY_SOURCE_CHANGED: .gitignore line endings' })
  assert.equal((await runDeliveryWorkflow(root, deps)).ok, true)
  assert.equal(paidLaunches, 1, 'retry the exact delivery guard before paying for another agent on EOL-only source rejection')
  write('delivery-agent.json', { identity: 'delivery:fixture:C4:0', status: 'needs-agent', sessionId: 'saved-session', provider: 'codex', verificationError: 'DELIVERY_SOURCE_CHANGED' })
  const changedContent = await runDeliveryWorkflow(root, { ...deps,
    command: async step => { if (step === 'delivery-verify') throw new Error('DELIVERY_SOURCE_CHANGED: actual content changed'); return deps.command(step) },
  })
  assert.equal(changedContent.ok, false)
  assert.equal(paidLaunches, 1, 'actual content drift must remain a controller rejection')

  write('delivery-agent.json', { identity: 'delivery:fixture:C4:0', status: 'needs-agent', sessionId: 'saved-session', provider: 'codex', verificationError: 'DELIVERY_UNCOMMITTED_CHANGES: stale EOL status' })
  assert.equal((await runDeliveryWorkflow(root, deps)).ok, true)
  assert.equal(paidLaunches, 1, 'an authoritative retry can close EOL-only rejection without replaying the paid agent')
  assert.equal(JSON.parse(readFileSync(join(root, 'delivery-agent.json'), 'utf8')).status, 'finished')
  let realUncommittedLaunches = 0, retryCommitCalls = 0
  write('delivery-agent.json', { identity: 'delivery:fixture:C4:0', status: 'needs-agent', sessionId: 'saved-session', provider: 'codex', verificationError: 'DELIVERY_UNCOMMITTED_CHANGES' })
  const realUncommitted = await runDeliveryWorkflow(root, { ...deps,
    command: async step => { if (step === 'delivery-verify' && ++retryCommitCalls === 1) throw new Error('DELIVERY_UNCOMMITTED_CHANGES'); return deps.command(step) },
    launch: async context => { realUncommittedLaunches++; assert.equal(context.sessionId, 'saved-session') },
  })
  assert.equal(realUncommitted.ok, true); assert.equal(realUncommittedLaunches, 1)

  write('delivery-agent.json', { identity: 'delivery:fixture:C4:0', status: 'running', childPid: 55, sessionId: 'saved-session', provider: 'codex' })
  assert.equal((await runDeliveryWorkflow(root, { ...deps, processAlive: () => true })).error, 'DELIVERY_AGENT_IN_PROGRESS')
  assert.equal(paidLaunches, 1)
  let resumed
  await runDeliveryWorkflow(root, { ...deps, launch: async context => { resumed = context.sessionId } })
  assert.equal(resumed, 'saved-session')
  assert.ok(workflowPrompt('security', { ...deliveryState, baseCheckpointId: 'C4' }, { packagePolicies: { frozen: 'keep-current' } }, root).includes('shell-quote'))

  write('delivery-agent.json', { identity:'delivery:fixture:C4:0', status:'running', sessionId:'saved-session', provider:'codex' })
  const interrupted = await runDeliveryWorkflow(root, { ...deps, launch: async context => { context.onSession('same-paid-session'); context.onSpawn(9); throw new Error('DELIVERY_AGENT_IDLE_TIMEOUT') } })
  assert.equal(interrupted.ok, false)
  const retained = JSON.parse(readFileSync(join(root,'delivery-agent.json'),'utf8'))
  assert.equal(retained.sessionId, 'same-paid-session'); assert.equal(retained.childPid, undefined)
  assert.equal(retained.status, 'running'); assert.equal(retained.resumeError, 'DELIVERY_AGENT_IDLE_TIMEOUT')
  await runDeliveryWorkflow(root, { ...deps, launch: async context => { assert.equal(context.sessionId, 'same-paid-session') } })

  const cleanupState = { ...deliveryState, status: 'done', cleanup: { status: 'ready', baseHead: 'verified-head', changes: [{path:'docs/dependency-migration/fixture/MIGRATION_REPORT.md',kind:'document'}], archiveRoot: root } }
  let cleanupLaunches = 0, cleanupChecks = 0
  const cleanupCommands = []
  const cleanupDeps = { ...deps, cleanup: true,
    command: async step => {
      cleanupCommands.push(step)
      if (step === 'cleanup-prepare') return cleanupState
      if (step === 'cleanup-verify') { if (++cleanupChecks === 1) throw new Error('DELIVERY_VERIFICATION_FAILED'); return {status:'done'} }
      throw new Error(`Unexpected cleanup command: ${step}`)
    },
    launch: async context => {
      cleanupLaunches++
      assert.equal(context.phase, 'cleanup')
      assert.match(context.prompt, /Remove only this run/)
      assert.match(context.prompt, /node check.cjs/)
      assert.match(context.prompt, /exact prepared cleanup bytes/)
      context.onSession('cleanup-session')
    },
  }
  assert.equal((await runDeliveryWorkflow(root, cleanupDeps)).ok, false)
  assert.equal((await runDeliveryWorkflow(root, cleanupDeps)).ok, true)
  assert.equal(cleanupLaunches, 1, 'failed cleanup verification resumes without another paid agent')
  assert.deepEqual(cleanupCommands, ['cleanup-prepare','cleanup-verify','cleanup-prepare','cleanup-verify'])
  for (const phase of ['security','delivery']) {
    const prompt = workflowPrompt(phase, { ...deliveryState, baseCheckpointId: 'C4' }, {}, root)
    assert.match(prompt, /EVERY selected direct package/)
    assert.match(prompt, /major upgrade enables/)
    assert.match(prompt, /EVERY changed source\/config file/)
    assert.match(prompt, /DEPLOOM-MIGRATION-NOTE:fixture:/)
    assert.match(prompt, /complete appendix of changed transitive packages/)
  }
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
