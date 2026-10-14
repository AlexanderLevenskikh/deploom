export type StorageProgress = { operationId: string; stage: 'scan' | 'validate' | 'clean' | 'done' | 'canceled'; path: string; processed: number; total: number; percent: number | null; files: number; bytes: number }
export type StorageItem = { path: string; category: string; reason: string; eligible: boolean; bytes: number; files: number; signature: string }
export type StorageStatus = { phase: 'idle' | 'inspect' | 'clean' | 'done' | 'failed' | 'canceled'; action?: 'inspect' | 'clean'; automatic?: boolean; control?: 'pausing' | 'paused' | 'resuming' | 'canceling'; operationId?: string; progress?: StorageProgress; result?: StorageResult; error?: string }
export type StorageResult = { root: string; items: StorageItem[]; volumes: { path: string; freeBytes: number }[]; eligible: number; eligibleBytes: number; protectedBytes: number; removed: number; compacted?: number; canceled?: boolean; partial?: boolean; failed: number; protected: number; reclaimedBytes: number; results?: { path: string; status: string; reason?: string }[] }
export type StorageControlCommand = 'pause' | 'resume' | 'cancel'
export function storageBlocksMigration(status?: StorageStatus): boolean { return status?.phase === 'clean' }
export function storageOperationActive(status?: StorageStatus): boolean { return status?.phase === 'inspect' || status?.phase === 'clean' }
