// Draft artifact reader — production read path for run-scoped Draft results.
//
// Pure Node (no Electron imports) so the exact same code that main.ts trusts
// can be loaded by cross-language regression tests: Python writes the Draft
// artifacts, the compiled dist-electron/draft-artifact-reader.js reads them.
//
// The byte contract (T1): Python writes every artifact as UTF-8 with explicit
// LF newlines (no BOM) and records SHA-256 of the raw file bytes in
// result.json#hashes. This reader hashes the raw bytes too, so a natively
// produced Windows Draft (CRLF would have broken the old string-based hash)
// is accepted. Any single-byte corruption is rejected.
//
// The ownership contract (T5): when an expected identity is supplied, missing
// or mismatched workspace/project/run identity, artifacts that point outside
// the run's own draft sibling directory, or hash mismatches all fail with a
// distinguishable code — never a silent undefined.

import { createHash } from 'node:crypto'
import { existsSync, readFileSync, realpathSync, statSync } from 'node:fs'
import { isAbsolute, join, resolve, relative, sep } from 'node:path'

export type DraftStatus = 'DRAFT_READY' | 'DRAFT_PARTIAL'

export type DraftReadFailureCode =
  | 'manifest-missing'
  | 'invalid-json'
  | 'unsupported-schema'
  | 'unsupported-status'
  | 'run-identity'
  | 'workspace-identity'
  | 'project-identity'
  | 'artifact-path'
  | 'hash-mismatch'

export type DraftReadResult =
  | { ok: true; artifact: DraftResultArtifact }
  | { ok: false; code: DraftReadFailureCode; detail: string }

export type DraftResultArtifact = {
  schemaVersion: number
  status: DraftStatus
  runId: string
  workspaceId?: string
  projectId?: string
  mode?: string
  generatedAt?: string
  elapsedMs?: number
  deadline?: { deadlineSeconds?: number; remainingMs?: number; phase?: string }
  policyHash?: string
  settings?: Record<string, unknown>
  summary?: string
  partialReason?: string | null
  verificationStatus?: string
  authority?: string
  compatibility?: string
  metadata?: {
    total?: number
    unknown?: number
    unknownPackages?: string[]
    /** R11: Draft scan lifecycle — how much was actually processed before a
     * deadline/error vs. waiting, interrupted, failed or missing OSV. */
    processed?: number
    processedTotal?: number
    pending?: number
    interrupted?: number
    registryFailed?: number
    osvUnknown?: number
    metadataKnown?: number
    metadataTotal?: number
    securityKnown?: number
    securityTotal?: number
    securityUnknown?: number
    /** R12: honest causes for a goal shortfall — an explicit candidate limit
     * (candidateTruncated) is never the same as a proven no-target, and the
     * projected figures are post-plan (recomputed from final planned targets). */
    noTarget?: number
    candidateTruncated?: number
    candidateTruncatedTargetless?: number
    candidateTruncatedProposed?: number
    blocked?: number
    ok?: number
    postPlanLagOk?: number
    postPlanLagOkPct?: number
    /** F1: the policy gate (user's minLagOkPct) and the +5 p.p. planning
     * reserve are SEPARATE required counts and SEPARATE shortfalls; the
     * aggregate postPlanShortfall is the POLICY one, never the reserve. */
    postPlanScopeTotal?: number
    postPlanPolicyRequired?: number
    postPlanReserveRequired?: number
    postPlanPolicyShortfall?: number
    postPlanReserveShortfall?: number
    postPlanShortfall?: number
    /** F3: projected security on the EXACT chosen/kept versions + coverage and
     * the feasibility verdict (feasible | unknown | blocked). */
    postPlanCritical?: number
    postPlanHigh?: number
    postPlanSecurityKnown?: number
    postPlanSecurityUnknown?: number
    postPlanSecurityTotal?: number
    postPlanGoal?: string
  }
  proposals?: Record<string, unknown>
  /** F2: per-project post-plan numbers (sizes and acceptance policies differ);
   * the renderer reads the SELECTED project from here and falls back to the
   * (single-project) aggregate metadata. */
  perProject?: Record<string, {
    postPlanLagOk?: number
    postPlanLagOkPct?: number
    postPlanScopeTotal?: number
    postPlanPolicyRequired?: number
    postPlanReserveRequired?: number
    postPlanPolicyShortfall?: number
    postPlanReserveShortfall?: number
    postPlanCritical?: number
    postPlanHigh?: number
    postPlanSecurityKnown?: number
    postPlanSecurityUnknown?: number
    postPlanSecurityTotal?: number
    postPlanGoal?: string
    noTarget?: number
    candidateTruncated?: number
    candidateTruncatedTargetless?: number
    candidateTruncatedProposed?: number
    blocked?: number
    ok?: number
    proposed?: number
    scopeTotal?: number
  }>
  artifacts?: { manifest: string; plan: string; prompt: string; summary: string }
  hashes?: { plan: string; prompt: string }
  inputHashes?: Record<string, string>
  /** G3: per-project resolved planner-input files (re-hashed for staleness). */
  inputFilesByProject?: Record<string, Array<{ name: string; hash?: string }>>
  projects?: unknown[]
}

