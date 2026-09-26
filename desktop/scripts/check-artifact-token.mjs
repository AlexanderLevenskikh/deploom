// A08: per-project artifact paths must never collide when display names
// slugify to the same ASCII token. The real production projectArtifactToken
// from main.ts appends a case-stable hex digest of the ORIGINAL name, so
// 'Проект один'/'Проект два', 'foo/bar'/'foo-bar' and 'Foo'/'foo' all get
// distinct artefact identities on every filesystem (including case-insensitive
// Windows), while the same name stays deterministic.
import fs from 'node:fs'
import { createHash } from 'node:crypto'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../electron/main.ts', import.meta.url), 'utf8')
const start = source.indexOf('function projectArtifactToken(')
const end = source.indexOf('function baselineIntentPath(', start)
if (start < 0 || end < 0) throw new Error('projectArtifactToken not found in main.ts')
const slice = source.slice(start, end)
const js = ts.transpileModule(slice, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const { projectArtifactToken } = new Function('createHash', js + '\nreturn { projectArtifactToken };')(createHash)

const pairs = [
  ['Кириллица через пробелы', 'Проект один', 'Проект два'],
  ['Слэш против дефиса', 'foo/bar', 'foo-bar'],
  ['Регистр на Windows', 'Foo', 'foo'],
  ['Длинные коллизии slug', 'a b c d e', 'a-b-c-d-e'],
]
for (const [label, a, b] of pairs) {
  const ta = projectArtifactToken(a)
  const tb = projectArtifactToken(b)
  if (ta === tb) throw new Error(`A08: ${label} — '${a}' and '${b}' must not collide, both got ${ta}`)
}
// Determinism: the same name always maps to the same token.
for (const name of ['Проект один', 'foo/bar', 'Проект Два']) {
  if (projectArtifactToken(name) !== projectArtifactToken(name)) throw new Error(`A08: token for '${name}' must be deterministic`)
}
// The readable slug prefix stays for debuggability.
if (!projectArtifactToken('Проект один').startsWith('project-')) throw new Error('A08: token must keep a readable slug prefix')
// Tokens are filesystem-safe (no separators or reserved chars).
for (const name of ['Проект один', 'foo/bar', 'a:b*?', 'Ударение émojis 😀']) {
  const token = projectArtifactToken(name)
  if (/[\\/:*?"<>|]/.test(token)) throw new Error(`A08: token must be filesystem-safe, got ${token}`)
}

console.log('Project artifact tokens collision-resistant OK')
