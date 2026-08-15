import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const desktop = join(dirname(fileURLToPath(import.meta.url)), '..')
const root = join(desktop, '..')
const builder = readFileSync(join(desktop, 'electron-builder.yml'), 'utf8')
const release = readFileSync(join(root, '.github', 'workflows', 'release.yml'), 'utf8')
const desktopReadme = readFileSync(join(desktop, 'README.md'), 'utf8')

for (const expected of ['provider: github', 'owner: AlexanderLevenskikh', 'repo: deploom', 'releaseType: release']) {
  if (!builder.includes(expected)) throw new Error(`electron-builder updater target is missing: ${expected}`)
}
for (const required of [
  'release/latest.yml',
  'release/latest-linux.yml',
  'release/latest-mac.yml',
  'app-update.yml',
  '.exe.blockmap',
  'gh release create',
  'DepLoom-Setup-$version-x64.exe',
  'DepLoom-$version-x64.AppImage',
]) {
  if (!release.includes(required)) throw new Error(`release workflow is missing updater/platform asset contract: ${required}`)
}
if (!release.includes('macOS x64/arm64 packages are CI-validated but intentionally not published')) throw new Error('macOS unsigned/non-public release boundary must be explicit')
if (!desktopReadme.includes('AlexanderLevenskikh/deploom')) throw new Error('Desktop update documentation does not name the public update repository')
if (!desktopReadme.includes('latest-linux.yml') || !desktopReadme.includes('latest-mac.yml')) throw new Error('Desktop docs must explain platform update metadata')
console.log('Public GitHub update target OK: Windows/Linux publish; macOS gated on signing')
