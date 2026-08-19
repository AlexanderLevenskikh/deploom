#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

EXPECTED_VERSION = "0.2.58"

PROJECT_RAIL = r'''import { ChevronRight, FolderPlus, RefreshCw, Search, Trash2 } from 'lucide-react'
import { useLanguage } from '../i18n'
import type { ProjectSpec, WorkspaceDetails } from '../types'

type Props = {
  details: WorkspaceDetails
  selected?: ProjectSpec
  onSelectProject: (name: string) => void
  active: boolean
  onRefreshAll: () => void
  onAddProject: () => void
  onAddWorkspace: () => void
  onRemoveProject: (name: string) => void
}

export function ProjectRail({ details, selected, active, onSelectProject, onRefreshAll, onAddProject, onAddWorkspace, onRemoveProject }: Props) {
  const { t } = useLanguage()

  const requestRemove = (project: ProjectSpec) => {
    if (!window.confirm(t('projectRail.remove.confirm', { name: project.name }))) return
    onRemoveProject(project.name)
  }

  return (
    <aside className="project-rail">
      <div className="rail-title"><strong>{t('projectRail.projects')}</strong><Search size={17} /></div>
      <div className="project-list">
        {details.projects.map((project) => {
          const level = details.projectLevels[project.name]
          const tone = level?.status === 'red' ? 'danger' : level?.status === 'yellow' ? 'warning' : level?.status === 'green' ? 'success' : 'muted'
          const levelLabel = level?.status === 'red' ? t('levels.red') : level?.status === 'yellow' ? t('levels.yellow') : level?.status === 'green' ? t('levels.green') : t('levels.unknown')
          return <div key={project.name} className={`project-row-shell ${selected?.name === project.name ? 'selected' : ''}`}>
            <button className="project-row-main" onClick={() => onSelectProject(project.name)}>
              <span className={`status-dot ${tone}`} title={t('projectRail.currentLevel', { level: levelLabel })} />
              <span className="project-copy"><strong>{project.name}</strong><small>{project.git?.baseBranch || project.git?.sourceBranch || t('projectRail.notConfigured')}</small></span>
              <ChevronRight size={16} />
            </button>
            <button
              className="project-row-remove"
              disabled={active}
              title={t('projectRail.remove.title')}
              aria-label={t('projectRail.remove.aria', { name: project.name })}
              onClick={() => requestRemove(project)}
            >
              <Trash2 size={14} />
            </button>
          </div>
        })}
        {details.projects.length === 0 ? <p className="empty-copy">{t('projectRail.empty', { path: details.workspace.settingsPath })}</p> : null}
      </div>
      <div className="rail-actions">
        <button className="button secondary wide rail-refresh" disabled={active || details.projects.length === 0} onClick={onRefreshAll}><RefreshCw className={active ? 'spin' : ''} size={16} /> {t('projectRail.refreshAll')}</button>
        <button className="add-workspace" disabled={active} onClick={onAddProject}><FolderPlus size={17} /> {t('projectRail.addProject')}</button>
        <button className="add-workspace subtle" onClick={onAddWorkspace}><FolderPlus size={16} /> {t('projectRail.workspaceAction')}</button>
      </div>
    </aside>
  )
}
'''

WORKSPACE_DIALOG = r'''import { FolderGit2, GitFork, X } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useLanguage } from '../i18n'

type Props = {
  onClose: () => void
  onPickDirectory: () => Promise<string | undefined>
  onConnectExisting: (path: string) => Promise<void>
  onCreate: (input: { parentPath: string; folderName: string; teamRemote?: string }) => Promise<void>
}

export function WorkspaceDialog({ onClose, onPickDirectory, onConnectExisting, onCreate }: Props) {
  const { t } = useLanguage()
  const [parentPath, setParentPath] = useState('')
  const [folderName, setFolderName] = useState('deploom-workspace')
  const [teamRemote, setTeamRemote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  const chooseParent = async () => {
    setError(undefined)
    try {
      const path = await onPickDirectory()
      if (path) setParentPath(path)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }

  const connectExisting = async () => {
    if (busy) return
    setError(undefined)
    try {
      const path = await onPickDirectory()
      if (!path) return
      setBusy(true)
      await onConnectExisting(path)
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }

  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !parentPath.trim() || !folderName.trim()) return
    setBusy(true)
    setError(undefined)
    try {
      await onCreate({
        parentPath: parentPath.trim(),
        folderName: folderName.trim(),
        teamRemote: teamRemote.trim() || undefined,
      })
      onClose()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose() }}>
      <section className="dialog workspace-dialog" role="dialog" aria-modal="true" aria-labelledby="workspace-dialog-title">
        <div className="dialog-title">
          <div>
            <h2 id="workspace-dialog-title">{t('workspaceDialog.title')}</h2>
            <p>{t('workspaceDialog.description')}</p>
          </div>
          <button type="button" className="icon-button" disabled={busy} aria-label={t('common.close')} onClick={onClose}><X size={17} /></button>
        </div>

        <div className="workspace-connect-card">
          <div><FolderGit2 size={19} /><div><strong>{t('workspaceDialog.connect.title')}</strong><span>{t('workspaceDialog.connect.description')}</span></div></div>
          <button type="button" className="button secondary" disabled={busy} onClick={() => void connectExisting()}>{t('workspaceDialog.connect.choose')}</button>
        </div>

        <div className="setup-divider"><span>{t('workspaceDialog.orCreate')}</span></div>

        <form className="workspace-create-form" onSubmit={(event) => void create(event)}>
          <label>{t('workspaceDialog.parentFolder')}<div className="input-action"><input value={parentPath} onChange={(event) => setParentPath(event.target.value)} /><button type="button" className="icon-button" disabled={busy} aria-label={t('workspaceDialog.parentFolder')} onClick={() => void chooseParent()}><FolderGit2 size={17} /></button></div></label>
          <label>{t('workspaceDialog.folderName')}<input value={folderName} onChange={(event) => setFolderName(event.target.value)} /></label>
          <label>{t('workspaceDialog.teamRemote')} <span className="optional">{t('common.optional')}</span><input value={teamRemote} onChange={(event) => setTeamRemote(event.target.value)} placeholder="git@example.invalid/team/dependency-roadmap.git" /></label>
          <div className="workspace-registry-note">{t('workspaceDialog.registryNote')}</div>
          {error ? <div className="dialog-error" role="alert">{error}</div> : null}
          <div className="dialog-actions">
            <button type="button" className="button secondary" disabled={busy} onClick={onClose}>{t('common.cancel')}</button>
            <button type="submit" className="button primary" disabled={busy || !parentPath.trim() || !folderName.trim()}><GitFork size={15} /> {busy ? t('workspaceDialog.creating') : t('workspaceDialog.create')}</button>
          </div>
        </form>
      </section>
    </div>
  )
}
'''

