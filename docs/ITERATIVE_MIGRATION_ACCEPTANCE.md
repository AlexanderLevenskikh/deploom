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

- `tests/regression/test_iterative_migration_schemas.py` — 31 тестов: контракт
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

Полный прогон `python run_tool_tests.py --suite all` на HEAD повторной
приёмки (2026-09-30): **1510 OK (4 skipped)**. `--suite production-fast`:
**64 OK**.

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
- Полноценный «агент исполняет ТЗ и присылает репорт» теперь соединён в
  Desktop: `flow:iterative:agent` диспатчит выбранного провайдера (open code /
  claude / codex) с durable lease (возобновление той же сессии после убийства
  владельца), принимает репорт-объект ремонта и отвергает forbidden-мутации
  планировщика через baseline hash diff до вердикта. Реальный провайдерский
  бинарь на живом проекте в этом окружении не прогонялся (BLOCKED, см. ниже).
- Реальную миграцию пользовательского проекта (не фикстуру) не гоняли.

## Повторная приёмка v0.2.154 (2026-09-30): 7 блокеров закрыты

Повторная ревью v0.2.153 (HEAD `089c702`, см.
`docs/ITERATIVE_MIGRATION_REVIEW_V02153_2026-09-29.md`) выставила 7
блокирующих замечаний. Все закрыты сверху `089c702`:

| # | Блокер | Фикс |
| --- | --- | --- |
| 1 | Одношаговая ручная механика («выполнить следующий шаг») | Durable supervisor `flow:iterative:drive` (commit `aedf8d7`): один вызов гонит plan-next → materialize → precheck → verify-exact → следующая когорта до агент-гейта / finish / ошибки / бюджета (50 итераций, 45 мин); begin и успешный repair автоматически продолжают до следующего гейта/финиша; одношаговый `flow:iterative:step` удалён end-to-end. Restart-safe: решение пересчитывается из durable-состояния Python на каждой итерации. |
| 2 | Bounded target discovery при пустом roadmap | `_discover_targets` на begin: жёлтый lag-policy по прямым зависимостям из npm dist-tags (commit `5a24117`), версии не выдумываются, evidence пишется в событие. |
| 3 | Промежуточные сводки (лаг/health) | `progress.preview`-релей ревью-прогресса на каждую когорту по durable-состоянию (commit `eba6f98`). |
| 4 | Forbidden-мутации планировщика агентом | Full-baseline hash diff: `classifyTrialMutations`/`forbiddenTrialViolations`, removal-детекция, FORBIDDEN_MUTATION до feedback/verify (commit `2e9798b`); Python `_validate_changed_files` остаётся defense-in-depth. |
| 5 | После убийства владельца lease не возобновлялся | `decideAgentLeaseDispatch`: живой/мёртвый PID → in-progress/resume той же сессии (commit `93b2377`). |
| 6 | Планирование без учёта совместимости движка | Runtime pre-check `_npm_engines_node` + ENGINES_INCOMPATIBLE-деферрал до materialize (commit `6aec594`). |
| 7 | Финальные экраны/повторная проверка evidence | Phase/end-экраны в панели + переэкспорт при stale/missing + вся копируемость через Electron IPC (commit `eba6f98`). |

### Гейты, фактически прогнанные на этой ревизии

- Python: `tests/regression/test_iterative_*.py` — 68 OK; новые
  `test_iterative_engine_precheck.py` (6), `test_iterative_target_discovery.py`
  (4), `test_iterative_runtime_request.py` (14, повторный) — OK; физический
  acceptance `test_iterative_migration_physical.py` — PASS (~103s).
- Desktop: `tsc -p tsconfig.electron.json` OK; `npm run build` (vite) OK;
  `npm run lint` — 15 pre-existing warnings, 0 errors.
- Контракт-чеки OK: check:iterative-runner (включая новую секцию 10 —
  durable-supervisor), check:iterative-agent, check:iterative-begin,
  check:task-clipboard, check:i18n, check:ui-lifecycle.

### Честный BLOCKED (фиксируется, а не маскируется)

- **E12/E13**: реальный провайдерский бинарь (opencode/claude/codex) и
  согласованный изолированный частный проект для live-ремонта не
  предоставлены — live-агент на живом проекте не прогнан; покрытие — только
  синтетический scripted-агент и contract-чеки.
- **E9**: физический `npm install` под выбранным Node вне fake-фикстуры с
  реальным сетевым кэшем не гонялся (в физическом acceptance используется
  `npm_config_offline=true` + сеянный кэш).
- **CI по тегу**: наблюдение GitHub Actions из этого окружения недоступно
  (`gh` отсутствует) — CI-статус публикуемого тега фиксируется отдельно после
  релиза.

## Второй раунд ревью v0.2.154 (2026-09-30): P1.1/P1.2 → публикация

Параллельная ревизия перед публикацией (`docs/ITERATIVE_MIGRATION_REVIEW_V02154_2026-09-30.md`) выставила 2 приоритетных замечания. Оба закрыты одним коммитом `87ae4ac` до публикации:

