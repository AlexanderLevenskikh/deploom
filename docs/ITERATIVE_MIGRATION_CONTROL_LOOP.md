# Управляющий цикл накопительной миграции и фиксы аудита

Дата: 2026-10-04. Относится к текущей реализации на `master` (после фиксов B1–B7).
Это дизайн-документ: он объясняет, как устроен действующий управляющий цикл, что именно
было исправлено по аудиту `AUDIT_2026-10-04.md` и чем это доказано. Документ не заменяет
`docs/MIGRATION_VALIDATION.md`, `docs/ITERATIVE_REPAIR_RECOVERY.md` и `docs/STORAGE_COHORT_REVIEW.md`.

## 1. Цикл

Накопительная (итеративная) миграция — рекомендуемый основной процесс. Desktop-compatible
координатор строит план от проверенного инкумбента к целям ограниченными когортами и никогда
не выдаёт недостижимую цель за обновлённую.

```text
begin  → контрольная verify C0 на sealed source snapshot (база, не апгрейд)
plan-next → bounded greedy когорта, exact assignment fingerprint
materialize → изолированное частное дерево, npm install по exact spec, offline cache
precheck → диагностика команд проекта
repair → изолированный checkout, typed feedback (repaired|partial|blocked)
verify-exact → авторитетная перепроверка на repaired bytes
accept checkpoint → кумулятивное verified-состояние (последний verified checkpoint сохраняется)
repeat / finish → MIGRATION_REPORT.md + DEVELOPER_UPGRADE_GUIDE.md
```

Ключевые инварианты:

- **Draft — предложение.** Успех resolver, sealed snapshot, найденный инкумбент и зелёная
  верификация — разные состояния. Exit code 0 или имя режима `green` не доказывают, что
  проектные проверки прошли.
- **C0 — не принятый апгрейд.** Verified база не считается «accepted checkpoint» и не завышает
  счётчик `acceptedCheckpoints`; при нуле обновлений и недостигнутой цели результат честный
  `PARTIAL_VERIFIED` / `NO_VERIFIED_UPGRADE`, а не ложный частичный успех.
- **Infra/UNKNOWN никогда не становится совместимостью.** Сбой инфраструктуры, недоступные
  evidence, timeout и solver UNKNOWN не превращаются в ложные constraints или нулевые
  уязвимости; отказ закрывается fail-closed, а не замалчивается.
- **Новый кандидат = новый бюджет ремонта.** `repairAttemptsForRevision` пересчитывается на
  новый кандидат/принятый checkpoint; первый отказ позднего кандидата не отбрасывает его
  мгновенно из-за остатка чужого бюджета.
- **Stop/resume сохраняет последний verified checkpoint и тот же кандидат.** Перезапуск не
  тратит попытку повторно и не бросает in-flight attempt.

## 2. Фиксы аудита (B1–B7) и их коммиты

| ID | Проблема | Исправление | Коммит |
| --- | --- | --- | --- |
| B1 | После неудачного install кандидат остаётся PLANNED/фазой MATERIALIZING, CLI 0; Desktop бесконечно повторял дорогие materialize, `maxInfraRetries` не применялся. | Инфраструктурный сбой классифицируется как `infraBlocked`; цикл останавливается на исчерпании попыток materialize; `plan-next --retry-infra` возобновляет тот же кандидат и тот же checkpoint. | `430af83` + демо `51bfc73` |
| B2 | `repairAttemptsForRevision` жил в общих ledger.counters и не сбрасывался на новом кандидате. | Счётчик привязан к revision/checkpoint и сбрасывается при новом кандидате/принятом checkpoint. | `430af83` |
| B3 | Finish считал VERIFIED C0 принятым checkpoint → `PARTIAL_VERIFIED` при нуле обновлений, завышенный `acceptedCheckpoints`. | C0 — не апгрейд; подсчёт accepted и итоговый verdict корректны при нуле апдейтов. | `430af83` |
| B4 | Подбор видел исходные группы и половины, но не произвольные комбинации; дерево исчерпывалось без рабочей связки. | `iterative_cohort_planner` получил bounded split-then-recombine closure; честный `SCOPE_EXHAUSTED`/`PARTIAL_VERIFIED`, когда связки нет. | `ef45229` |
| B5 | В `NEEDS_COHORT_EXPANSION` сначала сохранялся REJECTED, потом валидировался feedback → изменение durable state до ошибки. | Feedback валидируется до записи; неверный companions не оставляет REJECTED в durable state. | `430af83` |
| B6 | Byte-cap attempt.log не соблюдался: 8 МиБ вывода → 6.24 МиБ после close-trim; stream держал весь stdout/stderr в памяти. | `iterative-stream.ts` обрезает attempt.log **во время** стриминга (~128 КиБ / 800 строк), полный failure-артефакт сохраняется отдельно. | `fe70465` |
| B7 | Старый trial GC работал синхронно перед дорогой проверкой; `_has_links`+`rmtree` обходили всё дерево без ограничения файлов/времени (5000 файлов/0,5 МБ ≈ 3,73 с). | «Detach fast, delete later»: hot-path reaper делает только O(1) rename со скромным бюджетом обхода; удаление больших деревьев уходит в maintenance-pass. 5000-файловая фикстура отцепляется за ~0,04 с. | `5249399` |

