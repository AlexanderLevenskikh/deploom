export type MigrationValidationProfile = {
  commands: string[]
  unitCommand?: string
  compareExistingFailures?: boolean
  deferredChecks?: string
}

export function validationProfilesEqual(a?: MigrationValidationProfile, b?: MigrationValidationProfile): boolean {
  const normalized = (p?: MigrationValidationProfile) => ({ commands: (p?.commands || []).map(c => c.trim()).filter(Boolean), unitCommand: (p?.unitCommand || '').trim(), compareExistingFailures: p?.compareExistingFailures === true, deferredChecks: (p?.deferredChecks || '').trim() })
  return JSON.stringify(normalized(a)) === JSON.stringify(normalized(b))
}

export function validateMigrationProfile(value: MigrationValidationProfile): MigrationValidationProfile {
  if (!value || !Array.isArray(value.commands) || value.commands.length === 0 || value.commands.length > 20 || value.commands.some(c => typeof c !== 'string' || !c.trim() || /[\r\n]/.test(c))) throw new Error('VALIDATION_COMMANDS_REQUIRED')
  const commands = value.commands.map(c => c.trim())
  const unitCommand = String(value.unitCommand || '').trim()
  if (value.compareExistingFailures && (!commands.includes(unitCommand) || /&&|\|\||[;|\r\n]|--reporter|--outputFile/.test(unitCommand))) throw new Error('VITEST_COMMAND_MUST_BE_A_SINGLE_SELECTED_COMMAND')
  if (typeof (value.deferredChecks ?? '') !== 'string' || (value.deferredChecks?.length ?? 0) > 4000) throw new Error('DEFERRED_CHECKS_INVALID')
  return { commands: value.commands.map(c => c.trim()), unitCommand, compareExistingFailures: value.compareExistingFailures === true, deferredChecks: (value.deferredChecks || '').trim() }
}