const DRAFT_SIBLING_NAMES = { plan: 'plan.json', prompt: 'prompt.md', summary: 'summary.md' } as const

export function sha256Bytes(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex')
}

// Draft input identity, byte-identical to the Python writer (draft_input_hash):
// SHA-256 over the ordered existing files below as name\0bytes\0. A hash
// difference also catches deletions and additions, not just content edits (T5).
// F4: the fingerprint is the FULL planner input set, not only the lockfiles:
// project/local settings and the dashboard policy (exclusions, per-package lag
// policy) all influence what the plan contains, so changing any of them makes
// a previously generated Draft stale. The cryptographic framing and file order
// MUST stay in lockstep with dependency_live_roadmap_generator.draft_input_hash.
export const DRAFT_INPUT_FILENAMES = [
  'package.json', 'yarn.lock', 'pnpm-lock.yaml', 'package-lock.json',
  'npm-shrinkwrap.json', 'package-manager.json',
  '.dependency-roadmap/settings.project.json',
  '.dependency-roadmap/settings.local.json',
  '.dependency-roadmap/state/dashboard-state.json',
] as const

export function draftInputHashForProject(projectPath: string): string {
  const hash = createHash('sha256')
  for (const filename of DRAFT_INPUT_FILENAMES) {
    const input = join(projectPath, filename)
    if (!existsSync(input)) continue
    try {
      hash.update(Buffer.from(filename, 'utf8'))
      hash.update(Buffer.from([0]))
      hash.update(readFileSync(input))
      hash.update(Buffer.from([0]))
    } catch {
      continue
    }
  }
  return hash.digest('hex')
}

// G3: hash the ACTUAL resolved input files the planner recorded in the
// manifest (inputFilesByProject). Byte-lockstep framing with the Python side
// (draft_input_hash): name\0bytes\0 in declaration order. Names starting with
// './' are project-relative (workspace relocation keeps their identity stable);
// absolute names (workspace-root settings under a NESTED project layout) are
// hashed as-is. This makes a nested project bind its Draft to the root
// settings file, so editing those settings marks the run stale.
export function draftInputHashFromFiles(projectPath: string, files: ReadonlyArray<{ name: string; hash?: string }>): string {
  const hash = createHash('sha256')
  for (const entry of files) {
    const filename = String(entry?.name ?? '').trim()
    if (!filename) continue
    const input = filename.startsWith('./') ? join(projectPath, filename.slice(2)) : isAbsolute(filename) ? filename : join(projectPath, filename)
    if (!existsSync(input)) continue
    try {
      hash.update(Buffer.from(filename, 'utf8'))
      hash.update(Buffer.from([0]))
      hash.update(readFileSync(input))
      hash.update(Buffer.from([0]))
    } catch {
      continue
    }
  }
  return hash.digest('hex')
}

export function baselinePoliciesKey(policies: Record<string, unknown> | undefined | null): string {
  const entries = Object.entries(policies ?? {})
    .filter(([, policy]) => policy === 'keep-current' || policy === 'required')
    .sort(([a], [b]) => a.localeCompare(b))
  return JSON.stringify(entries)
}

export type DraftStalenessFacts = {
  projectPath: string
  generatedAt?: string
  /** manifests inputHashes[project] captured at read time (T5). */
  inputHashRecorded?: string
  /** G3: the resolved planner-input file names (manifest.inputFilesByProject),
   * re-hashed instead of the fixed DRAFT_INPUT_FILENAMES so a nested project
   * whose settings live outside its own directory stays bound to them. */
  inputFiles?: { name: string; hash?: string }[]
  /** The policy the run was accepted under (manifest.settings). */
  storedPolicy?: {
    targetLevel?: unknown
    minLagOkPct?: unknown
    lagPolicyMonths?: unknown
    intentJson?: unknown
    /** F1/F4: the FULL acceptance policy as captured by the planner. */
    acceptancePolicyJson?: unknown
  }
  /** The policy the user would run now. */
  currentPolicy?: {
    targetLevel?: unknown
    minLagOkPct?: unknown
    lagPolicyMonths?: unknown
    policies?: Record<string, unknown>
    /** F1: the merged acceptance policy the desktop currently enforces. */
    acceptancePolicy?: Record<string, unknown>
  }
}

