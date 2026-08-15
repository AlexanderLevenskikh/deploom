export type MigrationGatePolicy = {
  verificationCommands: string[]
  integrationVerificationCommands: string[]
  source: 'migration-settings' | 'release-gates' | 'package-scripts' | 'none'
}

type UnknownRecord = Record<string, unknown>

function asRecord(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : {}
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return [...new Set(value.filter((item): item is string => typeof item === 'string').map((item) => item.trim()).filter(Boolean))]
}

function migrationRecord(value: unknown): UnknownRecord {
  return asRecord(asRecord(value).migration)
}

function releaseRecord(value: unknown): UnknownRecord {
  return asRecord(asRecord(value).release)
}

function projectRecord(settings: UnknownRecord, projectName: string): UnknownRecord {
  const projects = Array.isArray(settings.projects) ? settings.projects : []
  return projects.map(asRecord).find((item) => String(item.name ?? '') === projectName) ?? {}
}

function packageScriptCommands(packageJson: unknown, packageManager: 'yarn' | 'npm' | 'pnpm'): string[] {
  const scripts = asRecord(asRecord(packageJson).scripts)
  // Prefer focused checks over a monolithic `lint`; this gives the agent a
  // concrete failing subsystem and avoids executing two equivalent lint
  // entry points. Existing project scripts only; nothing is generated.
  // Run production build before tests: test runners commonly write caches or
  // result files inside the source tree, and strict build plugins can then
  // reject those artifacts as unused inputs.
  const candidates = ['lint:types', 'typecheck', 'lint:styles', 'lint:scripts', 'build', 'test:unit', 'test']
  const selectedNames: string[] = []
  for (const name of candidates) {
    if (typeof scripts[name] !== 'string') continue
    if (name === 'typecheck' && selectedNames.includes('lint:types')) continue
    if (name === 'test' && selectedNames.includes('test:unit')) continue
    selectedNames.push(name)
  }
  return selectedNames.map((name) => packageManager === 'yarn' ? `yarn ${name}` : `${packageManager} run ${name}`)
}

export function migrationGatePolicy(settingsValue: unknown, projectName: string, packageJson: unknown, packageManager: 'yarn' | 'npm' | 'pnpm' = 'yarn'): MigrationGatePolicy {
  const settings = asRecord(settingsValue)
  const project = projectRecord(settings, projectName)
  const hasProjectMigration = Object.prototype.hasOwnProperty.call(project, 'migration')
  const globalMigration = migrationRecord(settings)
  const projectMigration = hasProjectMigration ? migrationRecord(project) : migrationRecord(asRecord(project.git))
  const migration = { ...globalMigration, ...projectMigration }

  const configured = stringList(migration.verificationCommands)
  const configuredIntegration = stringList(migration.integrationVerificationCommands)
  if (configured.length || configuredIntegration.length) {
    const verificationCommands = configured.length ? configured : configuredIntegration
    return {
      verificationCommands,
      integrationVerificationCommands: configuredIntegration.length ? configuredIntegration : verificationCommands,
      source: 'migration-settings',
    }
  }

  const globalRelease = releaseRecord(settings)
  const projectRelease = Object.prototype.hasOwnProperty.call(project, 'release')
    ? releaseRecord(project)
    : releaseRecord(asRecord(project.git))
  const releaseCommands = stringList({ ...globalRelease, ...projectRelease }.finalGateCommands)
  if (releaseCommands.length) {
    return { verificationCommands: releaseCommands, integrationVerificationCommands: releaseCommands, source: 'release-gates' }
  }

  const discovered = packageScriptCommands(packageJson, packageManager)
  return {
    verificationCommands: discovered,
    integrationVerificationCommands: discovered,
    source: discovered.length ? 'package-scripts' : 'none',
  }
}
