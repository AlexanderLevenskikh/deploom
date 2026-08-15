const DEFAULT_STATE_PATHS = [
  '.dependency-roadmap/desktop/flow-state.json',
  '.dependency-roadmap/groups.override.json',
  '.dependency-roadmap/history',
  '.dependency-roadmap/settings.local.example.json',
  '.dependency-roadmap/state',
  'knowledge',
]

export function teamStatePaths(settingsPath: string, settings: Record<string, unknown>): string[] {
  const configured = ['groupsConfig', 'historyDir', 'dashboardState']
    .flatMap((key) => typeof settings[key] === 'string' ? [String(settings[key])] : [])
  return [...new Set([settingsPath, ...DEFAULT_STATE_PATHS, ...configured])]
    .filter((value) => value && !value.includes('.dependency-roadmap/desktop/downloads'))
}