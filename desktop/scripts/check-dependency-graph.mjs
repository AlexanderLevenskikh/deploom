import assert from 'node:assert/strict'
import ts from 'typescript'
import { readIterativeGraphView } from '../dist-electron/iterative-graph-view.js'
import { buildDependencyGraphSnapshot } from "../dist-electron/dependency-graph.js"
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

const root = mkdtempSync(join(tmpdir(), "deploom-graph-"))
const manifest = (name, value) => {
  const dir = join(root, "node_modules", ...name.split("/"))
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, "package.json"), JSON.stringify(value))
}

manifest("vite", { version: "4.3.9", dependencies: { react: "^18.0.0" } })
manifest("@vitejs/plugin-react", { version: "5.1.2", peerDependencies: { vite: "^4.2.0 || ^5" }, optionalDependencies: { react: "*" } })
manifest("react", { version: "18.2.0" })

const snapshot = buildDependencyGraphSnapshot(root, "fixture", {
  candidates: [
    { name: "vite", kind: "dev", requestedSpec: "^4.3.9", currentVersion: "4.3.9" },
    { name: "@vitejs/plugin-react", kind: "dev", requestedSpec: "^5.1.2", currentVersion: "5.1.2" },
    { name: "react", kind: "runtime", requestedSpec: "^18.2.0", currentVersion: "18.2.0" },
    { name: "missing-package", kind: "dev", requestedSpec: "^1.0.0" },
  ],
  intent: {
    policies: { vite: "required", "missing-package": "keep-current" },
    deferredCohorts: [{ id: "vite-build", label: "Vite / build tooling", packages: ["vite", "@vitejs/plugin-react"], authority: "DIAGNOSTIC_HINT" }],
  },
})

if (snapshot.packages.length !== 4) throw new Error("direct package projection lost candidates")
if (snapshot.manifestCoverage.observed !== 3 || snapshot.manifestCoverage.missing[0] !== "missing-package") throw new Error("manifest coverage must be explicit")
if (!snapshot.edges.some((edge) => edge.source === "@vitejs/plugin-react" && edge.target === "vite" && edge.kind === "peer")) throw new Error("peer edge missing")
if (!snapshot.edges.some((edge) => edge.source === "vite" && edge.target === "react" && edge.kind === "dependency")) throw new Error("dependency edge missing")
if (!snapshot.edges.every((edge) => edge.authority === "OBSERVED_LOCAL_MANIFEST")) throw new Error("observed relation authority drifted")
if (snapshot.authorityBoundary.proofAuthority !== false) throw new Error("graph must never claim proof authority")
if (snapshot.packages.find((item) => item.name === "vite")?.policy !== "required") throw new Error("user policy projection missing")

// The nearest local installation shadows hoisted packages; lookup stops at Git.
mkdirSync(join(root, '.git'))
const nested = join(root, 'packages', 'app')
mkdirSync(nested, { recursive: true })
const { installedManifestReader } = await import('../dist-electron/project-manifest.js')
let readInstalled = installedManifestReader(nested)
assert.equal(readInstalled('react').version, '18.2.0', 'hoisted package must be observed')
assert.equal(readInstalled('@vitejs/plugin-react').version, '5.1.2', 'scoped hoisted package must be observed')
assert.equal(readInstalled('../package'), undefined, 'no path traversal')
mkdirSync(join(nested, 'node_modules', 'react'), { recursive: true })
writeFileSync(join(nested, 'node_modules', 'react', 'package.json'), '\uFEFF' + JSON.stringify({ version: '19.0.0' }))
assert.equal(readInstalled('react').version, '19.0.0', 'local BOM manifest shadows root')
writeFileSync(join(nested, 'node_modules', 'react', 'package.json'), '{broken')
assert.equal(readInstalled('react'), undefined, 'corrupt local manifest must not borrow root version')
mkdirSync(join(nested, 'node_modules', 'vite'))
assert.equal(readInstalled('vite'), undefined, 'local package without a manifest must not borrow root metadata')
mkdirSync(join(nested, '.git'))
readInstalled = installedManifestReader(nested)
assert.equal(readInstalled('@vitejs/plugin-react'), undefined, 'never borrow installation above another repository')
const hoistedGraph = buildDependencyGraphSnapshot(join(root, 'packages'), 'nested-fixture', { candidates: [{ name: 'vite', kind: 'dev', requestedSpec: '*' }], intent: {} })
assert.equal(hoistedGraph.manifestCoverage.observed, 1)

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8")
const component = readFileSync(new URL("../src/components/DependencyGraphWorkspace.tsx", import.meta.url), "utf8")
const projection = readFileSync(new URL("../src/data/dependencyGraphProjection.ts", import.meta.url), "utf8")
const preload = readFileSync(new URL("../electron/preload.cts", import.meta.url), "utf8")
const main = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf8")

