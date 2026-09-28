import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'

// R6 review P1#1: the Desktop's ISOLATED repair working surface.
//
// The source/config repair agent edits a project's tracked files, so it must
// never run in the ORIGINAL checkout: the generator's source guard rejects
// dirty trees, and a dirty original would block every later authoritative
// Baseline command. Instead the episode gets its own clone of the project at
// the exact source commit the episode was opened against. The clone is
// deliberately made LOCAL-ONLY (its origin remote is removed), so the
// generator treats it as a local provenance source; the repair-capture
// authorization (DEPLOOM_BASELINE_SOURCE_REPAIR=1 + a pinned source commit)
// is the ONLY way a dirty tree is accepted for capture, and it is fail-closed:
// without it the guard still rejects the same tree, and with it the HEAD must
// equal the pinned commit or capture is refused.
//
// All re-verifications of the episode run against this checkout through a
// REPAIR SETTINGS file (a copy of the workspace settings whose project path
// points at the clone) plus the repair-capture environment. The original
// checkout, its refs and its lockfiles are never touched.

export type RepairCheckoutCommandSpec = {
  label: string
  command: string
  args: string[]
  cwd: string
  env?: Record<string, string | undefined>
  [key: string]: unknown
}

function gitSync(args: string[], cwd: string): { code: number; stdout: string; stderr: string } {
  const result = spawnSync('git', args, { cwd, encoding: 'utf8' })
  return { code: result.status ?? -1, stdout: result.stdout ?? '', stderr: result.stderr ?? '' }
}

function assertGit(step: string, result: { code: number; stdout: string; stderr: string }): void {
  if (result.code !== 0) {
    const detail = (result.stderr || result.stdout).trim().slice(-900)
    throw new Error(`BASELINE_REPAIR_CHECKOUT_FAILED: ${step} завершилась с кодом ${result.code}${detail ? `: ${detail}` : ''}`)
  }
}

/** The exact HEAD commit of a repository (the anchor for repair capture). */
export function resolveGitHead(repoPath: string): string {
  const result = gitSync(['rev-parse', 'HEAD'], repoPath)
  if (result.code !== 0) {
    throw new Error(`BASELINE_REPAIR_CHECKOUT_FAILED: не удалось прочитать HEAD проекта (${repoPath}): ${(result.stderr || result.stdout).trim().slice(-500)}`)
  }
  const head = result.stdout.trim()
  if (!/^[0-9a-f]{40}$/i.test(head)) {
    throw new Error(`BASELINE_REPAIR_CHECKOUT_FAILED: HEAD проекта (${repoPath}) не является commit: ${head.slice(0, 64)}`)
  }
  return head.toLowerCase()
}

/**
 * Clone `originalPath` into `targetDir` as a local-only repository on a branch
 * named `sourceBranch` at exactly `sourceCommit`. The clone shares the object
 * store (--shared) but has no remotes, so the generator's source guard takes
 * its LOCAL provenance path; the branch keeps HEAD non-detached, which the
 * guard requires. Returns `targetDir`.
 */
export function createIsolatedRepairCheckout(input: {
  originalPath: string
  sourceBranch: string
  sourceCommit: string
  targetDir: string
}): string {
  const { originalPath, sourceBranch, sourceCommit, targetDir } = input
  if (!sourceBranch.trim()) throw new Error('BASELINE_REPAIR_CHECKOUT_FAILED: sourceBranch пуст')
  if (!/^[0-9a-f]{40}$/i.test(sourceCommit)) {
    throw new Error(`BASELINE_REPAIR_CHECKOUT_FAILED: sourceCommit не является commit: ${sourceCommit.slice(0, 64)}`)
  }
  if (existsSync(targetDir)) rmSync(targetDir, { recursive: true, force: true })
  mkdirSync(dirname(targetDir), { recursive: true })
  assertGit('git clone --shared', gitSync(['clone', '--shared', '--no-checkout', '--quiet', '--', originalPath, targetDir], originalPath))
  // Local-only: removing the clone's origin remote makes the generator take the
  // local provenance path (nothing to fetch / diverge from).
  const remotes = gitSync(['remote'], targetDir)
  if (remotes.code === 0 && remotes.stdout.trim()) {
    const remove = gitSync(['remote', 'remove', 'origin'], targetDir)
    // A clone without an origin is harmless; anything else is a real failure.
    assertGit('git remote remove origin', remove)
  }
  assertGit('git checkout -B sourceBranch', gitSync(['checkout', '-B', sourceBranch, sourceCommit], targetDir))
  return targetDir
}

