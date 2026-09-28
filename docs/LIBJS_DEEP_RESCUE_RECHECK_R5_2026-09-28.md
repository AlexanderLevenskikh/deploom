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
- Реальный libjs run в этой приёмке не выполнялся. Проверенный REAL_RUN_ACCEPTANCE по-прежнему содержит пустые before/after/checkpoint/restart/first-useful-upgrade.

Материалы: `.dependency-roadmap/audit-reviews/recheck-r5-2026-09-28/`: suite.txt, diagnostic-repair.json, matching-result.json, red-checkpoint-result.json и воспроизводящие scripts.

## Конкретное следующее задание

1. Убрать закрытие repair по packages + общему migration pass. Ввести отдельное доказательство успешного исправления конкретного запроса или оставить resolution только authoritative verifier.
2. Добавить негативные тесты одинаковых имён при разных версиях/snapshots, красной pre-existing команды и отсутствующей команды.
3. Подключить Baseline repair dispatch/return и пройти end-to-end фикстуру с restart.
4. Зафиксировать реальный полезный checkpoint libjs и actual before/after. Уже закрытые путь и Python project-green guard не переделывать.
