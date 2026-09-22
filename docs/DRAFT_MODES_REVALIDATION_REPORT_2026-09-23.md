# DepLoom — Раздел 6: сдача ревалидации Draft-режимов (R1–R10)

Дата: 2026-09-23. Базовый документ: `docs/DRAFT_MODES_REVALIDATION_AND_FOLLOWUP_2026-09-22.md`.
Раздел 6 исходного задания (первый документ): `docs/DRAFT_MODES_AUDIT_AND_AGENT_TASK_2026-09-22.md`.

Изменения внесены в `dependency_live_roadmap_generator.py`, `desktop/electron/main.ts`,
`desktop/electron/preload.cts`, `desktop/src/{App.tsx,App.css,types.ts}`, `desktop/src/components/{FlowWorkspace.tsx,BaselineIntentDialog.tsx}`,
`desktop/src/hooks/useDependencyFlow.ts` и тесты. Ничего не удалялось из unrelated-кода; guards verified FLOW сохранены.

## 1. Таблица R1–R10

| Дефект | Вердикт | Конкретное доказательство |
| --- | --- | --- |
| R1 · Draft финализируется через legacy-выводы, теряет partial на быстром workspace | **Исправлено** | Отдельная финализация через run-scoped `artifacts/runs/<runId>/draft/*`; request `mock`-сценария: `tests/test_draft_baseline_contract.py` (`run-scoped`, `full-chain`) и `tests/test_draft_flow_integration_physical.py::test_fresh_workspace_fast_draft_publishes_partial_without_legacy_outputs` — fresh workspace + deadline 0.05 s → `DRAFT_PARTIAL`, все 4 артефакта есть, legacy `dependency-roadmap.{md,json,html}` отсутствуют, EXIT=0. Missing/битый manifest в Desktop — typed FAILED (`DRAFT_RESULT_MISSING`, `DRAFT_RUN_ID_MISSING` в `main.ts`). |
| R2 · Полный локальный inventory до сети отсутствовал | **Исправлено** | `_draft_local_inventory_rows()` собирается до deadline-чувствительной работы; offline/быстрый прогон даёт полный список (`metadata.total=2`, `unknown=2`, `unknownPackages=['uuid','is-number']`) с честными unknown (тесты `test_draft_row_without_registry_metadata_is_unknown...`, `test_generator_tolerates_offline_registry_in_draft_mode`, интеграционный fast-draft). |
| R3 · Ложные no-vuln / «100%» на неизвестных данных | **Исправлено** | `compute_project_health` считает vuln-unknown строки в `unknown`; prompt: «Lag OK: 0/1 известных, 2 без lag-данных (покрытие 0%), …, уязвимости неизвестны для 2 пакет(ов)» (пример ниже). В shared-подсистемах ничего не показано как «0 уязвимостей» как факт. |
| R4 · Размытая run identity, отсутствие persistent result и авт-открытия prompt | **Исправлено** | Строгая явная передача `{workspaceId?, projectName?, runId?}` в `flow:get-current-draft-result`; `draftInputStaleness` (сравнение mtime settings/package.json/lockfiles с `generatedAt`); карточка «Последний Draft» персистентна, ack по `workspaceId::projectName`; авто-открытие prompt только при совпадении `draftLaunch.runId === draftResult.runId`. |
| R5 · Draft перезаписывал shared verified-выводы | **Исправлено** | Draft публикует только run-scoped артефакты; legacy MD/JSON/HTML не пишутся (интеграционный тест, строка «no legacy output»); runtime-проверка: предзасеянные verified-файлы (`dependency-roadmap.md/json/html`, `state/verified-proof.json`) байт-идентичны после холодного/warm/offline Draft (ниже, п. 6). |
| R6 · Разные корни артефактов writer/reader | **Исправлено** | Единый пакет `artifacts_dir_for_draft()`/`draftManifestPath()`; читатель разрешает тот же `<ws>/.dependency-roadmap/artifacts`; source-contract: одинаковые строки writer/reader (`tests/test_draft_baseline_contract.py::test_desktop_draft_writer_and_reader_resolve_the_same_artifact_root`). |
| R7 · Менеджеры: manifest-only / pnpm / Yarn Berry | **Исправлено** | `test_draft_flow_integration_physical.py::test_pnpm_manifest_only_draft_is_available_and_marks_unsupported_manager` — pnpm-only (lockfileVersion 9.0) → `DRAFT_READY`, EXIT=0, исходники не изменены, `PACKAGE_MANAGER_PNPM_UNSUPPORTED` в stderr как ограничение следующего Verified шага, а не запрет плана. |
| R8 · Нет состояний процесса и прогресса | **Исправлено** | `LiveDataClient.progress()` + `_draft_progress()`: `[draft-progress] {runId,project,step,inventory|scan|solve|finalize,package,operation,retry,completed,total,status}` на каждый fetch/retry/пакет/этап; `useDependencyFlow.onJobOutput` парсит в `DraftProgressPayload`; `DraftLiveProgress` в карточке (элапсд-тикер 1 s, heartbeat live/stale 8 s, текущие пакет/операция/retry, завершено/итого). E2E подтверждён на реальном прогоне (`run-e2e-progress`: inventory 1/1 → scan uuid → registry retry → finalize DRAFT_PARTIAL). |
| R9 · «Три режима» и численные критерии цели | **Исправлено (частично: одно продуктовое эффективное правило, см. ограничения)** | Единая versioned policy включает `targetLevel` и `minLagOkPct` и проходит UI→planner→prompt→manifest/hash: `BaselineIntentDialog` получил «Цель запуска» Yellow/Green и «Минимум актуальности» 0–100 % (default 80, участвует в dirty-fingerprint и buildIntent); main.ts передаёт `DEPLOOM_BASELINE_TARGET_LEVEL/MIN_LAG_OK_PCT`; planner: `health_yellow_ratio()/health_green_ratio()/health_planning_ratio()` на основе эффективных значений (желтый = pct, зелёный = pct+10, планирование = pct+5; legacy 80/90/85 сохраняются как default), порог статуса `< minLagOkPct` вместо `<80`, `green_*`-проекции в health; prompt содержит численные цели вместо голого hash. Проба: 80→90 меняет `yellow_required` 8→9, `green_required` 9→10, status red reason «<90%», hash 80≠90≠90+green; e2e `run-r9-p90` (green/90): policyHash `60a1ad9a…` совпадает с probe, prompt «Цель запуска: уровень `green`, минимум актуальности `90%`», manifest `settings` содержит оба поля. Acceptance/audit получают это через manifest `settings` + `policyHash`; partial никогда не равносилен достижению цели (тест `test_r9_green_closure_is_honest_not_partial_as_success`). Старый source-contract тест, запрещавший freshness-контроль, обновлён (`tests/test_block_psi53_search_depth_ux.py` теперь требует контроль и его участие в dirty/hash). |
| R10 · Покрытие не подтверждает пользовательский результат | **Исправлено** | Добавлены полнокровные интеграционные сценарии `tests/test_draft_flow_integration_physical.py` (реальные generator-процессы, 4 сценария: partial на чистом workspace без legacy, изменение policy 80→90 (hash/gate/prompt), pnpm manifest-only, реальный `npm test` smoke) и регрессионные тесты R1–R9 в `test_draft_baseline_contract.py`. Итоги: unit 853 OK, интеграционные 4 OK, `npm test` на фикстуре с scripts → `SMOKE_test_OK` (записан отдельно от mock, п. 2). |

