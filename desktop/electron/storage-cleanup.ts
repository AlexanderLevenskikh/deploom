import type { StorageProgress, StorageStatus, StorageResult } from './storage-status.js'
export type { StorageProgress, StorageItem, StorageStatus, StorageResult, StorageControlCommand } from './storage-status.js'
export type StorageRunControls = { operationId: string; mode: () => 'run' | 'pause' | 'cancel'; bind: (write: (mode: 'run' | 'pause' | 'cancel') => void) => () => void; terminate: () => boolean }

export class StorageCleanupController {
  private state: StorageStatus = { phase: 'idle' }
  private queued?: ReturnType<typeof setTimeout>
  status(): StorageStatus { return this.state }
  private publish(state: StorageStatus): void { this.state = state; try { this.changed(state) } catch { /* renderer closure cannot interrupt maintenance */ } }
  private automaticSuspended = false
  suspendAutomatic(): void {
    this.automaticSuspended = true
    if (this.queued) { clearTimeout(this.queued); this.queued = undefined }
  }
  scheduleAutomatic(): void {
    if (this.queued || this.automaticSuspended) return
    const start = async () => {
      this.queued = undefined
      if (this.automaticSuspended) return
      if (this.busy || this.active()) { this.queued = setTimeout(() => void start(), 2000); this.queued.unref(); return }
      try {
        const preview = await this.maintenance('inspect', `auto-${Date.now()}`, () => {}, true)
        if (this.automaticSuspended) return
        if (!preview.canceled && preview.eligible > 0 && !this.active()) await this.maintenance('clean', `auto-${Date.now()}`, () => {}, true)
        else if (this.active()) this.scheduleAutomatic()
      } catch { /* the UI exposes failure; no silent deletion retry */ }
    }
    this.queued = setTimeout(() => void start(), 1000)
    this.queued.unref()
  }
  private busy = false
  private cleaning = false
  private requestedMode: 'run' | 'pause' | 'cancel' = 'run'
  private requestedAt = 0
  private writeControl?: (mode: 'run' | 'pause' | 'cancel') => void
  control(command: unknown, operationId: unknown): StorageStatus {
    if (!this.busy || operationId !== this.state.operationId) throw new Error('Storage operation is no longer active')
    if (command !== 'pause' && command !== 'resume' && command !== 'cancel') throw new Error('Invalid storage control')
    if (this.requestedMode === 'cancel') return this.state
    const mode = command === 'resume' ? 'run' : command
    if (mode === this.requestedMode) return this.state
    const previousMode = this.requestedMode
    const previousState = this.state
    this.requestedMode = mode; this.requestedAt = Date.now()
    if (mode === 'cancel' && this.queued) { clearTimeout(this.queued); this.queued = undefined }
    this.publish({ ...this.state, control: command === 'pause' ? 'pausing' : command === 'resume' ? 'resuming' : 'canceling' })
    try { this.writeControl?.(mode) } catch (error) { this.requestedMode = previousMode; this.publish(previousState); throw error }
    return this.state
  }
  assertMigrationAllowed(): void { if (this.cleaning) throw new Error('Storage cleanup is running; wait before starting a migration') }
  private preview?: StorageResult
  constructor(private readonly run: (action: 'inspect' | 'clean', payload: object, line: (line: string) => void, controls: StorageRunControls) => Promise<string>, private readonly workspaces: () => string[], private readonly active: () => boolean, private readonly changed: (state: StorageStatus) => void = () => {}) {}
  async maintenance(action: unknown, operationId: unknown, emit: (event: StorageProgress) => void, automatic = false): Promise<StorageResult> {
    if (action !== 'inspect' && action !== 'clean') throw new Error('Invalid storage action')
    if (typeof operationId !== 'string' || !/^[a-zA-Z0-9-]{1,80}$/.test(operationId)) throw new Error('Invalid storage operation')
    if (this.busy) throw new Error('Storage maintenance is already running')
    if (action === 'clean' && this.active()) throw new Error('Stop active migrations before cleaning storage')
    if (action === 'clean' && !this.preview) throw new Error('Inspect storage before cleaning')
    this.busy = true
    this.requestedMode = 'run'; this.requestedAt = 0
    this.cleaning = action === 'clean'
    this.publish({ phase: action, action, operationId, automatic })
    if (action === 'inspect') this.preview = undefined
    try {
      const output = await this.run(action, { workspaces: this.workspaces(), ...(action === 'clean' ? { plan: this.preview } : { eligibleOnly: automatic }) }, line => {
        if (line.startsWith('STORAGE_CONTROL_V1 ')) {
          try {
            const ack = JSON.parse(line.slice('STORAGE_CONTROL_V1 '.length))
            if (ack.operationId === operationId && ack.state === 'paused' && this.requestedMode === 'pause') this.publish({ ...this.state, control: 'paused' })
            if (ack.operationId === operationId && ack.state === 'running' && this.requestedMode === 'run') this.publish({ ...this.state, control: undefined })
          } catch { /* only acknowledged controls change the presentation */ }
          return
        }
        if (!line.startsWith('STORAGE_PROGRESS_V1 ')) return
        try {
          const progress = { ...JSON.parse(line.slice('STORAGE_PROGRESS_V1 '.length)), operationId } as StorageProgress
          this.publish({ ...this.state, progress }); emit(progress)
        } catch { /* malformed progress has no authority */ }
      }, {
        operationId, mode: () => this.requestedMode,
        bind: write => { write(this.requestedMode); this.writeControl = write; return () => { if (this.writeControl === write) this.writeControl = undefined } },
        terminate: () => this.requestedMode === 'cancel' && Date.now() - this.requestedAt > 30_000,
      })
      const line = output.split(/\r?\n/).reverse().find(item => item.startsWith('STORAGE_RESULT_V1 '))
      if (!line) throw new Error('Storage maintenance returned no result')
      const result = JSON.parse(line.slice('STORAGE_RESULT_V1 '.length)) as StorageResult
      this.preview = action === 'inspect' && !result.canceled ? result : undefined
      this.publish({ ...this.state, phase: result.canceled ? 'canceled' : 'done', control: undefined, result })
      return result
    } catch (error) {
      this.publish({ ...this.state, phase: this.state.control === 'canceling' ? 'canceled' : 'failed', control: undefined, error: error instanceof Error ? error.message : String(error) })
      throw error
    } finally {
      if (action === 'clean') this.preview = undefined
      this.busy = false
      this.cleaning = false
      this.writeControl = undefined
    }
  }
}