/**
 * Write the episode's repair settings file: a copy of the workspace settings
 * whose project entry's `path` points at the isolated repair checkout. The
 * generator then runs the authoritative re-verification against the repaired
 * tree instead of the original one.
 *
 * R7 review P1#1 (workspace base): the file MUST live in the SAME directory as
 * the source settings. The generator resolves its workspace base from the
 * settings file's directory -- the special case is only the immediate
 * `.dependency-roadmap` folder -- so a repair settings file placed under
 * `.../.dependency-roadmap/state/` would shift the base one level down and
 * silently change the meaning of EVERY relative path (handoff, history, cache,
 * groups, outputs): the re-verification would write its durable repair handoff
 * somewhere the Desktop never reads. Same directory => same base => both
 * processes share one durable state. This is enforced, not convention: a
 * misdirected target path is a hard error.
 */
export function buildRepairSettingsFile(input: {
  sourceSettingsPath: string
  projectName: string
  repairCheckoutPath: string
  targetPath: string
}): string {
  const { sourceSettingsPath, projectName, repairCheckoutPath, targetPath } = input
  const sourceDir = dirname(sourceSettingsPath)
  const targetDir = dirname(targetPath)
  if (targetDir !== sourceDir) {
    throw new Error(
      `BASELINE_REPAIR_SETTINGS_WORKSPACE_BASE: repair settings должен лежать рядом с исходным settings (${sourceDir}), а не в ${targetDir}; иначе генератор сменит workspace base и сломает durable state paths`,
    )
  }
  let settings: Record<string, unknown>
  try {
    settings = JSON.parse(readFileSync(sourceSettingsPath, 'utf8')) as Record<string, unknown>
  } catch {
    throw new Error(`BASELINE_REPAIR_SETTINGS_UNREADABLE: не удалось прочитать settings (${sourceSettingsPath})`)
  }
  const projects = Array.isArray(settings.projects) ? settings.projects : []
  let remapped = false
  const nextProjects = projects.map((entry) => {
    const record = entry as Record<string, unknown>
    if (String(record.name ?? '') !== projectName) return record
    remapped = true
    return { ...record, path: repairCheckoutPath }
  })
  if (!remapped) {
    throw new Error(`BASELINE_REPAIR_SETTINGS_PROJECT_MISSING: проект ${projectName} не найден в settings (${sourceSettingsPath})`)
  }
  const next = { ...settings, projects: nextProjects }
  mkdirSync(dirname(targetPath), { recursive: true })
  writeFileSync(targetPath, `${JSON.stringify(next, null, 2)}\n`, 'utf8')
  return targetPath
}

/**
 * Build the FRESH authoritative re-verification command after a repair:
 * same baseline command, but its `--project-settings` value is redirected to
 * the repair settings file (the isolated repair checkout), and the approved
 * environment adds DEPLOOM_BASELINE_RESUME=restart, RECOVERY_PROOF_REUSE=0 and
 * the fail-closed repair-capture authorization pinned to the episode's source
 * commit. A repair changed the source identity, so this is never a checkpoint
 * resume.
 */
export function applyRepairReVerification(
  spec: RepairCheckoutCommandSpec,
  repair: { settingsPath: string; sourceCommit: string },
): RepairCheckoutCommandSpec {
  let swapped = false
  const args: string[] = []
  for (let index = 0; index < spec.args.length; index += 1) {
    if (spec.args[index] === '--project-settings' && index + 1 < spec.args.length) {
      args.push('--project-settings', repair.settingsPath)
      swapped = true
      index += 1
    } else {
      args.push(spec.args[index])
    }
  }
  if (!swapped) {
    throw new Error('BASELINE_REPAIR_REVERIFY_SETTINGS_MISSING: baseline command не содержит --project-settings для перенаправления на repair checkout')
  }
  return {
    ...spec,
    label: `${spec.label} (повторная авторитетная верификация после repair)`,
    args,
    env: {
      ...spec.env,
      DEPLOOM_BASELINE_RESUME: 'restart',
      DEPLOOM_BASELINE_RECOVERY_PROOF_REUSE: '0',
      DEPLOOM_BASELINE_SOURCE_REPAIR: '1',
      DEPLOOM_BASELINE_SOURCE_COMMIT: repair.sourceCommit.toLowerCase(),
    },
  }
}
