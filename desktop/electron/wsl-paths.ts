import { existsSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import { win32, join } from 'node:path'
import { tmpdir } from 'node:os'

const repositories = new Set<string>()

export function isWslPath(value: string): boolean {
  return /^[/\\]{2}(?:wsl\.localhost|wsl\$)[/\\][^/\\]+[/\\]/i.test(value)
}

/** Trust only the actual repository selected by the user, for child processes.
 * Never writes safe.directory to the user's global Git configuration. */
export function registerWslRepository(selectedPath: string): void {
  if (process.platform !== 'win32' || !isWslPath(selectedPath)) return
  let directory: string
  try { directory = realpathSync.native(selectedPath) } catch { return }
  while (isWslPath(directory)) {
    if (existsSync(win32.join(directory, '.git'))) {
      repositories.add(directory.replace(/\\/g, '/').replace(/\/$/, ''))
      return
    }
    const parent = win32.dirname(directory)
    if (parent === directory) return
    directory = parent
  }
}

export function wslGitEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const env = { ...source }
  const raw = env.GIT_CONFIG_COUNT || '0'
  if (!/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) throw new Error('Invalid GIT_CONFIG_COUNT')
  let count = Number(raw)
  const inherited = new Set<string>()
  for (let i = 0; i < count; i += 1) {
    if (env[`GIT_CONFIG_KEY_${i}`] === 'safe.directory') inherited.add(env[`GIT_CONFIG_VALUE_${i}`] || '')
  }
  for (const repository of repositories) {
    if (inherited.has(repository)) continue
    env[`GIT_CONFIG_KEY_${count}`] = 'safe.directory'
    env[`GIT_CONFIG_VALUE_${count}`] = repository
    count += 1
  }
  if (count) env.GIT_CONFIG_COUNT = String(count)
  return env
}

let bootstrap: string | undefined

/** A batch file gives each step its own expansion of errorlevel. A nested cmd
 * executes the original command unchanged; popd always preserves its exit code. */
export function uncShellInvocation(commandLine: string, cwd: string, env: NodeJS.ProcessEnv) {
  if (!bootstrap) {
    const directory = mkdtempSync(join(tmpdir(), 'deploom-unc-'))
    bootstrap = join(directory, 'run.cmd')
    writeFileSync(bootstrap, [
      '@echo off',
      'setlocal DisableDelayedExpansion',
      'pushd "%DEPLOOM_UNC_CWD%"',
      'if errorlevel 1 exit /b 1',
      '"%ComSpec%" /d /s /c %DEPLOOM_UNC_COMMAND%',
      'set "DEPLOOM_UNC_EXIT=%errorlevel%"',
      'popd',
      'exit /b %DEPLOOM_UNC_EXIT%',
      '',
    ].join('\r\n'), 'utf8')
    process.once('exit', () => { try { rmSync(directory, { recursive: true, force: true }) } catch { /* OS cleanup */ } })
  }
  return {
    command: env.ComSpec || env.COMSPEC || 'cmd.exe',
    args: ['/d', '/s', '/c', `""${bootstrap}""`],
    cwd: win32.dirname(bootstrap),
    env: { ...env, ComSpec: env.ComSpec || env.COMSPEC || 'cmd.exe', DEPLOOM_UNC_CWD: cwd, DEPLOOM_UNC_COMMAND: commandLine },
    windowsVerbatimArguments: true,
  }
}
