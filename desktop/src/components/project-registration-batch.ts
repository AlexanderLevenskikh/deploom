/** Register one explicit selection; successful entries survive a later failure. */
export async function registerProjectBatch<T extends { path: string; packagePath?: string }>(
  entries: readonly T[],
  submit: (entry: T) => Promise<void>,
  completed: Set<string>,
  onRegistered: (paths: string[]) => void,
): Promise<void> {
  for (const entry of entries) {
    const key = entry.packagePath || entry.path
    if (completed.has(key)) continue
    await submit(entry)
    completed.add(key)
    onRegistered([...completed])
  }
}
