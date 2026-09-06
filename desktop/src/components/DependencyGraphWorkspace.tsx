import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  CircleDot,
  Focus,
  GitBranch,
  Layers3,
  Maximize2,
  Minimize2,
  Minus,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import type { BaselineDecision, DependencyGraphSnapshot, ProjectSpec, WorkspaceDetails } from '../types'
import {
  deriveGraphGroups,
  deriveGroupEdges,
  derivePackageViews,
  primaryGroupForPackage,
  type GraphGroup,
  type GraphPackageView,
} from '../data/dependencyGraphProjection'
import { useLanguage } from '../i18n'

type GraphMode = 'packages' | 'groups' | 'verified'
type Selected = { kind: 'package'; id: string } | { kind: 'group'; id: string } | undefined
type Point = { x: number; y: number }
type PackageLayout = { positions: Map<string, Point>; labels: Array<{ id: string; label: string; x: number }>; width: number; height: number }
type GroupLayout = { positions: Map<string, Point>; width: number; height: number }

const PACKAGE_WIDTH = 218
const PACKAGE_HEIGHT = 60
const GROUP_WIDTH = 226
const GROUP_HEIGHT = 98

function statusLabel(status: GraphPackageView['status'], ru: boolean): string {
  const labels: Record<GraphPackageView['status'], [string, string]> = {
    'active-issue': ['Активная проблема', 'Active issue'],
    critical: ['Critical security', 'Critical security'],
    deferred: ['Отложено', 'Deferred'],
    required: ['Обязательно', 'Required'],
    integrated: ['Интегрировано', 'Integrated'],
    'verified-scope': ['Verified scope', 'Verified scope'],
    pending: ['Ожидает', 'Pending'],
  }
  return labels[status][ru ? 0 : 1]
}

function packageLayout(packages: GraphPackageView[], groups: GraphGroup[], mode: GraphMode): PackageLayout {
  const buckets = new Map<string, { label: string; packages: GraphPackageView[] }>()
  for (const item of packages) {
    const primary = mode === 'verified'
      ? { id: `status-${item.status}`, label: statusLabel(item.status, false) }
      : primaryGroupForPackage(item.name, groups) ?? { id: `kind-${item.kind}`, label: item.kind }
    const bucket = buckets.get(primary.id) ?? { label: primary.label, packages: [] }
    bucket.packages.push(item)
    buckets.set(primary.id, bucket)
  }

  const ordered = [...buckets.entries()].sort((a, b) => a[1].label.localeCompare(b[1].label))
  const positions = new Map<string, Point>()
  const labels: PackageLayout['labels'] = []
  let tallest = 1
  ordered.forEach(([id, bucket], column) => {
    bucket.packages.sort((a, b) => a.name.localeCompare(b.name))
    tallest = Math.max(tallest, bucket.packages.length)
    const x = 42 + column * 250
    labels.push({ id, label: bucket.label, x })
    bucket.packages.forEach((item, row) => positions.set(item.name, { x, y: 74 + row * 78 }))
  })
  return {
    positions,
    labels,
    width: Math.max(940, 60 + Math.max(1, ordered.length) * 250),
    height: Math.max(520, 120 + tallest * 78),
  }
}

function groupLayout(groups: GraphGroup[]): GroupLayout {
  const columns = Math.min(4, Math.max(1, Math.ceil(Math.sqrt(groups.length))))
  const positions = new Map<string, Point>()
  groups.forEach((group, index) => {
    const column = index % columns
    const row = Math.floor(index / columns)
    positions.set(group.id, { x: 54 + column * 278, y: 62 + row * 146 })
  })
  return {
    positions,
    width: Math.max(940, 80 + columns * 278),
    height: Math.max(520, 100 + Math.ceil(groups.length / columns) * 146),
  }
}

