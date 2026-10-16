// Desktop-side policy for PROVIDER RUNTIME files in the repair-agent trial
// (iterative migration increment 2).
//
// The repair agent works in the isolated trial and may only change the
// project's source/config. A provider runtime can legitimately appear inside
// that trial while the agent works (OpenCode bootstraps its plugin manifests,
// keeps its database, ...). Those files must NEVER count as source repair
// evidence and must NEVER be blamed on the agent as a dependency mutation —
// but they are also not a license to edit project manifests. This module owns
// the CONFIRMED runtime recognition (narrow, provider-specific shape), the
// policy version and the adapter registry. The dependency guard itself stays
// in iterative-agent.ts (ITERATIVE_FORBIDDEN_NAMES mirrors the Python
// FORBIDDEN_TRIAL_RELATIVES), which combines both in a single-pass classifier
// (classifyTrialChanges).
//
// Rules are intentionally narrow and FAIL CLOSED:
//   - a provider is recognized by an explicit adapter, never by folder
//     presence, and the project-relative path must stay inside the workspace;
//   - a whole provider folder (.opencode/.claude/.codex) is NEVER exempt, and
//     input config (.opencode/config.json ...) is NOT runtime evidence;
//   - only the exact, verified OpenCode plugin pair is recognized;
//   - claude/codex and unknown providers get a STRICT adapter (no exemption)
//     until their launchers are verified to place runtime files in a trial;
//   - policy version travels with a persisted blocked-mutation record so an
//     upgraded policy never misreads an old record as "same blocking defect".
import { lstatSync, readFileSync, realpathSync } from 'node:fs'
import { isAbsolute, join, relative, resolve, sep } from 'node:path'

/** Bump this only when the runtime-recognition rules change in a way that
 *  invalidates previously persisted definitions (e.g. a new adapter or a new
 *  narrowing of an existing one). */
export const AGENT_RUNTIME_POLICY_VERSION = 2

/** Provider + project location inside the trial workspace. */
export type AgentRuntimeScope = { provider: string; projectRelative: string }

export type TrialMutationKind = 'modified' | 'added' | 'removed'

/** Why a changed trial file is treated the way it is. */
export type TrialChangeCategory =
  | 'source-config' // ordinary project source/config — allowed repair evidence
  | 'dependency-blocked' // planner-owned dependency state — MUST reject the repair
  | 'confirmed-runtime' // provider runtime file confirmed by a narrow shape — not repair evidence, not a violation
  | 'other' // recognized but neither repair evidence nor a violation
  | 'unreadable' // content could not be measured — fail closed

export type ClassifiedTrialChange = {
  /** Workspace-relative path, forward slashes. */
  path: string
  kind: TrialMutationKind
  category: TrialChangeCategory
  reason: string
}

export type AgentRuntimePolicy = {
  provider: string
  version: number
  /** Workspace-relative runtime paths (forward slashes) that the provider's
   *  narrow convention confirms as its own runtime files. Empty means this
   *  session exempts nothing. The set is recomputed per call because the
   *  recognition must observe the CURRENT trial bytes. */
  runtimeRelativePaths(): Set<string>
}

function projectInsideWorkspace(workspaceRoot: string, projectRelative: string): boolean {
  const root = resolve(workspaceRoot)
  const project = resolve(workspaceRoot, projectRelative)
  if (project === root) return true
  const rest = relative(root, project)
  return rest !== '' && rest !== '..' && !rest.startsWith(`..${sep}`) && !rest.startsWith(`..${'/'}`) && !isAbsolute(rest)
}

const OPENCODE_RUNTIME_ROOT = '.opencode'
const OPENCODE_RUNTIME_FILES = ['package.json', 'package-lock.json']

/** Recognize ONLY OpenCode's own plugin installation, never project manifests.
 *  The explicit provider/project scope, physical paths (no symlink roots), and
 *  the plugin-only manifest+lock pair are all required. Returns the
 *  workspace-relative runtime pair, or an empty set when the bytes do not
 *  match (missing, linked or unreadable runtime files keep the ordinary guard). */
