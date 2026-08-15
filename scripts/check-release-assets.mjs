import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const workflow = readFileSync(join(root, '.github', 'workflows', 'release.yml'), 'utf8')
const builder = readFileSync(join(root, 'desktop', 'electron-builder.yml'), 'utf8')
const main = readFileSync(join(root, 'desktop', 'electron', 'main.ts'), 'utf8')

for (const needle of [
  'package-windows:',
  'package-linux:',
  'package-macos:',
  'publish-release:',
  'DepLoom-Setup-$version-x64.exe',
  'DepLoom-$version-x64.AppImage',
  'latest.yml',
  'latest-linux.yml',
  'latest-mac.yml',
  'deploom-macos-${{ matrix.arch }}-unsigned-smoke',
  'gh release create "$GITHUB_REF_NAME"',
  'GH_TOKEN: ${{ github.token }}',
  'contents: write',
]) {
  if (!workflow.includes(needle)) throw new Error(`Missing public GitHub release/platform asset contract: ${needle}`)
}
for (const needle of [
  'appId: io.github.alexanderlevenskikh.deploom',
  'productName: DepLoom',
  'provider: github',
  'owner: AlexanderLevenskikh',
  'repo: deploom',
  'releaseType: release',
  'target: AppImage',
  'artifactName: DepLoom-${version}-x64.${ext}',
  'syncDesktopName: true',
  '- dmg',
  '- zip',
]) {
  if (!builder.includes(needle)) throw new Error(`Missing electron-builder public/cross-platform contract: ${needle}`)
}
if (/setFeedURL\s*\(/.test(main)) throw new Error('Desktop updater must use packaged app-update.yml instead of a runtime token/feed override')
if (workflow.includes('release-assets/DepLoom-') && !workflow.includes('latest-linux.yml')) throw new Error('Linux release payload must include latest-linux.yml')
console.log('Public GitHub Windows/Linux release + macOS smoke/update provider contract OK')
