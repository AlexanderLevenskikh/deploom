import type {
  BaselineDecision,
  DependencyGraphObservedEdge,
  DependencyGraphPackage,
  DependencyGraphSnapshot,
  MigrationProgress,
  ProjectLevel,
  TargetClosure,
  TeamFlowState,
} from '../types'

export type GraphGroupState = 'active' | 'deferred' | 'inferred' | 'display'
export type GraphGroup = {
  id: string
  label: string
  packages: string[]
  active: boolean
  deferred: boolean
  state: GraphGroupState
  authority: 'DIAGNOSTIC_HINT' | 'USER_POLICY' | 'DISPLAY_ONLY'
  confidence?: number
  predicate?: string
  reasons: string[]
  boundaryPackages: string[]
  warningPackages: string[]
}
export type GraphGroupEdge = { source: string; target: string; observed: boolean; bridgePackages: string[] }
export type GraphPackageStatus = 'active-issue' | 'critical' | 'deferred' | 'required' | 'integrated' | 'verified-scope' | 'pending'
export type GraphPackageView = DependencyGraphPackage & {
  status: GraphPackageStatus
  plannedTarget?: string
  migrationGroup?: string
  boundary: boolean
  warning: boolean
  critical: boolean
}

type EcosystemRule = { id: string; label: string; test: (name: string) => boolean }
const norm = (value: string) => value.toLowerCase()
const base = (value: string) => norm(value).split('/').pop() ?? norm(value)

const ecosystemRules: EcosystemRule[] = [
  { id: 'vite-build', label: 'Vite / build tooling', test: (name) => {
    const value = norm(name)
    return value === 'vite' || value === 'vitest' || value === 'rollup'
      || value.startsWith('@vitejs/') || value.startsWith('vite-')
      || value.startsWith('@sentry/vite') || value.startsWith('@storybook/builder-vite')
      || value.startsWith('@storybook/react-vite') || value.startsWith('unplugin')
  }},
  { id: 'storybook', label: 'Storybook', test: (name) => norm(name) === 'storybook' || norm(name).startsWith('@storybook/') || norm(name).includes('storybook') },
  { id: 'eslint', label: 'ESLint', test: (name) => {
    const value = norm(name)
    return value === 'eslint' || value.startsWith('eslint-') || value.startsWith('@typescript-eslint/') || value === '@babel/eslint-parser'
  }},
  { id: 'stylelint', label: 'Stylelint', test: (name) => {
    const value = norm(name)
    return value === 'stylelint' || value.startsWith('stylelint-') || value.startsWith('@stylelint/')
  }},
  { id: 'typescript', label: 'TypeScript tooling', test: (name) => {
    const value = norm(name)
    return value === 'typescript' || value.startsWith('@typescript-eslint/') || ['ts-node', 'ts-jest', 'typescript-eslint'].includes(value)
  }},
  { id: 'react', label: 'React', test: (name) => {
    const value = norm(name)
    return ['react', 'react-dom', '@types/react', '@types/react-dom'].includes(value)
      || value.startsWith('@vitejs/plugin-react') || value.startsWith('@storybook/react')
  }},
  { id: 'webpack', label: 'Webpack / loaders', test: (name) => {
    const value = norm(name)
    return value === 'webpack' || value.startsWith('webpack-') || value.startsWith('@storybook/builder-webpack') || base(value).endsWith('-loader')
  }},
  { id: 'babel', label: 'Babel', test: (name) => norm(name).startsWith('@babel/') || norm(name) === 'babel-jest' },
]

const dedupe = (values: string[]) => [...new Set(values)].sort()

export function deriveGraphGroups(snapshot: DependencyGraphSnapshot, decision?: BaselineDecision): GraphGroup[] {
  const direct = new Set(snapshot.packages.map((item) => item.name))
  const byId = new Map<string, GraphGroup>()
  const ensure = (group: GraphGroup) => {
    const previous = byId.get(group.id)
    if (!previous) { byId.set(group.id, group); return }
    byId.set(group.id, {
      ...previous, ...group,
      packages: dedupe([...previous.packages, ...group.packages]),
      boundaryPackages: dedupe([...previous.boundaryPackages, ...group.boundaryPackages]),
      warningPackages: dedupe([...previous.warningPackages, ...group.warningPackages]),
      reasons: dedupe([...previous.reasons, ...group.reasons]),
      active: previous.active || group.active,
      deferred: previous.deferred || group.deferred,
      state: group.active ? 'active' : previous.active ? 'active' : group.deferred ? 'deferred' : previous.state,
      authority: group.active ? 'DIAGNOSTIC_HINT' : group.deferred ? 'USER_POLICY' : previous.authority,
    })
  }

  for (const cohort of snapshot.intent.deferredCohorts ?? []) {
    const packages = cohort.packages.filter((name) => direct.has(name))
    if (!packages.length) continue
    ensure({
      id: cohort.id, label: cohort.label, packages, active: false, deferred: true,
      state: 'deferred', authority: 'USER_POLICY', confidence: cohort.confidence,
      predicate: cohort.predicate, reasons: ['user-confirmed deferred group'],
      boundaryPackages: (cohort.boundaryPackages ?? []).filter((name) => direct.has(name)),
      warningPackages: (cohort.warningPackages ?? []).filter((name) => direct.has(name)),
    })
  }

  const suggested = decision?.suggestedCohort
  if (suggested) ensure({
    id: suggested.id, label: suggested.label,
    packages: suggested.packages.filter((name) => direct.has(name)),
    active: true, deferred: false, state: 'active', authority: 'DIAGNOSTIC_HINT',
    confidence: suggested.confidence, predicate: suggested.predicate, reasons: [...suggested.reasons],
    boundaryPackages: suggested.boundaryPackages.filter((name) => direct.has(name)),
    warningPackages: suggested.warningPackages.filter((name) => direct.has(name)),
  })

  for (const rule of ecosystemRules) {
    const packages = snapshot.packages.map((item) => item.name).filter(rule.test)
    if (packages.length < 2) continue
    ensure({
      id: rule.id, label: rule.label, packages, active: false, deferred: false,
      state: 'inferred', authority: 'DIAGNOSTIC_HINT',
      reasons: ['ecosystem prior for navigation only'], boundaryPackages: [], warningPackages: [],
    })
  }

  const covered = new Set([...byId.values()].flatMap((group) => group.packages))
  for (const kind of ['runtime', 'dev', 'peer'] as const) {
    const packages = snapshot.packages.filter((item) => item.kind === kind && !covered.has(item.name)).map((item) => item.name)
    if (!packages.length) continue
    ensure({
      id: `display-${kind}`,
      label: kind === 'runtime' ? 'Other runtime' : kind === 'dev' ? 'Other dev tooling' : 'Other peer surface',
      packages, active: false, deferred: false, state: 'display', authority: 'DISPLAY_ONLY',
      reasons: ['visual bucket, not a compatibility claim'], boundaryPackages: [], warningPackages: [],
    })
  }

  return [...byId.values()].filter((group) => group.packages.length > 0).sort((a, b) => {
    const rank = (group: GraphGroup) => group.active ? 0 : group.deferred ? 1 : group.state === 'inferred' ? 2 : 3
    return rank(a) - rank(b) || a.label.localeCompare(b.label)
  })
}

