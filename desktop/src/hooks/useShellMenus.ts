import { useEffect } from 'react'
/** Native details keep keyboard semantics; dismiss only shell action menus. */
export function useShellMenus() {
  useEffect(() => {
    const menus = () => [...document.querySelectorAll<HTMLDetailsElement>('.app-settings-menu[open], .project-actions-menu[open], .app-update-control[open]')]
    const pointer = (event: PointerEvent) => { for (const menu of menus()) if (event.target instanceof Node && !menu.contains(event.target)) menu.open = false }
    const key = (event: KeyboardEvent) => { if (event.key !== 'Escape') return; const opened = menus(); if (!opened.length) return; event.preventDefault(); for (const menu of opened) menu.open = false; opened.at(-1)?.querySelector<HTMLElement>('summary')?.focus() }
    document.addEventListener('pointerdown', pointer); document.addEventListener('keydown', key)
    return () => { document.removeEventListener('pointerdown', pointer); document.removeEventListener('keydown', key) }
  }, [])
}
