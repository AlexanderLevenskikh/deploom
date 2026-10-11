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

function optionId(menuId: string, index: number): string {
  return `${menuId}-option-${index}`
}

export function QuickSelect({ value, options, onChange, ariaLabel, className = '', disabled = false }: Props) {
  const [open, setOpen] = useState(false)
  const [menuStyle, setMenuStyle] = useState<CSSProperties>({})
  const [activeIndex, setActiveIndex] = useState(-1)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const optionRefs = useRef<Array<HTMLDivElement | null>>([])
  const menuId = useRef(`quick-select-${Math.random().toString(36).slice(2)}`).current
  const typeAheadTimer = useRef<number | undefined>(undefined)
  const typeAheadQuery = useRef('')
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
      if (typeAheadTimer.current !== undefined) window.clearTimeout(typeAheadTimer.current)
    }
  }, [open])

  const scrollActiveIntoView = (index: number) => {
    const node = optionRefs.current[index]
    node?.scrollIntoView({ block: 'nearest' })
  }

  const openMenu = () => {
    if (disabled) return
    const baseIndex = Math.max(0, options.findIndex((option) => option.value === value))
    setOpen(true)
    setActiveIndex(baseIndex)
    // Move focus onto the popup listbox (APG listbox popup pattern) so arrow
    // keys drive the active option instead of getting stuck on the trigger.
    window.requestAnimationFrame(() => {
      menuRef.current?.focus()
      scrollActiveIntoView(baseIndex)
    })
  }

  const choose = (next: string) => {
    if (next !== value) onChange(next)
    setOpen(false)
    setActiveIndex(-1)
    window.requestAnimationFrame(() => buttonRef.current?.focus())
  }

  const clamp = (index: number) => Math.max(0, Math.min(options.length - 1, index))

  const step = (direction: 1 | -1) => {
    let target = clamp(activeIndex < 0 ? (direction === 1 ? 0 : options.length - 1) : activeIndex + direction)
    for (let guard = 0; guard < options.length && options[target]?.disabled; guard += 1) target = clamp(target + direction)
    if (target !== activeIndex) { setActiveIndex(target); scrollActiveIntoView(target) }
  }

  const handleListKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault(); step(1)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault(); step(-1)
    } else if (event.key === 'Home') {
      event.preventDefault(); const target = clamp(0); setActiveIndex(target); scrollActiveIntoView(target)
    } else if (event.key === 'End') {
      event.preventDefault(); const target = clamp(options.length - 1); setActiveIndex(target); scrollActiveIntoView(target)
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      const option = options[activeIndex]
      if (option && !option.disabled) choose(option.value)
    } else if (event.key === 'Escape') {
      event.preventDefault(); choose(value)
    } else if (event.key === 'Tab') {
      // APG listbox popup: Tab commits the active option and closes the popup.
      // Without this, focus jumps out of the portal while a dangling menu stays
      // open, which breaks keyboard navigation.
      event.preventDefault()
      const option = options[activeIndex]
      choose(option && !option.disabled ? option.value : value)
    } else if (event.key.length === 1) {
      // Type-ahead: accumulate a short prefix and jump to the first match.
      typeAheadQuery.current = (typeAheadQuery.current + event.key.toLowerCase()).slice(-24)
      if (typeAheadTimer.current !== undefined) window.clearTimeout(typeAheadTimer.current)
      typeAheadTimer.current = window.setTimeout(() => { typeAheadQuery.current = '' }, 500)
      const query = typeAheadQuery.current
      const match = options.findIndex((option) => !option.disabled && option.label.toLowerCase().startsWith(query))
      if (match >= 0) { event.preventDefault(); setActiveIndex(match); scrollActiveIntoView(match) }
    }
  }

  const onTriggerKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Enter' || event.key === ' ') {
      if (event.key === 'Enter' || event.key === ' ') return
      event.preventDefault()
      if (!open) openMenu()
    }
  }

  useEffect(() => {
    if (!open) return
    const closeOnOutside = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (target && (buttonRef.current?.contains(target) || menuRef.current?.contains(target))) return
      setOpen(false)
      setActiveIndex(-1)
    }
    document.addEventListener('pointerdown', closeOnOutside, true)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutside, true)
    }
  }, [open])

  return (
    <span className={`quick-select ${className}`.trim()}>
      <button
        ref={buttonRef}
        type="button"
        className={`quick-select-trigger${open ? ' open' : ''}`}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled}
        onClick={() => (open ? choose(value) : openMenu())}
        onKeyDown={onTriggerKeyDown}
      >
        <span>{selected?.label ?? value}</span>
        <ChevronDown size={14} />
      </button>
      {open ? createPortal(
        <div
          ref={menuRef}
          id={menuId}
          className="quick-select-menu"
          style={menuStyle}
          role="listbox"
          aria-label={ariaLabel}
          tabIndex={-1}
          aria-activedescendant={activeIndex >= 0 ? optionId(menuId, activeIndex) : undefined}
          onKeyDown={handleListKeyDown}
        >
          {options.map((option, index) => {
            const active = index === activeIndex
            return (
              <div
                key={option.value}
                ref={(node) => { optionRefs.current[index] = node }}
                id={optionId(menuId, index)}
                role="option"
                aria-selected={option.value === value}
                aria-disabled={option.disabled || undefined}
                className={`${option.value === value ? 'selected' : ''}${active ? ' active' : ''}`}
                onMouseEnter={() => { if (!option.disabled) setActiveIndex(index) }}
                onClick={() => { if (!option.disabled) choose(option.value) }}
              >
                <span>{option.label}</span>
                {option.value === value ? <Check size={14} /> : null}
              </div>
            )
          })}
        </div>,
        document.body,
      ) : null}
    </span>
  )
}
