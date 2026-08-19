type TextFn = (ru: string, en: string) => string

const ANSI = /\u001B\[[0-?]*[ -/]*[@-~]/g
const FOREIGN = /FOREIGN_REGISTRY_URL:\s*resolved=(https?:\/\/[^\s;|]+);\s*package artifact URL is outside configured registry\s+([^|]+?)(?=\s*\||$)/gi

function packageFromArtifactUrl(raw: string): string | undefined {
  try {
    const url = new URL(raw)
    const parts = url.pathname.split('/').filter(Boolean).map((part) => decodeURIComponent(part))
    if (!parts.length) return undefined
    if (parts[0].startsWith('@') && parts.length >= 2) return `${parts[0]}/${parts[1]}`
    return parts[0]
  } catch {
    return undefined
  }
}

export function presentRunError(raw: string, text: TextFn): string {
  const clean = String(raw || '').replace(ANSI, '').trim()
  const matches = [...clean.matchAll(FOREIGN)]
  if (!matches.length) return clean

  const urls = matches.map((match) => match[1])
  const configured = matches.map((match) => match[2].trim()).find(Boolean) || text('настроенный registry', 'configured registry')
  const origins = [...new Set(urls.flatMap((value) => { try { return [new URL(value).origin] } catch { return [] } }))]
  const packages = [...new Set(urls.map(packageFromArtifactUrl).filter((value): value is string => Boolean(value)))]
  const examples = packages.slice(0, 8)

  return [
    text('Registry policy mismatch', 'Registry policy mismatch'),
    '',
    text(`Workspace использует registry: ${configured}`, `Workspace registry: ${configured}`),
    text(`Найдено ${matches.length} package artifact URL из другого registry${origins.length ? `: ${origins.join(', ')}` : ''}.`, `Found ${matches.length} package artifact URLs from another registry${origins.length ? `: ${origins.join(', ')}` : ''}.`),
    '',
    text('Это safety stop, а не несовместимость зависимостей. Если проект должен использовать публичный npm registry, создайте/подключите для него workspace без корпоративного registry override. Если проект обязан использовать Nexus — исправьте lockfile/registry source, а не обходите проверку.', 'This is a safety stop, not a dependency incompatibility. If the project should use the public npm registry, create/connect a workspace without the corporate registry override. If the project must use Nexus, fix the lockfile/registry source instead of bypassing the check.'),
    examples.length ? '' : undefined,
    examples.length ? `${text('Примеры', 'Examples')}: ${examples.join(', ')}${packages.length > examples.length ? ` +${packages.length - examples.length}` : ''}` : undefined,
    '',
    text('Полный исходный текст остаётся в «Логи».', 'The full raw error remains available in Logs.'),
  ].filter((line): line is string => line !== undefined).join('\n')
}
