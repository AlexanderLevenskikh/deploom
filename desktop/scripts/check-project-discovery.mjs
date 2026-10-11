// Cross-language contract for the bounded project-manifest discovery walk.
//
// The Desktop keeps its pre-flight walk in a single module
// (dist-electron/project-discovery.js). Python `project_topology` remains the
// canonical owner of topology for an actual migration. This check pins the
// Desktop walk against the Python walk on the SAME fixture trees: the candidate
// set and the depth-truncation behaviour must agree, so the renderer never shows
// a chooser that differs from what Python would discover.
//
// Usage (from desktop/): npm run build && npm run check:project-discovery
import { execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH,
  PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS,
  canonicalLockfileForDir,
  discoverProjectDirectories,
  resolveProjectPackageSelection,
} from '../dist-electron/project-discovery.js'

const repoRoot = resolve(fileURLToPath(new URL('../../', import.meta.url)))

function pythonDiscover(rootPath, maxDepth) {
  const code = [
    'import sys, json',
    'from pathlib import Path',
    `sys.path.insert(0, ${JSON.stringify(repoRoot)})`,
    'from project_topology import discover_project_package_directories',
    `root = Path(${JSON.stringify(rootPath)}).resolve()`,
    `candidates = discover_project_package_directories(root, max_depth=${maxDepth})`,
    'print(json.dumps(sorted(p.resolve().relative_to(root).as_posix() for p in candidates)))',
  ].join('; ')
  const stdout = execFileSync('python', ['-c', code], { encoding: 'utf8', timeout: 120_000 }).trim()
  return new Set(JSON.parse(stdout))
}

function expect(condition, message) {
  if (!condition) throw new Error(message)
}

const roots = []
function fixture(name) {
  const root = mkdtempSync(join(tmpdir(), `deploom-disc-${name}-`))
  roots.push(root)
  return root
}

function chain(base, parts) {
  let current = base
  for (const part of parts) { current = join(current, part); mkdirSync(current) }
  return current
}

