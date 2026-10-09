export type StorageProgress = { operationId: string; stage: 'scan' | 'validate' | 'clean' | 'done'; path: string; processed: number; total: number; percent: number | null; files: number; bytes: number }
export type StorageItem = { path: string; category: string; reason: string; eligible: boolean; bytes: number; files: number; signature: string }
export type StorageStatus = { phase: 'idle' | 'inspect' | 'clean' | 'done' | 'failed'; action?: 'inspect' | 'clean'; automatic?: boolean; operationId?: string; progress?: StorageProgress; result?: StorageResult; error?: string }
export type StorageResult = { root: string; items: StorageItem[]; volumes: { path: string; freeBytes: number }[]; eligible: number; eligibleBytes: number; protectedBytes: number; removed: number; failed: number; protected: number; reclaimedBytes: number; results?: { path: string; status: string; reason?: string }[] }

export class StorageCleanupController {
  private state: StorageStatus = { phase: 'idle' }
  private queued?: ReturnType<typeof setTimeout>
  status(): StorageStatus { return this.state }
  private publish(state: StorageStatus): void { this.state = state; try { this.changed(state) } catch { /* renderer closure cannot interrupt maintenance */ } }
  scheduleAutomatic(): void {
    if (this.queued) return
    const start = async () => {
      this.queued = undefined
      if (this.busy || this.active()) { this.queued = setTimeout(() => void start(), 2000); this.queued.unref(); return }
      try {
        const preview = await this.maintenance('inspect', `auto-${Date.now()}`, () => {}, true)
        if (preview.eligible > 0 && !this.active()) await this.maintenance('clean', `auto-${Date.now()}`, () => {}, true)
        else if (this.active()) this.scheduleAutomatic()
      } catch { /* the UI exposes failure; no silent deletion retry */ }
    }
    this.queued = setTimeout(() => void start(), 1000)
    this.queued.unref()
  }
  private busy = false
  private cleaning = false
  assertMigrationAllowed(): void { if (this.cleaning) throw new Error('Storage cleanup is running; wait before starting a migration') }
  private preview?: StorageResult
  constructor(private readonly run: (action: 'inspect' | 'clean', payload: object, line: (line: string) => void) => Promise<string>, private readonly workspaces: () => string[], private readonly active: () => boolean, private readonly changed: (state: StorageStatus) => void = () => {}) {}
  async maintenance(action: unknown, operationId: unknown, emit: (event: StorageProgress) => void, automatic = false): Promise<StorageResult> {
    if (action !== 'inspect' && action !== 'clean') throw new Error('Invalid storage action')
    if (typeof operationId !== 'string' || !/^[a-zA-Z0-9-]{1,80}$/.test(operationId)) throw new Error('Invalid storage operation')
    if (this.busy) throw new Error('Storage maintenance is already running')
    if (action === 'clean' && this.active()) throw new Error('Stop active migrations before cleaning storage')
    if (action === 'clean' && !this.preview) throw new Error('Inspect storage before cleaning')
    this.busy = true
    this.cleaning = action === 'clean'
    this.publish({ phase: action, action, operationId, automatic })
    if (action === 'inspect') this.preview = undefined
    try {
      const output = await this.run(action, { workspaces: this.workspaces(), ...(action === 'clean' ? { plan: this.preview } : { eligibleOnly: automatic }) }, line => {
        if (!line.startsWith('STORAGE_PROGRESS_V1 ')) return
        try {
          const progress = { ...JSON.parse(line.slice('STORAGE_PROGRESS_V1 '.length)), operationId } as StorageProgress
          this.publish({ ...this.state, progress }); emit(progress)
        } catch { /* malformed progress has no authority */ }
      })
      const line = output.split(/\r?\n/).reverse().find(item => item.startsWith('STORAGE_RESULT_V1 '))
      if (!line) throw new Error('Storage maintenance returned no result')
      const result = JSON.parse(line.slice('STORAGE_RESULT_V1 '.length)) as StorageResult
      this.preview = action === 'inspect' ? result : undefined
      this.publish({ ...this.state, phase: 'done', result })
      return result
    } catch (error) {
      this.publish({ ...this.state, phase: 'failed', error: error instanceof Error ? error.message : String(error) })
      throw error
    } finally {
      if (action === 'clean') this.preview = undefined
      this.busy = false
      this.cleaning = false
    }
  }
}