## 3. Bounded trial GC (детали B7)

- **Hot path (`reap_orphan_verification_trials`, перед дорогой проверкой)** тратит не более
  `HOT_SWEEP_BYTES/64КБ`, `HOT_SWEEP_FILES/1024`, `HOT_SWEEP_SECONDS/0,5с`: повторно
  сканирует уже отцепленный мусор и **отцепляет** (rename) мёртвые owned-триалы за O(1),
  не вызывая полный `_has_links`/`rmtree`.
- **Поддержание (`verification_storage_maintenance.py clean`)** — это сторона «удалить позже»:
  бюджет `MAINT_SWEEP_*` (512 МБ / 200к файлов / 120 с); также претендует на normal-name
  триалы мёртвых владельцев. Делеция бюджет-подходящего дерева: bounded scan → `_has_links`
  → atomic claim-rename (`gc_` locator) → `rmtree`. Oversize/пересекающееся бюджеты или ссылки
  → дерево остаётся на своём locator для следующего прохода.
- **Ретрай заблокированного удаления** не ждёт нового окна retirement: `gc_` locator кодирует
  исходный mtime, поэтому следующий проход видит его сразу. Активные деревья и неизвестные
  владельцы никогда не трогаются; junction/симлинк-защита сохранена.
- **Отличимость «нет мусора» от «не удалось просканировать»**: `storage_available(root)`
  возвращает, сертифицирован ли корень (нет junction в предках, есть `trials/`); поле
  `available` выводится в ответе `maintenance`.
- **Как проверено**: `tests/regression/test_storage_and_adaptive_cohorts.py` (16 тестов),
  `desktop/scripts/check-storage-maintenance.mjs` (настоящий Python IPC + junction), физическая
  NTFS-фикстура 5000 файлов: hot path 0,038 с, maintenance удаляет всё, `_has_links` на hot path
  не вызывается вовсе.

## 4. Интеграционная демонстрация (настоящий coordinator)

`scripts/iterative-node-drive.mjs` — headless-драйвер, который реплицирует цикл
`flow:iterative:drive` настоящего Desktop-координатора через собранный `dist-electron`
(не отправляет feedback за приложение) и общается с Python CLI через
`scripts/run-iterative-demo.py`. Профили:

- **happy-path-24**: 24 прямые зависимости, жадные когорты, ETARGET-отложение, stop/resume
  сохраняет C1 и тот же кандидат, неверный ремонт отклонён.
- **failure-matrix**: упавший install → `infraBlocked`, остановка на 3 попытках при лимите 2,
  C0 сохранён, `--retry-infra` лечит, wrong repair отклонён.
- **cross-group-closure**: exclusive-or пара; честный `PARTIAL_VERIFIED`, когда вторая часть
  intrinsically RED; `terminalOutcome.outcome` читается из `run.json`.

Детали запуска и фикстур: `docs/ITERATIVE_MIGRATION_DEMO.md`.

## 5. Валидация

- `python run_tool_tests.py --suite all` → **1670 tests OK (4 skipped)** за ~659 с
  (unit 1197, regression 430, acceptance 43).
- Desktop: `npm run lint` (0 errors), `npm run build`, `check:iterative-stream/attempt/runner/
  scenario/begin/migration/agent/archive`, `check:storage-maintenance`, `check:baseline-intent`,
  `check:ui-shell`.
- `python scripts/check-public-sanitization.py` → OK.
- Ограничения: `--suite production-fast` — отдельный CI-гейт, не входит в `all`; полный
  Electron E2E и paid-agent прогон не выполнялись (демо идёт через настоящий Node coordinator,
  но не через UI). Полный ReFS/гигабайтный benchmark выходит за рамки этой сессии.
