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
  if (!run || !config) return { stale: true, reason: 'STATE_UNREADABLE: run.json or run-config.json missing' }
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
