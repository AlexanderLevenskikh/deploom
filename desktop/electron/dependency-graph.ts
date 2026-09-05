import { readFileSync } from 'node:fs'
import { join } from 'node:path'

export type DependencyGraphCandidate = {
  name: string
  kind: 'runtime' | 'dev' | 'peer'
  requestedSpec: string
  currentVersion?: string
}

export type DependencyGraphIntent = {
  policies?: Record<string, 'auto' | 'keep-current' | 'required'>
  deferredCohorts?: Array<{
    id: string
    label: string
    packages: string[]
    predicate?: string
    confidence?: number
    authority: 'DIAGNOSTIC_HINT'
    deferredAt?: string
    decisionId?: string
    boundaryPackages?: string[]
    warningPackages?: string[]
  }>
}

export type DependencyGraphPackage = {
  name: string
  kind: 'runtime' | 'dev' | 'peer'
  requestedSpec: string
  currentVersion?: string
  installedVersion?: string
  policy: 'auto' | 'keep-current' | 'required'
  manifestObserved: boolean
}

export type DependencyGraphObservedEdge = {
  source: string
  target: string
  kind: 'dependency' | 'peer' | 'optional'
  authority: 'OBSERVED_LOCAL_MANIFEST'
}

export type DependencyGraphSnapshot = {
  schemaVersion: 1
  project: string
  capturedAt: string
  packages: DependencyGraphPackage[]
  edges: DependencyGraphObservedEdge[]
  intent: DependencyGraphIntent
  manifestCoverage: { observed: number; total: number; missing: string[] }
  authorityBoundary: {
    packageList: 'PROJECT_MANIFEST'
    relations: 'OBSERVED_LOCAL_MANIFEST'
    cohorts: 'DIAGNOSTIC_HINT_OR_USER_POLICY'
    proofAuthority: false
  }
}

type InstalledManifest = {
  version?: unknown
  dependencies?: Record<string, unknown>
  peerDependencies?: Record<string, unknown>
  optionalDependencies?: Record<string, unknown>
}

function installedManifest(projectPath: string, packageName: string): InstalledManifest | undefined {
  try {
    return JSON.parse(
      readFileSync(join(projectPath, 'node_modules', ...packageName.split('/'), 'package.json'), 'utf8'),
    ) as InstalledManifest
  } catch {
    return undefined
  }
}

function relationNames(value: unknown): string[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  return Object.keys(value as Record<string, unknown>)
}

export function buildDependencyGraphSnapshot(
  projectPath: string,
  projectName: string,
  plan: { candidates: DependencyGraphCandidate[]; intent: DependencyGraphIntent },
): DependencyGraphSnapshot {
  const direct = new Set(plan.candidates.map((item) => item.name))
  const missing: string[] = []
  const packages: DependencyGraphPackage[] = []
  const edges = new Map<string, DependencyGraphObservedEdge>()
  let observed = 0

  for (const candidate of [...plan.candidates].sort((a, b) => a.name.localeCompare(b.name))) {
    const manifest = installedManifest(projectPath, candidate.name)
    if (manifest) observed += 1
    else missing.push(candidate.name)

    packages.push({
      name: candidate.name,
      kind: candidate.kind,
      requestedSpec: candidate.requestedSpec,
      currentVersion: candidate.currentVersion,
      installedVersion: manifest && typeof manifest.version === 'string' ? manifest.version : undefined,
      policy: plan.intent.policies?.[candidate.name] ?? 'auto',
      manifestObserved: Boolean(manifest),
    })

    if (!manifest) continue
    const sections: Array<[DependencyGraphObservedEdge['kind'], unknown]> = [
      ['dependency', manifest.dependencies],
      ['peer', manifest.peerDependencies],
      ['optional', manifest.optionalDependencies],
    ]
    for (const [kind, raw] of sections) {
      for (const target of relationNames(raw)) {
        if (!direct.has(target) || target === candidate.name) continue
        const key = `${candidate.name}\0${target}\0${kind}`
        edges.set(key, { source: candidate.name, target, kind, authority: 'OBSERVED_LOCAL_MANIFEST' })
      }
    }
  }

  return {
    schemaVersion: 1,
    project: projectName,
    capturedAt: new Date().toISOString(),
    packages,
    edges: [...edges.values()].sort((a, b) =>
      `${a.source}:${a.target}:${a.kind}`.localeCompare(`${b.source}:${b.target}:${b.kind}`),
    ),
    intent: {
      policies: { ...(plan.intent.policies ?? {}) },
      deferredCohorts: (plan.intent.deferredCohorts ?? []).map((cohort) => ({
        ...cohort,
        packages: [...cohort.packages],
        boundaryPackages: [...(cohort.boundaryPackages ?? [])],
        warningPackages: [...(cohort.warningPackages ?? [])],
        authority: 'DIAGNOSTIC_HINT' as const,
      })),
    },
    manifestCoverage: { observed, total: plan.candidates.length, missing: missing.sort() },
    authorityBoundary: {
      packageList: 'PROJECT_MANIFEST',
      relations: 'OBSERVED_LOCAL_MANIFEST',
      cohorts: 'DIAGNOSTIC_HINT_OR_USER_POLICY',
      proofAuthority: false,
    },
  }
}
