// Collapse verbose HTTP/HTML failures from a public update feed into a compact
// message suitable for the header tooltip/log panel. Public GitHub Releases do
// not need a client authentication classification or token UI.
export function summarizeUpdaterError(raw: string, limit = 400): string {
  const text = raw.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()
  return text.length > limit ? `${text.slice(0, limit - 3)}...` : text
}
