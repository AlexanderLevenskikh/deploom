import { Check, ChevronDown } from 'lucide-react'
import { useMemo, useRef, useState } from 'react'

type Props = {
  value: string
  options: readonly string[]
  placeholder: string
  ariaLabel: string
  title?: string
  onChange: (value: string) => void
  onCommit: (value: string) => Promise<void>
}

export function ModelPicker({ value, options, placeholder, ariaLabel, title, onChange, onCommit }: Props) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const closeTimer = useRef<number | undefined>(undefined)
  const needle = value.trim().toLowerCase()
  const visible = useMemo(() => options.filter((option) => !needle || option.toLowerCase().includes(needle)).slice(0, 40), [needle, options])

  const commit = async (next = value) => {
    setBusy(true)
    try { await onCommit(next) } finally { setBusy(false) }
  }

  const choose = async (option: string) => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current)
    onChange(option)
    setOpen(false)
    await commit(option)
  }

  return (
    <div className="model-picker">
      <div className="model-picker-input">
        <input
          aria-label={ariaLabel}
          value={value}
          placeholder={placeholder}
          title={title}
          autoComplete="off"
          spellCheck={false}
          disabled={busy}
          onFocus={() => setOpen(true)}
          onChange={(event) => { onChange(event.target.value); setOpen(true) }}
          onBlur={() => {
            closeTimer.current = window.setTimeout(() => setOpen(false), 120)
            void commit()
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              setOpen(false)
              void commit()
            }
            if (event.key === 'Escape') setOpen(false)
          }}
        />
        <button type="button" className="model-picker-toggle" tabIndex={-1} aria-label={ariaLabel} onMouseDown={(event) => event.preventDefault()} onClick={() => setOpen((current) => !current)}><ChevronDown size={14} /></button>
      </div>
      {open && visible.length ? <div className="model-picker-menu" role="listbox" aria-label={ariaLabel}>
        {visible.map((option) => <button key={option} type="button" role="option" aria-selected={option === value} onMouseDown={(event) => event.preventDefault()} onClick={() => void choose(option)}><span>{option}</span>{option === value ? <Check size={13} /> : null}</button>)}
      </div> : null}
    </div>
  )
}
