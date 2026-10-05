import { createHash } from 'node:crypto'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

// Producer-consumer contract for the iterative migration ТЗ (task) artifact.
//
// The Python coordinator (iterative_migration.py) owns planning and writes a
// durable run dir. Its export-task step publishes the dependency-adaptation
// assignment ("ТЗ") under <runDir>/task/<artifactId>/{task.ru.md, task.en.md,
// task-manifest.json} plus a task/current.json pointer (iterative_task.py).
// The Desktop is the CONSUMER of that artifact. It never re-implements the
// prompt builder, never invents an executable target for a deferred package,
// and never rewrites the producer's files: it reads the pointer, the manifest
// and the text, checks identity drift against the alive durable state, and
// hands the bytes to a normal-force clipboard write.
//
// The durable run dir location is a producer-consumer contract: the Desktop
// runs the Python coordinator with --run-dir set to
// <settings base>/.dependency-roadmap/iterative/<project-token>/.

export const ITERATIVE_STATE_RELATIVE_DIR = ['.dependency-roadmap', 'iterative'] as const

export interface IterativeTaskManifest {
  schemaVersion: number
  builder: string
  builderVersion: string
  artifactSource?: string
  artifactId: string
  languages: string[]
  runId: string
  workspaceId: string
  projectId: string
  projectName: string
  baseCheckpointId: string
  targetCheckpointId: string
  policyHash: string
  scopeHash: string
  commandSetHash: string
  exactVersions: Record<string, string>
  actions: Array<{ package: string; from: string; to: string }>
  deferred: Array<{ package: string; current: string; lagPolicyTarget: string; reason: string }>
  completeness: { policySatisfied: boolean; denominator: number; remaining: number }
  verification: { status: string; commands: string[] }
  audit: { status: string; evidenceRef: string }
  sourceSnapshotKey: string
  manifestHash: string
  lockfileHash: string
  resolvedStateKey: string
  contents: Record<string, { contentHash: string; contentBytes: number }>
  contentHash: string
  stale: boolean
  staleReason?: string
  createdAt: string
}

export interface IterativeTaskView {
  manifest: IterativeTaskManifest
  /** language -> markdown body, exactly as published by the producer */
  text: Record<string, string>
}

export type TaskStaleness = { stale: boolean; reason?: string }

/** Field names the task builder needs; empty array means "sufficient" (mirror
 * of iterative_task.run_input_errors) so the Desktop can diagnose a lacking
 * durable state and tell the user to capture a fresh baseline instead of
 * starting one implicitly. */
export function missingTaskExportInput(runDir: string): string[] {
  const missing: string[] = []
  const run = readJson(join(runDir, 'run.json'))
  const config = readJson(join(runDir, 'run-config.json'))
  if (!run) {
    missing.push('run.json')
  } else if (!String(run.runId ?? '').trim()) {
    missing.push('run.runId')
  }
  if (!config) {
    missing.push('run-config.json')
  } else {
    for (const key of ['projectName', 'projectDir', 'targets', 'policyHash']) {
      if (!(key in config)) missing.push(`run-config.${key}`)
    }
  }
  if (!existsSync(join(runDir, 'checkpoints'))) {
    missing.push('checkpoints/*')
  } else {
    const activeId = String(run?.activeCheckpointId ?? '')
    const checkpoints = readCheckpointIds(runDir)
    if (!activeId) missing.push('run.activeCheckpointId')
    else if (!checkpoints.includes(activeId)) missing.push(`checkpoint:${activeId}`)
  }
  return missing
}

/** Consumer-side identity guard. A task is usable for a human ONLY while its
 * pointer still matches the alive durable state (same run, same active
 * checkpoint, same policy). Drift means a newer state exists that the task
 * does not describe; dispatching a drifted task would hand over stale exact
 * versions. When the manifest itself is marked stale by the producer, that
 * wins. */
