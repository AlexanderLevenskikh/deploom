import fs from 'node:fs'

const monitor = fs.readFileSync('src/data/processMonitor.ts', 'utf8')
const view = fs.readFileSync('src/components/RunMonitor.tsx', 'utf8')
const hook = fs.readFileSync('src/hooks/useDependencyFlow.ts', 'utf8')
const main = fs.readFileSync('electron/main.ts', 'utf8')

const requireText = (source, pattern, message) => {
  if (!pattern.test(source)) throw new Error(message)
}

requireText(hook, /startedAt:\s*number/, 'active logical run must persist startedAt')
requireText(hook, /activeRunStartedAt:\s*selectedActiveRun\?\.startedAt/, 'hook must expose active run start')
requireText(monitor, /runStartedAt !== undefined[\s\S]*entry\.receivedAt >= runStartedAt/, 'monitor must scope by logical run, not only child job id')
requireText(monitor, /runElapsedSeconds/, 'monitor must expose total logical run elapsed time')
requireText(monitor, /attemptElapsedSeconds/, 'monitor must expose current attempt elapsed time')
requireText(monitor, /Baseline solve-and-verify \\w\+ started;.*maxIterations/, 'monitor must parse Baseline iteration budget')
requireText(monitor, /Baseline verify \\w\+ iteration \(\\d\+\):.*assignment=/, 'monitor must parse exact-assignment iterations')
requireText(monitor, /graph certification \\w\+ \\d\+\\\/\\d\+/, 'monitor must recognize graph certification')
requireText(monitor, /blocked exact failing assignment/, 'monitor must recognize confirmed exact conflicts')
requireText(view, /if \(state\.dependency\)[\s\S]*else if \(state\.retry\)/, 'live work progress must take precedence over retry counter')
requireText(view, /monitor\.totalTime/, 'monitor must render total time')
requireText(view, /monitor\.attemptTime/, 'monitor must render current attempt time')
requireText(main, /BASELINE_CONSTRAINT_BUDGET_EXHAUSTED/, 'deterministic Baseline budget exhaustion must be non-retryable')

console.log('Run monitor logical-run / retry / elapsed-time contracts OK')
