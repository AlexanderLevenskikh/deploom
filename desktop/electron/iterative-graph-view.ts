import { readFileSync } from 'node:fs'
import { join } from 'node:path'

export type IterativeGraphView = { runId: string; checkpointId: string; accepted: Record<string, string>; rejected: Array<{ id: string; packages: string[]; reason: string }> }
const read = (path: string): Record<string, any> | undefined => { try { return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, '')) } catch { return undefined } }
// Display projection only: acceptance remains owned by the migration verifier.
export function readIterativeGraphView(runDir: string): IterativeGraphView | undefined {
  const run = read(join(runDir, 'run.json'))
  if (typeof run?.runId !== 'string' || !run.runId || !/^C\d+$/.test(run.activeCheckpointId)) return
  const chain: Record<string, any>[] = []
  const seen = new Set<string>()
  let id: string | undefined = run.activeCheckpointId
  while (id) {
    if (!/^C\d+$/.test(id) || seen.has(id)) return
    seen.add(id)
    const checkpoint = read(join(runDir, 'checkpoints', id + '.json'))
    if (!checkpoint || checkpoint.checkpointId !== id || checkpoint.status !== 'VERIFIED' || checkpoint.verification?.status !== 'passed' || !checkpoint.fullAssignment || typeof checkpoint.fullAssignment !== 'object' || Array.isArray(checkpoint.fullAssignment)) return
    chain.push(checkpoint)
    id = checkpoint.parentCheckpointId || undefined
  }
  if (chain.at(-1)?.checkpointId !== 'C0') return
  const initial = chain.at(-1)!.fullAssignment
  const accepted: Record<string, string> = {}
  for (const [name, version] of Object.entries(chain[0].fullAssignment)) if (typeof version === 'string' && initial[name] !== version) accepted[name] = version
  const ledger = read(join(runDir, 'ledger.json'))
  const rejected = (Array.isArray(ledger?.deferrals) ? ledger.deferrals : []).filter((entry: any) => typeof entry.candidateId === 'string' && seen.has(entry.baseCheckpointId) && Array.isArray(entry.packages)).map((entry: any) => ({ id: entry.candidateId, packages: entry.packages.filter((name: unknown): name is string => typeof name === 'string'), reason: typeof entry.reason === 'string' ? entry.reason : '' }))
  const latest = read(join(runDir, 'run.json'))
  if (latest?.runId !== run.runId || latest?.activeCheckpointId !== run.activeCheckpointId) return
  return { runId: run.runId, checkpointId: run.activeCheckpointId, accepted, rejected }
}
