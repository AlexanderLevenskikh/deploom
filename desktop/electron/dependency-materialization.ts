import type { ScopeAction } from './migration-progress.js'

export type PackageManager = 'yarn' | 'npm' | 'pnpm'

export type DependencyMaterializationResult = {
  text: string
  changedPackages: string[]
}

const DIRECT_SECTIONS = new Set(['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'])

function detectIndent(text: string): string {
  const match = /\n([ \t]+)"/.exec(text)
  return match?.[1] || '  '
}

export function applyDependencyActionsToPackageJson(text: string, actions: readonly ScopeAction[]): DependencyMaterializationResult {
  const manifest = JSON.parse(text) as Record<string, unknown>
  const changed = new Set<string>()
  for (const action of actions) {
    if (!DIRECT_SECTIONS.has(action.section)) {
      throw new Error(`DEPENDENCY_MATERIALIZATION_SCOPE_INVALID: ${action.package}: unsupported direct dependency section ${action.section || '<empty>'}`)
    }
    const rawSection = manifest[action.section]
    if (!rawSection || typeof rawSection !== 'object' || Array.isArray(rawSection)) {
      throw new Error(`DEPENDENCY_MATERIALIZATION_SCOPE_INVALID: ${action.package}: section ${action.section} is missing or not an object`)
    }
    const section = rawSection as Record<string, unknown>
    if (action.action === 'remove') {
      if (Object.prototype.hasOwnProperty.call(section, action.package)) {
        delete section[action.package]
        changed.add(action.package)
      }
      continue
    }
    if (action.action !== 'update') {
      throw new Error(`DEPENDENCY_MATERIALIZATION_SCOPE_INVALID: ${action.package}: unsupported action ${action.action}`)
    }
    if (!action.target) {
      throw new Error(`DEPENDENCY_MATERIALIZATION_SCOPE_INVALID: ${action.package}: empty target`)
    }
    if (!Object.prototype.hasOwnProperty.call(section, action.package)) {
      throw new Error(`DEPENDENCY_MATERIALIZATION_SCOPE_INVALID: ${action.package}: direct declaration is missing from ${action.section}`)
    }
    if (section[action.package] !== action.target) {
      section[action.package] = action.target
      changed.add(action.package)
    }
  }
  const newline = text.includes('\r\n') ? '\r\n' : '\n'
  const hadTrailingNewline = text.endsWith('\r\n') || text.endsWith('\n')
  const serialized = JSON.stringify(manifest, null, detectIndent(text))
  const rendered = (newline === '\r\n' ? serialized.replace(/\n/g, '\r\n') : serialized) + (hadTrailingNewline ? newline : '')
  return { text: rendered, changedPackages: [...changed].sort() }
}

export function dependencyMaterializationInstallSpec(manager: PackageManager): { command: string; args: string[] } {
  if (manager === 'yarn') return { command: 'yarn', args: ['install'] }
  if (manager === 'pnpm') return { command: 'pnpm', args: ['install', '--no-frozen-lockfile'] }
  return { command: 'npm', args: ['install', '--no-audit', '--no-fund'] }
}

const INFRA_PATTERN = /(?:ENOENT|not recognized as an internal or external command|command not found|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|socket hang up|SELF_SIGNED_CERT|unable to get local issuer|certificate has expired|ENOSPC|EPERM|EACCES|HTTP\s+(?:429|500|502|503|504)\b)/i
const DEPENDENCY_PATTERN = /(?:ERESOLVE|unable to resolve dependency tree|could not resolve dependency|No matching version found|Couldn't find any versions|YN0060|YN0082|peer dependency|conflicting peer dependency|resolution field .* incompatible)/i

export function classifyDependencyMaterializationFailure(output: string): 'infrastructure' | 'dependency' | 'unknown' {
  if (INFRA_PATTERN.test(output)) return 'infrastructure'
  if (DEPENDENCY_PATTERN.test(output)) return 'dependency'
  return 'unknown'
}
