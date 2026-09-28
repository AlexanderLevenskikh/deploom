# Приёмка R5 — v0.2.144, 28.09.2026

Проверен чистый HEAD 1af1503 (реализация b336f50). Вердикт: два из трёх блокеров R4 закрыты; Desktop resolution и полный автоматический repair пока не приняты.

## Закрыто

1. Python producer и Desktop consumer используют `.dependency-roadmap/state/repair-requests.json`.
2. Python требует project_result.ok для repair-resolved. Повторный независимый сценарий показал: resolver=True, project=False → файл остаётся, resolvedEvents=[]. Это прежний воспроизводимый дефект R4, теперь устранён.
3. Сверка Desktop дополнена project, mode и покрытием имён пакетов; чужой проект/режим и непокрытая группа больше не подходят. Однако этого недостаточно для подтверждения исправления.

## Открытые замечания

### P1 — совпадение имён пакетов не подтверждает нужные версии и snapshot

`desktop/electron/repair-handoff.ts:169–181`, вызов `desktop/electron/main.ts:4555–4559`.

matchingRepairRequests получает только project, mode, packages. Значения assignment, fingerprint, snapshotIdentity, проверенные команды и overrides вообще не участвуют в сверке. Покрытие имён пакетов называется FULL assignment в комментарии, но это не проверка assignment.

Свежий Node probe на скомпилированном production helper: два запроса одного проекта/режима — a@2/source-old и a@3/source-other. Evidence содержит packages=[a]. Helper возвращает **оба** requestId. Main затем удалит оба при ready/pass batch, хотя две разные версии не могли быть проверены как одно состояние.

Исправление: передавать фактическое cumulative assignment, identity проверенного состояния и результаты нужных команд. Закрывать конкретный request только по подходящему доказательству. Безопасный минимальный вариант: Desktop только исполняет repair и запускает повторный Baseline, а удаление запроса делает один authoritative verifier после всех проверок. Не расширять сравнение ещё одним списком имён.

Негативный тест: совпадают project/mode/names, но различаются version либо snapshot/контекст → неподтверждённый запрос остаётся. Позитивный тест должен проверять matching полного состояния, а не подмножество имён.

### P1 — Desktop трактует отсутствие новых регрессий как успешное исправление

`desktop/electron/main.ts:4540–4559`; `desktop/electron/migration-verification.ts:417–440`.

Вход в удаление repair-запросов использует verification.status=pass. Этот status означает отсутствие новых baseline→post регрессий, а не обязательный успех всех project-команд.

Свежий probe: checkpoint с migrationOutcome=ready и `yarn lint: baselineExit=1, postExit=1` даёт assessment.status=pass. Совпадающий repair-запрос для этого lint проходит matching; при batchState.ready Main закрывает его, хотя команда всё ещё красная. Выходы production helpers сохранены в red-checkpoint-result.json.

Исправление: независимо от общей политики допустимых baseline failures требовать свежий успешный результат конкретных repair-проверок. Непроверенная, отсутствующая или красная команда не закрывает request. Само разрешение pre-existing failures для обычной миграции менять не требуется.

### Неподтверждённый обязательный этап — автоматический dispatch и возврат

Новый consumer по-прежнему встроен в уже запущенный runGroupAgentSession. Обработчик, который по Baseline REPAIR_REQUIRED создаёт/возобновляет repair-исполнение и возвращает исправленное состояние на cumulative verification, не добавлен. Поиск repair-required-terminal в Desktop не находит потребителя; обращения к repair-файлу используются для feedback и resolution.

Нужен сквозной сценарий Baseline failure → dispatch → source/config change → повторная verification → checkpoint → restart. Парсер файла и ручная подмена успешного verifier этот сценарий не подтверждают.

## Проверки

- 120 тестов A–F и выбранных регрессий прошли с workspace-temp адаптером.
- Отдельный штатный G с тем же tempfile-адаптером: 9 тестов, 8 ошибок SOURCE_SNAPSHOT_CAPTURE_FAILED / SOURCE_DIRECTORY_UNREADABLE при чтении desktop/vendor/__pycache__ в sandbox. Это ограничение запуска, не свидетельство восьми дефектов продукта; зелёным этот прогон не считается. Изолированный диагностический probe выше завершился успешно.
- Electron TypeScript compile, migration-verification script и check-repair-handoff прошли.
- Повторный diagnostic-repair probe подтвердил исправление Python; физическая проверка в нём подменена, реальных установок нет.
- Два новых Node probes воспроизвели Desktop-дефекты на production helpers.
- `git diff --check` прошёл.
- Реальный run целевого проекта в этой приёмке не выполнялся. Проверенный REAL_RUN_ACCEPTANCE по-прежнему содержит пустые before/after/checkpoint/restart/first-useful-upgrade.