export type DraftStaleness = { stale: boolean; reason?: string }

// Numeric acceptance-policy dimensions are compared per-dimension and only
// when the CURRENT policy communicates them: an absent current value is not
// evidence of a change (older clients/tests send only level+percentage), while
// a genuinely changed limit still invalidates the run (F4 acceptance: High,
// lagMonths and exclusions must show up as stale, not only target/%).
function policyDimensionValue(raw: unknown): number | undefined {
  const parsed = Number(raw)
  return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : undefined
}

function acceptancePolicyLimitsDiffer(stored: Record<string, unknown>, current: Record<string, unknown>): boolean {
  for (const dim of ['lagPolicyMonths', 'maxKnownHigh', 'maxKnownModerate', 'maxKnownLow']) {
    const currentValue = policyDimensionValue(current[dim])
    if (currentValue === undefined) continue
    const storedValue = policyDimensionValue(stored[dim])
    if (storedValue !== undefined && storedValue !== currentValue) return true
  }
  return false
}

function storedPolicyFromSnapshot(stored: Record<string, unknown> | undefined): Record<string, unknown> | undefined {
  if (!stored) return undefined
  const result: Record<string, unknown> = {}
  // F1: the planner pins the full policy in settings.acceptancePolicyJson; it
  // is the authoritative goal. G3: the TOP-LEVEL intentJson is a separate
  // snapshot (policies: keep-current/required) that lives NEXT TO the
  // acceptance policy -- returning only the acceptancePolicy object used to
  // discard it, silently skipping the keep-current/required staleness check.
  // Merge both so neither snapshot is lost.
  const acceptancePolicyJson = typeof stored.acceptancePolicyJson === 'string' ? stored.acceptancePolicyJson.trim() : ''
  let parsedPolicy: Record<string, unknown> | undefined
  if (acceptancePolicyJson) {
    try {
      const parsed = JSON.parse(acceptancePolicyJson) as unknown
      if (parsed && typeof parsed === 'object') parsedPolicy = parsed as Record<string, unknown>
    } catch {
      // unparseable snapshot: fall back to the flat fields below
    }
  }
  if (parsedPolicy) Object.assign(result, parsedPolicy)
  if (stored.intentJson !== undefined) result.intentJson = stored.intentJson
  // Legacy manifests without an acceptancePolicyJson keep the flat fields.
  if (result.targetLevel === undefined) Object.assign(result, stored)
  return result
}

