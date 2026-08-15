export function releaseBranchForAction(
  action: 'release' | 'publish',
  requested?: string,
  saved?: string,
  configured?: string,
): string {
  const normalized = {
    requested: requested?.trim(),
    saved: saved?.trim(),
    configured: configured?.trim(),
  }
  return action === 'publish'
    ? normalized.saved || normalized.configured || normalized.requested || 'libs-release'
    : normalized.requested || normalized.configured || normalized.saved || 'libs-release'
}