## 2. Результаты интегрированных тестов

- `run_tool_tests.py` (unit+regression+acceptance default): **853 tests OK** (4 skipped).
- `tests/test_draft_baseline_contract.py`: **19 OK** (включая R9-тесты и полный контракт артефактов).
- `tests/test_draft_flow_integration_physical.py`: **4 OK** — реальные запуски `dependency_live_roadmap_generator.py` (Python 3.14, .venv) на свежесоздаваемых фикстурах.
- Desktop: `npx tsc -p tsconfig.electron.json` E=0, `npx tsc -b` R=0, `npm run lint` 0 errors, `check:baseline-intent`, `check:ui-lifecycle`, `check:human-flow` OK, `npm run build` OK (vite, 1805 modules).
- Реальный проект с scripts: `npm test` в фикстуре (`"test": "node build.js test"`) → exit 0, вывод `SMOKE_test_OK` — записан как реальный smoke, отдельно от mock-тестов. `registry.npmjs.org` в среде недоступен, поэтому полный `npm install` не выполнен (см. п. 8).

## 3. Сценарий partial на чистом workspace (эквивалент Desktop)

Desktop UI (Electron backend) в этой среде не запускался; частичная выдача доказана на полной цепочке генератора тем же аргументом, который использует `main.ts` (`--draft-baseline --mode draft --run-id … --artifacts-dir …`):

- Fresh workspace, deadline 0.05 s → `DRAFT_PARTIAL`, EXIT=0, все 4 артефакта в `runs/<runId>/draft`, legacy-выводов нет, manifest: `verificationStatus=NOT_VERIFIED`, `authority=PLANNING_ONLY`, `compatibility=UNKNOWN`, `metadata.total=2, unknown=2`.
- Fast-прогон на фикстуре `draft-e2e-r2` (1 dep): `DRAFT_PARTIAL`, `unknownPackages:['uuid']`, EXIT=0.
- Manifest отсутствует/битый в Desktop → typed `DRAFT_RESULT_MISSING`/`DRAFT_RUN_ID_MISSING` (source-contract тест), без «готово» и без успеха.

## 4. Cold / warm / offline timings (реальные замеры)

Фикстура: 2 зависимости (uuid, is-number), lockfile + settings; registry недоступен (DNS fail). Полная цепочка процесса, deadline 60 s / 0.05 s:

| Прогон | Exit | Status | elapsedMs (manifest) | wall (сек) |
| --- | --- | --- | --- | --- |
| Cold | 0 | DRAFT_READY | 1670 | 2.14 |
| Warm (тот же workspace, повтор) | 0 | DRAFT_READY | 1611 | 2.05 |
| Offline fast (deadline 0.05 s) | 0 | DRAFT_PARTIAL | 66 | 0.52 |

