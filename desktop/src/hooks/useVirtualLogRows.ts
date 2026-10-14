import { useEffect, useMemo, useState, type RefObject, type UIEvent } from 'react'
import { rawLogOffsets, rawLogWindow } from '../data/rawLogWindow'
/** Measured row window shared by long activity histories. Full data stays in memory. */
export function useVirtualLogRows<T>(items: readonly T[], container: RefObject<HTMLDivElement | null>, active: boolean) {
  const [position, setPosition] = useState(0)
  const [viewport, setViewport] = useState(480)
  const [heights, setHeights] = useState(new Map<T, number>())
  const offsets = useMemo(() => rawLogOffsets(items.map(item => heights.get(item) ?? 70)), [items, heights])
  const { start, end } = rawLogWindow(offsets, position, viewport, 12)
  useEffect(() => {
    const element = container.current
    if (!element || !active) return
    let frame: number | undefined
    const measure = () => {
      setViewport(element.clientHeight); setPosition(element.scrollTop)
      setHeights(previous => {
        const next = new Map<T, number>()
        for (const item of items) { const height = previous.get(item); if (height !== undefined) next.set(item, height) }
        for (const row of element.querySelectorAll<HTMLElement>('[data-activity-row]')) {
          const item = items[Number(row.dataset.activityRow)]
          if (item !== undefined) next.set(item, Math.max(1, row.getBoundingClientRect().height))
        }
        return next.size === previous.size && [...next].every(([item,height]) => previous.get(item) === height) ? previous : next
      })
    }
    const schedule = () => { if (frame === undefined) frame = requestAnimationFrame(() => { frame = undefined; measure() }) }
    schedule()
    const observer = new ResizeObserver(schedule)
    observer.observe(element)
    for (const row of element.querySelectorAll<HTMLElement>('[data-activity-row]')) observer.observe(row)
    return () => { observer.disconnect(); if (frame !== undefined) cancelAnimationFrame(frame) }
  }, [items, container, active, start, end])
  return { start, end, before:offsets[start], after:offsets[items.length]-offsets[end], onScroll:(event:UIEvent<HTMLDivElement>)=>setPosition(event.currentTarget.scrollTop) }
}
