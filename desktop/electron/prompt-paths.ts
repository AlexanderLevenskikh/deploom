export function rebindOperationalProjectPath(prompt: string, canonicalProjectPath: string, workerProjectPath: string): string {
  if (!canonicalProjectPath || !workerProjectPath || canonicalProjectPath === workerProjectPath) return prompt
  const escapedCanonicalPath = JSON.stringify(canonicalProjectPath).slice(1, -1)
  const escapedWorkerPath = JSON.stringify(workerProjectPath).slice(1, -1)
  return prompt
    .replaceAll(canonicalProjectPath, workerProjectPath)
    .replaceAll(escapedCanonicalPath, escapedWorkerPath)
}
