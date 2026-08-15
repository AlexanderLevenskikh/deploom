import { summarizeUpdaterError } from '../dist-electron/updater-error.js'

const summarized = summarizeUpdaterError('<!DOCTYPE html>\n<html>\n<body>  Public GitHub release unavailable  </body>\n</html>')
if (summarized.includes('<') || summarized.includes('>')) throw new Error('HTML markup must be stripped from updater errors')
if (!summarized.includes('Public GitHub release unavailable')) throw new Error('Summarized updater error lost diagnostic text')
const long = summarizeUpdaterError('x'.repeat(1000), 50)
if (long.length !== 50 || !long.endsWith('...')) throw new Error('Long error text must be truncated with an ellipsis')
console.log('Updater public-release error formatting OK')
