// R2: the pre-A08 storage identity was a case-preserved slug (no digest). An
// upgrade must keep finding USER DATA saved under those legacy keys:
//   - baseline-intent/<legacy-slug>.json is migrated ONCE, atomically, to the
//     current hashed path when the loader runs and the hashed file is absent;
//     the legacy file is left untouched (downgrade-safe), and a second run is
//     a no-op (never overwrite a current file).
//   - baseline-project-output/<legacy-slug> stays the output root until the
//     project records a NEW baseline under the hashed dir.
// This runs the REAL production functions extracted from main.ts (types erased
// by transpile, projectArtifactToken and dependencies injected), against a
// real temp workspace on disk.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import ts from 'typescript'

const { join } = path
const source = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')

const trans = (text) => ts.transpileModule(text, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const compile = (js, globals, names) => new Function(...Object.keys(globals), js + `\nreturn { ${names.join(', ')} };`)(...Object.values(globals))
const runModule = (text) => {
  const module = { exports: {} }
  const nodeRequire = createRequire(import.meta.url)
  new Function('require', 'module', 'exports', trans(text))(nodeRequire, module, module.exports)
  return module.exports
}

// G1: the migration/load machinery must run against the REAL production
// normalizers (loader semantics decide what "well formed" means), not the
// identity stubs the earlier N1/F3 harness used.
const { mergeTargetPolicy } = runModule(fs.readFileSync(new URL('../electron/acceptance-policy.ts', import.meta.url), 'utf8'))
const { normalizeBudgetField } = runModule(fs.readFileSync(new URL('../electron/baseline-intent.ts', import.meta.url), 'utf8'))
const normStart = source.indexOf('function normalizeBaselineIntent(')
const normEnd = source.indexOf('function projectArtifactToken(', normStart)
if (normStart < 0 || normEnd < 0) throw new Error('G1: normalizeBaselineIntent slice not found in main.ts')
const { normalizeBaselineIntent } = compile(
  trans(source.slice(normStart, normEnd)),
  { normalizeBudgetField, mergeTargetPolicy },
  ['normalizeBaselineIntent'],
)

// Real production projectArtifactToken (A08 hashed identity).
const tokenSliceStart = source.indexOf('function projectArtifactToken(')
const tokenSliceEnd = source.indexOf('function legacyArtifactSlug(', tokenSliceStart)
if (tokenSliceStart < 0 || tokenSliceEnd < 0) throw new Error('R2: projectArtifactToken slice not found in main.ts')
const { projectArtifactToken } = compile(
  trans(source.slice(tokenSliceStart, tokenSliceEnd)),
  { createHash: (await import('node:crypto')).createHash },
  ['projectArtifactToken'],
)

const atomicWriteJsonSync = (file, value) => {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const tmp = file + `.tmp-${process.pid}`
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8')
  fs.renameSync(tmp, file)
}

// The migration machinery (legacy slug -> baseline intent file), plus the
// loader/budget check that must migrate before reading.
const migStart = source.indexOf('function legacyArtifactSlug(')
const migEnd = source.indexOf('function projectInstalledVersion(', migStart)
if (migStart < 0 || migEnd < 0) throw new Error('R2: intent-migration slice not found in main.ts')
// N1: ownership of a legacy file is decided against the workspace's project
// list, so the check injects readProjects over a mutable project set.
let PROJECTS = []
const readProjects = () => PROJECTS
const intentGlobals = {
  join, existsSync: fs.existsSync, readFileSync: fs.readFileSync, realpathSync: fs.realpathSync,
  atomicWriteJsonSync, projectArtifactToken, normalizeBaselineIntent, normalizeBudgetField,
  mergeTargetPolicy, readProjects,
}
const intentApi = compile(
  trans(source.slice(migStart, migEnd)),
  intentGlobals,
  ['legacyArtifactSlug', 'legacyBaselineIntentPath', 'legacySlugProjectCount', 'legacyIntentResolutionNeeded',
   'currentBaselineIntentIsValid', 'migrateLegacyBaselineIntent', 'saveBaselineIntent', 'loadBaselineIntent',
   'baselineIntentHasPersistedBudgetMinutes'],
)

// The output-dir resolvers (join/existsSync/projectArtifactToken/legacyArtifactSlug +
// legacySlugProjectCount over the injected project list).
const outStart = source.indexOf('function baselineProjectOutputWriteDir(')
const outEnd = source.indexOf('function snapshotProjectArtifacts(', outStart)
if (outStart < 0 || outEnd < 0) throw new Error('R2/N1: baselineProjectOutputWriteDir slice not found in main.ts')
const outGlobals = {
  join, existsSync: fs.existsSync, projectArtifactToken, legacyArtifactSlug: intentApi.legacyArtifactSlug,
  legacySlugProjectCount: intentApi.legacySlugProjectCount, readProjects,
}
const outApi = compile(
  trans(source.slice(outStart, outEnd)),
  outGlobals,
  ['baselineProjectOutputDir', 'baselineProjectOutputWriteDir'],
)
const baselineProjectOutputDir = outApi.baselineProjectOutputDir
const baselineProjectOutputWriteDir = outApi.baselineProjectOutputWriteDir
const legacyIntentResolutionNeeded = intentApi.legacyIntentResolutionNeeded

// Both read paths must migrate the legacy file before reading the current one.
const reads = source.slice(migStart, migEnd)
if (reads.split('migrateLegacyBaselineIntent(workspace, projectName)').length - 1 !== 2) {
  throw new Error('R2: loadBaselineIntent AND baselineIntentHasPersistedBudgetMinutes must both migrate first')
}

const wsRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'deploom-r2-'))
const workspace = { path: wsRoot }
const project = 'My Project'
const slash = (p) => path.join(wsRoot, '.dependency-roadmap', 'desktop', p)
try {
  const token = projectArtifactToken(project)
  const legacyIntentPath = slash(path.join('baseline-intent', `${intentApi.legacyArtifactSlug(project)}.json`))
  const currentIntentPath = slash(path.join('baseline-intent', `${token}.json`))
  // F1: unambiguous ownership must be PROVEN (exactly one matching project);
  // an empty project list now blocks migration, so tests 1-6 declare the
  // single unambiguous owner instead of relying on "count 0 <= 1".
  PROJECTS = [{ name: project }]

  // 1. A well-formed full legacy intent (mode, target, lag window, explicit
  //    budget, package policy) migrates to the hashed path with EVERY field
  //    preserved; the legacy file stays untouched (downgrade-safe).
  const legacyPayload = {
    schemaVersion: 1, executionMode: 'BACKGROUND', targetLevel: 'yellow', minLagOkPct: 90,
    lagPolicyMonths: 6, budgetMinutes: 60,
    policies: { 'left-pad': 'required', 'ansi-regex': 'keep-current' },
  }
  fs.mkdirSync(path.dirname(legacyIntentPath), { recursive: true })
  fs.writeFileSync(legacyIntentPath, JSON.stringify(legacyPayload, null, 2), 'utf8')
  intentApi.migrateLegacyBaselineIntent(workspace, project)
  if (!fs.existsSync(currentIntentPath)) throw new Error('R2: the legacy intent must be migrated to the hashed path')
  const migrated = JSON.parse(fs.readFileSync(currentIntentPath, 'utf8'))
  if (JSON.stringify(migrated) !== JSON.stringify(legacyPayload)) {
    throw new Error(`R2: migrated intent content must be preserved; got ${JSON.stringify(migrated)}`)
  }
  if (!fs.existsSync(legacyIntentPath)) throw new Error('R2: the legacy intent file must be left untouched for downgrade safety')

  // 2. Idempotence + never overwrite a CURRENT file: pre-write a different
  //    current file, remove the legacy one, and re-run -- the current one must
  //    stay.
  fs.writeFileSync(currentIntentPath, JSON.stringify({ schemaVersion: 2, policies: { p: 'keep' } }), 'utf8')
  intentApi.migrateLegacyBaselineIntent(workspace, project)
  const kept = JSON.parse(fs.readFileSync(currentIntentPath, 'utf8'))
  if (kept.policies?.p !== 'keep') throw new Error('R2: migration must never overwrite an existing current file')

  // 3. The load path migrates: reading through loadBaselineIntent sees the
  //    legacy intent (current file absent, legacy present).
  const ws2 = path.join(wsRoot, 'ws2')
  fs.mkdirSync(path.join(ws2, '.dependency-roadmap', 'desktop', 'baseline-intent'), { recursive: true })
  fs.writeFileSync(
    path.join(ws2, '.dependency-roadmap', 'desktop', 'baseline-intent', `${intentApi.legacyArtifactSlug(project)}.json`),
    JSON.stringify({ schemaVersion: 1, executionMode: 'FAST', policies: {} }),
    'utf8',
  )
  const loaded = intentApi.loadBaselineIntent({ path: ws2 }, project)
  if (loaded?.executionMode !== 'FAST') throw new Error('R2: loadBaselineIntent must observe the migrated legacy intent')

  // 4. The persisted-budget check migrates and honors an explicit budget.
  const ws2b = path.join(wsRoot, 'ws2b')
  const ws2bLegacy = path.join(ws2b, '.dependency-roadmap', 'desktop', 'baseline-intent',
    `${intentApi.legacyArtifactSlug(project)}.json`)
  fs.mkdirSync(path.dirname(ws2bLegacy), { recursive: true })
  fs.writeFileSync(ws2bLegacy, JSON.stringify({ schemaVersion: 1, executionMode: 'BACKGROUND', policies: {}, budgetMinutes: 60 }), 'utf8')
  if (!intentApi.baselineIntentHasPersistedBudgetMinutes({ path: ws2b }, project)) {
    throw new Error('R2: baselineIntentHasPersistedBudgetMinutes must see the migrated budgetMinutes=60')
  }

  // 5. Malformed legacy payloads are never migrated (defaults instead of
  //    corrupting user data).
  const ws3 = path.join(wsRoot, 'ws3')
  const badLegacy = path.join(ws3, '.dependency-roadmap', 'desktop', 'baseline-intent', `${intentApi.legacyArtifactSlug(project)}.json`)
  fs.mkdirSync(path.dirname(badLegacy), { recursive: true })
  fs.writeFileSync(badLegacy, '[1,2,3]', 'utf8')
  intentApi.migrateLegacyBaselineIntent({ path: ws3 }, project)
  if (fs.existsSync(path.join(ws3, '.dependency-roadmap', 'desktop', 'baseline-intent', `${token}.json`))) {
    throw new Error('R2: a malformed legacy payload must not be migrated')
  }

  // 6. baselineProjectOutputDir falls back to the legacy slug dir only while
  //    the hashed dir does not exist yet, and prefers the hashed dir once it
  //    does.
  const legacyOut = slash(path.join('baseline-project-output', intentApi.legacyArtifactSlug(project)))
  const hashedOut = slash(path.join('baseline-project-output', token))
  fs.mkdirSync(legacyOut, { recursive: true })
  if (baselineProjectOutputDir(workspace, project) !== legacyOut) {
    throw new Error('R2: output dir must fall back to the legacy slug dir while no hashed dir exists')
  }
  fs.mkdirSync(hashedOut, { recursive: true })
  if (baselineProjectOutputDir(workspace, project) !== hashedOut) {
    throw new Error('R2: output dir must prefer the hashed dir once it exists')
  }
  const ws4 = path.join(wsRoot, 'ws4')
  const ws4Hashed = path.join(ws4, '.dependency-roadmap', 'desktop', 'baseline-project-output', token)
  fs.mkdirSync(ws4Hashed, { recursive: true })
  if (baselineProjectOutputDir({ path: ws4 }, project) !== ws4Hashed) {
    throw new Error('R2: a project without any legacy output dir must use the hashed dir')
  }

  // N1-7. An AMBIGUOUS legacy slug must never be assigned to either project.
  // Two distinct projects slugify to the same 'project' key; ONE legacy file
  // cannot be attributed to either, so it is preserved untouched, no hashed
  // file is written for any of them, and a clear resolution reason is surfaced.
  PROJECTS = []
  const wsAmb = path.join(wsRoot, 'ws-amb')
  const projectA = 'Проект один'
  const projectB = 'Проект два'
  const ambSlug = intentApi.legacyArtifactSlug(projectA)
  if (ambSlug !== intentApi.legacyArtifactSlug(projectB)) {
    throw new Error('R2/N1: fixture projects must share the lossy legacy slug')
  }
  const ambLegacy = path.join(wsAmb, '.dependency-roadmap', 'desktop', 'baseline-intent', `${ambSlug}.json`)
  const ambTokenA = path.join(wsAmb, '.dependency-roadmap', 'desktop', 'baseline-intent', `${projectArtifactToken(projectA)}.json`)
  const ambTokenB = path.join(wsAmb, '.dependency-roadmap', 'desktop', 'baseline-intent', `${projectArtifactToken(projectB)}.json`)
  fs.mkdirSync(path.dirname(ambLegacy), { recursive: true })
  fs.writeFileSync(ambLegacy, JSON.stringify({ schemaVersion: 1, executionMode: 'BACKGROUND', policies: { 'shared-pkg': 'required' }, budgetMinutes: 60 }), 'utf8')
  PROJECTS = [{ name: projectA, path: wsAmb }, { name: projectB, path: wsAmb }]
  intentApi.migrateLegacyBaselineIntent({ path: wsAmb }, projectA)
  intentApi.migrateLegacyBaselineIntent({ path: wsAmb }, projectB)
  if (fs.existsSync(ambTokenA) || fs.existsSync(ambTokenB)) {
    throw new Error('R2/N1: an ambiguous legacy intent must never be assigned to either project')
  }
  if (!fs.existsSync(ambLegacy)) throw new Error('R2/N1: the ambiguous legacy file must be preserved untouched')
  if (!legacyIntentResolutionNeeded({ path: wsAmb }, projectA)) {
    throw new Error('R2/N1: an ambiguous legacy intent must surface a resolution reason for BOTH projects')
  }
  if (!legacyIntentResolutionNeeded({ path: wsAmb }, projectB)) {
    throw new Error('R2/N1: an ambiguous legacy intent must surface a resolution reason')
  }
  // With the REAL normalizer a missing current intent loads as DEFAULTS, so a
  // bare "executionMode" probe is not distinguishing; the legacy's own fields
  // (policies['shared-pkg'], budgetMinutes=60) must NOT leak through either.
  const ambiguousLoad = intentApi.loadBaselineIntent({ path: wsAmb }, projectA)
  if (ambiguousLoad?.policies?.['shared-pkg'] === 'required' || ambiguousLoad?.budgetMinutes === 60) {
    throw new Error('R2/N1: an ambiguous legacy intent must NOT silently become one project\'s settings')
  }
  if (intentApi.baselineIntentHasPersistedBudgetMinutes({ path: wsAmb }, projectA)) {
    throw new Error('R2/N1: an ambiguous legacy intent must not report a persisted budget')
  }

  // N1-8. A NEW baseline writes into the project-unique hashed dir; a shared
  // legacy output dir is never a write destination, and the legacy READ
  // fallback is refused while the slug is ambiguous.
  PROJECTS = [{ name: projectA, path: wsAmb }, { name: projectB, path: wsAmb }]
  const ambLegacyOut = path.join(wsAmb, '.dependency-roadmap', 'desktop', 'baseline-project-output', ambSlug)
  fs.mkdirSync(ambLegacyOut, { recursive: true })
  const writeA = baselineProjectOutputWriteDir({ path: wsAmb }, projectA)
  const writeB = baselineProjectOutputWriteDir({ path: wsAmb }, projectB)
  if (writeA === writeB) {
    throw new Error('R2/N1: two projects must never share a baseline output WRITE destination')
  }
  if (writeA === ambLegacyOut || writeB === ambLegacyOut) {
    throw new Error('R2/N1: the legacy output dir must never be a WRITE destination')
  }
  if (!writeA.endsWith(projectArtifactToken(projectA)) || !writeB.endsWith(projectArtifactToken(projectB))) {
    throw new Error('R2/N1: write destinations must be the per-project hashed dirs')
  }
  if (baselineProjectOutputDir({ path: wsAmb }, projectA) !== writeA) {
    throw new Error('R2/N1: an ambiguous legacy output slug must not be used as a read fallback for either project')
  }
  // With ONE unambiguous project the legacy output dir stays a read fallback.
  PROJECTS = []
  const wsUnamb = path.join(wsRoot, 'ws-unamb')
  const unambLegacyOut = path.join(wsUnamb, '.dependency-roadmap', 'desktop', 'baseline-project-output', intentApi.legacyArtifactSlug(project))
  fs.mkdirSync(unambLegacyOut, { recursive: true })
  PROJECTS = [{ name: project, path: wsUnamb }]
  if (baselineProjectOutputDir({ path: wsUnamb }, project) !== unambLegacyOut) {
    throw new Error('R2/N1: an unambiguous project keeps the legacy output read fallback')
  }
  PROJECTS = []

  // F1-9. On a case-insensitive volume 'Demo' and 'demo' are the SAME physical
  // legacy file. Production ownership must decide by FILESYSTEM identity
  // (realpath canonicalizes the on-disk casing), so the two projects are both
  // blocked -- the single legacy file is never assigned to either -- and both
  // get a resolution reason. On a case-SENSITIVE volume the slugs are distinct
  // files and each migrates its own (correct POSIX behaviour); the check
  // adapts to whichever filesystem it runs on.
  const wsCase = path.join(wsRoot, 'ws-case')
  const projDemo = 'Demo'
  const projDemoLower = 'demo'
  const caseSlug = intentApi.legacyArtifactSlug(projDemo)
  const caseLegacy = path.join(wsCase, '.dependency-roadmap', 'desktop', 'baseline-intent', `${caseSlug}.json`)
  fs.mkdirSync(path.dirname(caseLegacy), { recursive: true })
  fs.writeFileSync(caseLegacy, JSON.stringify({ schemaVersion: 1, executionMode: 'BACKGROUND', policies: { x: 'required' }, budgetMinutes: 45 }), 'utf8')
  const caseAlternate = path.join(wsCase, '.dependency-roadmap', 'desktop', 'baseline-intent', `${intentApi.legacyArtifactSlug(projDemoLower)}.json`)
  const caseInsensitive = fs.existsSync(caseAlternate)
  PROJECTS = [{ name: projDemo, path: wsCase }, { name: projDemoLower, path: wsCase }]
  const tokenDemo = path.join(wsCase, '.dependency-roadmap', 'desktop', 'baseline-intent', `${projectArtifactToken(projDemo)}.json`)
  const tokenDemoLower = path.join(wsCase, '.dependency-roadmap', 'desktop', 'baseline-intent', `${projectArtifactToken(projDemoLower)}.json`)
  intentApi.migrateLegacyBaselineIntent({ path: wsCase }, projDemo)
  intentApi.migrateLegacyBaselineIntent({ path: wsCase }, projDemoLower)
  if (caseInsensitive) {
    if (fs.existsSync(tokenDemo) || fs.existsSync(tokenDemoLower)) {
      throw new Error('F1: a case-only collision must never assign the single legacy file to both projects')
    }
    if (!legacyIntentResolutionNeeded({ path: wsCase }, projDemo) || !legacyIntentResolutionNeeded({ path: wsCase }, projDemoLower)) {
      throw new Error('F1: both case-colliding projects must surface a resolution reason on a case-insensitive volume')
    }
  } else {
    if (!fs.existsSync(tokenDemo)) {
      throw new Error('F1: on a case-sensitive volume the exact-case project migrates its own file')
    }
  }
  PROJECTS = []

  // F1-10. A legacy file with NO resolvable owner (empty/unreadable project
  // list) is NOT proof of a single owner: it must never be auto-migrated, must
  // surface an explainable resolution reason, and the legacy OUTPUT dir must
  // not be used as a read fallback either.
  const wsEmpty = path.join(wsRoot, 'ws-empty')
  const emptyLegacy = path.join(wsEmpty, '.dependency-roadmap', 'desktop', 'baseline-intent', `${intentApi.legacyArtifactSlug(project)}.json`)
  fs.mkdirSync(path.dirname(emptyLegacy), { recursive: true })
  fs.writeFileSync(emptyLegacy, JSON.stringify({ schemaVersion: 1, executionMode: 'BACKGROUND', policies: {} }), 'utf8')
  const emptyCurrent = path.join(wsEmpty, '.dependency-roadmap', 'desktop', 'baseline-intent', `${token}.json`)
  const wsEmptyOut = path.join(wsRoot, 'ws-empty-out')
  const emptyLegacyOut = path.join(wsEmptyOut, '.dependency-roadmap', 'desktop', 'baseline-project-output', intentApi.legacyArtifactSlug(project))
  PROJECTS = []
  intentApi.migrateLegacyBaselineIntent({ path: wsEmpty }, project)
  if (fs.existsSync(emptyCurrent)) throw new Error('F1: unknown ownership must not auto-migrate the legacy file')
  if (!legacyIntentResolutionNeeded({ path: wsEmpty }, project)) {
    throw new Error('F1: unknown ownership must surface an explainable resolution reason')
  }
  fs.mkdirSync(emptyLegacyOut, { recursive: true })
  if (baselineProjectOutputDir({ path: wsEmptyOut }, project) !== baselineProjectOutputWriteDir({ path: wsEmptyOut }, project)) {
    throw new Error('F1: unknown ownership must not use the legacy output read fallback')
  }

  // F3-11. The resolution banner ENDS once THIS project holds a valid saved
  // current intent -- the legacy file is deliberately kept, but no longer
  // demands attention. Saving for project A must not clear project B, and a
  // corrupt current file is not a successful restoration.
  PROJECTS = [{ name: projectA, path: wsAmb }, { name: projectB, path: wsAmb }]
  intentApi.saveBaselineIntent({ path: wsAmb }, projectA, { executionMode: 'FAST', targetLevel: 'yellow', policies: { restored: 'required' } })
  if (legacyIntentResolutionNeeded({ path: wsAmb }, projectA)) {
    throw new Error('F3: saving current settings must end the resolution banner for THAT project')
  }
  if (!legacyIntentResolutionNeeded({ path: wsAmb }, projectB)) {
    throw new Error('F3: saving for project A must NOT clear the banner for project B')
  }
  const restoredA = intentApi.loadBaselineIntent({ path: wsAmb }, projectA)
  if (restoredA.policies?.restored !== 'required') {
    throw new Error('F3: the explicitly restored current intent is what the loader applies')
  }
  fs.writeFileSync(ambTokenA, '{corrupt', 'utf8')
  if (!legacyIntentResolutionNeeded({ path: wsAmb }, projectA)) {
    throw new Error('F3: a corrupt current intent must not count as a successful restoration')
  }
  PROJECTS = []

  // G1-12. A semantically BROKEN current intent (valid JSON object whose
  // loader-significant fields the production normalizer would silently drop)
  // is NOT a successful restoration: the warning stays and the loader returns
  // DEFAULTS, never the user's settings. Runs against the REAL normalizer, so
  // "would be dropped" is defined by the production loader, not a stub.
  const wsG1 = path.join(wsRoot, 'ws-g1')
  const g1Legacy = path.join(wsG1, '.dependency-roadmap', 'desktop', 'baseline-intent', `${intentApi.legacyArtifactSlug(projectA)}.json`)
  const g1TokenA = path.join(wsG1, '.dependency-roadmap', 'desktop', 'baseline-intent', `${projectArtifactToken(projectA)}.json`)
  const g1TokenB = path.join(wsG1, '.dependency-roadmap', 'desktop', 'baseline-intent', `${projectArtifactToken(projectB)}.json`)
  fs.mkdirSync(path.dirname(g1Legacy), { recursive: true })
  fs.writeFileSync(g1Legacy, JSON.stringify({ schemaVersion: 1, executionMode: 'BACKGROUND', policies: { 'shared-pkg': 'required' }, budgetMinutes: 60 }), 'utf8')
  PROJECTS = [{ name: projectA, path: wsG1 }, { name: projectB, path: wsG1 }]
  if (!intentApi.legacyIntentResolutionNeeded({ path: wsG1 }, projectA)) {
    throw new Error('G1: missing current intent must keep the resolution warning')
  }
  for (const [label, raw] of [['invalid-json', '{corrupt'], ['array', '[1,2,3]'], ['null', 'null']]) {
    fs.writeFileSync(g1TokenA, raw, 'utf8')
    if (intentApi.currentBaselineIntentIsValid({ path: wsG1 }, projectA)) {
      throw new Error(`G1: ${label} must not be a valid restoration`)
    }
    if (!intentApi.legacyIntentResolutionNeeded({ path: wsG1 }, projectA)) {
      throw new Error(`G1: ${label} must keep the resolution warning`)
    }
  }
  fs.writeFileSync(g1TokenA, JSON.stringify({
    schemaVersion: 2, policies: 'broken', productMode: 'broken', targetLevel: 'broken',
    minLagOkPct: 'broken', acceptancePolicy: 'broken',
  }), 'utf8')
  if (intentApi.currentBaselineIntentIsValid({ path: wsG1 }, projectA)) {
    throw new Error('G1: a semantically broken current intent must not validate')
  }
  if (!intentApi.legacyIntentResolutionNeeded({ path: wsG1 }, projectA)) {
    throw new Error('G1: a semantically broken current intent must keep the resolution warning')
  }
  const brokenLoad = intentApi.loadBaselineIntent({ path: wsG1 }, projectA)
  if (JSON.stringify(brokenLoad.policies ?? {}) !== JSON.stringify({}) ||
      brokenLoad.targetLevel !== 'yellow' || brokenLoad.minLagOkPct !== 80) {
    throw new Error('G1: a semantically broken intent must load as DEFAULTS, not as the user settings')
  }
  fs.writeFileSync(g1TokenA, JSON.stringify({ schemaVersion: 2, policies: { pkg: 'broken' } }), 'utf8')
  if (intentApi.currentBaselineIntentIsValid({ path: wsG1 }, projectA)) {
    throw new Error('G1: an invalid policies value must not validate')
  }
  fs.writeFileSync(g1TokenA, JSON.stringify({ schemaVersion: 1, executionMode: 'BACKGROUND', policies: {} }), 'utf8')
  if (!intentApi.currentBaselineIntentIsValid({ path: wsG1 }, projectA)) {
    throw new Error('G1: a valid legacy v1 intent must validate even with unrelated optional fields missing')
  }
  intentApi.saveBaselineIntent({ path: wsG1 }, projectA, { executionMode: 'FAST', targetLevel: 'yellow', policies: { restored: 'required' } })
  if (!intentApi.currentBaselineIntentIsValid({ path: wsG1 }, projectA)) {
    throw new Error('G1: saveBaselineIntent must persist a semantically valid intent')
  }
  if (intentApi.legacyIntentResolutionNeeded({ path: wsG1 }, projectA)) {
    throw new Error('G1: saving current settings must end the resolution warning for THAT project')
  }
  if (!intentApi.legacyIntentResolutionNeeded({ path: wsG1 }, projectB)) {
    throw new Error('G1: saving for project A must NOT clear the warning for project B')
  }
  if (intentApi.loadBaselineIntent({ path: wsG1 }, projectA).policies?.restored !== 'required') {
    throw new Error('G1: the saved, semantically valid intent is what the loader applies')
  }
  if (fs.existsSync(g1TokenB)) {
    throw new Error('G1: opening/planning must never create another project\'s current intent')
  }
  PROJECTS = []

  console.log('Baseline legacy-artifact migration (R2) OK')
} finally {
  fs.rmSync(wsRoot, { recursive: true, force: true })
}