function openCodeRuntimePaths(workspaceRoot: string, projectRelative: string): Set<string> {
  const result = new Set<string>()
  const root = resolve(workspaceRoot)
  const project = resolve(workspaceRoot, projectRelative)
  if (!projectInsideWorkspace(workspaceRoot, projectRelative)) return result
  // Inspect only ancestors inside the private tree, including intermediate
  // monorepo roots. Never search the host's parent directories.
  if (project !== root) {
    for (const path of openCodeRuntimePaths(workspaceRoot, relative(root, resolve(project, '..')) || '.')) result.add(path)
  }
  const directory = join(project, OPENCODE_RUNTIME_ROOT)
  try {
    const physical = realpathSync(directory)
    const samePath = process.platform === 'win32'
      ? physical.toLowerCase() === directory.toLowerCase() : physical === directory
    if (!samePath) return result
    const manifestPath = join(directory, 'package.json')
    const lockPath = join(directory, 'package-lock.json')
    if (!lstatSync(manifestPath).isFile() || !lstatSync(lockPath).isFile()) return result
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
    const deps = manifest.dependencies
    if (Object.keys(manifest).some(key => key !== 'dependencies')) return result
    if (!deps || Object.keys(deps).length !== 1 || typeof deps['@opencode-ai/plugin'] !== 'string') return result
    const version = deps['@opencode-ai/plugin']
    if (!/^\d+\.\d+\.\d+(?:-[\w.-]+)?$/.test(version)) return result
    const lock = JSON.parse(readFileSync(lockPath, 'utf8'))
    const lockedRoot = lock.packages?.['']
    if (lock.name !== '.opencode' || lock.lockfileVersion !== 3 || !lockedRoot) return result
    if (Object.keys(lockedRoot).some(key => key !== 'dependencies')) return result
    if (JSON.stringify(lockedRoot.dependencies) !== JSON.stringify(deps)) return result
    const plugin = lock.packages?.['node_modules/@opencode-ai/plugin']
    if (plugin?.version !== version) return result
    for (const file of OPENCODE_RUNTIME_FILES) {
      result.add(relative(root, realpathSync(join(directory, file))).split(sep).join('/'))
    }
  } catch {
    // Missing, linked or unrecognized runtime files keep the ordinary guard.
  }
  return result
}

/** Backwards-compatible entry point for callers that only need the recognized
 *  runtime pair (contract checks, logging). Prefer the classifier
 *  (classifyTrialChanges) for a full per-change verdict. */
export function openCodeRuntimeManifestPaths(workspaceRoot: string, scope?: AgentRuntimeScope): Set<string> {
  if (scope?.provider !== 'opencode') return new Set<string>()
  return openCodeRuntimePaths(workspaceRoot, scope.projectRelative)
}

type RuntimeAdapter = {
  provider: string
  runtimeRelativePaths(workspaceRoot: string, projectRelative: string): Set<string>
}

function strictRuntime(provider: string): RuntimeAdapter {
  return { provider, runtimeRelativePaths: () => new Set<string>() }
}

/** Explicit adapter registry. A provider absent here (or any unknown name)
 *  runs STRICT — no in-trial runtime exemption — until its launcher is
 *  verified to place runtime files inside a trial. */
const ADAPTERS: Readonly<Record<string, RuntimeAdapter>> = {
  opencode: { provider: 'opencode', runtimeRelativePaths: openCodeRuntimePaths },
  claude: strictRuntime('claude'),
  codex: strictRuntime('codex'),
}

export function runtimePolicyFor(provider: string, ctx: { workspaceRoot: string; projectRelative: string }): AgentRuntimePolicy {
  const adapter = ADAPTERS[provider] ?? strictRuntime(provider)
  return {
    provider,
    version: AGENT_RUNTIME_POLICY_VERSION,
    runtimeRelativePaths: () => adapter.runtimeRelativePaths(ctx.workspaceRoot, ctx.projectRelative),
  }
}