| # | Замечание | Фикс |
| --- | --- | --- |
| P1.1 | Экспортированный task-артефакт блокировал coordinator: агент требовал ТЗ, экспорт недоступен при его отсутствии, кнопки панели disabled при stale | Task — человекочитаемый вид durable-состояния, гейтом не является. `refreshIterativeTaskArtifact()` пересобирает ТЗ из текущего durable-состояния перед диспатчем агента, на выходе в agent-gate и в copy-task; отклонение `TASK_STALE` убрано из repair-пути; «Продолжить»/«Исправить агентом» не блокируются staleness (уведомление информационное: «пересобирается автоматически»). Контракт закреплён в check-iterative-runner (секция 10). |
| P1.2 | Discovery не учитывал совместимость с движком: брался dist-tags.latest, deferral честным не был | Engine-aware bounded discovery: резолюция эффективной версии Node перенесена до discovery и передаётся в `_discover_targets(node_version)`; `_npm_versions` (bounded, top-12). При registry-известном несовместимом latest bounded-поиск выбирает наивысшую VERIFIED-compatible версию (`discovered-compatible` + evidence в событии); при отсутствии альтернативы latest остаётся целью, а #6 пре-чек деферралит честно (ENGINES_INCOMPATIBLE + reason); неизвестные engines — abstention (enginesUnknown), unpinned Node — engineUnchecked. Ложная совместимость и выдуманные версии исключены. 4 новых теста. |

Гейты на финальной ревизии `87ae4ac`: `test_iterative_target_discovery.py` 8 OK, `test_iterative_*.py` 76 OK, физический acceptance PASS (~50s), Desktop tsc (electron) + lint (15 pre-existing warnings, 0 errors) + vite build, check:iterative-runner / iterative-agent / iterative-begin / i18n OK.

**Публикация v0.2.154**: `push-branch-and-tag.ps1 -Tag v0.2.154` — локальная валидация release-коммита в чистом worktree **PASSED** (npm ci, lint, build, все check:*), ветка `master` и аннотированный тег `v0.2.154` запушены на SSH-origin. Source SHA фиксов `87ae4ac`; release-коммит `0108caf` (дополнительно включает ревью-док V02154); тег `a03ab53` → `0108caf`; удалённый `master` = `0108caf`. Публикацией тега запущен `.github/workflows/release.yml`; сборка установочников и статус CI из этого окружения не наблюдаемы (`gh` отсутствует) — фиксируются отдельным шагом.

## Постфикс-раунд после публикации (2026-09-30): P1 downgrade, P1 task-refresh, D3.1, CI-фикс

Постфикс-ревью `docs/ITERATIVE_MIGRATION_REVIEW_V02154_POSTFIX_2026-09-30.md`
(HEAD до публикации) + падение `test_runtime_env_prepends_node_dir` на Linux CI
выставили 3 замечания и 1 платформенный дефект. Все закрыты:

| # | Замечание | Фикс |
| --- | --- | --- |
| P1 (downgrade) | `_discover_targets` мог выбрать версию НИЖЕ текущей (candidate новее не проверялся; unknown engines пропускались) | Строгий never-downgrade: кандидат предлагается только если npm-semver доказывает его строго новее declared-пола (`_declared_floor`/`_strictly_newer`, `||`/`-`/частичные диапазоны и prerelease по настоящему semver). Нет подтверждённо совместимой версии новее текущей в bounded окне → текущая сохраняется, цель НЕ выставляется, пишется `no-newer-compatible` (declared/latest/rejected/reason), begin.discovery включает её в noCompatibleAlternative; plan-next видит NO_ACTIONABLE — без downgrade и без ложной complete. Regression: exact/equal/older/range/prerelease + через begin→plan-next (`_plan_next_locked`). |
| P1 (task-refresh) | Диспатч игнорировал результат refresh и мог отправить старое ТЗ (или пустое) | Диспатч различает input-missing (нет exportable state — продолжаем, промпт самодостаточен из durable assignment) и операционный export-failed (recoverable `TASK_EXPORT_FAILED`, старый текст не используется). Текст ТЗ в промпт — только через `taskDispatchable` (run/policy/checkpoint identity + проверенные content-hashes). `iterative_task.py` пишет task-файлы точными UTF-8 байтами и хэширует те же байты (contentHash совпадает с диском на любой платформе, включая Windows CRLF). |
| D3.1 (runtime/evidence) | Discovery-пробы шли через ambient npm/PATH, а не через выбранный runtime | `build_run_config` передаёт `runtime_env_for_path(nodePath)` (node-директория в PATH) в `_discover_targets` — пробы `npm view` выполняются тем же инструментом, что install/verify. |
| CI-фикс | `test_runtime_env_prepends_node_dir` падал на Linux (Windows-пути `X:/...` и жёсткий `;`) | Тест переведён на платформо-корректные пути (`os.pathsep`, POSIX-ветка `/opt/...`); контракт «node_dir в начале PATH, остальные entry сохранены» сохранён. |

Гейты постфикс-ревизии: `test_iterative_target_discovery.py` 15 OK,
`test_project_runtime.py` + engine_precheck + runtime_request + schemas 63 OK,
`test_iterative_*.py` 83 OK, `--suite all` **1521 OK (4 skipped)**,
`--suite production-fast` **64 OK**, физический acceptance PASS (~105s);
Desktop tsc (electron) OK, lint 15 pre-existing warnings / 0 errors, vite build
OK, check:iterative-migration/runner/agent/begin/task-clipboard/i18n/ui-lifecycle OK.

Честные ограничения не изменились: E12/E13 live-агент на реальном проекте,
E9 физический install под выбранным Node вне fake-фикстуры, наблюдение GitHub
Actions после публикации тега — отдельным шагом.
