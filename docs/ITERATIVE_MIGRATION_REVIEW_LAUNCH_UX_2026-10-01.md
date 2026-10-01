# Iterative migration launch-boundary UX: re-review round (2026-10-01)

Round after `v0.2.158`. Closes the two blocking remarks (Electron liveness, state
isolation on project switch) and the "one main scenario / one main button" UX
overhaul. **No release was performed in this round** — publication happens only
after the interactive acceptance below.

## Blockers closed

1. **P1 — liveness comes from Electron, not from a local React flag.**
   `flow:iterative:status` now returns `inFlight` = `iterativeStepInFlight.has(project.name)`
   (`desktop/electron/main.ts` status handler, `desktop/src/types.ts`). The panel
   computes `childAlive = stepBusy || runner?.inFlight === true`
   (`IterativeTaskPanel.tsx`). After FLOW → Graph → FLOW the component remounts,
   `status` is refreshed on mount and the running child is visible again: the
   activity strip shows Stop and never shows the fake "restored after restart"
   message. Behaviour pinned by `deriveMainAction` (in-flight → `running`/Stop).

2. **P2 — attempt/log/notes belong to one workspace+project.**
   The panel is mounted with `key={workspaceId:project}` (`FlowWorkspace.tsx`), so
   a project switch remounts it; an explicit reset effect clears attempt/log/
   note/noTargets on identity change; journal, interval and log readers guard
   every late response with `result.attempt.projectName === projectName` (the old
   in-flight `alive`-flag stays for unmounts). A stale answer cannot overwrite
   the new project's state.

## Requirement mapping

- **1. One main button** — a pure module `desktop/electron/iterative-scenario.ts`
  maps durable/Electron state to ONE `MainAction`. The panel renders exactly one
  primary button whose label/description follows the accepted table
  (Проверить проект / Повторить проверку / Начать обновление / Остановить /
  Продолжить обновление / Исправить агентом / Посмотреть результат). The old
  competing lower control row ("Начать миграцию (C0)" + "авто-поиск") is gone;
  internal steps live in an expandable diagnostics block only.
- **2. No internal terms in the main UI** — C0/begin/done/failed/roadmap targets/
  verified checkpoint/step names were removed from the visible states and moved
  to diagnostics. User states: «Подбираем версии», «Проверяем текущие
  зависимости», «Обновляем пакеты», «Нужны исправления», «Обновление
  приостановлено». No "готовы к следующему шагу" on blocked/errored states.
- **3. Start without saved targets** — an empty roadmap is a DECISION, not a
  "done": the panel offers «Подобрать обновления автоматически» /
  «Настроить обновление» and auto-continues after the choice. Backend
  distinguishes outcomes: `begin` now refuses to create a run when discovery
  returned ONLY `registry-unavailable`/`discovery-budget-skipped`
  (`DISCOVERY_UNSETTLED`, non-fixable-run, no state files to delete), so
  "updates not needed" vs "could not fetch data" vs "budget exhausted" cannot be
  confused with a completed migration. Counts are also carried in the begin
  outcome (`discovery` report).
- **4. Settings/Draft access before launch** — pre-run section links to
  «Настроить состав и политику / Draft» (existing scope dialog), states the
  Draft-vs-launch difference, and notes the Node version (unset by default) is
  configured in the project settings above. No mandatory Dashboard detour.
- **5. Prep blockers before the expensive search** — `project_readiness_preflight`
  (begin) shares the SAME code as the capture-time check
  (`source_snapshot.incomplete_submodules`); the capture-time check stays.
  A submodule blocker fails in ~1s before discovery/snapshot with
  `SOURCE_SUBMODULE_INCOMPLETE`, a user-facing summary and the exact command
  (`git submodule update --init --recursive -- "<path>"`), never executed
  automatically. Snapshot protection is not disabled and the incomplete checkout
  is not silently excluded.
