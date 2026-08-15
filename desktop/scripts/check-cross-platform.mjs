import { readFileSync } from 'node:fs'
import { commandEnvironment, resolveExecutable, processTreeDetached, normalizePathForComparison } from '../dist-electron/process-launcher.js'
import { portablePathKey, isToolManagedWorktreePath } from '../dist-electron/worktree-ownership.js'

if (resolveExecutable('python', { platform: 'linux' }) !== 'python3') throw new Error('Linux must resolve logical python to python3')
if (resolveExecutable('python', { platform: 'darwin' }) !== 'python3') throw new Error('macOS must resolve logical python to python3')
if (resolveExecutable('python', { platform: 'win32', env: { PATH: '' }, pathValue: '', fileExists: () => false }) !== 'python') throw new Error('Windows must keep the python launcher name')
const macGuiEnv = commandEnvironment({ PATH: '/usr/bin:/bin', HOME: '/Users/demo' }, 'darwin')
if (!macGuiEnv.PATH?.includes('/opt/homebrew/bin') || !macGuiEnv.PATH?.includes('/Users/demo/.local/bin')) throw new Error('macOS GUI PATH must include common user/Homebrew tool locations')
const linuxGuiEnv = commandEnvironment({ PATH: '/usr/bin:/bin', HOME: '/home/demo' }, 'linux')
if (!linuxGuiEnv.PATH?.includes('/usr/local/bin') || !linuxGuiEnv.PATH?.includes('/home/demo/.volta/bin')) throw new Error('Linux GUI PATH must include common user tool locations')
if (!processTreeDetached('linux') || !processTreeDetached('darwin') || processTreeDetached('win32')) throw new Error('POSIX children must own a process group; Windows uses taskkill /T')
if (normalizePathForComparison('/Work/Repo', 'linux') === normalizePathForComparison('/work/repo', 'linux')) throw new Error('POSIX containment must preserve case sensitivity')
if (normalizePathForComparison('C:/Work/Repo', 'win32') !== normalizePathForComparison('c:/work/repo', 'win32')) throw new Error('Windows containment must be case-insensitive')
if (portablePathKey('/Work/Repo', 'linux') === portablePathKey('/work/repo', 'linux')) throw new Error('POSIX worktree ownership keys must preserve case')
if (portablePathKey('C:/Work/Repo', 'win32') !== portablePathKey('c:/work/repo', 'win32')) throw new Error('Windows worktree ownership keys must ignore case')
if (isToolManagedWorktreePath('/tmp/CaseTemp/dependency-flow-planner-1', '/tmp/casetemp', 'linux')) throw new Error('POSIX ownership must never claim a case-distinct TEMP root')

const builder = readFileSync(new URL('../electron-builder.yml', import.meta.url), 'utf8')
const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
const main = readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const prepareTool = readFileSync(new URL('./prepare-tool.mjs', import.meta.url), 'utf8')
const solverRequirements = readFileSync(new URL('../../requirements-solver.txt', import.meta.url), 'utf8')
for (const expected of [
  'appId: io.github.alexanderlevenskikh.deploom',
  'productName: DepLoom',
  'target: AppImage',
  '- dmg',
  '- zip',
  'DepLoom-Setup-${version}-${arch}.${ext}',
  'DepLoom-${version}-${arch}.${ext}',
  'artifactName: DepLoom-${version}-x64.${ext}',
  'syncDesktopName: true',
]) {
  if (!builder.includes(expected)) throw new Error(`Cross-platform packager contract missing: ${expected}`)
}
for (const script of ['package:win', 'package:linux', 'package:mac:x64', 'package:mac:arm64']) {
  const command = pkg.scripts?.[script]
  if (!command) throw new Error(`Missing packaging script: ${script}`)
  if (!command.startsWith('npm run build && ')) {
    throw new Error(`Packaging script must build dist + dist-electron in its own fresh CI job: ${script}`)
  }
}
if (!pkg.scripts?.['package:win:ci']?.startsWith('npm run build && ')) {
  throw new Error('Cross-compiled Windows packaging must also build dist + dist-electron first')
}
if (!pkg.scripts?.['package:win:ci']?.includes('TOOL_VENDOR_PLATFORM=win_amd64')) {
  throw new Error('Cross-compiled Windows packaging must vendor the Windows Z3 wheel')
}
if (!solverRequirements.includes('z3-solver==4.13.0.0')) {
  throw new Error('Packaged Solver must use the cross-platform Z3 pin verified by all release targets')
}
if (!prepareTool.includes('macosx_11_0_x86_64') || !prepareTool.includes('macosx_11_0_arm64')) {
  throw new Error('macOS tool vendoring must target the wheel tags supported by the shared Z3 pin')
}
if (pkg.desktopName !== 'io.github.alexanderlevenskikh.deploom.desktop') throw new Error('Linux desktopName must match the public app id for WM_CLASS/launcher association')
if (!main.includes("process.kill(-pid, 'SIGTERM')") || !main.includes("process.kill(-pid, 'SIGKILL')")) throw new Error('POSIX process-tree termination is not wired')
if ((main.match(/detached: processTreeDetached\(\)/g) || []).length < 3) throw new Error('All command/server spawn paths must create a POSIX process group')
if (!main.includes("app.setAppUserModelId('io.github.alexanderlevenskikh.deploom')")) throw new Error('Desktop runtime app id is stale')
if (!main.includes("process.platform === 'win32' ? 'icon.ico' : 'icon.png'")) throw new Error('BrowserWindow icon is not platform-aware')
if (!main.includes('normalizePathForComparison(normalize(item.path)) === normalizePathForComparison(normalize(workspacePath))')) throw new Error('Workspace identity must preserve POSIX path case')
if ((main.match(/commandEnvironment\(/g) || []).length < 3) throw new Error('All GUI child-process paths must use the augmented cross-platform command environment')
for (const expected of ["'Проверка Node.js'", "`Проверка package manager (${manager})`", "'Проверка bundled Z3'"]) {
  if (!main.includes(expected)) throw new Error(`Cross-platform environment preflight missing: ${expected}`)
}
for (const expected of [
  'function atomicWriteJsonSync(',
  "mainWindow.webContents.on('will-navigate'",
  'WORKSPACE_FOLDER_OUTSIDE_PARENT',
  'refs/deploom/restart-backups/',
  'RELEASE_CLEANUP_DIRTY_WORKTREE',
  "child.stdin.on('error'",
  "killer.on('error'",
]) {
  if (!main.includes(expected)) throw new Error(`Desktop durability/security hardening missing: ${expected}`)
}
for (const forbidden of [
  'writeFileSync(statePath(), `${JSON.stringify(state, null, 2)}',
  'writeFileSync(path, `${JSON.stringify(current, null, 2)}',
  'writeFileSync(path, `${JSON.stringify(state, null, 2)}',
]) {
  if (main.includes(forbidden)) throw new Error(`Non-atomic desktop/team state write returned: ${forbidden}`)
}

console.log('Cross-platform desktop runtime/package contracts OK')
