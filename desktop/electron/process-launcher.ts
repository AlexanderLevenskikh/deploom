import { existsSync } from 'node:fs'
import { delimiter, join } from 'node:path'

export type ResolvedInvocation = { command: string; args: string[]; resolvedExecutable: string; windowsVerbatimArguments?: boolean }

type ResolveOptions = {
  platform?: NodeJS.Platform
  env?: NodeJS.ProcessEnv
  pathValue?: string
  fileExists?: (path: string) => boolean
}

const WINDOWS_EXECUTABLE_EXTENSIONS = ['.exe', '.com', '.cmd', '.bat'] as const

/** Build the environment used by GUI-launched child processes.
 *
 * Finder/desktop launchers often provide a much smaller PATH than an
 * interactive shell. Keep the inherited PATH authoritative, but append common
 * POSIX tool locations and package-manager homes so git/python3/node/yarn and
 * agent CLIs remain discoverable without running user shell startup scripts.
 */
export function commandEnvironment(
  source: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
): NodeJS.ProcessEnv {
  if (platform === 'win32') return { ...source }
  const home = source.HOME?.trim()
  const bunBin = source.BUN_INSTALL?.trim() ? join(source.BUN_INSTALL, 'bin') : undefined
  const inherited = (source.PATH ?? '').split(delimiter).map((item) => item.trim()).filter(Boolean)
  const extras = [
    source.NVM_BIN,
    source.FNM_MULTISHELL_PATH,
    source.PNPM_HOME,
    source.VOLTA_HOME ? join(source.VOLTA_HOME, 'bin') : undefined,
    bunBin,
    home ? join(home, '.local', 'bin') : undefined,
    home ? join(home, '.volta', 'bin') : undefined,
    home ? join(home, '.asdf', 'shims') : undefined,
    home ? join(home, '.local', 'share', 'pnpm') : undefined,
    platform === 'darwin' ? '/opt/homebrew/bin' : undefined,
    platform === 'linux' ? '/home/linuxbrew/.linuxbrew/bin' : undefined,
    '/usr/local/bin',
    '/usr/bin',
    '/bin',
  ].filter((item): item is string => Boolean(item && item.trim()))
  return { ...source, PATH: [...new Set([...inherited, ...extras])].join(delimiter) }
}

function windowsSearchDirectories(env: NodeJS.ProcessEnv, pathValue: string, platform: NodeJS.Platform): string[] {
  const entries = pathValue.split(platform === 'win32' ? ';' : delimiter).map((item) => item.trim()).filter(Boolean)
  const extras = [
    env.NVM_SYMLINK,
    env.NVM_HOME,
    env.VOLTA_HOME ? join(env.VOLTA_HOME, 'bin') : undefined,
    env.FNM_MULTISHELL_PATH,
    env.APPDATA ? join(env.APPDATA, 'npm') : undefined,
    env.ProgramFiles ? join(env.ProgramFiles, 'nodejs') : undefined,
    env.LOCALAPPDATA ? join(env.LOCALAPPDATA, 'Programs', 'nodejs') : undefined,
    env.USERPROFILE ? join(env.USERPROFILE, 'scoop', 'shims') : undefined,
    env.USERPROFILE ? join(env.USERPROFILE, '.local', 'bin') : undefined,
  ].filter((item): item is string => Boolean(item && item.trim()))
  return [...new Set([...entries, ...extras])]
}

/** Resolve CLI shims deterministically when Electron was launched outside a shell.
 *
 * Windows package managers installed by npm/Corepack/nvm are commonly `*.cmd`,
 * not `*.exe`.  Looking only for exe/com makes a perfectly healthy Yarn install
 * fail as `spawn yarn ENOENT` in parallel worktrees.
 */
