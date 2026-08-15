import { AlertCircle, X } from 'lucide-react'
import type { MigrationBranchProgress } from '../types'

type Props = { branch: MigrationBranchProgress; onClose: () => void }

export function BranchFailureModal({ branch, onClose }: Props) {
  const requiresUserAction = /USER_ACTION_REQUIRED|APPROVAL_REQUIRED|SAFETY_STOP|MANUAL_INTERVENTION_REQUIRED/i.test(branch.runtime?.detail ?? '')
  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={`Причина ошибки ${branch.label}`} onClick={onClose}>
    <div className="modal-card migration-error-modal" onClick={(event) => event.stopPropagation()}>
      <div className="modal-head"><div><AlertCircle className={requiresUserAction ? "danger-text" : "warning-text"} size={18} /><strong>{requiresUserAction ? "Требуется вмешательство" : "Автопилот исправит"}</strong></div><button type="button" className="icon-button" title="Закрыть" onClick={onClose}><X size={17} /></button></div>
      <div className="modal-summary"><div><span>Группа</span><strong>{branch.label}</strong></div><div><span>Ветка</span><strong>{branch.branch}</strong></div><div><span>Цели</span><strong>{branch.metPackages} из {branch.packages.length}</strong></div></div>
      <section className="modal-section"><h4>Причина</h4><pre className={`migration-error-detail ${requiresUserAction ? "error" : "warning"}`}>{branch.runtime?.detail || 'Worker завершился без подробного сообщения.'}</pre></section>
      <p className="modal-note">{requiresUserAction ? "Автономное продолжение остановлено на safety boundary: прочитайте причину выше и устраните указанное условие." : "Вмешательство пользователя не требуется: после завершения активных siblings автопилот передаст эту причину Supervisor и перестроит residual plan."}</p>
      <div className="modal-actions"><button type="button" className="button primary" onClick={onClose}>Понятно</button></div>
    </div>
  </div>
}