import { isAbsolute, relative, resolve } from 'node:path'

const DEFAULT_STATE_PATHS = [
  '.dependency-roadmap/desktop/flow-state.json',
  '.dependency-roadmap/groups.override.json',
  '.dependency-roadmap/history',
  '.dependency-roadmap/settings.local.example.json',
  '.dependency-roadmap/state',
  'knowledge',
]

export function teamStatePaths(settingsPath: string, settings: Record<string, unknown>, workspaceRoot?: string): string[] {
  const configured = ['groupsConfig', 'historyDir', 'dashboardState']
    .flatMap((key) => typeof settings[key] === 'string' ? [String(settings[key])] : [])
  return [...new Set([settingsPath, ...DEFAULT_STATE_PATHS, ...configured])]
    .map(value => workspaceRoot ? relative(resolve(workspaceRoot), resolve(workspaceRoot, value)).replace(/\\/g, '/') : value.replace(/\\/g, '/'))
    .filter(value => value && value !== '.' && value !== '.dependency-roadmap' && !isAbsolute(value) && !value.split('/').includes('..') && !value.startsWith(':') && !value.split('/').some(part => LOCAL_STATE_DIRECTORIES.includes(part)) && !value.endsWith('/settings.local.json'))
}
// Exclusions also protect accidentally tracked/staged runtime copies. Literal
// positives prevent configured paths from becoming broad Git pathspecs.
const LOCAL_STATE_DIRECTORIES = ['iterative', 'snapshot-objects', 'cache', 'tmp', 'downloads', 'node_modules', 'run-archive', 'checkpoint-build-upgrades', '.dependency-roadmap-worktrees']
export function teamStatePathspecs(paths: string[]): string[] {
  if (!paths.length) throw new Error('No safe team state paths to commit')
  return [...paths.map(value => `:(literal)${value}`),
    ...LOCAL_STATE_DIRECTORIES.map(value => `:(glob,exclude)**/${value}/**`),
    ':(glob,exclude)**/settings.local.json', ':(glob,exclude)**/.env',
  ]
}
