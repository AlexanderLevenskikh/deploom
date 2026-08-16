import { ExternalLink, FileQuestion, RefreshCw } from 'lucide-react'
import { useLanguage } from '../i18n'
import type { WorkspaceDetails } from '../types'

type Props = { details: WorkspaceDetails; onRefresh: () => Promise<void>; onOpenExternal: () => Promise<void> }

export function DashboardWorkspace({ details, onRefresh, onOpenExternal }: Props) {
  const { t } = useLanguage()
  return (
    <section className="dashboard-workspace">
      <div className="dashboard-toolbar">
        <div><strong>{t('dashboard.title')}</strong><span>{details.dashboardExists ? t('dashboard.fresh') : t('dashboard.missing')}</span></div>
        <div><button className="button secondary" onClick={() => void onRefresh()}><RefreshCw size={16} /> {t('common.refresh')}</button><button className="button secondary" disabled={!details.dashboardExists} onClick={() => void onOpenExternal()}><ExternalLink size={16} /> {t('common.openSeparately')}</button></div>
      </div>
      {details.dashboardUrl ? <iframe className="dashboard-frame" title="Dependency roadmap dashboard" src={details.dashboardUrl} sandbox="allow-scripts allow-forms allow-downloads allow-popups allow-same-origin" /> : <div className="dashboard-empty"><FileQuestion size={38} /><h2>{t('dashboard.buildFirst')}</h2><p>{t('dashboard.buildHint')}</p></div>}
    </section>
  )
}
