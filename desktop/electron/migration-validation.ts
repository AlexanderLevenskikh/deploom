import { existsSync, readFileSync, writeFileSync, mkdirSync, renameSync } from 'node:fs'
import { join } from 'node:path'

import { validateMigrationProfile, type MigrationValidationProfile } from './migration-validation-profile.js'
export { validateMigrationProfile, type MigrationValidationProfile } from './migration-validation-profile.js'


export function migrationProfilePath(runDir: string): string { return join(runDir, 'verification-profile.json') }

export function readMigrationProfile(runDir: string, projectDir: string): MigrationValidationProfile & { suggestedUnitCommand?: string } {
  const manifest = JSON.parse(readFileSync(join(projectDir, 'package.json'), 'utf8').replace(/^\uFEFF/, '')) as { scripts?: Record<string, string> }
  const scripts = manifest.scripts || {}
  const yarn = existsSync(join(projectDir, 'yarn.lock'))
  const pnpm = existsSync(join(projectDir, 'pnpm-lock.yaml'))
  const prefix = yarn ? 'yarn ' : pnpm ? 'pnpm run ' : 'npm run '
  const requiresEnvironment = (name: string) => /playwright|e2e|creevey|cypress|selenium/i.test(name + ' ' + (scripts[name] || ''))
  const names = ['lint:types', 'typecheck', 'flow:check', 'lint:flow', 'typecheck:flow', 'flow', 'lint:styles', 'lint:scripts', 'lint', 'build', 'test:unit', 'test'].filter(name => typeof scripts[name] === 'string' && scripts[name].trim() && !requiresEnvironment(name))
  const deferred = Object.keys(scripts).filter(requiresEnvironment).map(name => `${name}: requires a separate environment; not included`).join('\n')
  const unit = ['test:unit', 'test'].find(name => /\bvitest\b/.test(scripts[name] || ''))
  const suggestedUnitCommand = unit ? prefix + unit + (yarn || pnpm ? ' --run src' : ' -- --run src') : undefined
  const path = migrationProfilePath(runDir)
  const configPath = join(runDir, 'run-config.json')
  const pinned = existsSync(join(runDir, 'run.json')) && existsSync(configPath) ? JSON.parse(readFileSync(configPath, 'utf8')).validationProfile : undefined
  const stored = pinned ? validateMigrationProfile(pinned) : existsSync(path) ? validateMigrationProfile(JSON.parse(readFileSync(path, 'utf8'))) : { commands: names.map(name => prefix + name), compareExistingFailures: false, unitCommand: '', deferredChecks: deferred }
  return { ...stored, suggestedUnitCommand }
}

export function readMigrationScope(runDir: string): { mode?: string; existingFailures?: number; total?: number; skipped?: number } | undefined {
  const path = join(runDir, 'run-config.json')
  if (!existsSync(path) || !existsSync(join(runDir, 'run.json'))) return undefined
  return JSON.parse(readFileSync(path, 'utf8')).validationScope
}

export function saveMigrationProfile(runDir: string, value: MigrationValidationProfile): void {
  const profile = validateMigrationProfile(value)
  mkdirSync(runDir, { recursive: true })
  const path = migrationProfilePath(runDir)
  const temp = path + '.tmp'
  writeFileSync(temp, JSON.stringify(profile), 'utf8')
  renameSync(temp, path)
}
