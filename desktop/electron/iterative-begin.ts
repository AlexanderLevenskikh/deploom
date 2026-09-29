// Desktop producer of the Python `iterative_migration.py begin` step.
//
// The Desktop starts a NEW durable iterative run (R1): it owns where the
// Python CLI is pointed (project-dir, target level, exact lag-policy targets
// and the verify/check commands) and hands begin a machine-checkable run-config
// input. `begin` then captures C0 and, if the control verification is RED,
// publishes a bootstrap repair request and enters BOOTSTRAP_REPAIR; the
// coordinator resumes from the durable state exactly as after a restart.
//
// The targets map mirrors the product's own fallback (dashboard-state rows:
// lagPolicyTarget, then lag_target, then a concrete min_lag_<N>m, then
// min_lag_12m) so the iterative run plans toward the SAME versions the roadmap
// already accepted — never a Desktop-invented version.
import { existsSync, readFileSync } from 'node:fs'

export type IterativeBeginOptions = {
  projectDir: string
  projectName: string
  targetLevel: 'yellow' | 'green'
  workspaceId?: string
  projectId?: string
  toolBuildId?: string
  targetsFile?: string
  // R8: the independent audit runs with the user's ACTUAL package lag policy.
  // dashboardStatePath points at the workspace dashboard-state.json (the same
  // file the targets come from); auditPolicy pins the goal thresholds so the
  // audit evidence is bound to the same numbers the roadmap enforces.
  dashboardStatePath?: string
  auditPolicy?: { lagPolicyMonths?: number; minLagOkPct?: number; maxKnownHigh?: number }
  // D3: explicit Node.js for project/CI (exact or major). Empty = no claim —
  // no --requested-node is passed and the run makes no CI-runtime commitment.
  requestedNode?: string
}

/** Extract exact target versions from one dashboard-state row batch.
 * The field fallback is the product's own: an explicit lagPolicyTarget, then
 * the snake-case lag_target, then the concrete min_lag_<N>m boundary for the
 * row's threshold, then min_lag_12m. Rows without a concrete version, or a
 * version that is only a marker (latest/—), are skipped. */
export function targetsFromDashboardRows(rows: unknown[]): Record<string, string> {
  const targets: Record<string, string> = {}
  for (const raw of rows) {
    if (!raw || typeof raw !== 'object') continue
    const row = raw as Record<string, any>
    const name = String(row.package ?? row.name ?? '').trim()
    if (!name) continue
    if (row.scopeExcluded) continue
    const months = Number(row.lagThresholdMonths ?? row.lagMonths ?? 12) || 12
    const candidate = String(
      row.lagPolicyTarget ??
      row.lag_target ??
      row[`min_lag_${months}m`] ??
      row.min_lag_12m ??
      '',
    ).trim()
    if (!candidate || /^(latest|-|—)$/i.test(candidate)) continue
    targets[name] = candidate
  }
  return targets
}

/** Targets for one project from the tracked dashboard-state file
 * (`.dependency-roadmap/state/dashboard-state.json`, keyed by `projects`). */
export function targetsFromDashboardState(dashboardStatePath: string, projectName: string): Record<string, string> {
  if (!dashboardStatePath || !existsSync(dashboardStatePath)) return {}
  try {
    const parsed = JSON.parse(readFileSync(dashboardStatePath, 'utf8')) as Record<string, any>
    const rows = parsed?.projects?.[projectName]
    return targetsFromDashboardRows(Array.isArray(rows) ? rows : [])
  } catch {
    return {}
  }
}

/** Invocation shape for the begin step. `--run-dir` is a TOP-LEVEL CLI
 * argument, so it must precede the subcommand; `--targets-file` is present
 * only when a targets file was actually produced (an empty run is legal and
 * will report NO_ACTIONABLE until the roadmap provides concrete targets). */
export function iterativeBeginInvocation(
  runDir: string,
  options: IterativeBeginOptions,
  scriptPath: string,
  python: string = 'python',
): { command: string; args: string[] } {
  const args = [scriptPath, '--run-dir', runDir, 'begin', '--project-dir', options.projectDir]
  if (options.projectName) args.push('--project-name', options.projectName)
  args.push('--target-level', options.targetLevel)
  if (options.targetsFile) args.push('--targets-file', options.targetsFile)
  if (options.dashboardStatePath) args.push('--dashboard-state', options.dashboardStatePath)
  const auditPolicy = options.auditPolicy
  if (auditPolicy) {
    if (auditPolicy.lagPolicyMonths !== undefined) args.push('--lag-months', String(auditPolicy.lagPolicyMonths))
    if (auditPolicy.minLagOkPct !== undefined) args.push('--min-lag-ok-pct', String(auditPolicy.minLagOkPct))
    if (auditPolicy.maxKnownHigh !== undefined) args.push('--max-known-high', String(auditPolicy.maxKnownHigh))
  }
  if (options.workspaceId) args.push('--workspace-id', options.workspaceId)
  if (options.projectId) args.push('--project-id', options.projectId)
  if (options.toolBuildId) args.push('--tool-build-id', options.toolBuildId)
  if (options.requestedNode) args.push('--requested-node', options.requestedNode)
  return { command: python, args }
}
