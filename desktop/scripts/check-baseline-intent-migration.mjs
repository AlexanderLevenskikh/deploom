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
import ts from 'typescript'

const { join } = path
const source = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')

const trans = (text) => ts.transpileModule(text, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const compile = (js, globals, names) => new Function(...Object.keys(globals), js + `\nreturn { ${names.join(', ')} };`)(...Object.values(globals))

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
const normalizeBaselineIntent = (value) => value // normalizer semantics live in check-baseline-intent.mjs
const normalizeBudgetField = (raw) => ({ budgetMinutesExplicit: typeof raw?.budgetMinutes === 'number' })

// The migration machinery (legacy slug -> baseline intent file), plus the
// loader/budget check that must migrate before reading.
const migStart = source.indexOf('function legacyArtifactSlug(')
const migEnd = source.indexOf('function saveBaselineIntent(', migStart)
if (migStart < 0 || migEnd < 0) throw new Error('R2: intent-migration slice not found in main.ts')
const intentGlobals = {
  join, existsSync: fs.existsSync, readFileSync: fs.readFileSync,
  atomicWriteJsonSync, projectArtifactToken, normalizeBaselineIntent, normalizeBudgetField,
}
const intentApi = compile(
  trans(source.slice(migStart, migEnd)),
  intentGlobals,
  ['legacyArtifactSlug', 'legacyBaselineIntentPath', 'migrateLegacyBaselineIntent', 'loadBaselineIntent', 'baselineIntentHasPersistedBudgetMinutes'],
)

// The output-dir fallback (join/existsSync/projectArtifactToken/legacyArtifactSlug).
const outStart = source.indexOf('function baselineProjectOutputDir(')
const outEnd = source.indexOf('function snapshotProjectArtifacts(', outStart)
if (outStart < 0 || outEnd < 0) throw new Error('R2: baselineProjectOutputDir slice not found in main.ts')
const { baselineProjectOutputDir } = compile(
  trans(source.slice(outStart, outEnd)),
  { join, existsSync: fs.existsSync, projectArtifactToken, legacyArtifactSlug: intentApi.legacyArtifactSlug },
  ['baselineProjectOutputDir'],
)

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

  // 1. A well-formed legacy intent migrates to the hashed path with its
  //    content preserved; the legacy file stays untouched (downgrade-safe).
  const legacyPayload = { schemaVersion: 1, executionMode: 'BACKGROUND', policies: {}, budgetMinutes: 60 }
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

  console.log('Baseline legacy-artifact migration (R2) OK')
} finally {
  fs.rmSync(wsRoot, { recursive: true, force: true })
}
