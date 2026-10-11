/** Prefix sums for measured log rows; unmeasured rows use the caller's estimate. */
export function rawLogOffsets(heights: readonly number[]): number[] {
  const offsets = [0]
  for (const height of heights) offsets.push(offsets[offsets.length - 1] + Math.max(1, Number.isFinite(height) ? height : 16))
  return offsets
}

/** Find the row containing a scroll offset, clamped even after a buffer shrink. */
function rowAt(offsets: readonly number[], position: number): number {
  let low = 0
  let high = Math.max(0, offsets.length - 2)
  while (low < high) {
    const middle = Math.ceil((low + high) / 2)
    if (offsets[middle] <= position) low = middle
    else high = middle - 1
  }
  return low
}

export function rawLogWindow(offsets: readonly number[], scrollTop: number, viewport: number, overscan: number): { start: number; end: number } {
  const count = Math.max(0, offsets.length - 1)
  if (!count) return { start: 0, end: 0 }
  const top = Math.max(0, Math.min(scrollTop, offsets[count]))
  const start = Math.max(0, rowAt(offsets, top) - overscan)
  const end = Math.min(count, Math.max(start + 1, rowAt(offsets, top + Math.max(1, viewport)) + 1 + overscan))
  return { start, end }
}