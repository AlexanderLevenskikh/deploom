import { createHash } from 'node:crypto'
import { existsSync, readFileSync, realpathSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { homedir } from 'node:os'
import { dependencyInputIdentity } from './acceptance-policy.js'

import type { AuditContext } from './project-audit-types.js'
export type { AuditContext } from './project-audit-types.js'
export function stableAuditJson(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(stableAuditJson).join(',') + ']'
  if (value && typeof value === 'object') return '{' + Object.entries(value).filter(([, item]) => item !== undefined).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => JSON.stringify(key) + ':' + stableAuditJson(item)).join(',') + '}'
  return JSON.stringify(value) ?? 'null'
}
const digest = (value: unknown) => createHash('sha256').update(stableAuditJson(value)).digest('hex')

/** Use the existing Node/Python dependency input contract, including inherited
 * manifests and shared lockfiles. Credentials enter only an opaque digest. */
export function projectAuditContext(projectPath: string, inputs: Record<string, unknown>, env: NodeJS.ProcessEnv = process.env, userHome = homedir()): AuditContext {
  const project = realpathSync.native(projectPath)
  const dependency = dependencyInputIdentity(project)
  const configFiles = new Set<string>()
  let folder = project
  while (true) {
    for (const name of ['.npmrc', '.yarnrc', '.yarnrc.yml']) configFiles.add(join(folder, name))
    if (existsSync(join(folder, '.git')) || dirname(folder) === folder) break
    folder = dirname(folder)
  }
  for (const name of ['.npmrc', '.yarnrc', '.yarnrc.yml']) configFiles.add(join(userHome, name))
  for (const key of ['NPM_CONFIG_USERCONFIG', 'npm_config_userconfig', 'NPM_CONFIG_GLOBALCONFIG', 'npm_config_globalconfig']) if (env[key]) configFiles.add(resolve(env[key]!))
  const referenced = new Set<string>()
  const registryFiles = [...configFiles].sort().map(path => {
    if (!existsSync(path)) return [path, null]
    if (!statSync(path).isFile() || statSync(path).size > 1024 * 1024) throw new Error('AUDIT_REGISTRY_CONTEXT_UNREADABLE')
    const raw = readFileSync(path)
    for (const match of raw.toString('utf8').matchAll(/\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[-+][^}]*)?\}/g)) referenced.add(match[1])
    return [path, createHash('sha256').update(raw).digest('hex')]
  })
  const registryEnvironment = Object.entries(env).filter(([key]) => /^(npm_|yarn_|node_auth_token|https?_proxy|no_proxy|corepack_)/i.test(key) || referenced.has(key)).sort(([a], [b]) => a.localeCompare(b))
  const policyHash = digest({ intent: inputs.intent, lagOverrides: inputs.lagOverrides, projectName: inputs.projectName })
  const runtimeHash = digest({ requestedNode: inputs.requestedNode, host: process.version, environment: Object.entries(env).filter(([key]) => /^(path$|node_options$|node_binary$|nvm_|fnm_|volta_)/i.test(key)).sort(([a], [b]) => a.localeCompare(b)) })
  const registryHash = digest([registryFiles, registryEnvironment])
  const lock = dependency.files.find(name => /(^|\/)(yarn\.lock|pnpm-lock\.yaml|package-lock\.json|npm-shrinkwrap\.json)$/.test(name))
  const resource = realpathSync.native(lock ? dirname(resolve(project, lock)) : project)
  const value = { dependencyHash: dependency.hash, policyHash, runtimeHash, registryHash }
  return { ...value, resource: process.platform === 'win32' ? resource.toLowerCase() : resource, hash: digest(value) }
}

export function auditContextDifference(saved: AuditContext | undefined, current: AuditContext): string | undefined {
  if (!saved) return 'unbound-evidence'
  if (saved.dependencyHash !== current.dependencyHash) return 'dependencies-changed'
  if (saved.policyHash !== current.policyHash) return 'policy-changed'
  if (saved.runtimeHash !== current.runtimeHash) return 'runtime-changed'
  if (saved.registryHash !== current.registryHash) return 'registry-context-changed'
  return undefined
}