EN_KEYS = '''  "projectRail.remove.confirm": "Remove {name} from this workspace? The project folder, Git repository, and saved artifacts on disk will not be deleted.",
  "projectRail.remove.title": "Remove project from workspace",
  "projectRail.remove.aria": "Remove {name}",
  "projectRail.workspaceAction": "Workspace…",
  "workspaceDialog.title": "Add workspace",
  "workspaceDialog.description": "A workspace owns shared registry and policy configuration. Projects with different registry policies are best kept in separate workspaces.",
  "workspaceDialog.connect.title": "Connect existing",
  "workspaceDialog.connect.description": "Choose an existing Git workspace.",
  "workspaceDialog.connect.choose": "Choose folder",
  "workspaceDialog.orCreate": "or create a new workspace",
  "workspaceDialog.parentFolder": "Parent folder",
  "workspaceDialog.folderName": "Workspace folder name",
  "workspaceDialog.teamRemote": "Team remote",
  "workspaceDialog.registryNote": "A new workspace is created without a registry override: the public npm registry is used by default. Nexus can be configured later in settings.project.json.",
  "workspaceDialog.create": "Create workspace",
  "workspaceDialog.creating": "Creating…",
'''

RU_KEYS = '''  "projectRail.remove.confirm": "Удалить {name} из этого workspace? Папка проекта, Git-репозиторий и сохранённые артефакты на диске не удаляются.",
  "projectRail.remove.title": "Удалить проект из workspace",
  "projectRail.remove.aria": "Удалить {name}",
  "projectRail.workspaceAction": "Workspace…",
  "workspaceDialog.title": "Добавить workspace",
  "workspaceDialog.description": "Workspace хранит общую registry/policy конфигурацию. Проекты с разной registry-политикой удобно держать в разных workspace.",
  "workspaceDialog.connect.title": "Подключить существующий",
  "workspaceDialog.connect.description": "Выберите уже созданный Git workspace.",
  "workspaceDialog.connect.choose": "Выбрать папку",
  "workspaceDialog.orCreate": "или создать новый workspace",
  "workspaceDialog.parentFolder": "Родительская папка",
  "workspaceDialog.folderName": "Имя папки workspace",
  "workspaceDialog.teamRemote": "Team remote",
  "workspaceDialog.registryNote": "Новый workspace создаётся без registry override: по умолчанию используется публичный npm registry. Nexus можно задать позже в settings.project.json.",
  "workspaceDialog.create": "Создать workspace",
  "workspaceDialog.creating": "Создаю…",
'''

def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)

def add_locale_keys(text: str, keys: str, *, anchor: str, label: str) -> str:
    if '"workspaceDialog.title":' in text:
        return text
    return replace_once(text, anchor, anchor + keys, label)

def patch_i18n_check(text: str) -> str:
    if "'src/components/WorkspaceDialog.tsx'," not in text:
        text = replace_once(
            text,
            "  'src/components/SetupScreen.tsx',\n",
            "  'src/components/SetupScreen.tsx',\n  'src/components/WorkspaceDialog.tsx',\n",
            "i18n-workspace-dialog-contract",
        )
    return text