function edgePath(source: Point, target: Point, nodeWidth: number, nodeHeight: number): string {
  const x1 = source.x + nodeWidth / 2
  const y1 = source.y + nodeHeight / 2
  const x2 = target.x + nodeWidth / 2
  const y2 = target.y + nodeHeight / 2
  const bend = Math.max(34, Math.abs(x2 - x1) * 0.36)
  return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`
}

export function DependencyGraphWorkspace({
  details,
  project,
  baselineDecision,
  onGetSnapshot,
  onOpenFlow,
  fullscreen,
  leftPaneHidden,
  rightPaneHidden,
  onToggleFullscreen,
  onToggleLeftPane,
  onToggleRightPane,
}: {
  details: WorkspaceDetails
  project: ProjectSpec
  baselineDecision?: BaselineDecision
  onGetSnapshot: (projectName: string) => Promise<DependencyGraphSnapshot>
  onOpenFlow: () => void
  fullscreen: boolean
  leftPaneHidden: boolean
  rightPaneHidden: boolean
  onToggleFullscreen: () => void
  onToggleLeftPane: () => void
  onToggleRightPane: () => void
}) {
  const { language } = useLanguage()
  const ru = language === 'ru'
  const text = (russian: string, english: string) => ru ? russian : english
  const [snapshot, setSnapshot] = useState<DependencyGraphSnapshot>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>()
  const [mode, setMode] = useState<GraphMode>('packages')
  const [search, setSearch] = useState('')
  const [showRelations, setShowRelations] = useState(true)
  const [showBridges, setShowBridges] = useState(true)
  const [focusActive, setFocusActive] = useState(false)
  const [zoom, setZoom] = useState(0.9)
  const [selected, setSelected] = useState<Selected>()

  useEffect(() => {
    if (!fullscreen) return
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onToggleFullscreen() }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [fullscreen, onToggleFullscreen])

  const reload = async () => {
    setLoading(true)
    setError(undefined)
    try {
      setSnapshot(await onGetSnapshot(project.name))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(undefined)
    void onGetSnapshot(project.name).then((next) => {
      if (!cancelled) setSnapshot(next)
    }).catch((cause) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause))
    }).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => { cancelled = true }
  }, [details.teamState?.updatedAt, project.name, onGetSnapshot])

  const groups = useMemo(() => snapshot ? deriveGraphGroups(snapshot, baselineDecision) : [], [baselineDecision, snapshot])
  const groupEdges = useMemo(() => snapshot ? deriveGroupEdges(groups, snapshot.edges) : [], [groups, snapshot])
  const packageViews = useMemo(
    () => snapshot ? derivePackageViews(
      snapshot,
      groups,
      baselineDecision,
      details.teamState,
      project.name,
      details.projectLevels[project.name],
      details.targetClosure,
      details.migrationProgress,
    ) : [],
    [baselineDecision, details.migrationProgress, details.projectLevels, details.targetClosure, details.teamState, groups, project.name, snapshot],
  )

  const activeGroup = groups.find((group) => group.active)
  const deferredGroups = groups.filter((group) => group.deferred)
  const query = search.trim().toLowerCase()

  const visiblePackages = useMemo(() => {
    let result = packageViews
    if (query) result = result.filter((item) => item.name.toLowerCase().includes(query))
    if (focusActive && activeGroup) {
      const focus = new Set([...activeGroup.packages, ...activeGroup.boundaryPackages])
      result = result.filter((item) => focus.has(item.name))
    }
    return result
  }, [activeGroup, focusActive, packageViews, query])

  const visibleGroups = useMemo(() => {
    let result = groups
    if (query) result = result.filter((group) =>
      group.label.toLowerCase().includes(query) || group.packages.some((name) => name.toLowerCase().includes(query)),
    )
    if (focusActive && activeGroup) {
      const connected = new Set([activeGroup.id])
      for (const edge of groupEdges) {
        if (edge.source === activeGroup.id) connected.add(edge.target)
        if (edge.target === activeGroup.id) connected.add(edge.source)
      }
      result = result.filter((group) => connected.has(group.id))
    }
    return result
  }, [activeGroup, focusActive, groupEdges, groups, query])

  const packageMap = new Map(visiblePackages.map((item) => [item.name, item]))
  const packagePositions = useMemo(() => packageLayout(visiblePackages, visibleGroups, mode), [mode, visibleGroups, visiblePackages])
  const groupPositions = useMemo(() => groupLayout(visibleGroups), [visibleGroups])
  const health = details.projectLevels[project.name]
  const baselineCompleted = Boolean(details.teamState?.projects[project.name]?.completedActions?.includes('baseline'))
  const partial = deferredGroups.length > 0
  const selectedPackage = selected?.kind === 'package' ? packageViews.find((item) => item.name === selected.id) : undefined
  const selectedGroup = selected?.kind === 'group' ? groups.find((item) => item.id === selected.id) : undefined

  const focusCurrent = () => {
    setFocusActive(true)
    setSearch('')
    setMode(activeGroup ? 'groups' : 'verified')
    if (activeGroup) setSelected({ kind: 'group', id: activeGroup.id })
  }

  if (loading && !snapshot) {
    return <div className="dependency-graph-loading"><RefreshCw className="spin" size={18} /> {text('Строю проекцию графа…', 'Building graph projection…')}</div>
  }

  if (error && !snapshot) {
    return <div className="dependency-graph-error"><AlertTriangle size={18} /><span>{error}</span><button className="button secondary" onClick={() => void reload()}>{text('Повторить', 'Retry')}</button></div>
  }

  if (!snapshot) return null

  const canvas = mode === 'groups' ? groupPositions : packagePositions
  const verifiedCount = packageViews.filter((item) => ['verified-scope', 'integrated', 'required'].includes(item.status)).length
  const deferredCount = packageViews.filter((item) => item.status === 'deferred').length

  return (
    <section className="dependency-graph-workspace">
      <div className="dependency-graph-summary">
        <div><span>{text('Пакеты', 'Packages')}</span><strong>{snapshot.packages.length}</strong></div>
        <div><span>{text('Наблюдаемые связи', 'Observed relations')}</span><strong>{snapshot.edges.length}</strong></div>
        <div><span>Manifest coverage</span><strong>{snapshot.manifestCoverage.observed}/{snapshot.manifestCoverage.total}</strong></div>
        <div><span>Verified scope</span><strong>{baselineCompleted ? `${verifiedCount}/${snapshot.packages.length}` : '—'}</strong></div>
        <div><span>{text('Отложено', 'Deferred')}</span><strong>{deferredCount}</strong></div>
        <div className={`graph-health ${health?.status ?? 'unknown'}`}><span>Health</span><strong>{health?.status?.toUpperCase() ?? 'UNKNOWN'}{typeof health?.lagOkPct === 'number' ? ` · ${health.lagOkPct.toFixed(1)}%` : ''}</strong></div>
      </div>

      {baselineDecision?.suggestedCohort ? (
        <div className="graph-blockage-panel">
          <div className="graph-blockage-icon"><AlertTriangle size={19} /></div>
          <div className="graph-blockage-copy">
            <div className="graph-blockage-title"><strong>{text('Текущая точка сложности', 'Current blockage')}</strong><span className="graph-hint-badge">DIAGNOSTIC_HINT</span></div>
            <p><code>{baselineDecision.repeatedPredicate || baselineDecision.predicate || baselineDecision.reason}</code>{' → '}<strong>{baselineDecision.suggestedCohort.label}</strong>{' · '}{Math.round(baselineDecision.suggestedCohort.confidence * 100)}%</p>
            <div className="graph-blockage-packages">{baselineDecision.suggestedCohort.packages.slice(0, 12).map((name) => <code key={name}>{name}</code>)}</div>
            {baselineDecision.suggestedCohort.boundaryPackages.length ? <small>{text('Bridge / boundary:', 'Bridge / boundary:')} {baselineDecision.suggestedCohort.boundaryPackages.join(', ')}</small> : null}
            <small>{text(
              'Эта группа объясняет следующий search neighborhood, но не является доказательством несовместимости. Каждый новый incumbent всё равно проверяется целиком.',
              'This group explains the next search neighborhood; it is not proof of incompatibility. Every new incumbent is still verified as a whole project.',
            )}</small>
          </div>
          <div className="graph-blockage-actions">
            <button className="button primary" onClick={focusCurrent}><Focus size={15} /> {text('Показать на графе', 'Focus graph')}</button>
            <button className="button secondary" onClick={onOpenFlow}>{text('К решению во FLOW', 'Open decision in FLOW')} <ChevronRight size={15} /></button>
          </div>
        </div>
      ) : partial ? (
        <div className="graph-partial-panel">
          <ShieldCheck size={18} />
          <div><strong>VERIFIED_PARTIAL_SCOPE</strong><span>{text(`Рабочий scope подтверждён, ${deferredGroups.length} group(s) сохранены для следующих проходов.`, `The working scope is verified; ${deferredGroups.length} group(s) remain for later passes.`)}</span></div>
        </div>
      ) : null}

      <div className="dependency-graph-toolbar">
        <div className="graph-mode-switch" role="tablist" aria-label={text('Режим графа', 'Graph mode')}>
          <button className={mode === 'packages' ? 'active' : ''} onClick={() => setMode('packages')}><Network size={15} /> Packages</button>
          <button className={mode === 'groups' ? 'active' : ''} onClick={() => setMode('groups')}><Layers3 size={15} /> Groups</button>
          <button className={mode === 'verified' ? 'active' : ''} onClick={() => setMode('verified')}><ShieldCheck size={15} /> Verified Scope</button>
        </div>
        <label className="graph-search"><Search size={14} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={text('Найти пакет или группу…', 'Find package or group…')} /></label>
        <label className="graph-check"><input type="checkbox" checked={showRelations} onChange={(event) => setShowRelations(event.target.checked)} /> {text('Связи', 'Relations')}</label>
        <label className="graph-check"><input type="checkbox" checked={showBridges} onChange={(event) => setShowBridges(event.target.checked)} /> Bridges</label>
        <button className={`graph-tool-button${focusActive ? ' active' : ''}`} disabled={!activeGroup} onClick={() => setFocusActive((value) => !value)}><Focus size={14} /> {text('Фокус', 'Focus')}</button>
        <span className="graph-toolbar-spacer" />
        <button className="graph-icon-button" disabled={fullscreen} title={text(leftPaneHidden ? 'Показать список проектов' : 'Скрыть список проектов', leftPaneHidden ? 'Show project rail' : 'Hide project rail')} onClick={onToggleLeftPane}>{leftPaneHidden ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}</button>
        <button className="graph-icon-button" disabled={fullscreen} title={text(rightPaneHidden ? 'Показать мониторинг' : 'Скрыть мониторинг', rightPaneHidden ? 'Show monitoring' : 'Hide monitoring')} onClick={onToggleRightPane}>{rightPaneHidden ? <PanelRightOpen size={15} /> : <PanelRightClose size={15} />}</button>
        <button className={`graph-icon-button${fullscreen ? ' active' : ''}`} title={text(fullscreen ? 'Выйти из полноэкранного графа (Esc)' : 'Развернуть граф на всё окно', fullscreen ? 'Exit Graph fullscreen (Esc)' : 'Use the whole window for Graph')} onClick={onToggleFullscreen}>{fullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
        <button className="graph-icon-button" title={text('Уменьшить', 'Zoom out')} onClick={() => setZoom((value) => Math.max(0.55, Number((value - 0.1).toFixed(2))))}><Minus size={15} /></button>
        <span className="graph-zoom">{Math.round(zoom * 100)}%</span>
        <button className="graph-icon-button" title={text('Увеличить', 'Zoom in')} onClick={() => setZoom((value) => Math.min(1.5, Number((value + 0.1).toFixed(2))))}><Plus size={15} /></button>
        <button className="graph-icon-button" title={text('Обновить', 'Refresh')} onClick={() => void reload()}><RefreshCw size={15} /></button>
      </div>

      <div className="dependency-graph-main">
        <div className="dependency-graph-canvas">
          <div className="graph-scroll">
            <svg className="graph-svg" style={{ width: canvas.width * zoom, height: canvas.height * zoom }} viewBox={`0 0 ${canvas.width} ${canvas.height}`} role="img" aria-label={text('Граф зависимостей проекта', 'Project dependency graph')}>
              <defs><marker id="graph-arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" className="graph-arrow" /></marker></defs>

              {mode !== 'groups' ? packagePositions.labels.map((label) => <g key={label.id} className="graph-column-label"><text x={label.x} y={36}>{label.label}</text></g>) : null}

              {mode !== 'groups' && showRelations ? snapshot.edges.map((edge) => {
                const source = packagePositions.positions.get(edge.source), target = packagePositions.positions.get(edge.target)
                if (!source || !target || !packageMap.has(edge.source) || !packageMap.has(edge.target)) return null
                return <path key={`${edge.source}:${edge.target}:${edge.kind}`} d={edgePath(source, target, PACKAGE_WIDTH, PACKAGE_HEIGHT)} className={`graph-edge graph-edge-${edge.kind}`} markerEnd="url(#graph-arrow)" />
              }) : null}

              {mode === 'groups' ? groupEdges.map((edge) => {
                const source = groupPositions.positions.get(edge.source), target = groupPositions.positions.get(edge.target)
                if (!source || !target) return null
                if (!showBridges && !edge.observed) return null
                return <g key={`${edge.source}:${edge.target}`}><path d={edgePath(source, target, GROUP_WIDTH, GROUP_HEIGHT)} className={`graph-group-edge${edge.observed ? ' observed' : ''}`} />{showBridges && edge.bridgePackages.length ? <title>{`bridge: ${edge.bridgePackages.join(', ')}`}</title> : null}</g>
              }) : null}

              {mode === 'groups' ? visibleGroups.map((group) => {
                const point = groupPositions.positions.get(group.id)
                if (!point) return null
                const bridgeCount = groupEdges.filter((edge) => edge.source === group.id || edge.target === group.id).flatMap((edge) => edge.bridgePackages).length
                return (
                  <g key={group.id} transform={`translate(${point.x} ${point.y})`} className={`graph-group-node ${group.state}${selected?.kind === 'group' && selected.id === group.id ? ' selected' : ''}`} onClick={() => setSelected({ kind: 'group', id: group.id })} role="button" tabIndex={0}>
                    <rect width={GROUP_WIDTH} height={GROUP_HEIGHT} rx="12" />
                    <text className="graph-node-title" x="14" y="25">{group.label.slice(0, 31)}</text>
                    <text className="graph-node-subtitle" x="14" y="46">{group.packages.length} packages · {group.authority}</text>
                    <text className="graph-node-meta" x="14" y="67">{group.active ? `${Math.round((group.confidence ?? 0) * 100)}% confidence` : group.deferred ? 'deferred / revisitable' : `${bridgeCount} bridge signals`}</text>
                    {group.active ? <circle cx={GROUP_WIDTH - 17} cy="18" r="6" className="graph-pulse-dot" /> : null}
                  </g>
                )
              }) : visiblePackages.map((item) => {
                const point = packagePositions.positions.get(item.name)
                if (!point) return null
                return (
                  <g key={item.name} transform={`translate(${point.x} ${point.y})`} className={`graph-package-node status-${item.status}${item.boundary ? ' boundary' : ''}${item.warning ? ' warning' : ''}${selected?.kind === 'package' && selected.id === item.name ? ' selected' : ''}`} onClick={() => setSelected({ kind: 'package', id: item.name })} role="button" tabIndex={0}>
                    <rect width={PACKAGE_WIDTH} height={PACKAGE_HEIGHT} rx="9" />
                    <text className="graph-node-title" x="12" y="22">{item.name.length > 29 ? `${item.name.slice(0, 27)}…` : item.name}</text>
                    <text className="graph-node-subtitle" x="12" y="42">{(item.currentVersion || item.installedVersion || '?')}{item.plannedTarget ? ` → ${item.plannedTarget}` : ''} · {statusLabel(item.status, ru)}</text>
                    {item.boundary && showBridges ? <circle cx={PACKAGE_WIDTH - 16} cy="16" r="5" className="graph-bridge-dot" /> : null}
                  </g>
                )
              })}
            </svg>
          </div>
          <div className="graph-legend">
            <span><i className="legend-dot verified" /> verified/integrated</span>
            <span><i className="legend-dot deferred" /> deferred</span>
            <span><i className="legend-dot active" /> active issue</span>
            <span><i className="legend-dot required" /> required</span>
            <span><i className="legend-line observed" /> observed manifest relation</span>
            <span><i className="legend-line hint" /> DIAGNOSTIC_HINT</span>
          </div>
        </div>

        <aside className="dependency-graph-details">
          {selectedPackage ? (
            <>
              <div className="graph-details-heading"><GitBranch size={17} /><div><strong>{selectedPackage.name}</strong><span>{selectedPackage.kind}</span></div></div>
              <dl>
                <div><dt>{text('Текущая', 'Current')}</dt><dd>{selectedPackage.currentVersion || selectedPackage.installedVersion || '—'}</dd></div>
                <div><dt>{text('План', 'Planned')}</dt><dd>{selectedPackage.plannedTarget || '—'}</dd></div>
                <div><dt>package.json</dt><dd>{selectedPackage.requestedSpec}</dd></div>
                <div><dt>{text('Политика', 'Policy')}</dt><dd>{selectedPackage.policy}</dd></div>
                <div><dt>{text('Состояние', 'State')}</dt><dd>{statusLabel(selectedPackage.status, ru)}</dd></div>
                <div><dt>Manifest evidence</dt><dd>{selectedPackage.manifestObserved ? 'OBSERVED_LOCAL_MANIFEST' : text('недоступно', 'unavailable')}</dd></div>
                {selectedPackage.migrationGroup ? <div><dt>Migration group</dt><dd>{selectedPackage.migrationGroup}</dd></div> : null}
              </dl>
              {selectedPackage.boundary ? <div className="graph-details-note bridge"><CircleDot size={14} /> {text('Boundary / bridge package в текущем explanatory neighborhood.', 'Boundary / bridge package in the current explanatory neighborhood.')}</div> : null}
              {selectedPackage.critical ? <div className="graph-details-note critical"><AlertTriangle size={14} /> Critical security</div> : null}
              <div className="graph-authority-note">{text('Статус узла отображает существующие Flow/user-policy facts. Сам граф ничего не доказывает и не создаёт constraints.', 'Node status projects existing Flow/user-policy facts. The graph itself proves nothing and creates no constraints.')}</div>
            </>
          ) : selectedGroup ? (
            <>
              <div className="graph-details-heading"><Layers3 size={17} /><div><strong>{selectedGroup.label}</strong><span>{selectedGroup.authority}</span></div></div>
              <dl>
                <div><dt>{text('Пакеты', 'Packages')}</dt><dd>{selectedGroup.packages.length}</dd></div>
                <div><dt>{text('Состояние', 'State')}</dt><dd>{selectedGroup.active ? 'active suggestion' : selectedGroup.deferred ? 'deferred' : selectedGroup.state}</dd></div>
                {typeof selectedGroup.confidence === 'number' ? <div><dt>Confidence</dt><dd>{Math.round(selectedGroup.confidence * 100)}%</dd></div> : null}
                {selectedGroup.predicate ? <div><dt>Predicate</dt><dd><code>{selectedGroup.predicate}</code></dd></div> : null}
              </dl>
              <div className="graph-detail-packages">{selectedGroup.packages.map((name) => <code key={name}>{name}</code>)}</div>
              {selectedGroup.boundaryPackages.length ? <div className="graph-details-note bridge"><CircleDot size={14} /> Bridge: {selectedGroup.boundaryPackages.join(', ')}</div> : null}
              <div className="graph-authority-note">{selectedGroup.authority === 'USER_POLICY' ? text('Deferred state — подтверждённая пользовательская scope policy. Состав группы, confidence и причины остаются explanatory metadata.', 'Deferred state is confirmed user scope policy. Group composition, confidence and reasons remain explanatory metadata.') : text('Эта группа — навигационный/search hint, не Solver component и не доказательство incompatibility.', 'This group is a navigation/search hint, not a Solver component or proof of incompatibility.')}</div>
            </>
          ) : (
            <>
              <div className="graph-details-heading"><Network size={17} /><div><strong>{text('Как читать граф', 'How to read the graph')}</strong><span>projection only</span></div></div>
              <div className="graph-explain-list">
                <p><CheckCircle2 size={14} /> <strong>Verified Scope</strong> {text('показывает область последнего завершённого Baseline/Flow state.', 'shows the area represented by the latest completed Baseline/Flow state.')}</p>
                <p><CircleDot size={14} /> <strong>Groups</strong> {text('— динамические neighborhoods. Они могут пересекаться через bridge packages.', 'are dynamic neighborhoods and may overlap through bridge packages.')}</p>
                <p><Network size={14} /> <strong>Relations</strong> {text('берутся из локально наблюдаемых package manifests, если node_modules доступен.', 'come from locally observed package manifests when node_modules is available.')}</p>
                <p><ShieldCheck size={14} /> {text('Ни один визуальный элемент не является proof authority.', 'No visual element is proof authority.')}</p>
              </div>
              {snapshot.manifestCoverage.missing.length ? <div className="graph-details-note"><AlertTriangle size={14} />{text(`Для ${snapshot.manifestCoverage.missing.length} direct package(s) локальный manifest не найден; соответствующие relation edges скрыты, а не угаданы.`, `${snapshot.manifestCoverage.missing.length} direct package manifest(s) are unavailable; those relation edges are omitted rather than guessed.`)}</div> : null}
            </>
          )}
        </aside>
      </div>
    </section>
  )
}
