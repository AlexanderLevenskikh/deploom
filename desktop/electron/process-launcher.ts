import { existsSync } from 'node:fs'
import { posix, win32 } from 'node:path'
import { uncShellInvocation, wslGitEnvironment } from './wsl-paths.js'

export type ResolvedInvocation = { command: string; args: string[]; resolvedExecutable: string; windowsVerbatimArguments?: boolean; cwd?: string; env?: NodeJS.ProcessEnv }

type ResolveOptions = {
  platform?: NodeJS.Platform
  cwd?: string
  env?: NodeJS.ProcessEnv
  pathValue?: string
  fileExists?: (path: string) => boolean
}

const WINDOWS_EXECUTABLE_EXTENSIONS = ['.exe', '.com', '.cmd', '.bat'] as const

function pathApi(platform: NodeJS.Platform) {
  return platform === 'win32' ? win32 : posix
}

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
  // Private migration workspaces are short-lived. A detached Nx watcher can
  // recreate .nx entries while the next candidate retires its old trial.
  source = { ...source, NX_DAEMON: 'false' }
  if (platform === 'win32') return wslGitEnvironment(source)
  const path = pathApi(platform)
  const home = source.HOME?.trim()
  const bunBin = source.BUN_INSTALL?.trim() ? path.join(source.BUN_INSTALL, 'bin') : undefined
  const inherited = (source.PATH ?? '').split(path.delimiter).map((item) => item.trim()).filter(Boolean)
  const extras = [
    source.NVM_BIN,
    source.FNM_MULTISHELL_PATH,
    source.PNPM_HOME,
    source.VOLTA_HOME ? path.join(source.VOLTA_HOME, 'bin') : undefined,
    bunBin,
    home ? path.join(home, '.local', 'bin') : undefined,
    home ? path.join(home, '.volta', 'bin') : undefined,
    home ? path.join(home, '.asdf', 'shims') : undefined,
    home ? path.join(home, '.local', 'share', 'pnpm') : undefined,
    platform === 'darwin' ? '/opt/homebrew/bin' : undefined,
    platform === 'linux' ? '/home/linuxbrew/.linuxbrew/bin' : undefined,
    '/usr/local/bin',
    '/usr/bin',
    '/bin',
  ].filter((item): item is string => Boolean(item && item.trim()))
  return { ...source, PATH: [...new Set([...inherited, ...extras])].join(path.delimiter) }
}

/** Give bundled Python entry points their release's dependencies even on a
 * clean host. Commands may already be resolved to python3 or an absolute exe. */
export function bundledPythonEnvironment(
  command: string,
  toolDirectory: string,
  source: NodeJS.ProcessEnv,
  platform: NodeJS.Platform = process.platform,
): NodeJS.ProcessEnv {
  const path = pathApi(platform)
  if (!/^python(?:\d+(?:\.\d+)*)?(?:\.exe)?$/i.test(path.basename(command))) return { ...source }
  const vendor = path.join(toolDirectory, 'vendor')
  const inherited = (source.PYTHONPATH ?? '').split(path.delimiter).filter(Boolean)
  return {
    ...source,
    PYTHONUNBUFFERED: '1',
    PYTHONPATH: [vendor, ...inherited.filter(entry => entry !== vendor)].join(path.delimiter),
  }
}

function windowsSearchDirectories(env: NodeJS.ProcessEnv, pathValue: string, platform: NodeJS.Platform): string[] {
  const path = pathApi(platform)
  const entries = pathValue.split(path.delimiter).map((item) => item.trim()).filter(Boolean)
  const extras = [
    env.NVM_SYMLINK,
    env.NVM_HOME,
    env.VOLTA_HOME ? path.join(env.VOLTA_HOME, 'bin') : undefined,
    env.FNM_MULTISHELL_PATH,
    env.APPDATA ? path.join(env.APPDATA, 'npm') : undefined,
    env.ProgramFiles ? path.join(env.ProgramFiles, 'nodejs') : undefined,
    env.LOCALAPPDATA ? path.join(env.LOCALAPPDATA, 'Programs', 'nodejs') : undefined,
    env.USERPROFILE ? path.join(env.USERPROFILE, 'scoop', 'shims') : undefined,
    env.USERPROFILE ? path.join(env.USERPROFILE, '.local', 'bin') : undefined,
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
  const path = pathApi(platform)
  const env = options.env ?? process.env
  const fileExists = options.fileExists ?? existsSync
  const pathValue = options.pathValue ?? env.PATH ?? ''
  const directories = windowsSearchDirectories(env, pathValue, platform)

  for (const entry of directories) {
    for (const extension of WINDOWS_EXECUTABLE_EXTENSIONS) {
      const candidate = path.join(entry, `${command}${extension}`)
      if (fileExists(candidate)) return candidate
    }
    if (command === 'opencode') {
      const npmBinary = path.join(entry, 'node_modules', 'opencode-ai', 'bin', 'opencode.exe')
      if (fileExists(npmBinary)) return npmBinary
    }
  }
  if (command === 'codex' && env.LOCALAPPDATA) {
    const codexBinary = path.join(env.LOCALAPPDATA, 'Programs', 'OpenAI', 'Codex', 'bin', 'codex.exe')
    if (fileExists(codexBinary)) return codexBinary
  }
  if (command === 'claude' && env.USERPROFILE) {
    const claudeBinary = path.join(env.USERPROFILE, '.local', 'bin', 'claude.exe')
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
    if (options.cwd?.startsWith('\\\\') || options.cwd?.startsWith('//')) {
      return { ...uncShellInvocation(cmdCommandLine(resolvedExecutable, args), options.cwd, env), resolvedExecutable }
    }
    return { command: comspec, args: ['/d', '/s', '/c', cmdCommandLine(resolvedExecutable, args)], resolvedExecutable, windowsVerbatimArguments: true }
  }
  if (platform === 'win32' && (options.cwd?.startsWith('\\\\') || options.cwd?.startsWith('//')) && /^(?:cmd|cmd\.exe)$/i.test(win32.basename(resolvedExecutable)) && args.at(-2)?.toLowerCase() === '/c') {
    return { ...uncShellInvocation(`"${args.at(-1)}"`, options.cwd, env), resolvedExecutable }
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

// A UTF-8 codepoint can span arbitrary pipe chunks. Decode only complete
// prefixes; the fallback decoder sees invalid bytes only after reassembly.
export function createProcessOutputDecoder(decode: (chunk: Buffer) => string = decodeProcessOutputChunk): { write: (chunk: Buffer) => string; end: () => string } {
  let pending = Buffer.alloc(0)
  return {
    write(chunk) {
      const bytes = pending.length ? Buffer.concat([pending, chunk]) : chunk
      let lead = bytes.length - 1
      while (lead >= 0 && bytes.length - lead <= 4 && (bytes[lead] & 0xc0) === 0x80) lead -= 1
      const first = bytes[lead]
      const width = first >= 0xc2 && first <= 0xdf ? 2 : first >= 0xe0 && first <= 0xef ? 3 : first >= 0xf0 && first <= 0xf4 ? 4 : 1
      const end = lead >= 0 && bytes.length - lead < width ? lead : bytes.length
      pending = Buffer.from(bytes.subarray(end))
      return end ? decode(bytes.subarray(0, end)) : ''
    },
    end() { const tail = pending; pending = Buffer.alloc(0); return tail.length ? decode(tail) : '' },
  }
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
