// A07: the run-action effective target must prefer an explicit caller input
// over a persisted green run. A saved green overriding an explicit yellow made
// a planned yellow release silently run green (sticky goal). Extract the real
// production expression from main.ts so this contract cannot drift.
import fs from 'node:fs'

const source = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const marker = 'const effectiveTarget: ClosureTarget = '
const start = source.indexOf(marker)
if (start < 0) throw new Error('effectiveTarget expression not found in main.ts')
const exprStart = start + marker.length
const end = source.indexOf('input.target = effectiveTarget', start)
if (end < 0) throw new Error('effectiveTarget assignment not found in main.ts')
const expression = source.slice(exprStart, end).trim().replace(/;\s*$/, '')
const effectiveTarget = new Function('savedRun', 'input', `return ${expression}`)

// A07: an explicit caller target is authoritative.
if (effectiveTarget({ target: 'green' }, { target: 'yellow' }) !== 'yellow') {
  throw new Error('explicit yellow must win over a persisted green')
}
if (effectiveTarget({ target: 'green' }, { target: 'green' }) !== 'green') {
  throw new Error('explicit green must be honoured')
}
// Persisted green is only a fallback when the caller passes no target.
if (effectiveTarget({ target: 'green' }, {}) !== 'green') {
  throw new Error('persisted green must apply when the caller passes no target')
}
// Defaults to yellow otherwise.
if (effectiveTarget({}, {}) !== 'yellow') {
  throw new Error('no target anywhere must default to yellow')
}
if (effectiveTarget({ target: 'yellow' }, {}) !== 'yellow') {
  throw new Error('persisted yellow must be honoured as a fallback')
}

console.log('Effective target precedence OK')
