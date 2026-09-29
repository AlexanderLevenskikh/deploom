# Итеративная миграция DepLoom — фактический acceptance (2026-09-29)

Этот файл фиксирует **фактически прогнанные** проверки итеративной миграции:
команды, результаты, ограничения. Он не заменяет живой журнал
`docs/ITERATIVE_MIGRATION_IMPLEMENTATION_STATUS.md`, а фиксирует доказательства
«работает на реальных границах».

## Python-ядро: накопительный цикл C0→C1→C2, drain, restart

Файл: `tests/acceptance/test_iterative_migration_physical.py`.
Сценарий `test_vertical_loop_first_upgrade_repair_and_recovery` — реальные
git/npm/check.js, без подмены npm:

1. `begin` — C0: исходный замер (source snapshot, manifest/lock hashes, control
   verify, classification RED).
2. `plan-next` — выбор когорты (order-agnostic: is-number или is-finite).
3. `materialize` — физический `npm install` точных версий в isolated trial,
   offline из сеянного npm-кэша (`npm_config_offline=true`).
4. `precheck` — диагностический прогон check.js в trial → RED.
5. Scripted repair `src/config.js` под фактический assignment кандидата
   (regex-замена, не «v1→…»).
6. `verify-exact` — authoritative `verify_assignment` на repaired bytes → C1.
7. Вторая когорта: первый repair «wrong» → attempt 0 → verify-exact не прошёл →
   вторая попытка repair → C2.
8. Недостижимая `is-string@99.99.99` → `materialize.failed` (kind=resolver) →
   INCONCLUSIVE — без превращения в false incompatibility.
9. Финальный `plan-next` → NO_ACTIONABLE (не POLICY_SATISFIED): отложенное
   остаётся в знаменателе health.
10. `finish` — MIGRATION_REPORT.md + DEVELOPER_UPGRADE_GUIDE.md.

Каждый шаг запускается отдельным subprocess (restart boundary между шагами).
Время ~27s. Результат: PASS.

Границы, выловленные этим тестом (починены до зелёного):
- read-only snapshot-контейнеры → после materialize снимается write-protection
  в trial;
- `run_budget_ok` парсил UTC-string как local time → deadline улетал в прошлое
  (фикс: `datetime.strptime(...).replace(tzinfo=timezone.utc).timestamp()`);
- снапшоты активного checkpoint пишутся в `run_dir/sources/<checkpointId>`; C0 —
  в `run_dir/sources/C0`;
- verify-exact RED без поля `tail` → KeyError (fixture `BaselineProjectFailure`
  теперь несёт `output`);
- `satisfied` требует точного совпадения target в incumbent — иначе NO_ACTIONABLE.

## Regression-набор (Python)

- `tests/regression/test_iterative_migration_schemas.py` — 19 тестов: контракт
  run/checkpoint/ledger/candidate/feedback, схема, ожидания.
- `tests/regression/test_explicit_planner_deferral_export.py` — 4 теста:
  явный PEER_RESOLUTION_DEFERRED принимается в export без ROADMAP_TARGET_DESYNC;
  strict policy с deferral → корректный partial-plan (отложенный остаётся в
  остатке, валидатор не блокирует).
- `tests/regression/test_iterative_task_export.py` — 8 тестов: артефакт ТЗ
  (RU/EN + общий манифест, deferred неизменяем и в остатке, policySatisfied
  =false), недостаточный durable JSON → `TASK_INPUT_INSUFFICIENT` без запуска
  Baseline, identity-drift → stale, атомарная публикация + supersede при новом
  checkpoint, **kill/restart**: потеря указателя → идемпотентный повторный
  экспорт той же папки артефакта; частичный набор → свежий полный набор.

Полный прогон `python run_tool_tests.py --suite all`: **1446 OK (4 skipped)**
(fresh run после всех правок). `--suite production-fast`: **64 OK**.

## Desktop: потребитель артефакта, IPC, clipboard, UI

Check-скрипты (контрактные, без Electron и с реальным Electron):

- `check:iterative-migration` — запускает РЕАЛЬНЫЙ `iterative_migration.py
  export-task` на минимальном durable run dir и читает артефакт обратно через
  TypeScript-модуль (`desktop/electron/iterative-migration.ts`):
  точные версии, deferred с reason, completeness{policySatisfied=false,
  denominator, remaining}, per-language content-hash; stale при drift policy;
  диагноз недостающих полей. Результат: OK.
- `check:task-clipboard` — DI-запись в clipboard с read-back-проверкой на
  fake-писателях: ok / tampered / device-failure / no-language / oversized.
  Результат: OK.
- `check:electron-clipboard` — **реальный Electron** (`npx electron` v43.2.0),
  write→read-back round-trip синтетического многострочного кириллического
  маркера; exit 0 при byte-identical read-back. Результат: OK (321 bytes).
  В headless-окружении (Linux без DISPLAY) честно печатает SKIP с причиной.
- Всё связано в npm (`check:*` + `precheck:` компиляция electron TS) и в CI
  (`.github/workflows/ci.yml`, инвариант `test_desktop_check_inventory.py`
  зелёный: каждый check-скрипт обязан быть и в npm, и в CI).

Desktop-сборка: `npm run build` OK; `npm run lint` 0 errors.
Затронутые контрактные чеки зелёные: flow-state, ui-shell, interaction-contracts,
i18n, process-launcher, run-slot-isolation, ui-lifecycle, dashboard-state,
baseline-intent, iterative-migration, task-clipboard, electron-clipboard,
desktop-check-inventory.

## Ограничения и открытое

- Причина исходного сбоя clipboard-кнопки пользователя в «Draft» (web Clipboard
  API в renderer) НЕ воспроизведена нами: новый путь копирования ТЗ идёт через
  typed IPC + реальный Electron clipboard с read-back-подтверждением. Для
  dashboard-пути сохранён web Clipboard API + ручной fallback
  (PromptPreviewDialog не трогали по этой задаче) — если сбой воспроизведётся,
  перевести и его на confirmed-запись.
- Полноценный «агент исполняет ТЗ и присылает репорт» — следующий этап:
  coordinator (Desktop) получил чтение/экспорт/отправку артефакта, но
  запуск repair-агента по артефакту не соединял (это отдельный пункт
  follow-up, surface уже есть в `repair-dispatch.ts`).
- Реальную миграцию пользовательского проекта (не фикстуру) не гоняли.
