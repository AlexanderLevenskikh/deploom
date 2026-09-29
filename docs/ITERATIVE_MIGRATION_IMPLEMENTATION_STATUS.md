# Итеративная миграция DepLoom — статус реализации

Дата начала: 2026-09-29. План: `docs/ITERATIVE_MIGRATION_PLAN_2026-09-29.md`.
Исследованный для плана HEAD: `ca67872` (v0.2.149). Задание: `docs/ITERATIVE_MIGRATION_AGENT_TASK_2026-09-29.md`.

Этот файл — живой журнал реализации: этап, commit, реализованное поведение, проверки, открытые пункты, следующий шаг.

## Решения по архитектуре (рекорд, не обещание)

- Python остаётся владельцем детерминированных planner/materializer/verifier и durable-состояния. Новый модуль `iterative_migration.py` выделяет primitives из существующего generator/verifier, а не копирует solve-цикл.
- Новый execution path — отдельные bounded шаги поверх одного durable run directory; Desktop-координатор решает, когда вызывать шаг, и запускает repair-агента.
- Проверенные primitives для переиспользования: `plan_progressive_extension` (plan-next), `verify_assignment` (authoritative materialize+verify), `materialize_private_tree` / `source_snapshot` durable containers (isolated trial/checkpoint bytes), `_apply_assignment` (exact manifest application), `build_repair_request` (diagnostics handoff), `manual_dependency_audit.build_report` (audit).
- Trial агента — физически материализованный isolated workspace с точными версиями (install с exact spec), независим от verifier trial. Acceptance authority — всегда `verify_assignment` на repaired bytes (verify-exact), никакой «слова агента» или stdout-хвоста.
- C0 — «Исходный замер»: durable source snapshot + manifest/lock hashes + контрольная проверка (control verify) + classification RED. Baseline остаётся названием исходного замера; процесс называется «Итеративная миграция».

## Этапы

(заполняется по мере реализации; см. commits ниже)

## Журнал

### 2026-09-29 — рекогносцировка и контракты (в работе)

Прочитаны ключевые модули: `block_psi_progressive_baseline.py`, `dependency_live_roadmap_generator.py` (main/CLI/Draft/baseline snapshot), `baseline_constraint_verifier.py` (verify_assignment, config, materialize/apply), `verification_proof.py`, `source_snapshot.py`, `resolved_dependency_state.py`, `baseline_repair_handoff.py`, `dependency_compatibility_evidence.py`, `block_v_recovery.py`, `block_psi_anytime.py`, `manual_dependency_audit.py`, `run_tool_tests.py`; Desktop: `main.ts`, `repair-dispatch.ts`, `repair-checkout.ts`, `baseline-repair-result.ts`, `materialization-proof.ts`, `resolved-state-proof.ts`, `migration-verification.ts`, `planner-session.ts`, `agent-session.ts`, `acceptance-policy.ts`, `preload.cts`, `desktop/src` (flow screen, i18n).

Ключевые находки (подробный inventory — в рекогносцировке агентов):
- `verify_assignment` = единственный authoritative verifier; trial disposable, source snapshot sealed.
- `plan_progressive_extension` = ровно тот «небольшой шаг от incumbent» из плана; `assignment_fingerprint` — exact identity.
- Repair handoff desktop уже читает `repair-requests.json`; typed feedback агента необходимо расширить (сейчас `repaired|partial|blocked`).
- `npm_config_cache` наследуется в install env (block_vex_storage.package_manager_cache_environment) → физический тест может быть офлайн через `npm cache add <tarball>` + `--offline`.
- Все `*.py` репозитория бандлятся в packaged tool (`electron-builder.yml` extraResources) — новый модуль попадёт в релиз автоматически.

Открытые пункты: (см. следующий шаг)

### 2026-09-29 — Python-координатор: накопительный цикл C0→C1→C2 + drain + restart — готово