for (const required of ["WorkspaceTab = 'overview' | 'dependencies' | 'report' | 'journal'", "DependencyGraphWorkspace", "t('app.tab.dependencies')"]) {
  if (!app.includes(required)) throw new Error(`Graph tab contract missing: ${required}`)
}
for (const required of ["Packages", "Groups", "Verified Scope", "Current blockage", "DIAGNOSTIC_HINT", "OBSERVED_LOCAL_MANIFEST"]) {
  if (!component.includes(required)) throw new Error(`Graph explanatory UI missing: ${required}`)
}
for (const required of ["deriveGraphGroups", "deriveGroupEdges", "USER_POLICY", "DISPLAY_ONLY"]) {
  if (!projection.includes(required)) throw new Error(`Graph projection boundary missing: ${required}`)
}
if (!preload.includes("getDependencyGraphSnapshot")) throw new Error("preload graph IPC missing")
if (!main.includes("flow:dependency-graph-snapshot")) throw new Error("main graph IPC missing")

const runDir = join(root, 'iterative')
mkdirSync(join(runDir, 'checkpoints'), {recursive:true})
const put = (name, value) => writeFileSync(join(runDir, name), JSON.stringify(value))
put('run.json', {runId:'fixture',activeCheckpointId:'C1'})
const checkpoint = (id, parent, assignment) => ({checkpointId:id,parentCheckpointId:parent,status:'VERIFIED',verification:{status:'passed'},fullAssignment:assignment})
put('checkpoints/C0.json', checkpoint('C0',null,{react:'18.2.0',vite:'4.3.9'}))
put('checkpoints/C1.json', checkpoint('C1','C0',{react:'19.0.0',vite:'4.3.9'}))
put('ledger.json', {deferrals:[{candidateId:'trial-1',baseCheckpointId:'C0',packages:['vite'],reason:'build failed'},{candidateId:'foreign',baseCheckpointId:'C9',packages:['react']}]})
assert.deepEqual(readIterativeGraphView(runDir), {runId:'fixture',checkpointId:'C1',accepted:{react:'19.0.0'},rejected:[{id:'trial-1',packages:['vite'],reason:'build failed'}]})
const compiledProjection = ts.transpileModule(projection,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText
const {deriveGraphGroups,derivePackageViews} = await import('data:text/javascript;base64,' + Buffer.from(compiledProjection).toString('base64'))
const liveSnapshot = {...snapshot,iteration:readIterativeGraphView(runDir)}
const liveGroups = deriveGraphGroups(liveSnapshot)
const livePackages = derivePackageViews(liveSnapshot,liveGroups,undefined,undefined,'fixture',undefined,undefined,undefined)
assert.equal(livePackages.find(item=>item.name==='react').status,'accepted')
assert.equal(livePackages.find(item=>item.name==='react').plannedTarget,'19.0.0')
assert.equal(livePackages.find(item=>item.name==='vite').status,'rejected')
assert.equal(liveGroups.find(group=>group.id==='attempt-trial-1').state,'rejected')
assert.equal(livePackages.find(item=>item.name==='@vitejs/plugin-react').status,'pending','An untouched package must not become green')
put('checkpoints/C1.json', {...checkpoint('C1','C0',{react:'19.0.0'}),status:'PLANNED'})
assert.equal(readIterativeGraphView(runDir),undefined,'planned updates must never appear accepted')
put('checkpoints/C1.json', checkpoint('C1','C1',{react:'19.0.0'}))
assert.equal(readIterativeGraphView(runDir),undefined,'cyclic checkpoint evidence must fail closed')
rmSync(root, { recursive: true, force: true })
console.log("Dependency graph projection / authority contracts OK")
