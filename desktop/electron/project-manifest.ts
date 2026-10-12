import { readFileSync } from 'node:fs'
import { join } from 'node:path'

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