// A Draft is stale when it no longer describes the project as it is today:
//  - generatedAt is missing/invalid;
//  - any planner input changed since the run — compared BY CONTENT via the
//    recorded input hash (deletions and additions are caught too);
//  - the input identity is missing entirely (F4: an unbound result is
//    UNCHECKED, never fresh);
//  - the target policy (level / minimum-lag percentage / lag months / numeric
//    C-H-M-L limits / intent policies) differs from the run's.
// The historical result may still be opened with an explicit reason, but it is
// never presented as fresh (T5).
export function draftResultStaleness(facts: DraftStalenessFacts): DraftStaleness {
  const reasons: string[] = []
  if (!Number.isFinite(Date.parse(facts.generatedAt ?? ''))) {
    reasons.push('в manifest нет корректной даты генерации')
  }

  const recorded = facts.inputHashRecorded
  if (recorded) {
    let current = ''
    try {
      // G3: when the manifest records the resolved input file list, re-hash
      // exactly those files (project-relative and absolute) instead of the
      // fixed relative set, so a nested project's root settings changes are
      // detected. Legacy manifests without the list keep the fixed set.
      current = facts.inputFiles && facts.inputFiles.length
        ? draftInputHashFromFiles(facts.projectPath, facts.inputFiles)
        : draftInputHashForProject(facts.projectPath)
    } catch {
      current = ''
    }
    if (current !== recorded) {
      reasons.push('package.json/lockfile/settings изменились с момента генерации (включая удаление/добавление)')
    }
  } else {
    // F4: a run that never recorded its input identity can never prove it
    // still describes the project, so it is unchecked, not fresh.
    reasons.push('проект не привязан к входным файлам (нет input-идентичности)')
  }

  const stored = storedPolicyFromSnapshot(facts.storedPolicy as Record<string, unknown> | undefined)
  const current = facts.currentPolicy
  if (stored && current) {
    const currentMerged = (current.acceptancePolicy && typeof current.acceptancePolicy === 'object'
      ? current.acceptancePolicy
      : current) as Record<string, unknown>
    const storedTarget = String(stored.targetLevel ?? 'yellow')
    const currentTarget = String(currentMerged.targetLevel ?? 'yellow')
    const storedPct = Number(String(stored.minLagOkPct ?? '80'))
    const currentPct = Number(currentMerged.minLagOkPct ?? 80)
    let storedIntentKey = ''
    if (typeof stored.intentJson === 'string' && stored.intentJson) {
      try {
        const parsed = JSON.parse(stored.intentJson) as { policies?: unknown }
        if (parsed && typeof parsed === 'object') {
          storedIntentKey = baselinePoliciesKey(
            parsed.policies && typeof parsed.policies === 'object'
              ? parsed.policies as Record<string, unknown>
              : {},
          )
        }
      } catch {
        // unparseable stored intent: fall through, hash check already ran
      }
    }
    // F4: the whole acceptance policy (lag months + numeric C/H/M/L limits) is
    // part of the staleness decision, not only the level and the percentage.
    // G1: the numeric dimensions are compared per-dimension and only when the
    // CURRENT policy communicates them -- an absent current value is not
    // evidence of a change (older clients/tests send only level+percentage), so
    // a run that was accepted under the extended policy surface stays fresh for
    // them while a genuinely changed limit still invalidates the run.
    if (storedTarget !== currentTarget || (Number.isFinite(storedPct) && storedPct !== currentPct)) {
      reasons.push('цель/процент актуальности изменились с момента генерации')
    }
    if (acceptancePolicyLimitsDiffer(stored, currentMerged)) {
      reasons.push('лимиты acceptance-политики (High/Moderate/Low/lag-месяцы) изменились с момента генерации')
    }
    if (storedIntentKey && storedIntentKey !== baselinePoliciesKey(current.policies)) {
      reasons.push('политики keep-current/required изменились с момента генерации')
    }
  }

  return { stale: reasons.length > 0, reason: reasons.join('; ') }
}

export function artifactSafeSegment(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, '-').slice(0, 120) || 'run'
}

export function draftArtifactsRoot(workspacePath: string): string {
  return join(workspacePath, '.dependency-roadmap', 'artifacts')
}

export function draftManifestPath(workspacePath: string, runId: string): string {
  return join(draftArtifactsRoot(workspacePath), 'runs', artifactSafeSegment(runId), 'draft', 'result.json')
}

export function draftRunDir(workspacePath: string, runId: string): string {
  return join(draftArtifactsRoot(workspacePath), 'runs', artifactSafeSegment(runId), 'draft')
}

// realpathSync.native resolves on Windows with GetFinalPathNameByHandle, which
// expands 8.3 short-name components (e.g. a ~1-suffixed user-folder alias) that
// the JS fallback leaves untouched.
const realpathResolve = realpathSync.native as unknown as (p: string) => string

function canonicalPath(value: string): string {
  try {
    return realpathResolve(value)
  } catch {
    return resolve(value)
  }
}

function isInsideStrict(candidate: string, root: string): boolean {
  const rootResolved = canonicalPath(root)
  const candidateResolved = canonicalPath(candidate)
  if (candidateResolved === rootResolved) return false
  const rel = relative(rootResolved, candidateResolved)
  return rel !== '' && !rel.startsWith('..') && !isAbsolute(rel) && !rel.split(sep).includes('..')
}