def patch_ui_lifecycle_check(text: str) -> str:
    old = "  ['WorkspaceDialog', workspace, ['Подключить существующий', 'Создать workspace', 'dialog-actions']],\n"
    new = "  ['WorkspaceDialog', workspace, [\"t('workspaceDialog.connect.title')\", \"t('workspaceDialog.create')\", 'dialog-actions']],\n"
    if old in text:
        return replace_once(text, old, new, "ui-lifecycle-workspace-sentinels")
    if "t('workspaceDialog.connect.title')" in text:
        return text
    raise RuntimeError("ui-lifecycle-workspace-sentinels: current checker shape is unexpected")

def patch_interaction_check(text: str) -> str:
    old = "  ['WorkspaceDialog', workspace, ['dialog-actions', 'Отмена', 'Создать workspace', 'Подключить существующий']],\n"
    new = "  ['WorkspaceDialog', workspace, ['dialog-actions', \"t('common.cancel')\", \"t('workspaceDialog.create')\", \"t('workspaceDialog.connect.title')\"]],\n"
    if old in text:
        return replace_once(text, old, new, "interaction-workspace-sentinels")
    if "t('workspaceDialog.create')" in text:
        return text
    raise RuntimeError("interaction-workspace-sentinels: current checker shape is unexpected")

def write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".i18n-fix.tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    tmp.replace(path)

def main() -> None:
    parser = argparse.ArgumentParser(description="Fix i18n regression in DepLoom 0.2.58 lifecycle polish")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    version_path = repo / "VERSION"
    if not version_path.is_file():
        raise RuntimeError("VERSION missing")
    version = version_path.read_text(encoding="utf-8-sig").strip()
    if version != EXPECTED_VERSION:
        raise RuntimeError(f"fresh-source guard failed: expected VERSION {EXPECTED_VERSION}, got {version!r}")

    project_rail = repo / "desktop/src/components/ProjectRail.tsx"
    workspace_dialog = repo / "desktop/src/components/WorkspaceDialog.tsx"
    en = repo / "desktop/src/i18n/locales/en.ts"
    ru = repo / "desktop/src/i18n/locales/ru.ts"
    i18n_check = repo / "desktop/scripts/check-i18n.mjs"
    ui_check = repo / "desktop/scripts/check-ui-lifecycle.mjs"
    interaction_check = repo / "desktop/scripts/check-interaction-contracts.mjs"

    for path in (project_rail, workspace_dialog, en, ru, i18n_check, ui_check, interaction_check):
        if not path.is_file():
            raise RuntimeError(f"required file missing: {path}")

    current_rail = project_rail.read_text(encoding="utf-8")
    current_workspace = workspace_dialog.read_text(encoding="utf-8")
    if "project-row-remove" not in current_rail:
        raise RuntimeError("ProjectRail does not look like the lifecycle-polish version")
    if "workspace-registry-note" not in current_workspace:
        raise RuntimeError("WorkspaceDialog does not look like the lifecycle-polish version")

    en_text = add_locale_keys(
        en.read_text(encoding="utf-8"),
        EN_KEYS,
        anchor='  "projectRail.otherWorkspace": "Another workspace",\n',
        label="en-locale-keys",
    )
    ru_text = add_locale_keys(
        ru.read_text(encoding="utf-8"),
        RU_KEYS,
        anchor='  "projectRail.otherWorkspace": "Другой workspace",\n',
        label="ru-locale-keys",
    )

    updates = {
        project_rail: PROJECT_RAIL,
        workspace_dialog: WORKSPACE_DIALOG,
        en: en_text,
        ru: ru_text,
        i18n_check: patch_i18n_check(i18n_check.read_text(encoding="utf-8")),
        ui_check: patch_ui_lifecycle_check(ui_check.read_text(encoding="utf-8")),
        interaction_check: patch_interaction_check(interaction_check.read_text(encoding="utf-8")),
    }

    cyrillic = re.compile(r"[А-Яа-яЁё]")
    for path in (project_rail, workspace_dialog):
        if cyrillic.search(updates[path]):
            raise RuntimeError(f"postcondition failed: hard-coded Cyrillic remains in {path.name}")

    en_keys = set(re.findall(r'^\s*"([^"]+)":', en_text, flags=re.M))
    ru_keys = set(re.findall(r'^\s*"([^"]+)":', ru_text, flags=re.M))
    if en_keys != ru_keys:
        missing_ru = sorted(en_keys - ru_keys)
        extra_ru = sorted(ru_keys - en_keys)
        raise RuntimeError(f"locale key mismatch after patch: missingRu={missing_ru} extraRu={extra_ru}")

    changed = []
    for path, content in updates.items():
        if path.read_text(encoding="utf-8") != content:
            changed.append(path.relative_to(repo).as_posix())

    if args.dry_run:
        print("DEPLOOM_0_2_58_UI_I18N_FIX_DRY_RUN_PASS")
        print("changed=" + ",".join(changed))
        return

    for path, content in updates.items():
        if path.read_text(encoding="utf-8") != content:
            write_atomic(path, content)

    print("DEPLOOM_0_2_58_UI_I18N_FIX_APPLIED")
    print("changed=" + ",".join(changed))
    print("contract=lifecycle UI uses typed locale dictionaries; WorkspaceDialog is covered by check:i18n")

if __name__ == "__main__":
    main()
