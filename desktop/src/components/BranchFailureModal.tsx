import { AlertCircle, X } from 'lucide-react'
import { useLanguage } from '../i18n'
import type { MigrationBranchProgress } from '../types'

type Props = { branch: MigrationBranchProgress; onClose: () => void }

export function BranchFailureModal({ branch, onClose }: Props) {
  const { t } = useLanguage()
  const requiresUserAction = /USER_ACTION_REQUIRED|APPROVAL_REQUIRED|SAFETY_STOP|MANUAL_INTERVENTION_REQUIRED/i.test(branch.runtime?.detail ?? '')
  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={t('flow.failureReasonAria', { label: branch.label })} onClick={onClose}>
    <div className="modal-card migration-error-modal" onClick={(event) => event.stopPropagation()}>
      <div className="modal-head"><div><AlertCircle className={requiresUserAction ? 'danger-text' : 'warning-text'} size={18} /><strong>{requiresUserAction ? t('branchFailure.userRequired') : t('branchFailure.autopilot')}</strong></div><button type="button" className="icon-button" title={t('common.close')} onClick={onClose}><X size={17} /></button></div>
      <div className="modal-summary"><div><span>{t('branchFailure.group')}</span><strong>{branch.label}</strong></div><div><span>{t('branchFailure.branch')}</span><strong>{branch.branch}</strong></div><div><span>{t('branchFailure.targets')}</span><strong>{branch.metPackages} / {branch.packages.length}</strong></div></div>
      <section className="modal-section"><h4>{t('branchFailure.reason')}</h4><pre className={`migration-error-detail ${requiresUserAction ? 'error' : 'warning'}`}>{branch.runtime?.detail || t('branchFailure.noDetail')}</pre></section>
      <p className="modal-note">{requiresUserAction ? t('branchFailure.userNote') : t('branchFailure.autoNote')}</p>
      <div className="modal-actions"><button type="button" className="button primary" onClick={onClose}>{t('common.understood')}</button></div>
    </div>
  </div>
}