Материалы: `.dependency-roadmap/audit-reviews/recheck-r5-2026-09-28/`: suite.txt, diagnostic-repair.json, matching-result.json, red-checkpoint-result.json и воспроизводящие scripts.

## Закрытие пунктов задания (реализация 28.09.2026)

### 1–2. Desktop resolution убран; негативные тесты exact-tuple

Commit `1dc588a`. `repair-handoff.ts` — только read-only (parse/scan/read + `findOpenRepairRequest`); `matchingRepairRequests`/`resolveRepairRequest`/`ResolveOutcome` удалены; `main.ts` резолюцию не выполняет. Удаление запросов — только authoritative verifier (Python: свежая project-green verify точного tuple, `project_result.ok`). Добавлены:
- `test_repair_resolution_is_per_fingerprint_not_per_package_names` — 4 фазы: a@2/S1 → r1; план на a@3/S2 → r2; green a@2 закрывает только r1 (r2 остаётся); repair S2→S3 → green a@3 закрывает r2.
- `test_repair_request_stays_open_when_project_checks_are_disabled` — checks off → нет repair-resolved, файл и snapshot не тронуты.
- Тест вскрыл реальный баг: терминальный персист писал in-memory запросы текущего прогона и затирал stale durable-запросы. Исправлено на `_persist_open_repair_map(durable_repair_map, run_id=run_id)` (L13611–13613).

Санитизация: найдены и вымараны приватные токены (путь машины ревьюера, корпоративный префикс, внутренний скоуп) во всех audit-доках; `Public sanitization OK`.

Широкая регрессия 219 тестов зелёная (единственный фейл — тайминг-флак heartbeat-теста, изолированно проходит).

### 3. Baseline repair dispatch/return (+ restart) — реализовано, проверено фикстурой на фейковом CLI

Новые модули:
- `desktop/electron/repair-handoff.ts` — `extractRepairRequiredEnvelope()`: детектор терминала `repair-required-terminal` из канала `DEPLOOM_PROGRESS_V2` (run завершается exit 0, поэтому детектит Desktop, а не код возврата).
- `desktop/electron/repair-dispatch.ts` — машина состояний эпизода (cycle, exact-tuple закрытие через `sameRepairTuple`, restart-resume через durable `repair-dispatch-<token>.json`) и DI-обёртка `runBaselineRepairCycles()` — цепочка REPAIR_REQUIRED → запуск/возобновление repair-агента → свежая авторитетная пере-верификация → repairReturnPoint.
- `desktop/electron/baseline-repair-result.ts` — обязательный machine result агента (`repaired|partial|blocked`) и сборка repair-prompt (передаёт точный assignment/fingerprint/snapshot/failing-commands; запрет менять версии, коммитить, ослаблять проверки).
- `main.ts`: детекция после команды baseline (Verified, не Draft); при REPAIR_REQUIRED — цикл dispatch/return (до `DEFAULT_MAX_BASELINE_REPAIR_CYCLES=3`), повторная верификация — `DEPLOOM_BASELINE_RESUME=restart` (свежий прогон; ранее проверенные артефакты сохраняются на диске); blocked/exhausted — типизированные ошибки; зелёный финал — очистка dispatch-книг.
- `desktop/scripts/check-repair-dispatch.mjs` + npm `check:repair-dispatch` + `ci.yml`: e2e-фикстура на фейковых CLI/агенте — fresh dispatch → repair → пере-верификация → закрытие; restart mid-episode резюмирует ТУ ЖЕ сессию; blocked; exact-tuple (пере-верификация, оставившая A@2, не закрывает, а A@2+оставшийся A@3 закрывается по точному tuple).

**Граница подтверждения**: полное подтверждение — фикстурой с фейковыми исполняемыми файлами (тот же production-код `runBaselineRepairCycles` через DI). Production-путь подключён к реальным исполнителям (repair-агент — реальный provider/сессия через `agentStartSpec`/`agentResumeSpec`, пере-верификация — реальный Baseline CLI в worker pool), но сам этот производственный маршрут end-to-end выполнялся с фейковым CLI, а не с реальным прогоном целевого проекта. Реальная цепочка с реальным нуждой в repair на целевом проекте не наблюдалась; см. пункт 4.

### 4. Реальный run целевого проекта — в работе

REAL_RUN_ACCEPTANCE по-прежнему пуст; цель — реальный run целевого проекта (путь предоставлен ревьюером) и фактический before/after + checkpoint + restart + first-useful-upgrade.
