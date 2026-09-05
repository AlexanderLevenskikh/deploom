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

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8")
const component = readFileSync(new URL("../src/components/DependencyGraphWorkspace.tsx", import.meta.url), "utf8")
const projection = readFileSync(new URL("../src/data/dependencyGraphProjection.ts", import.meta.url), "utf8")
const preload = readFileSync(new URL("../electron/preload.cts", import.meta.url), "utf8")
const main = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf8")

for (const required of ["WorkspaceTab = 'flow' | 'graph' | 'dashboard'", "DependencyGraphWorkspace", ">Graph<"]) {
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

rmSync(root, { recursive: true, force: true })
console.log("Dependency graph projection / authority contracts OK")
