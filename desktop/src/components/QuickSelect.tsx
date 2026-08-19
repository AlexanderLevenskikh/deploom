import { Check, ChevronDown } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'

export type QuickSelectOption = {
  value: string
  label: string
  disabled?: boolean
}

type Props = {
  value: string
  options: readonly QuickSelectOption[]
  onChange: (value: string) => void
  ariaLabel: string
  className?: string
  disabled?: boolean
}

export function QuickSelect({ value, options, onChange, ariaLabel, className = '', disabled = false }: Props) {
  const [open, setOpen] = useState(false)
  const [menuStyle, setMenuStyle] = useState<CSSProperties>({})
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const selected = options.find((option) => option.value === value) ?? options[0]

  useLayoutEffect(() => {
    if (!open) return
    const reposition = () => {
      const button = buttonRef.current
      if (!button) return
      const rect = button.getBoundingClientRect()
      const padding = 8
      const gap = 4
      const menuMaxHeight = Math.max(120, Math.min(320, window.innerHeight - padding * 2))
      const width = Math.max(rect.width, 180)
      const left = Math.max(padding, Math.min(rect.left, window.innerWidth - width - padding))
      const roomBelow = window.innerHeight - rect.bottom - padding - gap
      const roomAbove = rect.top - padding - gap
      const useBelow = roomBelow >= Math.min(180, menuMaxHeight) || roomBelow >= roomAbove
      const maxHeight = Math.max(90, Math.min(menuMaxHeight, useBelow ? roomBelow : roomAbove))
      const top = useBelow
        ? Math.min(window.innerHeight - padding - maxHeight, rect.bottom + gap)
        : Math.max(padding, rect.top - gap - maxHeight)
      setMenuStyle({ position: 'fixed', left, top, width, maxHeight })
    }
    reposition()
    window.addEventListener('resize', reposition)
    window.addEventListener('scroll', reposition, true)
    return () => {
      window.removeEventListener('resize', reposition)
      window.removeEventListener('scroll', reposition, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const closeOnOutside = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (target && (buttonRef.current?.contains(target) || menuRef.current?.contains(target))) return
      setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setOpen(false)
        buttonRef.current?.focus()
      }
    }
    document.addEventListener('pointerdown', closeOnOutside, true)
    document.addEventListener('keydown', closeOnEscape, true)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutside, true)
      document.removeEventListener('keydown', closeOnEscape, true)
    }
  }, [open])

  const choose = (next: string) => {
    if (next !== value) onChange(next)
    setOpen(false)
    window.requestAnimationFrame(() => buttonRef.current?.focus())
  }

  return (
    <span className={`quick-select ${className}`.trim()}>
      <button
        ref={buttonRef}
        type="button"
        className={`quick-select-trigger${open ? ' open' : ''}`}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{selected?.label ?? value}</span>
        <ChevronDown size={14} />
      </button>
      {open ? createPortal(
        <div ref={menuRef} className="quick-select-menu" style={menuStyle} role="listbox" aria-label={ariaLabel}>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              role="option"
              aria-selected={option.value === value}
              className={option.value === value ? 'selected' : ''}
              disabled={option.disabled}
              onClick={() => choose(option.value)}
            >
              <span>{option.label}</span>
              {option.value === value ? <Check size={14} /> : null}
            </button>
          ))}
        </div>,
        document.body,
      ) : null}
    </span>
  )
}
