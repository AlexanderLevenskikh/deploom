// Regression checks for real filesystem/Git/Python discovery boundaries.
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { canonicalLockfileForDir, discoverProjectDirectoriesBounded, selectionFromReport, deepScanSuggestion, serializeDiscoveryReport } from '../dist-electron/project-discovery.js'
import { readProjectManifest } from '../dist-electron/project-manifest.js'
const root = mkdtempSync(join(tmpdir(), 'deploom-discovery-boundaries-'))
const source = fileURLToPath(new URL('../../', import.meta.url))
const physical = (path) => realpathSync.native(path)
const python = process.platform === 'win32' ? 'python' : 'python3'
function write(base, path, data) {
  const file = join(base, path); mkdirSync(dirname(file), { recursive: true })
  writeFileSync(file, typeof data === 'string' ? data : JSON.stringify(data))
}
function make(name, patterns, bom = false) {
  const repo = join(root, name); mkdirSync(repo)
  write(repo, 'package.json', (bom ? '\uFEFF' : '') + JSON.stringify({name, private: true, workspaces: patterns}))
  write(repo, 'package-lock.json', { lockfileVersion: 3, packages: {} })
  execFileSync('git', ['init', '-q', repo])
  return repo
}
function topology(path) {
  const code = 'import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from project_topology import resolve_project_topology;t=resolve_project_topology(Path(sys.argv[2]));print(json.dumps(t.as_dict()))'
  return JSON.parse(execFileSync(python, ['-c', code, source, path], {encoding:'utf8'}))
}
try {
  for (const [label, patterns, member, bom] of [
    ['bom', ['packages/*'], 'packages/app', true],
    ['dot', ['./packages/*'], 'packages/app'],
    ['class', ['packages/[!x]*'], 'packages/app'],
    ['brace', ['{apps,packages}/*'], 'apps/app'],
    ['globstar', ['packages/**/app'], 'packages/group/app'],
    ['zero-globstar', ['packages/**/app'], 'packages/app'],
    ['exclude', ['packages/*', '!packages/excluded'], 'packages/app'],
  ]) {
    const repo = make(label, patterns, bom); const path = join(repo, member)
    write(path, 'package.json', {name: 'fixture-app'})
    const canonical = topology(path)
    assert.equal(physical(canonicalLockfileForDir(path, repo).path), physical(canonical.canonicalLockfile), label)
    assert.equal(physical(canonicalLockfileForDir(path, join(repo, 'packages')).path), physical(canonical.canonicalLockfile), label+' subtree')
    assert.equal(physical((await discoverProjectDirectoriesBounded(repo)).candidates[0].lockfile.path), physical(canonical.canonicalLockfile), label+' scan')
  }
  const nested = make('nested', ['packages/*']); const child = join(nested, 'packages/app')
  write(child, 'package.json', {name:'nested-app'}); execFileSync('git',['init','-q',child])
  assert.equal(canonicalLockfileForDir(child,nested).kind,'none','never cross a nested Git boundary')
  const invalid = join(root,'invalid'); write(invalid,'front/package.json','{broken')
  const invalidReport = await discoverProjectDirectoriesBounded(invalid)
  assert.equal(invalidReport.candidates.length,0)
  assert.equal(invalidReport.issues[0].code,'PROJECT_PACKAGE_JSON_INVALID')
  assert.throws(()=>readProjectManifest(join(invalid,'front')),/PROJECT_PACKAGE_JSON_INVALID/)
  const missing = await discoverProjectDirectoriesBounded(join(root,'absent'))
  assert.equal(missing.issues[0].code,'PROJECT_DIRECTORY_UNREADABLE')
  assert.ok(missing.truncated)
  assert.equal(serializeDiscoveryReport(missing).issues[0].code,missing.issues[0].code)
  const unsupported = make('unsupported', ['packages/@(app|lib)'])
  write(unsupported,'packages/app/package.json',{name:'app'})
  assert.equal((await discoverProjectDirectoriesBounded(unsupported)).issues[0].code,'PROJECT_WORKSPACE_PATTERN_UNSUPPORTED')
  const partial = join(root,'partial');write(partial,'a/package.json',{name:'a'});write(partial,'z/package.json',{name:'z'})
  const limited = await discoverProjectDirectoriesBounded(partial,5,{maxEntries:2})
  assert.ok(limited.truncated);assert.equal(limited.candidates.length,1)
  assert.equal(selectionFromReport(partial,limited).status,'ambiguous')
  assert.ok(deepScanSuggestion(limited),'all incomplete scans offer retry/extension')
  const deep = join(root,'deep')
  for(let i=0;i<600;i++)mkdirSync(join(deep,'a','b','c','d','e','f','n'+i),{recursive:true})
  let yielded=false;setImmediate(()=>{yielded=true})
  const result=await discoverProjectDirectoriesBounded(deep,5,{maxEntries:20000,timeBudgetMs:2000})
  assert.ok(result.truncated);assert.ok(yielded,'deep traversal must yield below depth cutoff')
  console.log('Discovery boundaries: Python ownership, BOM/./classes/brace/globstar, nested Git, typed errors, partial selection and deep yielding OK')
} finally { rmSync(root,{recursive:true,force:true}) }
