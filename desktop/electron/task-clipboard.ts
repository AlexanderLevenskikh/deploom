import {
  copyPayloadTooLarge,
  taskCopyPayload,
  type IterativeTaskView,
} from './iterative-migration.js'

// The only way the UI ever touches the real OS clipboard for a task body:
// main.ts runs this with the real Electron clipboard as the writer, and the
// check script drives it with a fake one over the identical path. A "Copied"
// confirmation is only ever produced after the writer confirmed the exact
// bytes came back (clipboard.readText() equals what we wrote). Oversized or
// missing-language payloads are refused before any write, and the caller never
// supplies free-form content: the bytes are always the producer's artifact.

export type ClipboardWriter = {
  writeText(text: string): void
  readText(): string
}

export type CopyTaskResult = { ok: boolean; fingerprint?: string; error?: string }

export function copyTaskWithVerification(
  task: IterativeTaskView,
  language: string,
  writer: ClipboardWriter,
): CopyTaskResult {
  const payload = taskCopyPayload(task, language)
  if (!payload) {
    return { ok: false, error: `NO_TASK_LANGUAGE: ${language}` }
  }
  const tooLarge = copyPayloadTooLarge(payload)
  if (tooLarge) {
    return { ok: false, error: tooLarge }
  }
  writer.writeText(payload.text)
  try {
    const readBack = writer.readText()
    if (readBack !== payload.text) {
      return {
        ok: false,
        error: `CLIPBOARD_VERIFY_MISMATCH: readback length ${readBack.length} !== expected ${payload.text.length}`,
      }
    }
  } catch {
    return { ok: false, error: 'CLIPBOARD_READBACK_UNAVAILABLE' }
  }
  return { ok: true, fingerprint: payload.fingerprint }
}