export function deriveGroupEdges(groups: GraphGroup[], observedEdges: DependencyGraphObservedEdge[]): GraphGroupEdge[] {
  const result = new Map<string, GraphGroupEdge>()
  const membership = new Map<string, string[]>()
  for (const group of groups) for (const name of group.packages) membership.set(name, [...(membership.get(name) ?? []), group.id])

  const add = (left: string, right: string, packages: string[], observed: boolean) => {
    if (left === right) return
    const [source, target] = [left, right].sort()
    const key = `${source}\0${target}`
    const previous = result.get(key)
    result.set(key, {
      source, target, observed: Boolean(previous?.observed || observed),
      bridgePackages: dedupe([...(previous?.bridgePackages ?? []), ...packages]),
    })
  }

  for (let index = 0; index < groups.length; index += 1) {
    for (let other = index + 1; other < groups.length; other += 1) {
      const left = groups[index], right = groups[other], rightSet = new Set(right.packages)
      const overlap = left.packages.filter((name) => rightSet.has(name))
      if (overlap.length) add(left.id, right.id, overlap, false)
    }
  }
  for (const edge of observedEdges) {
    for (const source of membership.get(edge.source) ?? []) {
      for (const target of membership.get(edge.target) ?? []) add(source, target, [edge.source, edge.target], true)
    }
  }
  return [...result.values()].sort((a, b) => `${a.source}:${a.target}`.localeCompare(`${b.source}:${b.target}`))
}

export function derivePackageViews(
  snapshot: DependencyGraphSnapshot,
  groups: GraphGroup[],
  decision: BaselineDecision | undefined,
  teamState: TeamFlowState | undefined,
  projectName: string,
  level: ProjectLevel | undefined,
  closure: TargetClosure | undefined,
  migration: MigrationProgress | undefined,
): GraphPackageView[] {
  const active = new Set(decision?.suggestedCohort?.packages ?? [])
  const boundaries = new Set(decision?.suggestedCohort?.boundaryPackages ?? [])
  const warnings = new Set(decision?.suggestedCohort?.warningPackages ?? [])
  for (const group of groups.filter((item) => item.deferred)) {
    for (const name of group.boundaryPackages) boundaries.add(name)
    for (const name of group.warningPackages) warnings.add(name)
  }
  const critical = new Set(closure?.criticalPackages ?? [])
  const planned = new Map((closure?.lagBlockers ?? []).map((item) => [item.package, item.plannedTarget || item.required]))
  const migrationByPackage = new Map<string, { label: string; status: string }>()
  for (const branch of migration?.branches ?? []) for (const name of branch.packages) migrationByPackage.set(name, { label: branch.label, status: branch.status })
  const run = teamState?.projects[projectName]
  const baselineCompleted = Boolean(run?.completedActions?.includes('baseline'))
  const verifiedHealth = level?.status === 'yellow' || level?.status === 'green'

  return snapshot.packages.map((item) => {
    const migrationFact = migrationByPackage.get(item.name)
    const integrated = migrationFact?.status === 'merged' || migrationFact?.status === 'integrated'
    let status: GraphPackageStatus = 'pending'
    if (active.has(item.name)) status = 'active-issue'
    else if (critical.has(item.name)) status = 'critical'
    else if (item.policy === 'keep-current') status = 'deferred'
    else if (item.policy === 'required') status = 'required'
    else if (integrated) status = 'integrated'
    else if (baselineCompleted && verifiedHealth) status = 'verified-scope'
    return {
      ...item, status, plannedTarget: planned.get(item.name), migrationGroup: migrationFact?.label,
      boundary: boundaries.has(item.name), warning: warnings.has(item.name), critical: critical.has(item.name),
    }
  })
}

export function primaryGroupForPackage(name: string, groups: GraphGroup[]): GraphGroup | undefined {
  return groups.filter((group) => group.packages.includes(name)).sort((a, b) => {
    const rank = (group: GraphGroup) => group.active ? 0 : group.deferred ? 1 : group.state === 'inferred' ? 2 : 3
    return rank(a) - rank(b) || a.label.localeCompare(b.label)
  })[0]
}
