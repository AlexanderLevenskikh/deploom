export type AuditView = {
  status: string; evidenceRef?: string; checkpointId?: string; generatedAt?: string
  auditComplete?: boolean; stale?: boolean; running?: boolean; error?: string
  lagOkPct?: number; lagOk?: number; lagTotal?: number; lagUnknown?: number
  packageTotals?: Record<string, number>; policy?: Record<string, number | string>
  requiredTargets?: Array<{ package: string; target?: string; current?: string; met: boolean }>
  keptPackages?: string[]
  vulnerablePackages?: Array<{ package: string; severity?: string; direct?: boolean; nodes?: string[] }>
}
export type DeliveryView = { status: string; branch?: string; requestedBranch?: string; branchResolution?: string; workspaceRoot?: string; projectRelative?: string; head?: string; commits?: string[]; audit?: AuditView; error?: string }