- Определён и проверен физически полный вертикальный цикл: `begin` (C0 control verify) → `plan-next` → `materialize` (реальный `npm install` exact версий, offline из сеянного кэша) → `precheck` → scripted repair → `verify-exact` на repaired bytes → принятие C1 → вторая когорта wrong→right (attempt 0→1) → C2 → недостижимая версия → INCONCLUSIVE/NO_ACTIONABLE → `finish` с MIGRATION_REPORT и DEVELOPER_UPGRADE_GUIDE. Каждый шаг — отдельный subprocess (restart boundary).
- Ключевые границы, выловленные физическим тестом: снимки-контейнеры read-only → после materialize нужно снимать write-protection в trial; deadline в `run_budget_ok` трактовался как local time (фикс — UTC-парсинг); снапшоты активного checkpoint пишутся в `run_dir/sources/<checkpointId>`; verify-exact RED агрегирует `tail` из `BaselineProjectFailure.output`; `satisfied` = точное совпадение target в incumbent (деферренные/недостижимые → NO_ACTIONABLE, не POLICY_SATISFIED); отчёты строятся от parent checkpoints.
- Commits: `f979283` (feat: iterative migration Python coordinator, 8 файлов, 3921 вставка), `692bf8d` (fix: accept explicit PEER_RESOLUTION_DEFERRED в export дорожной карты; regression 4 теста).
- Проверки: `run_tool_tests.py --suite production-fast` OK; `--suite all` 1438 OK (4 skipped); физический acceptance `test_iterative_migration_physical.py` OK (~27s).
- Открытый пункт: eligibility и запуск repair-агента в Desktop-coordinator (репозиторий `repair-dispatch` уже умеет; в фоллоу-апе — соединение с task artifact'ом).

### 2026-09-29 — ТЗ-артефакт (task export) из durable state без Baseline — готово (follow-up, часть 1)

- Новый модуль `iterative_task.py`: RU/EN «Задание на адаптацию зависимостей» строится ТОЛЬКО из durable JSON (run.json/run-config.json/checkpoints/ledger); атомарная публикация `task/<artifactId>/{task.ru.md, task.en.md, task-manifest.json}` + указатель `current.json` (флипается последним, предыдущий набор сохраняется и помечается stale в history).
- Манифест: schemaVersion/builder/artifactId, идентичности (run/workspace/project/checkpoint), хэши (scope/policy/command-set/source snapshot/manifest/lockfile/resolved state), exact принятых версий, actions, deferred (явные отложенные с reason), completeness{policySatisfied, denominator, remaining}, verification/audit, per-language content-hash «связан» с телом.
- Контракт: отложенный пакет остаётся неизменяемым в знаменателе, lagPolicyTarget не подставляется в исполняемый target; policySatisfied=false. Недостаточный durable JSON → `TASK_INPUT_INSUFFICIENT` со списком полей, без запуска Baseline. `task_staleness` — идентичность (runId/checkpoint/policy) против живого состояния.
- CLI: `iterative_migration.py export-task [--language ru|en|both] [--check-only]`.
- Commits: `fa5bc3e` (feat: export task artifact..., 6 regression-тестов).
- Проверки: `--suite all` 1444 OK (4 skipped); regression `test_iterative_task_export.py` 6 OK.

### 2026-09-29 — Desktop: потребитель артефакта, панель «План обновления», typed IPC, verified clipboard — готово (follow-up, части 2–3)

- `desktop/electron/iterative-migration.ts` — строго потребитель: `currentTask`, `taskStaleness`, `missingTaskExportInput`, `taskCopyPayload` + лимит 512 КБ, `iterativeExportInvocation`; run dir контракт `<base>/.dependency-roadmap/iterative/<token>`.
- `desktop/electron/task-clipboard.ts` — DI-запись в clipboard с read-back проверкой: «Скопировано» только после подтверждения `clipboard.readText() ==` записанному; отказ при oversized/missing-language/stale.
- `main.ts`: `flow:iterative:task|export|copy-task|save-task` (python через `resolveExecutable`, бюджет 120s, `TASK_INPUT_INSUFFICIENT` без Baseline, stale-отказ копирования). Preload + `DependencyFlowApi` + `useDependencyFlow` + компонент `IterativeTaskPanel` (сумма принятых/отложенных/остатка, stale warning, список deferrals, действия Посмотреть/Скопировать/Сохранить/Переэкспортировать/Открыть папку, reload при смене run).
- Commits: `a48f50b` (feat: desktop consumer + check-iterative-migration.mjs с РЕАЛЬНЫМ python export→TS handoff), `86db6af` (feat: task panel, typed IPC, verified clipboard + check-task-clipboard.mjs).
- Проверки Desktop: `npm run build` OK, lint 0 errors; затронутые contract-checks зелёные (flow-state, ui-shell, interaction-contracts, i18n, process-launcher, run-slot-isolation, ui-lifecycle, dashboard-state, baseline-intent, iterative-migration, task-clipboard).

### Следующий шаг
1. Follow-up часть 4: acceptance — synthetic fixtures, kill/restart между checkpoint acceptance и записью ТЗ/между записью и UI-ack, реальный Electron clipboard на synthetic text в UI-тесте (запуск Electron в окружении сборки; mock writeText — только для контракта, реальный clipboard проверяется отдельно).
2. `docs/ITERATIVE_MIGRATION_ACCEPTANCE.md` — фактический прогон.
3. Финальный `git diff --check` + sanitization (`check-public-sanitization.py`) для нового `iterative_task.py` и дистрибутива.
4. Полный прогон: Python `--suite all` + `production-fast`, Desktop lint/build/`check:*`.
