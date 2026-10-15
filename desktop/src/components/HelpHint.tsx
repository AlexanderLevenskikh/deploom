import { CircleHelp } from 'lucide-react'
import { useId, type ReactNode } from 'react'
import './HelpHint.css'

export function HelpHint({ label, children }: { label: string; children: ReactNode }) {
  const id = useId()
  return <span className="help-hint"><button type="button" className="help-hint-trigger" aria-label={label} aria-describedby={id}><CircleHelp size={15} /></button><span className="help-hint-content" role="tooltip" id={id}>{children}</span></span>
}
