import { ExternalLink, FileQuestion, RefreshCw } from 'lucide-react'
import type { WorkspaceDetails } from '../types'

type Props = { details: WorkspaceDetails; onRefresh: () => Promise<void>; onOpenExternal: () => Promise<void> }

export function DashboardWorkspace({ details, onRefresh, onOpenExternal }: Props) {
  return (
    <section className="dashboard-workspace">
      <div className="dashboard-toolbar">
        <div><strong>Dependency dashboard</strong><span>{details.dashboardExists ? 'Свежий локальный HTML' : 'Отчёт ещё не создан'}</span></div>
        <div><button className="button secondary" onClick={() => void onRefresh()}><RefreshCw size={16} /> Обновить</button><button className="button secondary" disabled={!details.dashboardExists} onClick={() => void onOpenExternal()}><ExternalLink size={16} /> Открыть отдельно</button></div>
      </div>
      {details.dashboardUrl ? <iframe className="dashboard-frame" title="Dependency roadmap dashboard" src={details.dashboardUrl} sandbox="allow-scripts allow-forms allow-downloads allow-popups allow-same-origin" /> : <div className="dashboard-empty"><FileQuestion size={38} /><h2>Сначала постройте roadmap</h2><p>Запустите baseline или обычную генерацию во вкладке FLOW. После этого dashboard откроется здесь автоматически.</p></div>}
    </section>
  )
}