Пояснение: cold≈warm, потому что доминирует время ожидания недоступного registry (retry); deadline-контракт работает — offline fast завершился за 0.5 s без потери результата.

## 5. Пример prompt / manifest

`prompt.md` (run-cold, infra-умолчания):

```
# IMPORTANT — DRAFT BASELINE / PLANNING ONLY
...
runId: `run-cold`
workspaceId: `ws-ev`
projectId: `ev`
mode: `draft`
policyHash: `fb442736ac65f46dde76c5e251b3d328dde78d15f1546de23ce5433e9e714580`
Цель запуска: уровень `yellow`, минимум актуальности `80%` библиотек по lag-policy.
Draft не доказывает достижение цели: после применения требуется повторная verified acceptance по этой же политике.

# ev — Draft план
- Lag OK: 0/1 известных, 2 без lag-данных (покрытие 0%), C/H/M/L: 0/0/0/0, уязвимости неизвестны для 2 пакет(ов).
- Причина статуса: lag-policy target неизвестен для 2 зависимостей.
```

`result.json` (фрагмент):

```json
{
  "schemaVersion": 1, "status": "DRAFT_READY", "runId": "run-cold",
  "workspaceId": "ws-ev", "projectId": "ev", "mode": "draft",
  "policyHash": "fb442736ac65f46dde76c5e251b3d328dde78d15f1546de23ce5433e9e714580",
  "settings": { "mode": "draft", "draftDeadlineSeconds": "60.0",
                "targetLevel": "yellow", "minLagOkPct": "80" },
  "verificationStatus": "NOT_VERIFIED", "authority": "PLANNING_ONLY", "compatibility": "UNKNOWN",
  "metadata": { "total": 2, "unknown": 2, "unknownPackages": ["is-number", "uuid"] },
  "artifacts": { "manifest": ".../runs/run-cold/draft/result.json", ... },
  "hashes": { "plan": "...", "prompt": "..." }
}
```

Прогон R9 (green/90): policyHash `60a1ad9a95c2ef2a65968b8023f86e54c7986135b07a37beee4a78e735f90290`,
prompt: `Цель запуска: уровень `green`, минимум актуальности `90%` библиотек по lag-policy.`

## 6. Сохранность Verified до/после Draft

На чистом workspace предзасеены «ранее проверенные» артефакты (`dependency-roadmap.md/json/html`, `.dependency-roadmap/state/verified-proof.json`). После холодного, warm и offline-fast Draft:
- SHA-256 предзасеянных файлов **без изменений** (`VERIFIED_PRESERVED=True`);
- новые файлы — только run-scoped `artifacts/runs/<runId>/draft/*` и штатные служебные (`state/dashboard-state.json`, `.dependency-update-history/*`); legacy-отчёты не созданы и не тронуты.

## 7. UI screenshots

- `.playwright-mcp/flow-after-tab-return.png` — живой UI FLOW↔Graph после переключения вкладок (0 console errors/warnings, vite preview, demo).
- `.playwright-mcp/r10-flow-live.png` — текущее состояние FLOW-вкладки после изменений.
- Диалог «Состав Baseline» с новым контролом «Цель запуска»/«Минимум актуальности» гарантируется source-contract тестами (`test_block_psi53_search_depth_ux.py`, `test_draft_baseline_contract.py::test_r9_numeric_target_policy_changes_health_gate_and_hash`); в demo-данных этот диалог не моделируется, поэтому скриншот диалога не получен (см. п. 8 — demo UI не тест Electron backend).

## 8. Непроверенные сценарии и ограничения

- **Electron backend не запускался**: полный UI-сценарий (Desktop partial, авто-открытие prompt, живой прогресс в реальном приложении) проверен на уровне цепочки generator→артефакты и source/tool-контрактов; demo UI не является тестом Electron backend (как требует раздел 6).
- **Verified-путь офлайн недоступен**: `--capture-baseline`/generate жёстко требуют registry (`REGISTRY_METADATA_PREFETCH_FAILED`, exit 4). Поэтому «Verified до/после» доказан предзасеянными файлами, а не реальным Verified-прогоном. Fast/Deep VERIFIED (первый кандидат, timeout, deep fallback) не прогонялись — требует сети.
- **Три продуктовых режима**: реализовано одно эффективное продуктовое правило (Draft как отдельный bounded путь + Verified как единый путь), `--mode draft/fast/deep` различаются в manifest; «быстрый останавливается по своему контракту» и отдельные fast/deep acceptance офлайн не подтверждены (см. графу R9 в таблице: вердикт «частично» — режимы и численные критерии изменяют план/приёмку/hash, но полная матрица fast/deep Verified остаётся за сетью).
- **`npm install` не выполнялся** (registry недоступен); `npm test` на фикстуре с `node build.js` — реальный записанный smoke.
- Границы округления (ceil), High 0/1/2, unknown, scope, M/L, устаревший audit проверены юнит-тестами; «нет лага» как значение и «единичные» как конкретизация остаются в настройках/health (значение «нет лага» = lag_ok_12m/total, «единичные» = M+L≤20 при green-статусе).