- **6. Error once + a way forward** — a single error surface: short reason +
  next step; full technical reason in expandable diagnostics; «Скопировать
  диагностику» includes app version / stage / attempt id / technical reason.
  «Повторить проверку» re-runs begin; both preflight and unsettled-discovery
  stop BEFORE `run.json` is written, so a retry needs no manual state cleanup
  (asserted by tests).
- **7. Progress matches the stage** — separate activity line per stage
  (подготовка / подбор версий N/M / проверка текущих зависимостей / обновление
  пакетов), elapsed + Stop; no invented overall percentage.
- **8. Previous acceptance remarks** — see Blockers above.
- **9. Acceptance via user scenarios** — behavioral checks below; interactive UI
  verification and screenshots remain to be done (see checklist).

## What changed (files)

- `desktop/electron/iterative-scenario.ts` (new, pure): `deriveMainAction`,
  `parseIterativeFailure` (machine-readable failure envelope with `command`/`fixable`).
- `desktop/scripts/check-iterative-scenario.mjs` (new): 11 behavioral fixtures
  against the built module (fresh→check, no-targets→choice, in-flight→Stop,
  restored→resume, submodule→retry-check+command, agent→repair, satisfied→result,
  start/continue, envelope parsing).
- `desktop/electron/main.ts`: `inFlight` in status; `begin.discovery` report in
  the begin outcome; no-targets journal is a non-terminal `failed` record with an
  actionable reason.
- `desktop/src/components/IterativeTaskPanel.tsx`: single main action, user-facing
  states, no-targets choice, error-once + copy diagnostics, activity strip.
- `desktop/src/components/FlowWorkspace.tsx`, `App.tsx`: `key` remount by
  project, `workspaceId`/`appVersion` threading.
- `source_snapshot.py`: shared `incomplete_submodules` (preflight and capture),
  path parsed without the gitlink SHA.
- `iterative_migration.py`: `ProjectUnreadyError`, `project_readiness_preflight`,
  `DISCOVERY_UNSETTLED` guard, CLI envelope `command`/`fixable`.
- `types.ts`, `desktop/package.json`, `.github/workflows/ci.yml`: new outcome
  fields and `check:iterative-scenario` wiring.

## Test evidence

- New Python behavioral suite `tests/regression/test_iterative_preflight_readiness.py`
  (5): preflight raises with the command, shares the capture code, CLI fails fast
  (no `begin.capture`/discovery before the blocker), fixing the submodule makes a
  plain retry pass end-to-end, unsettled discovery creates no run/`SOURCE`.
- Electron scenario contract: `check:iterative-scenario` OK.
- Full Python `--suite all`: **1547 OK (4 skipped)**.
- Desktop: `tsc` (renderer + electron), `npm run build`, lint (15 warnings /
  0 errors — pre-existing baseline), `run-all-contract-checks` **63/63 passed**.
- `git diff --check` clean; `check-public-sanitization` OK (new files covered).

## Remaining for acceptance: interactive UI verification + screenshots

The behavioural backend/decision parts are covered by scripts; the visual Flow
panel and the real Electron→Python run need a human pass. Suggested checklist
(each with a screenshot of the main panel):

1. New project without roadmap: «Проверить проект» → the understandable choice
   (auto / настроить) — no "successful empty done".
2. Uninitialized submodule: clear error BEFORE discovery, command shown; fix →
   «Повторить проверку» works without manual cleanup.
3. Registry unavailable / budget exhausted: unsettled outcome is NOT a completed
   migration; retry offered.
4. Launch → Graph → FLOW: running work keeps Stop (no false "restored" message).
5. Project switch: no foreign status/logs; a late answer does not overwrite.
6. Stop + restart the app: correct restore without spending a new attempt.
7. Settings/Draft/copy-assignment reachable from FLOW pre-launch.

Release (via `push-github-branch-and-tag.ps1`) only after this pass.