try {
  const ignored = [...PROJECT_MANIFEST_DISCOVERY_IGNORED_DIRS]
  expect(ignored.includes('.git') && ignored.includes('node_modules'), 'ignored dirs must be pinned in the shared module')

  // 1. A nested package (no root manifest) resolves to a single candidate.
  const single = fixture('single')
  mkdirSync(join(single, 'front'), { recursive: true })
  writeFileSync(join(single, 'front', 'package.json'), '{"name":"front"}')
  const singleReport = discoverProjectDirectories(single)
  expect(singleReport.candidates.length === 1, `single fixture: expected 1 candidate, got ${singleReport.candidates.length}`)
  expect(singleReport.candidates[0].relative === 'front', `single fixture: expected 'front', got ${singleReport.candidates[0].relative}`)
  expect(resolveProjectPackageSelection(single).status === 'single', 'single fixture: selection must be single')

  // 2. A manifest exactly at the depth boundary is found; one level beyond is
  //    reported as a deeper-manifest hint for the scan-extension action.
  const depthRoot = fixture('depth')
  const atLimit = chain(depthRoot, Array.from({ length: PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH }, (_, i) => `lvl${i}`))
  writeFileSync(join(atLimit, 'package.json'), '{"name":"at-limit"}')
  const boundaryReport = discoverProjectDirectories(depthRoot, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH)
  expect(boundaryReport.candidates.length === 1, `depth boundary: expected 1 candidate, got ${boundaryReport.candidates.length}`)
  expect(boundaryReport.candidates[0].depth === PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH, 'depth boundary: candidate must sit at the limit')
  expect(!boundaryReport.truncated && boundaryReport.deeperManifests === 0, 'depth boundary: must not be truncated')

  const deepRoot = fixture('deep')
  const beyond = chain(deepRoot, Array.from({ length: PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH + 1 }, (_, i) => `lvl${i}`))
  writeFileSync(join(beyond, 'package.json'), '{"name":"beyond-limit"}')
  const truncatedReport = discoverProjectDirectories(deepRoot, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH)
  expect(truncatedReport.candidates.length === 0, `truncation: no candidate may come from beyond the limit, got ${truncatedReport.candidates.length}`)
  expect(truncatedReport.truncated === true && truncatedReport.deeperManifests === 1, 'truncation: must report the deeper manifest for the scan-extension hint')
  const deepReport = discoverProjectDirectories(deepRoot, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH + 1)
  expect(deepReport.candidates.length === 1, `deep scan: must find the manifest, got ${deepReport.candidates.length}`)

  // 3. Two peer packages -> ambiguous chooser state.
  const peers = fixture('peers')
  mkdirSync(join(peers, 'front'), { recursive: true })
  mkdirSync(join(peers, 'back'), { recursive: true })
  writeFileSync(join(peers, 'front', 'package.json'), '{"name":"front"}')
  writeFileSync(join(peers, 'back', 'package.json'), '{"name":"back"}')
  expect(discoverProjectDirectories(peers).candidates.length === 2, 'peers fixture: expected 2 candidates')
  expect(resolveProjectPackageSelection(peers).status === 'ambiguous', 'peers fixture: selection must be ambiguous')

  // 4. Workspace root with a shared root lockfile: both members must resolve to
  //    the root package-lock.json for grouping, even though their own dirs have none.
  const workspace = fixture('workspace')
  writeFileSync(join(workspace, 'package.json'), '{"name":"root","workspaces":["packages/*"]}')
  writeFileSync(join(workspace, 'package-lock.json'), '{"lockfileVersion":3}')
  mkdirSync(join(workspace, 'packages', 'app'), { recursive: true })
  mkdirSync(join(workspace, 'packages', 'lib'), { recursive: true })
  writeFileSync(join(workspace, 'packages', 'app', 'package.json'), '{"name":"app"}')
  writeFileSync(join(workspace, 'packages', 'lib', 'package.json'), '{"name":"lib"}')
  const workspaceReport = discoverProjectDirectories(workspace)
  expect(workspaceReport.candidates.length === 2, `workspace fixture: expected 2 candidates, got ${workspaceReport.candidates.length}`)
  expect(workspaceReport.candidates.every((candidate) => candidate.lockfile.kind === 'npm'), 'workspace fixture: both members must group under the root npm lock')
  expect(canonicalLockfileForDir(join(workspace, 'packages', 'app'), workspace).kind === 'npm', 'workspace fixture: member must see the root package-lock.json')

  // 5. shrinkwrap wins over package-lock at the same level, per npm semantics.
  const shrinkwrap = fixture('shrinkwrap')
  writeFileSync(join(shrinkwrap, 'package.json'), '{"name":"swr"}')
  writeFileSync(join(shrinkwrap, 'package-lock.json'), '{"lockfileVersion":3}')
  writeFileSync(join(shrinkwrap, 'npm-shrinkwrap.json'), '{"lockfileVersion":3}')
  expect(canonicalLockfileForDir(shrinkwrap, shrinkwrap).kind === 'npm-shrinkwrap', 'shrinkwrap fixture: npm-shrinkwrap must win over package-lock')

  // 6. Ignored payload dirs must never become candidates.
  const noise = fixture('noise')
  mkdirSync(join(noise, 'front'), { recursive: true })
  writeFileSync(join(noise, 'front', 'package.json'), '{"name":"front"}')
  for (const dir of ['node_modules/x', 'dist/a', 'build/b', '.git', '.turbo/c']) {
    mkdirSync(join(noise, dir), { recursive: true })
    writeFileSync(join(noise, dir, 'package.json'), '{"name":"payload"}')
  }
  const noiseReport = discoverProjectDirectories(noise)
  expect(noiseReport.candidates.length === 1 && noiseReport.candidates[0].relative === 'front', `noise fixture: payload dirs must be pruned, got ${noiseReport.candidates.map((c) => c.relative)}`)

  // 7. Cross-language agreement on the candidate set and depth truncation.
  const cases = [
    ['single', single, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH],
    ['depth', depthRoot, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH],
    ['deep', deepRoot, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH],
    ['deep+1', deepRoot, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH + 1],
    ['peers', peers, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH],
    ['workspace', workspace, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH],
    ['noise', noise, PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH],
  ]
  for (const [label, rootPath, maxDepth] of cases) {
    const ours = discoverProjectDirectories(rootPath, maxDepth).candidates.map((candidate) => candidate.relative).sort()
    const theirs = pythonDiscover(rootPath, maxDepth)
    const missing = ours.filter((item) => !theirs.has(item))
    const extras = [...theirs].filter((item) => !ours.includes(item))
    expect(missing.length === 0 && extras.length === 0, `${label}: candidate sets diverge from Python (missing=${missing}, extra=${extras})`)
    console.log(`${label}: candidates ${JSON.stringify(ours)} match Python`)
  }

  console.log(`Discovery contract OK (depth ${PROJECT_MANIFEST_DISCOVERY_MAX_DEPTH}, shared rule module validated against project_topology)`)
} finally {
  for (const root of roots) { try { rmSync(root, { recursive: true, force: true }) } catch { /* best effort */ } }
}