/** Read and validate a run-scoped Draft manifest and its sibling artifacts. */
export function readDraftResultArtifact(
  workspacePath: string,
  runId: string,
  expect?: { workspaceId?: string; projectId?: string },
): DraftReadResult {
  const manifestPath = draftManifestPath(workspacePath, runId)
  if (!existsSync(manifestPath)) return { ok: false, code: 'manifest-missing', detail: manifestPath }

  let parsed: Partial<DraftResultArtifact>
  try {
    parsed = JSON.parse(readFileSync(manifestPath, 'utf8')) as Partial<DraftResultArtifact>
  } catch {
    return { ok: false, code: 'invalid-json', detail: manifestPath }
  }
  if (typeof parsed !== 'object' || parsed === null) return { ok: false, code: 'invalid-json', detail: 'manifest is not an object' }
  if (parsed.schemaVersion !== 1) return { ok: false, code: 'unsupported-schema', detail: `schemaVersion=${JSON.stringify(parsed.schemaVersion)}` }
  if (parsed.status !== 'DRAFT_READY' && parsed.status !== 'DRAFT_PARTIAL') {
    return { ok: false, code: 'unsupported-status', detail: `status=${JSON.stringify(parsed.status)}` }
  }
  if (typeof parsed.runId !== 'string' || !parsed.runId) return { ok: false, code: 'run-identity', detail: 'runId is empty' }
  if (parsed.runId !== runId) return { ok: false, code: 'run-identity', detail: `runId=${parsed.runId} expected=${runId}` }
  if (expect) {
    if (expect.workspaceId !== undefined && parsed.workspaceId !== expect.workspaceId) {
      return { ok: false, code: 'workspace-identity', detail: `workspaceId=${JSON.stringify(parsed.workspaceId)} expected=${expect.workspaceId}` }
    }
    if (expect.projectId !== undefined && parsed.projectId !== expect.projectId) {
      return { ok: false, code: 'project-identity', detail: `projectId=${JSON.stringify(parsed.projectId)} expected=${expect.projectId}` }
    }
  }

  const artifacts = parsed.artifacts
  if (!artifacts || typeof artifacts !== 'object') return { ok: false, code: 'artifact-path', detail: 'artifacts section missing' }
  const runDir = draftRunDir(workspacePath, runId)
  for (const kind of ['plan', 'prompt', 'summary'] as const) {
    const raw = artifacts[kind]
    const expectedName = DRAFT_SIBLING_NAMES[kind]
    if (typeof raw !== 'string' || !raw) return { ok: false, code: 'artifact-path', detail: `${kind} path missing` }
    const expectedPath = join(runDir, expectedName)
    // Canonicalize (realpath resolves 8.3 short names, case and symlinks) so a
    // manifest written via a differently-cased or short-named workspace path
    // still validates against the run directory.
    if (canonicalPath(raw) !== canonicalPath(expectedPath)) {
      return { ok: false, code: 'artifact-path', detail: `${kind} must be the run-sibling ${expectedPath}, got ${raw}` }
    }
    if (!isInsideStrict(raw, runDir)) return { ok: false, code: 'artifact-path', detail: `${kind} escapes run dir: ${raw}` }
    if (!existsSync(raw) || !statSync(raw).isFile()) return { ok: false, code: 'artifact-path', detail: `${kind} not a file: ${raw}` }
  }

  const hashes = parsed.hashes
  if (!hashes || typeof hashes !== 'object') return { ok: false, code: 'hash-mismatch', detail: 'hashes section missing' }
  for (const kind of ['plan', 'prompt'] as const) {
    const rawHash = hashes[kind]
    const rawPath = artifacts[kind]
    if (typeof rawHash !== 'string' || !rawHash || typeof rawPath !== 'string' || !rawPath) {
      return { ok: false, code: 'hash-mismatch', detail: `${kind} hash or path missing` }
    }
    if (sha256Bytes(readFileSync(rawPath)) !== rawHash) {
      return { ok: false, code: 'hash-mismatch', detail: `${kind} bytes do not match manifest hash` }
    }
  }

  return { ok: true, artifact: parsed as DraftResultArtifact }
}

/** User-facing explanation for a failed Draft read (T1: distinct causes). */
export function draftReadFailureText(code: DraftReadFailureCode, detail: string): string {
  switch (code) {
    case 'manifest-missing':
      return `DRAFT_RESULT_MISSING: result.json для запуска не найден (${detail}).`
    case 'invalid-json':
      return `DRAFT_RESULT_INVALID_JSON: manifest не является корректным JSON (${detail}).`
    case 'unsupported-schema':
      return `DRAFT_RESULT_UNSUPPORTED_SCHEMA: неожиданный формат manifest (${detail}).`
    case 'unsupported-status':
      return `DRAFT_RESULT_UNSUPPORTED_STATUS: неожиданный статус результата (${detail}).`
    case 'run-identity':
      return `DRAFT_RESULT_RUN_IDENTITY: запуск не совпадает с ожидаемым (${detail}). Результат не отображается.`
    case 'workspace-identity':
      return `DRAFT_RESULT_WORKSPACE_IDENTITY: workspace не совпадает с ожидаемым (${detail}).`
    case 'project-identity':
      return `DRAFT_RESULT_PROJECT_IDENTITY: проект не совпадает с ожидаемым (${detail}).`
    case 'artifact-path':
      return `DRAFT_RESULT_ARTIFACT_PATH: артефакты принадлежат другому запуску или повреждены (${detail}).`
    case 'hash-mismatch':
      return `DRAFT_RESULT_HASH_MISMATCH: файлы результата не совпадают с зафиксированными в manifest (${detail}).`
    default:
      return `DRAFT_RESULT_UNKNOWN: не удалось прочитать результат (${detail}).`
  }
}