export function taskStaleness(runDir: string): TaskStaleness {
  const task = currentTask(runDir)
  if (!task) return { stale: true, reason: 'NO_TASK: no exportable task under task/' }
  if (task.manifest.stale) {
    return { stale: true, reason: task.manifest.staleReason || 'STALE_MARKED_BY_PRODUCER' }
  }
  const pointer = readJson(join(runDir, 'task', 'current.json'))
  const run = readJson(join(runDir, 'run.json'))
  const config = readJson(join(runDir, 'run-config.json'))
  if (!run || !config) {
    // R9: a legacy-imported artifact (artifactSource=legacy-baseline) is a
    // port of the user's OLD saved Baseline result, built without a fresh
    // run. It stays usable for preview/copy/save while no real run exists;
    // once a real run.json appears the ordinary identity checks below apply
    // and a mismatched legacy task becomes history, not a dispatchable scope.
    if (task.manifest.artifactSource === 'legacy-baseline' && !run) {
      return { stale: false }
    }
    return { stale: true, reason: 'STATE_UNREADABLE: run.json or run-config.json missing' }
  }
  const activeId = String(run.activeCheckpointId ?? '')
  if (activeId && pointer?.targetCheckpointId && activeId !== String(pointer.targetCheckpointId)) {
    return { stale: true, reason: `CHECKPOINT_DRIFT: task targets ${String(pointer.targetCheckpointId)}, durable state is ${activeId}` }
  }
  const policyHash = String(config.policyHash ?? '')
  if (policyHash && pointer?.policyHash && policyHash !== String(pointer.policyHash)) {
    return { stale: true, reason: 'POLICY_DRIFT: policy changed after the export' }
  }
  const runId = String(run.runId ?? '')
  if (runId && pointer?.runId && runId !== String(pointer.runId)) {
    return { stale: true, reason: 'RUN_DRIFT: run identity changed after the export' }
  }
  return { stale: false }
}

/** Read the task the current.json pointer resolves to. Returns undefined when
 * the pointer or the artifact set is missing/incomplete. */
export function currentTask(runDir: string): IterativeTaskView | undefined {
  const pointer = readJson(join(runDir, 'task', 'current.json'))
  if (!pointer) return undefined
  const artifactId = String(pointer.artifactId ?? '')
  if (!artifactId) return undefined
  const base = join(runDir, 'task', artifactId)
  const raw = readJson(join(base, 'task-manifest.json'))
  if (!raw) return undefined
  const manifest = raw as unknown as IterativeTaskManifest
  if (manifest.artifactId !== artifactId) return undefined
  const text: Record<string, string> = {}
  for (const language of manifest.languages ?? []) {
    const bodyPath = join(base, `task.${language}.md`)
    if (!existsSync(bodyPath)) continue
    text[language] = readFileSync(bodyPath, 'utf8')
  }
  if (Object.keys(text).length === 0) return undefined
  return { manifest, text }
}

/** Consumer-side content verification: every published body must hash to the
 * manifest's own per-language contentHash (the producer binds these). A task
 * whose bytes were corrupted or replaced after export is never dispatchable —
 * it could silently hand the agent a scope the manifest did not sign. */
export function taskContentMismatch(task: IterativeTaskView): string | undefined {
  for (const language of Object.keys(task.text)) {
    const expected = task.manifest.contents?.[language]?.contentHash
    if (!expected) return `CONTENT_HASH_MISSING:${language}`
    const actual = createHash('sha256').update(task.text[language], 'utf8').digest('hex')
    if (actual !== expected) return `CONTENT_HASH_MISMATCH:${language}`
  }
  return undefined
}

/** The dispatch guard. Only a task whose manifest identity matches the alive
 * durable state (run / policy / active checkpoint) AND whose published bodies
 * hash to the manifest may seed the agent prompt. Anything else is refused;
 * the repair prompt is exact and self-sufficient from the durable assignment
 * and repair requests, so a refused task never starts an attempt with stale or
 * contradictory text. */
export function taskDispatchable(
  runDir: string,
): { ok: true; task: IterativeTaskView } | { ok: false; reason: string } {
  const staleness = taskStaleness(runDir)
  if (staleness.stale) return { ok: false, reason: staleness.reason ?? 'STALE' }
  const task = currentTask(runDir)
  if (!task) return { ok: false, reason: 'NO_TASK' }
  const mismatch = taskContentMismatch(task)
  if (mismatch) return { ok: false, reason: mismatch }
  return { ok: true, task }
}

