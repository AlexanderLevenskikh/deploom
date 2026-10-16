import { Boxes, FolderPlus, HardDrive } from 'lucide-react'
import { useLanguage } from '../i18n'
import type { EnvironmentInfo, ProjectSpec, StorageStatus, WorkspaceDetails } from '../types'

type Props = {
  details: WorkspaceDetails
  environment: EnvironmentInfo
  storageStatus?: StorageStatus
  selectedName?: string
  onSelectProject: (name: string) => void
  onAddProject: () => void
  onAuditProject?: (name: string) => void
  auditBlocked?: boolean
  onOpenStorage: () => void
}

function OverviewFact({ label, value, ok }: { label: string; value: string; ok?: boolean }) {
  const dot = ok === undefined ? null : <i className={`status-dot ${ok ? 'success' : 'danger'}`} />
  return (
    <div className="overview-fact">
      <span>{label}</span>
      <strong>{dot}{value}</strong>
    </div>
  )
}

export function WorkspaceOverview({ details, environment, storageStatus, selectedName, onSelectProject, onAddProject, onOpenStorage, onAuditProject, auditBlocked }: Props) {
  const { t, text } = useLanguage()
  const agentAvailable = environment[details.workspace.agent]?.available
  const pythonAvailable = environment.python?.available
  const volumes = storageStatus?.result?.volumes ?? []
  const freeBytes = volumes.length ? volumes.map((volume) => volume.freeBytes).reduce((a, b) => a + b, 0) : undefined

  return (
    <section className="workspace-overview" aria-label={t('app.overview.title')}>
      <header className="overview-hero">
        <Boxes size={22} />
        <div>
          <h2>{details.workspace.name}</h2>
          <p>{t('app.overview.subtitle')}</p>
        </div>
      </header>

      <div className="overview-facts">
        <OverviewFact label={t('app.overview.path')} value={details.workspace.path} />
        <OverviewFact label={t('app.overview.templateBranch')} value={details.git.branch} />
        <OverviewFact label={t('app.overview.agent')} value={details.workspace.agent} ok={agentAvailable} />
        <OverviewFact label={t('app.overview.python')} value={pythonAvailable ? t('app.overview.ready') : t('app.overview.missing')} ok={pythonAvailable} />
        <OverviewFact
          label={t('app.overview.storage')}
          value={typeof freeBytes === 'number' ? `${Math.round(freeBytes / 1024 ** 3)} GB` : t('app.overview.storageCleanup')}
        />
      </div>
      <div className="overview-actions">
        <button className="button secondary" onClick={onOpenStorage}><HardDrive size={15} /> {t('app.overview.storageCleanup')}</button>
      </div>

      <div className="overview-projects">
        <div className="overview-projects-title"><strong>{t('app.overview.projects')}</strong><span className="rail-count">{details.projects.length}</span></div>
        <div className="overview-project-list">
          {details.projects.map((project: ProjectSpec) => (
            <div key={project.name} className="overview-project-with-actions" role="group" aria-label={project.name} onClick={(event) => { if (event.target instanceof Element && event.target.closest('button')) return; onSelectProject(project.name) }}><button
              className={`project-row-main overview-project-row${selectedName === project.name ? ' selected' : ''}`}
              onClick={(event) => { event.stopPropagation(); onSelectProject(project.name) }}
            >
              <span className="status-dot muted" />
              <span className="project-copy"><strong>{project.name}</strong><small>{project.path}</small></span>
            </button>{onAuditProject ? <button className="button secondary" disabled={auditBlocked} onClick={(event) => { event.stopPropagation(); onAuditProject(project.name) }}>{text('Провести аудит', 'Run audit')}</button> : null}</div>
          ))}
          {details.projects.length === 0 ? <p className="empty-copy">{t('app.overview.projectsEmpty')}</p> : null}
        </div>
        <div className="overview-actions">
          <button className="button secondary" onClick={onAddProject}><FolderPlus size={15} /> {t('projectRail.addProject')}</button>
        </div>
      </div>
    </section>
  )
}
