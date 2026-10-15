import { existsSync, lstatSync, realpathSync } from 'node:fs'
import { join, resolve } from 'node:path'

/** Only DepLoom's existing data directory, selected by the backend. */
export function defenderExclusionTarget(workspace: string): string {
  const target = join(resolve(workspace), '.dependency-roadmap')
  if (!existsSync(target) || !lstatSync(target).isDirectory() || lstatSync(target).isSymbolicLink() || realpathSync(target).toLowerCase() !== target.toLowerCase()) throw new Error('DEFENDER_PATH_INVALID: expected a physical .dependency-roadmap directory')
  return target
}
const quote = (value: string) => "'" + value.replace(/'/g, "''") + "'"
/** Encoded commands keep paths (including quotes/$/Unicode) out of shell syntax. */
export function defenderExclusionCommand(target: string): string {
  const script = `$ErrorActionPreference='Stop'; try { $target=${quote(target)}; Add-MpPreference -ExclusionPath $target; if (@((Get-MpPreference).ExclusionPath) -notcontains $target) { throw 'Exclusion was not applied' }; exit 0 } catch { exit 1 }`
  const encoded = Buffer.from(script, 'utf16le').toString('base64')
  return `$ErrorActionPreference='Stop'; $p=Start-Process -FilePath "$PSHOME\\powershell.exe" -Verb RunAs -WindowStyle Hidden -ArgumentList '-NoProfile -NonInteractive -EncodedCommand ${encoded}' -PassThru -Wait; exit $p.ExitCode`
}