/** The task text that may seed a REPAIR prompt.

 * Only the version-neutral BOOTSTRAP repair may attach the checkpoint-built ТЗ:
 * its isolated trial carries the SAME C0 assignment the ТЗ describes, so the
 * two version sets agree. A CANDIDATE repair works in a trial whose exact
 * assignment DIFFERS from the accepted checkpoint the exported ТЗ is built
 * from — attaching that ТЗ would hand the agent two contradictory version sets
 * in one message (the active-checkpoint identity check cannot see the trial
 * combination). A candidate repair therefore gets NO task text; its prompt is
 * exact and self-sufficient from the durable candidate assignment and the open
 * repair requests. */
export function repairPromptTaskText(
  bootstrap: boolean,
  dispatchable: { ok: true; task: IterativeTaskView } | { ok: false; reason: string },
): string {
  if (!bootstrap || !dispatchable.ok) return ''
  return dispatchable.task.text.ru ?? dispatchable.task.text.en ?? ''
}

/** Result of a best-effort export-task refresh: 'ok' produced a fresh
 * artifact; 'input-missing' — the durable state has no exportable bits (the
 * prompt is still self-sufficient from the durable assignment); 'export-failed'
 * — an OPERATIONAL write/generation failure, which must never fall back to old
 * task bytes. */
export type IterativeTaskRefreshResult =
  | { status: 'ok' }
  | { status: 'input-missing'; missing: string[] }
  | { status: 'export-failed'; exitCode: number; stderr: string; timedOut?: boolean }

/** The bytes meant for the OS clipboard: markdown body plus the manifest
 * fingerprint the UI can echo back ("task <runId> <artifactId>, hash …"). */
export function taskCopyPayload(
  task: IterativeTaskView,
  language: string = 'ru',
): { text: string; fingerprint: string } | undefined {
  const text = task.text[language]
  if (text === undefined) return undefined
  const contentHash = task.manifest.contents?.[language]?.contentHash ?? ''
  return { text, fingerprint: `${task.manifest.artifactId}#${contentHash}` }
}

export const MAX_TASK_COPY_BYTES = 512 * 1024

/** A task body beyond the cap is refused before touching the clipboard, so a
 * pathological producer payload can never blow an IPC/UI buffer. */
export function copyPayloadTooLarge(payload: { text: string }): string | undefined {
  const bytes = Buffer.byteLength(payload.text, 'utf8')
  if (bytes > MAX_TASK_COPY_BYTES) return `TASK_TOO_LARGE: ${bytes} bytes exceeds ${MAX_TASK_COPY_BYTES}`
  return undefined
}

/** Invocation shape for the export step the Desktop may run (DI: main.ts wires
 * the real python + bundled script via process-launcher). Nothing here
 * executes anything. */
export function iterativeExportInvocation(
  runDir: string,
  scriptPath: string,
  python: string = 'python',
  language: 'ru' | 'en' | 'both' = 'both',
): { command: string; args: string[] } {
  // --run-dir is a TOP-LEVEL argument of the Python CLI, before the subcommand.
  return { command: python, args: [scriptPath, '--run-dir', runDir, 'export-task', '--language', language] }
}

/** Deterministic per-project token for the run dir; the same sha256/slug scheme
 * the rest of the Desktop uses for per-project durable identity. */
export function iterativeProjectToken(project: string): string {
  const slug = String(project).replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'project'
  const digest = createHash('sha256').update(String(project), 'utf8').digest('hex').slice(0, 12)
  return `${slug}-${digest}`
}

export function iterativeRunDirRelativePath(project: string): string {
  return [...ITERATIVE_STATE_RELATIVE_DIR, iterativeProjectToken(project)].join('/')
}

export function iterativeRunDirPath(workspacePath: string, project: string): string {
  return join(workspacePath, iterativeRunDirRelativePath(project))
}

function readCheckpointIds(runDir: string): string[] {
  const dir = join(runDir, 'checkpoints')
  if (!existsSync(dir)) return []
  const ids: string[] = []
  for (const name of readdirSync(dir)) {
    if (!/^C\d+\.json$/.test(name)) continue
    const item = readJson(join(dir, name))
    if (item && String(item.checkpointId ?? '')) ids.push(String(item.checkpointId))
  }
  return ids
}

function readJson(path: string): Record<string, unknown> | undefined {
  if (!existsSync(path)) return undefined
  try {
    return JSON.parse(readFileSync(path, 'utf8')) as Record<string, unknown>
  } catch {
    return undefined
  }
}
