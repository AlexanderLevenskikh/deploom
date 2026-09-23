// Cross-language Draft reader probe (T1/T5/T8).
//
// Loads the SAME production reader module that Electron main.ts uses
// (desktop/dist-electron/draft-artifact-reader.js, compiled from
// desktop/electron/draft-artifact-reader.ts) and runs it against artifacts
// written by the real Python publish_draft_result on native Windows.
// Input: path to a JSON file { workspacePath, runId, expect?, hashes? }.
// Output: JSON { ok:true, status, runId, workspaceId, projectId, planHash, promptHash }
//   | { ok:false, code, detail, text }.

import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { readDraftResultArtifact, draftReadFailureText, draftResultStaleness } from '../desktop/dist-electron/draft-artifact-reader.js'

const payload = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const result = readDraftResultArtifact(payload.workspacePath, payload.runId, payload.expect)
if (result.ok) {
  const artifact = result.artifact
  const planPath = artifact.artifacts?.plan ?? ''
  const promptPath = artifact.artifacts?.prompt ?? ''
  const planBytes = planPath ? readFileSync(planPath) : Buffer.alloc(0)
  const promptBytes = promptPath ? readFileSync(promptPath) : Buffer.alloc(0)
  const out = {
    ok: true,
    status: artifact.status,
    runId: artifact.runId,
    workspaceId: artifact.workspaceId ?? null,
    projectId: artifact.projectId ?? null,
    planHash: createHash('sha256').update(planBytes).digest('hex'),
    promptHash: createHash('sha256').update(promptBytes).digest('hex'),
    promptHasCrlf: promptBytes.includes(Buffer.from('\r\n', 'utf8')),
    planHasCrlf: planBytes.includes(Buffer.from('\r\n', 'utf8')),
  }
  if (payload.staleness) {
    const stale = draftResultStaleness({
      projectPath: payload.staleness.projectPath,
      generatedAt: payload.staleness.generatedAt ?? artifact.generatedAt,
      inputHashRecorded: artifact.inputHashes?.[payload.staleness.projectName] ?? payload.staleness.inputHashRecorded,
      inputFiles: artifact.inputFilesByProject?.[payload.staleness.projectName],
      storedPolicy: payload.staleness.useManifestSettings ? (artifact.settings ?? undefined) : payload.staleness.storedPolicy,
      currentPolicy: payload.staleness.currentPolicy,
    })
    out.staleness = stale
  }
  process.stdout.write(JSON.stringify(out))
} else {
  process.stdout.write(JSON.stringify({ ok: false, code: result.code, detail: result.detail, text: draftReadFailureText(result.code, result.detail).slice(0, 30) }))
}
