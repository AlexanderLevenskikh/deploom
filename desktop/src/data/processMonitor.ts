import type { JobOutput } from '../types'

export type RunMonitorPhase =
  | 'idle'
  | 'scan'
  | 'planning'
  | 'solving'
  | 'verifying'
  | 'localizing'
  | 'reproducing'
  | 'retrying'
  | 'running'
  | 'finished'

export type RunMonitorState = {
  phase: RunMonitorPhase
  active: boolean
  currentOperation?: string
  elapsedSeconds?: number
  dependency?: { current: number; total: number; name: string }
  projectCheck?: { current: number; total: number; name: string; status?: 'pass' | 'red' }
  solver?: { componentsDone?: number; componentsTotal?: number; changed?: number; constraints?: number }
  assignment?: string
  localization?: {
    initialUnits: number
    currentUnits: number
    packages?: number
    checksStarted: number
    maxChecks: number
    activeChecks?: number
    completedChecks?: number
    shrinkHistory: number[]
  }
  reproduction?: { current: number; total: number; literals?: number }
  retry?: { current: number; total: number }
}

function number(value: string | undefined): number | undefined {
  if (value === undefined) return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function fieldNumber(line: string, field: string): number | undefined {
  const match = new RegExp(`(?:^|[,;]\\s*)${field}=(\\d+(?:\\.\\d+)?)`, 'i').exec(line)
  return number(match?.[1])
}

function pushShrink(history: number[], units: number): number[] {
  if (history[history.length - 1] === units) return history
  return [...history, units]
}

export function deriveRunMonitor(logs: readonly JobOutput[], active: boolean, jobId?: string): RunMonitorState {
  const scoped = jobId ? logs.filter((entry) => entry.jobId === jobId) : logs
  let state: RunMonitorState = { phase: active ? 'running' : 'idle', active }

  for (const entry of scoped) {
    if (entry.stream !== 'system') continue
    const line = entry.line.trim()
    let match: RegExpExecArray | null

    match = /\[dependency (\d+)\/(\d+)\]\s+([^:]+):/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'scan',
        dependency: { current: Number(match[1]), total: Number(match[2]), name: match[3].trim() },
      }
      continue
    }

    if (/target planning started/i.test(line)) {
      state = { ...state, phase: 'planning', dependency: undefined, currentOperation: undefined }
      continue
    }

    match = /peer solver \w+;\s+packages=(\d+),\s+components=(\d+),\s+candidates=(\d+)/i.exec(line)
    if (match) {
      state = { ...state, phase: 'solving', dependency: undefined, currentOperation: `Z3 · ${match[2]} components · ${match[3]} candidates` }
      continue
    }

    match = /exact z3 \w+ SUMMARY;\s+components=(\d+)\/(\d+),\s+changed=(\d+),\s+constraints=(\d+)/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'solving',
        solver: {
          componentsDone: Number(match[1]),
          componentsTotal: Number(match[2]),
          changed: Number(match[3]),
          constraints: Number(match[4]),
        },
      }
      continue
    }

    match = /Baseline verify \w+ iteration \d+:\s+materializing \d+ changed direct package\(s\),\s+assignment=([a-f0-9]+)/i.exec(line)
    if (match) {
      state = { ...state, phase: 'verifying', dependency: undefined, assignment: match[1], currentOperation: undefined }
      continue
    }

    match = /project check (\d+)\/(\d+) started:\s+(.+)$/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'verifying',
        projectCheck: { current: Number(match[1]), total: Number(match[2]), name: match[3].trim() },
      }
      continue
    }

    match = /project check (\d+)\/(\d+) (PASS|RED)(?: exit=\d+)?:\s+(.+)$/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'verifying',
        projectCheck: {
          current: Number(match[1]),
          total: Number(match[2]),
          name: match[4].trim(),
          status: match[3].toUpperCase() === 'PASS' ? 'pass' : 'red',
        },
      }
      continue
    }

    match = /Baseline localization \w+ started;\s+units=(\d+),\s+maxChecks=(\d+)/i.exec(line)
    if (match) {
      const units = Number(match[1])
      state = {
        ...state,
        phase: 'localizing',
        projectCheck: undefined,
        currentOperation: undefined,
        localization: {
          initialUnits: units,
          currentUnits: units,
          checksStarted: 0,
          maxChecks: Number(match[2]),
          shrinkHistory: [units],
        },
      }
      continue
    }

    match = /Baseline localization \w+ (?:wave-start|heartbeat);.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+)/i.exec(line)
    if (match && state.localization) {
      const currentUnits = Number(match[3])
      state = {
        ...state,
        phase: 'localizing',
        elapsedSeconds: fieldNumber(line, 'elapsedSeconds') ?? state.elapsedSeconds,
        localization: {
          ...state.localization,
          currentUnits,
          checksStarted: Number(match[1]),
          maxChecks: Number(match[2]),
          activeChecks: fieldNumber(line, 'active') ?? state.localization.activeChecks,
          completedChecks: fieldNumber(line, 'completed') ?? state.localization.completedChecks,
          shrinkHistory: pushShrink(state.localization.shrinkHistory, currentUnits),
        },
      }
      continue
    }

    match = /Baseline localization \w+ shrink;.*currentUnits=(\d+).*units=(\d+),\s+packages=(\d+)/i.exec(line)
    if (match && state.localization) {
      const units = Number(match[2])
      state = {
        ...state,
        phase: 'localizing',
        elapsedSeconds: fieldNumber(line, 'elapsedSeconds') ?? state.elapsedSeconds,
        localization: {
          ...state.localization,
          currentUnits: units,
          packages: Number(match[3]),
          shrinkHistory: pushShrink(state.localization.shrinkHistory, units),
        },
      }
      continue
    }

    match = /Baseline localization \w+ finish;.*checksStarted=(\d+),\s+maxChecks=(\d+),\s+currentUnits=(\d+),\s+units=(\d+),\s+packages=(\d+)/i.exec(line)
    if (match && state.localization) {
      const units = Number(match[4])
      state = {
        ...state,
        phase: 'localizing',
        elapsedSeconds: fieldNumber(line, 'elapsedSeconds') ?? state.elapsedSeconds,
        localization: {
          ...state.localization,
          currentUnits: units,
          packages: Number(match[5]),
          checksStarted: Number(match[1]),
          maxChecks: Number(match[2]),
          activeChecks: 0,
          shrinkHistory: pushShrink(state.localization.shrinkHistory, units),
        },
      }
      continue
    }

    match = /Baseline reproduction \w+ (\d+)\/(\d+) started;\s+literals=(\d+)/i.exec(line)
    if (match) {
      state = {
        ...state,
        phase: 'reproducing',
        reproduction: { current: Number(match[1]), total: Number(match[2]), literals: Number(match[3]) },
      }
      continue
    }

    match = /transient retry (\d+)\/(\d+)/i.exec(line)
    if (match) {
      state = { ...state, phase: 'retrying', retry: { current: Number(match[1]), total: Number(match[2]) } }
      continue
    }

    match = /:\s*([^:]+):\s*running;\s*elapsed=(\d+(?:\.\d+)?)s;\s*hardTimeout=/i.exec(line)
    if (match) {
      state = {
        ...state,
        currentOperation: match[1].trim(),
        elapsedSeconds: Number(match[2]),
      }
      continue
    }

    match = /completed;\s*elapsed=(\d+(?:\.\d+)?)s/i.exec(line)
    if (match) state = { ...state, elapsedSeconds: Number(match[1]) }
  }

  if (!active && state.phase !== 'idle') state = { ...state, active: false, phase: 'finished' }
  return state
}