import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export type WorkspaceCommandResult = { code: number; stdout: string; stderr: string }
export type WorkspaceCommandRunner = (command: string, args: string[], cwd: string, timeoutMs?: number) => Promise<WorkspaceCommandResult>

export const WORKSPACE_SETTINGS_PATH = '.dependency-roadmap/settings.project.json'

const WORKSPACE_GITIGNORE = `# Dependency Flow local/generated files
.dependency-roadmap/settings.local.json
.dependency-roadmap/artifacts/*
!.dependency-roadmap/artifacts/.gitkeep
.dependency-roadmap/desktop/downloads/

# Common local noise
.DS_Store
Thumbs.db
*.tmp
*.bak
`

function commandError(result: WorkspaceCommandResult, fallback: string): Error {
  return new Error(result.stderr.trim() || result.stdout.trim() || fallback)
}

export function createWorkspaceSkeleton(target: string): void {
  const roadmap = join(target, '.dependency-roadmap')
  for (const relative of [
    'artifacts',
    'state',
    'history/runs',
    'history/snapshots',
    'history/baselines',
    'desktop',
  ]) mkdirSync(join(roadmap, relative), { recursive: true })

  writeFileSync(join(target, WORKSPACE_SETTINGS_PATH), `${JSON.stringify({ schemaVersion: 1, projects: [] }, null, 2)}\n`, 'utf8')
  writeFileSync(join(roadmap, 'artifacts', '.gitkeep'), '', 'utf8')
  writeFileSync(join(target, '.gitignore'), WORKSPACE_GITIGNORE, 'utf8')
}

export async function initializeWorkspaceRepository(
  target: string,
  teamRemote: string | undefined,
  run: WorkspaceCommandRunner,
): Promise<void> {
  mkdirSync(target, { recursive: true })
  const init = await run('git', ['init'], target, 30_000)
  if (init.code !== 0) throw commandError(init, 'Не удалось создать Git-репозиторий workspace.')

  // Do not depend on the user's global init.defaultBranch. A workspace created
  // on two machines should start from the same branch name.
  const branch = await run('git', ['symbolic-ref', 'HEAD', 'refs/heads/main'], target, 15_000)
  if (branch.code !== 0) throw commandError(branch, 'Не удалось установить начальную ветку workspace.')

  createWorkspaceSkeleton(target)

  const add = await run('git', ['add', '--', '.gitignore', WORKSPACE_SETTINGS_PATH, '.dependency-roadmap/artifacts/.gitkeep'], target, 15_000)
  if (add.code !== 0) throw commandError(add, 'Не удалось подготовить начальное состояние workspace.')

  // A fresh repository needs a root commit before normal branch/status/recovery
  // logic can operate. Use command-local identity so workspace creation never
  // mutates or depends on the user's global Git configuration.
  const commit = await run('git', [
    '-c', 'user.name=Dependency Flow',
    '-c', 'user.email=dependency-flow@localhost',
    'commit', '-m', 'chore: initialize Dependency Flow workspace',
  ], target, 30_000)
  if (commit.code !== 0) throw commandError(commit, 'Не удалось создать начальный commit workspace.')

  const remote = String(teamRemote || '').trim()
  if (remote) {
    const addRemote = await run('git', ['remote', 'add', 'origin', remote], target, 15_000)
    if (addRemote.code !== 0) throw commandError(addRemote, 'Не удалось добавить командный remote workspace.')
  }

  if (!existsSync(join(target, '.git'))) throw new Error('Git workspace не был создан.')
}
