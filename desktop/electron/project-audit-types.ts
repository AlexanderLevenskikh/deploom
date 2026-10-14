import type { AuditView } from './iterative-audit-view.js'
export type AuditContext = { dependencyHash: string; policyHash: string; runtimeHash: string; registryHash: string; hash: string; resource: string }
export type ProjectAuditStatus = { phase: 'missing' | 'running' | 'done' | 'error' | 'interrupted'; operationId?: string; startedAt?: number; finishedAt?: number; error?: string; applicability?: string; previousPresent: boolean }
export type ProjectAuditView = { lifecycle: ProjectAuditStatus; audit?: AuditView; previous?: AuditView; blockedReason?: string }
