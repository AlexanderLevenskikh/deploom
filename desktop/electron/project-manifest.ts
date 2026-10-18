import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'

/** Read-only validation shared by discovery and the registration boundary. */
export function readProjectManifest(directory: string): Record<string, unknown> {
  const path = join(directory, 'package.json')
  try {
    const value: unknown = JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''))
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Expected a JSON object')
    return value as Record<string, unknown>
  } catch (error) {
    throw new Error(`PROJECT_PACKAGE_JSON_INVALID: ${path}: ${error instanceof Error ? error.message : String(error)}`)
  }
}

/** Display-only installed metadata; follow Node's ancestor lookup inside the repository. */
export function installedManifestReader(projectPath: string) {
  const selected = resolve(projectPath)
  const ancestors = [selected]
  let folder = selected
  while (!existsSync(join(folder, '.git')) && dirname(folder) !== folder) {
    folder = dirname(folder)
    ancestors.push(folder)
  }
  // Without a repository boundary, inspect only the explicit project. Never
  // borrow an unrelated ancestor's/global installation to fill missing evidence.
  const roots = existsSync(join(folder, '.git')) ? ancestors : [selected]
  return (packageName: string): Record<string, unknown> | undefined => {
    if (!/^(?:@[a-z0-9_~-][a-z0-9._~-]*\/)?[a-z0-9_~-][a-z0-9._~-]*$/i.test(packageName)) return
    for (const root of roots) {
      const directory = join(root, 'node_modules', ...packageName.split('/'))
      if (!existsSync(directory)) continue
      try { return readProjectManifest(directory) } catch { return }
    }
  }
}
