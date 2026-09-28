import { existsSync, readFileSync } from 'node:fs'
import type { RepairRequest } from './repair-handoff.js'

// R5 (2026-09-28), acceptance item 3: the repair agent's machine result
// contract and the repair prompt. The agent's prose is diagnostic; the
// orchestrator decides from this JSON, and the AUTHORITATIVE verdict is
// always the following fresh Baseline re-verification -- never the agent's
// own "all green" claim.

export type BaselineRepairAgentResult = {
  status: 'repaired' | 'partial' | 'blocked'
  reason: string
}

export function readBaselineRepairAgentResult(path: string): BaselineRepairAgentResult | undefined {
  if (!existsSync(path)) return undefined
  try {
    const value = JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
    const status = String(value.status ?? '').trim().toLowerCase()
    const reason = typeof value.reason === 'string' ? value.reason.trim() : ''
    if (status === 'repaired' || status === 'partial' || status === 'blocked') {
      return { status, reason } as BaselineRepairAgentResult
    }
  } catch {
    /* malformed result is treated as missing; the re-verification stays authoritative */
  }
  return undefined
}

function renderRequest(request: RepairRequest, index: number): string {
  const pairs = Object.entries(request.assignment)
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
    .map(([name, version]) => `${name}@${version}`)
    .join(', ')
  const commands = request.failingCommands.length
    ? `\n   failing commands: ${request.failingCommands.map((item) => `${item.command} (exit ${item.exitCode})`).join('; ')}`
    : ''
  const accepting = request.diagnosticsTail
    ? `\n   failure diagnostics tail:\n${indent(request.diagnosticsTail.slice(-1600), '     ')}`
    : ''
  return (
    `${index + 1}. requestId ${request.requestId}\n` +
    `   mode: ${request.mode}\n` +
    `   exact assignment (KEEP these exact versions): {${pairs || '(none)'}}\n` +
    `   fingerprint: ${request.fingerprint}\n` +
    `   snapshot identity the failure was observed on: ${request.snapshotIdentity || '(unknown)'}` +
    commands +
    accepting
  )
}

function indent(text: string, prefix: string): string {
  return text
    .split(/\r?\n/)
    .map((line) => `${prefix}${line}`)
    .join('\n')
}

export function buildBaselineRepairPrompt(input: {
  projectName: string
  projectPath: string
  savedPromptPath?: string
  resultPath: string
  requests: RepairRequest[]
  terminalRunId?: string
  cycle: number
  attempt: number
  resumeNote?: string
}): string {
  const requestsBlock = input.requests.length
    ? input.requests.map((request, index) => renderRequest(request, index)).join('\n')
    : '(no machine-actionable requests were present in the terminal envelope)'
  return `# Baseline source/config repair (${input.projectName})

The authoritative Baseline verification stopped with REPAIR_REQUIRED: the project's own checks
fail on the exact dependency tuples below. You are fixing the project SOURCE/CONFIG so those
tuples verify GREEN. The orchestrator re-runs the authoritative verification after you finish;
your word is never the proof.

Project: ${input.projectName}
Repository: ${input.projectPath}
${input.savedPromptPath ? `Approved dependency plan / prompt: ${input.savedPromptPath}` : ''}
Machine result file: ${input.resultPath}
Repair episode cycle ${input.cycle}, attempt ${input.attempt}${input.terminalRunId ? `, terminal run ${input.terminalRunId}` : ''}
${input.resumeNote ? `Resume note: ${input.resumeNote}` : ''}

## Open repair requests (close ALL of them)

${requestsBlock}

## Goal

Change source/config (in this working tree) so every exact tuple above passes the project's
checks. Repairs live in the project's own source/config -- dependency pins, build/test config,
types, code that legitimately must adapt to the target versions.

## Hard rules

1. Do NOT change the target versions in ${input.projectName}: the tuples above are the goal.
   No dependency refresh, no alternative versions, no unrelated upgrades.
2. Do NOT touch previously verified parts of the project beyond what the failing tuples require.
   No cleanup sweeps, no reformatting, no test rewrites.
3. Do not create branches, commit, push, stash, reset, rebase or merge. Work stays in the
   working tree exactly as the orchestrator left it.
4. Run the relevant existing checks to confirm your repair. Never bypass or weaken a check,
   hook, lint/type/tsc step, or test gate to make it pass.
5. If the failure cannot be repaired without changing approved dependency scope (the exact
   tuples) or without discarding ambiguous work, do not force it: report blocked.
6. When you finish, write exactly ONE JSON object to ${input.resultPath} (no markdown):
   - \`{"status":"repaired","reason":"..."}\` when you changed source/config and believe the
     exact tuples now pass project checks (the NEXT authoritative verification decides);
   - \`{"status":"partial","reason":"..."}\` when you made a best-effort change but could not
     verify all failing commands yourself;
   - \`{"status":"blocked","reason":"..."}\` when safe repair is impossible without changing
     the approved tuple scope or discarding work.
   The JSON file is mandatory; chat text is diagnostic only.

## Done means

- all open repair request tuples from the list above are addressed in the working tree;
- existing checks relevant to the failing commands have been run;
- no commit/branch/push/reset/stash/merge was performed;
- ${input.resultPath} contains the final machine-readable result.
`
}
