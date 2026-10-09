import { ExternalLink, LoaderCircle } from 'lucide-react'
import { useLanguage } from '../i18n'
import './AuditGoalSummary.css'
import type { AuditView } from '../../electron/iterative-audit-view'

export function AuditGoalSummary({ audit, initial, onOpenPath }: { audit?: AuditView; initial?: AuditView; onOpenPath: (path?: string) => Promise<void> }) {
  const { text } = useLanguage()
  const known = Boolean(audit && !audit.stale && !audit.running && audit.auditComplete !== false && ['PASS', 'FAIL'].includes(audit.status))
  const securityKnown = Boolean(audit && !audit.stale && !audit.running && (known || audit.securityComplete === true))
  const lagKnown = Boolean(audit && !audit.stale && !audit.running && (known || audit.lagComplete === true))
  const goalsMet = known && audit?.status === 'PASS' && (audit.requiredTargets ?? []).every(item => item.met)
  const totals = audit?.packageTotals
  const policy = audit?.policy
  const rows = [
    { label: 'Critical', value: totals?.critical, target: 0 },
    { label: 'High', value: totals?.high, target: policy?.maxKnownHigh },
    ...(policy?.maxKnownModerate !== undefined ? [{ label: 'Moderate', value: totals?.moderate, target: policy.maxKnownModerate }] : []),
    ...(policy?.maxKnownLow !== undefined ? [{ label: 'Low', value: totals?.low, target: policy.maxKnownLow }] : []),
  ]
  const lagTarget = policy?.targetLevel === 'green' ? 100 : policy?.minLagOkPct
  return <section className="audit-goals" aria-label={text('Аудит и выбранные цели', 'Audit and selected goals')} data-testid="audit-goals">
    <header><div><strong>{text('Аудит и выбранные цели', 'Audit and selected goals')}</strong><span>{audit?.running ? text('Проверяем исходное состояние…', 'Auditing the initial state…') : audit?.stale ? text('Данные устарели — нужен свежий аудит', 'Evidence is stale — a fresh audit is required') : audit?.checkpointId ? text(`Проверенный результат ${audit.checkpointId}`, `Verified result ${audit.checkpointId}`) : text('Исходное состояние проекта', 'Initial project state')}{audit?.generatedAt ? ` · ${new Date(audit.generatedAt).toLocaleString()}` : ''}</span></div>
      <strong className={known ? goalsMet ? 'success-text' : 'warning-text' : ''}>{audit?.running ? <LoaderCircle size={18} className="spin" /> : known ? goalsMet ? text('Выбранные цели выполнены', 'Selected goals met') : text('Цели ещё не выполнены', 'Goals not met yet') : audit?.stale ? text('Нужен свежий аудит', 'Fresh audit needed') : audit?.unknownPackages?.length ? text('Аудит частичный: есть неизвестные данные', 'Partial audit: some data unknown') : text('Аудит не подтверждён', 'Audit unconfirmed')}</strong>
    </header>
    <div className="audit-goals-grid">
      {rows.map(row => <div key={row.label}><span>{row.label}</span><strong><i className={`status-dot ${securityKnown && typeof row.value === 'number' && typeof row.target === 'number' ? row.value <= row.target ? 'success' : 'danger' : 'muted'}`} />{typeof row.value === 'number' ? row.value : '—'} <small>/ ≤{row.target ?? '?'}</small></strong></div>)}
      <div><span>{text('Актуальность', 'Freshness')}</span><strong><i className={`status-dot ${lagKnown && typeof audit?.lagOkPct === 'number' && typeof lagTarget === 'number' ? audit.lagOkPct >= lagTarget ? 'success' : 'danger' : 'muted'}`} />{typeof audit?.lagOkPct === 'number' ? `${audit.lagOkPct.toFixed(1)}%` : '—'} <small>/ ≥{lagTarget ?? '?'}%</small></strong><small>{audit?.lagOk ?? '?'}/{audit?.lagTotal ?? '?'} · {text('неизвестно', 'unknown')}: {audit?.lagUnknown ?? '?'}</small></div>
    </div>
    {securityKnown && audit?.vulnerablePackages?.some(p => p.severity === 'critical') ? <p className="danger-text">Critical: {audit.vulnerablePackages.filter(p => p.severity === 'critical').map(p => p.package).join(', ')}</p> : null}
    {audit?.requiredTargets?.length ? <div className="audit-required"><strong>{text('Обязательные версии', 'Required versions')}</strong>{audit.requiredTargets.map(item => <span key={item.package}><i className={`status-dot ${known ? item.met ? 'success' : 'danger' : 'muted'}`} />{item.package}: {item.current ?? '?'} → {item.target ?? text('цель не определена', 'target unresolved')}</span>)}</div> : null}
    {audit?.keptPackages?.length ? <p>{text('Сохраняем текущие версии', 'Keeping current versions')}: {audit.keptPackages.join(', ')}</p> : null}
    {initial?.generatedAt && initial.generatedAt !== audit?.generatedAt ? <p>{text('Исходный аудит', 'Initial audit')}: C{initial.packageTotals?.critical ?? '?'} / H{initial.packageTotals?.high ?? '?'} · {initial.lagOkPct ?? '?'}%</p> : null}
    {audit?.projectDir ? <p>{text('Проверено', 'Audited')}: {audit.projectDir}{audit.branch ? ` · ${audit.branch}` : ''}</p> : null}
    {audit?.unknownPackages?.length ? <details><summary>{text('Почему аудит частичный', 'Why the audit is partial')} ({audit.unknownPackages.length})</summary>{audit.unknownPackages.map(item => <p key={item.package}>{item.package}@{item.version ?? '?'}: {item.reason ?? text('Нет данных реестра', 'Registry data unavailable')}</p>)}</details> : null}
    {audit?.error ? <p role="alert" className="danger-text">{audit.error}</p> : null}
    <footer><span>{text('Полный аудит — при выборе проекта, после каждой принятой группы и после финала. Неизвестные данные показаны отдельно.', 'Full audit on project selection, after each accepted cohort and after completion. Unknown evidence is shown separately.')}</span>{audit?.evidenceRef ? <button className="button secondary" onClick={() => void onOpenPath(audit.evidenceRef)}><ExternalLink size={15} />{text('Доказательства аудита', 'Audit evidence')}</button> : null}</footer>
  </section>
}
