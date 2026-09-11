import { Check, Clipboard, ExternalLink, FileText, X } from 'lucide-react'
import { useEffect, useState } from 'react'

import { useLanguage } from '../i18n'
import type { ProjectPromptPreview } from '../types'

type Props = {
  preview: ProjectPromptPreview
  onClose: () => void
  onOpenPath: (path?: string) => Promise<void>
}

export function PromptPreviewDialog({ preview, onClose, onOpenPath }: Props) {
  const { text } = useLanguage()
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const copyPrompt = async () => {
    try {
      await navigator.clipboard.writeText(preview.content)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error))
    }
  }

  return (
    <div className="baseline-intent-backdrop" role="presentation">
      <section className="baseline-intent-dialog" role="dialog" aria-modal="true" aria-labelledby="draft-prompt-title">
        <header className="baseline-intent-header">
          <div>
            <FileText size={18} />
            <div>
              <strong id="draft-prompt-title">{text('Draft готов — промпт для агента', 'Draft is ready — agent prompt')}</strong>
              <span>{text('Это planning-only результат. Compatibility и корректность ещё не подтверждены physical verification.', 'This is a planning-only result. Compatibility and correctness are not yet proven by physical verification.')}</span>
            </div>
          </div>
          <button type="button" className="icon-button" aria-label={text('Закрыть', 'Close')} onClick={onClose}><X size={17} /></button>
        </header>

        <div className="baseline-fast-flow">
          <div>
            <strong>{text('Можно сразу передать коллегам или внешнему агенту', 'Ready to share with colleagues or an external agent')}</strong>
            <span>{text('Dashboard для Draft не требуется: состав зависимостей уже выбран до запуска, а ниже показан готовый handoff prompt.', 'Draft does not require the Dashboard: dependency scope was selected before the run and the ready handoff prompt is shown below.')}</span>
          </div>
        </div>

        <pre data-testid="draft-prompt-preview" style={{ margin: 0, maxHeight: '56vh', overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', padding: '16px', borderRadius: '10px', border: '1px solid var(--border, rgba(127, 127, 127, .3))', background: 'var(--panel, rgba(0, 0, 0, .08))', fontSize: '12px', lineHeight: 1.55 }}>{preview.content}</pre>

        {preview.stale ? <div className="resume-notice warning"><strong>{text('Prompt помечен как stale', 'Prompt is marked stale')}</strong><span>{text('Draft всё равно показан как planning artifact, но не используйте его как verified evidence.', 'The Draft is still shown as a planning artifact, but do not treat it as verified evidence.')}</span></div> : null}

        <footer className="baseline-intent-actions">
          <span className="baseline-intent-apply-hint" title={preview.path}>{preview.path}</span>
          <button type="button" className="button secondary" onClick={() => void onOpenPath(preview.path)}><ExternalLink size={16} />{text('Открыть файл', 'Open file')}</button>
          <button type="button" className="button primary" onClick={() => void copyPrompt()}>{copied ? <Check size={16} /> : <Clipboard size={16} />}{copied ? text('Скопировано', 'Copied') : text('Скопировать промпт', 'Copy prompt')}</button>
        </footer>
      </section>
    </div>
  )
}
