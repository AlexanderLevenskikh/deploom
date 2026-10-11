import { useEffect, useRef, type RefObject } from 'react'

const SELECTABLE = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/** Dialog controls reachable by keyboard, in tab order. Re-evaluated on every
 * key press: availability can change while the dialog is open (a discovery
 * finishes and enables a submit button, a section expands and adds controls),
 * so caching a first/last pair would trap focus on stale bounds. Elements with
 * no layout box (display:none, collapsed <details>) are skipped. */
function visibleFocusable(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(SELECTABLE)).filter((item) => item.getClientRects().length > 0)
}

type Options = {
  onClose?: () => void
  /** Whether to trap Tab inside the dialog while it is open. Default: true. */
  trap?: boolean
  /** Whether pressing Escape should call onClose. Default: true. */
  closeOnEscape?: boolean
}

/**
 * Shared dialog keyboard accessibility: moves focus into the dialog on open,
 * restores it to the previously focused element on unmount, fully traps Tab,
 * and closes on Escape. One implementation for every modal, so no dialog can
 * leave focus dangling outside or stay open after Escape.
 */
export function useDialogFocus<T extends HTMLElement>(ref: RefObject<T | null>, options: Options = {}): void {
  const { onClose, trap = true, closeOnEscape = true } = options
  const restoreRef = useRef<HTMLElement | null>(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const closeOnEscapeRef = useRef(closeOnEscape)
  closeOnEscapeRef.current = closeOnEscape

  useEffect(() => {
    if (typeof document === 'undefined') return
    restoreRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const root = ref.current
    if (!root) return
    // Prefer an explicitly marked non-destructive action (e.g. the primary
    // confirmation) over the first tabbable element, which is often the
    // top-right close button.
    const focusable = visibleFocusable(root)
    const preferred = focusable.find((item) => item.hasAttribute('data-autofocus')) ?? focusable[0]
    preferred?.focus()

    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (closeOnEscapeRef.current && onCloseRef.current) {
          event.preventDefault()
          onCloseRef.current()
        }
        return
      }
      if (!trap || event.key !== 'Tab' || !ref.current) return
      const current = document.activeElement instanceof HTMLElement ? document.activeElement : null
      const focusable = visibleFocusable(ref.current)
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey) {
        if (!current || current === first || !ref.current.contains(current)) { event.preventDefault(); last.focus() }
      } else if (!current || current === last || !ref.current.contains(current)) {
        event.preventDefault(); first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      restoreRef.current?.focus()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
}
