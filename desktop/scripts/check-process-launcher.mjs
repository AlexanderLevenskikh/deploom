import { win32 } from 'node:path'
import { decodeProcessOutputChunk, packageManagerResolutionHint, resolveExecutable, resolveSpawnInvocation } from '../dist-electron/process-launcher.js'
import { openCodeDatabaseEnv, openCodeDatabaseLocked, openCodeRuntimePaths } from '../dist-electron/opencode-runtime.js'

const appData = win32.join(String.raw`C:\virtual`, 'AppData', 'Roaming')
const yarnShim = win32.join(appData, 'npm', 'yarn.cmd')
const fakeExists = (path) =>
  win32.normalize(path).toLowerCase() === win32.normalize(yarnShim).toLowerCase()
const options = {
  platform: 'win32',
  env: { APPDATA: appData, COMSPEC: 'C:\\Windows\\System32\\cmd.exe', PATH: '' },
  pathValue: '',
  fileExists: fakeExists,
}

const resolved = resolveExecutable('yarn', options)
if (resolved !== yarnShim) throw new Error(`Expected Windows yarn.cmd shim, got ${resolved}`)
const invocation = resolveSpawnInvocation('yarn', ['install', '--frozen-lockfile'], options)
if (invocation.command !== options.env.COMSPEC) throw new Error(`Expected cmd.exe wrapper, got ${invocation.command}`)
if (invocation.resolvedExecutable !== yarnShim || invocation.args.slice(0, 3).join(' ') !== '/d /s /c') {
  throw new Error(`Unexpected yarn.cmd invocation: ${JSON.stringify(invocation)}`)
}
if (!invocation.args[3].includes('yarn.cmd') || !invocation.args[3].includes('--frozen-lockfile')) {
  throw new Error(`cmd wrapper lost package-manager arguments: ${JSON.stringify(invocation.args)}`)
}

if (!invocation.args[3].startsWith('""') || !invocation.args[3].endsWith('""')) {
  throw new Error(`cmd /s /c must wrap the whole quoted shim command in a second quote pair: ${JSON.stringify(invocation.args[3])}`)
}

const nvmShim = 'C:\\nvm4w\\nodejs\\opencode.cmd'
const nvmOptions = {
  platform: 'win32',
  env: { NVM_SYMLINK: 'C:\\nvm4w\\nodejs', COMSPEC: 'C:\\Windows\\System32\\cmd.exe', PATH: '' },
  pathValue: '',
  fileExists: (path) => path.replace(/\//g, '\\').toLowerCase() === nvmShim.toLowerCase(),
}
const opencodeInvocation = resolveSpawnInvocation('opencode', ['serve', '--hostname', '127.0.0.1', '--port', '43210'], nvmOptions)
if (opencodeInvocation.resolvedExecutable.replace(/\//g, '\\').toLowerCase() !== nvmShim.toLowerCase()) throw new Error(`Expected NVM OpenCode shim, got ${opencodeInvocation.resolvedExecutable}`)
const opencodeCommandLine = opencodeInvocation.args[3]
if (!opencodeCommandLine.startsWith('""') || !opencodeCommandLine.includes('opencode.cmd" "serve"') || !opencodeCommandLine.endsWith('"43210""')) {
  throw new Error(`OpenCode .cmd invocation is vulnerable to cmd /s quote stripping: ${JSON.stringify(opencodeCommandLine)}`)
}
if (opencodeInvocation.windowsVerbatimArguments !== true) throw new Error('cmd.exe wrapper must preserve the already-constructed /c command line verbatim')

const russianCmdError = Buffer.from([173,165,32,239,162,171,239,165,226,225,239,32,162,173,227,226,224,165,173,173,165,169,32,168,171,168,32,162,173,165,232,173,165,169,32,170,174,172,160,173,164,174,169])
if (decodeProcessOutputChunk(russianCmdError, 'win32') !== 'не является внутренней или внешней командой') {
  throw new Error('Windows OEM-866 diagnostics must be readable in OpenCode startup errors')
}


const isolatedRuntime = openCodeRuntimePaths(String.raw`C:\Temp`, 'job::group::demo', 2)
if (!isolatedRuntime.databasePath.endsWith('opencode.db') || !isolatedRuntime.directory.includes('runtime-2') || isolatedRuntime.root.includes('::')) {
  throw new Error(`OpenCode runtime paths must be isolated and filesystem-safe: ${JSON.stringify(isolatedRuntime)}`)
}
const isolatedEnv = openCodeDatabaseEnv({ PATH: 'keep-me' }, isolatedRuntime.databasePath)
if (isolatedEnv.OPENCODE_DB !== isolatedRuntime.databasePath || isolatedEnv.PATH !== 'keep-me' || isolatedEnv.FORCE_COLOR !== '0') {
  throw new Error('OpenCode isolated DB env must preserve the caller environment and override only runtime state')
}
for (const message of ['database is locked', 'SqliteError: SQLITE_BUSY', 'SQLITE_LOCKED: database table is locked']) {
  if (!openCodeDatabaseLocked(message)) throw new Error(`OpenCode DB lock must be classified as retryable infrastructure: ${message}`)
}
if (openCodeDatabaseLocked('provider returned HTTP 401')) throw new Error('Non-SQLite OpenCode failures must not be retried as database locks')

const missing = resolveExecutable('yarn', { ...options, fileExists: () => false })
const hint = packageManagerResolutionHint('yarn', missing)
if (!hint?.includes('не найден')) throw new Error('Missing package manager must produce an infrastructure hint')
if (packageManagerResolutionHint('git', 'git')) throw new Error('Non-package-manager commands must not get a package-manager hint')

console.log('Process launcher Windows shim resolution OK')