export function resolveExecutable(command: string, options: ResolveOptions = {}): string {
  if (command.includes('/') || command.includes('\\')) return command
  const platform = options.platform ?? process.platform
  // The desktop command model intentionally uses the logical name `python`.
  // Windows installations commonly expose `python.exe`, while Linux/macOS
  // reliably expose Python 3 as `python3` and often have no `python` alias.
  if (command === 'python' && platform !== 'win32') return 'python3'
  if (platform !== 'win32') return command
  const env = options.env ?? process.env
  const fileExists = options.fileExists ?? existsSync
  const pathValue = options.pathValue ?? env.PATH ?? ''
  const directories = windowsSearchDirectories(env, pathValue, platform)

  for (const entry of directories) {
    for (const extension of WINDOWS_EXECUTABLE_EXTENSIONS) {
      const candidate = join(entry, `${command}${extension}`)
      if (fileExists(candidate)) return candidate
    }
    if (command === 'opencode') {
      const npmBinary = join(entry, 'node_modules', 'opencode-ai', 'bin', 'opencode.exe')
      if (fileExists(npmBinary)) return npmBinary
    }
  }
  if (command === 'codex' && env.LOCALAPPDATA) {
    const codexBinary = join(env.LOCALAPPDATA, 'Programs', 'OpenAI', 'Codex', 'bin', 'codex.exe')
    if (fileExists(codexBinary)) return codexBinary
  }
  if (command === 'claude' && env.USERPROFILE) {
    const claudeBinary = join(env.USERPROFILE, '.local', 'bin', 'claude.exe')
    if (fileExists(claudeBinary)) return claudeBinary
  }
  return command
}

function quoteCmdArgument(value: string): string {
  // `cmd /d /s /c` receives one command-line string. Package-manager/bootstrap
  // arguments are ordinary CLI tokens, but paths may contain spaces or shell
  // metacharacters. Quote every token and escape characters that cmd expands.
  return `"${value
    .replace(/%/g, '%%')
    .replace(/\^/g, '^^')
    .replace(/"/g, '\\"')}"`
}

function cmdCommandLine(executable: string, args: string[]): string {
  const inner = [executable, ...args].map(quoteCmdArgument).join(' ')
  // With `/s`, cmd.exe applies its own quote stripping to the string following
  // `/c`. If the executable itself is quoted (normal for NVM/npm shims), the
  // *whole* command must have a second pair of quotes:
  //   cmd /d /s /c ""C:\\Program Files\\nodejs\\tool.cmd" "arg""
  // Without that outer pair cmd may try to execute a literal quoted filename.
  return `"${inner}"`
}

/** Return an invocation that Node can spawn with `shell:false` on every OS. */
export function resolveSpawnInvocation(command: string, args: string[], options: ResolveOptions = {}): ResolvedInvocation {
  const platform = options.platform ?? process.platform
  const env = options.env ?? process.env
  const resolvedExecutable = resolveExecutable(command, options)
  if (platform === 'win32' && /\.(?:cmd|bat)$/i.test(resolvedExecutable)) {
    const comspec = env.ComSpec || env.COMSPEC || 'cmd.exe'
    return { command: comspec, args: ['/d', '/s', '/c', cmdCommandLine(resolvedExecutable, args)], resolvedExecutable, windowsVerbatimArguments: true }
  }
  return { command: resolvedExecutable, args, resolvedExecutable }
}


/** Spawn long-lived/killable children as their own POSIX process group.
 *
 * Windows uses taskkill /T, so detached process groups are unnecessary there.
 * On Linux/macOS a negative PID signal can only terminate the whole descendant
 * tree when the child is the leader of its own process group.
 */
export function processTreeDetached(platform: NodeJS.Platform = process.platform): boolean {
  return platform !== 'win32'
}

/** Normalize filesystem paths for containment checks without breaking POSIX
 * case sensitivity. Windows paths are case-insensitive; Linux/macOS paths must
 * preserve case (macOS may use a case-sensitive volume).
 */
export function normalizePathForComparison(value: string, platform: NodeJS.Platform = process.platform): string {
  const normalized = value.replace(/[\\/]+$/g, '')
  return platform === 'win32' ? normalized.toLowerCase() : normalized
}

export function decodeProcessOutputChunk(chunk: Buffer, platform: NodeJS.Platform = process.platform): string {
  const utf8 = chunk.toString('utf8')
  if (platform !== 'win32' || !utf8.includes('\uFFFD')) return utf8
  try {
    // Localized diagnostics emitted by cmd.exe use the OEM console code page
    // on Russian Windows. Node's TextDecoder supports the WHATWG `ibm866`
    // label, which makes startup failures readable instead of mojibake.
    return new TextDecoder('ibm866').decode(chunk)
  } catch {
    return utf8
  }
}

export function packageManagerResolutionHint(command: string, resolvedExecutable: string): string | undefined {
  if (!['yarn', 'npm', 'pnpm', 'corepack'].includes(command)) return undefined
  if (resolvedExecutable !== command) return undefined
  return `${command} не найден в PATH/Node manager paths. Запустите DepLoom из окружения, где доступен ${command}, либо добавьте Node/npm/Corepack в PATH.`
}
