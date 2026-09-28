# R8: повторная приёмка R7 и инструкции для агентов

Дата: 28.09.2026. Проверена реализация 84376082c85b7adf03178d501acbe3bddee16c3b (fix(repair): R6/R7 review). В ходе чтения другой процесс кратковременно создал release commit; к завершению тестов HEAD снова 8437608. Проверка относится к implementation commit, а не к подтверждённому выпуску новой версии.

## Вердикт

Три конкретных дефекта R7 устранены на проверенном уровне кода и regression-проверок. Ошибочные утверждения real acceptance исправлены. Дополнительно обнаружен и исправлен блокер public sanitization: приватный идентификатор в комментарии нового теста. Логика теста не менялась. Полную приёмку реальной миграции закрывать пока нельзя: реальный project-green и два developer-документа всё ещё не доказаны.

## Что закрыто

1. **Workspace base repair settings.** Production adapter main.ts создаёт repair settings рядом с исходным settings. buildRepairSettingsFile дополнительно запрещает другой каталог. Python settings_workspace_base сохраняет базовый каталог при таком размещении; оба запуска используют один путь durable handoff. Фикстура G проверяет production checkout/settings helpers на настоящем Git-репозитории, remap проекта, отрицательный путь и параметры повторной verification. Это не полный запуск Python verifier через Desktop.
2. **Возобновление последней попытки.** Dispatcher различает idle, agent-running и verification-pending. Продолжение использует прежний attemptsUsed; in-flight попытка возобновляется даже при достигнутом maxCycles. Сценарии E/E2 читают состояние, записанное самим dispatcher до имитированного прерывания, и проверяют sessionId и лимит. Сценарий F проверяет отсутствие повторного dispatch при pending verification. Это управляемая имитация прерывания, а не физический kill Electron.
3. **Устаревший контракт фикстуры.** Вызовы приведены к новому API, ensureRepairCheckout передаётся. check:repair-dispatch теперь проходит после компиляции текущего кода.
4. **Честность real acceptance.** Локальный REAL_RUN_ACCEPTANCE теперь отмечает terminal как завершённый, TIME_TO_FIRST_PROJECT_VERIFIED и FIRST_USEFUL_UPGRADE как NOT_PROVEN, developer-документы как NOT_FOUND, общий статус как INCOMPLETE. Прежняя подмена project-green завершением solver-уровня исправлена.

## Дополнительная находка: public sanitization

Полная проверка tracked surface и двух новых документов обнаружила denylist-идентификатор в tests/regression/test_repair_settings_workspace_base.py:17. Это локальное имя пользователя в комментарии о Windows 8.3 paths. Комментарий заменён нейтральным описанием без изменения исполняемого кода. Требуется включить эту однострочную правку в следующий commit до release.

## Проверки этой приёмки

- Public sanitization: PASS после правки комментария; проверены tracked files плюс новые AGENTS.md и R8 без изменения Git index.
- git diff --check: PASS; diff теста содержит только одну замену комментария.
- npm run build: PASS (Electron TypeScript, frontend TypeScript, Vite).
- npm run check:repair-handoff: PASS.
- npm run check:repair-dispatch: PASS, включая новые сценарии restart и Git checkout/settings.
- python -m unittest discover -s tests/regression -p test_source_checkout_repair_capture.py -v: 4/4 PASS.
- python -m unittest discover -s tests/regression -p test_repair_settings_workspace_base.py -v: 2/2 PASS (до правки только комментария).
- Сборка и subprocess/Git-проверки выполнены с разрешённым запуском вне sandbox из-за ранее подтверждённых ограничений spawn/Temp.
- Полный Python suite, все Desktop checks, живой Electron с реальным repair-агентом и новый real run в этой приёмке не запускались. Результаты узких проверок не выдаются за эти проверки.

## Что остаётся для полной приёмки

Нужен сквозной запуск Desktop: REPAIR_REQUIRED → агент правит изолированный source/config → fresh authoritative verifier проверяет исправленные байты и exact assignment → закрывается исходный request → сохраняется project-green cumulative checkpoint. Приложить identity состояния, команды и exit codes, реальные before/after и файлы MIGRATION_REPORT.md и DEVELOPER_UPGRADE_GUIDE.md. Прежний CLI solver-run и зелёные unit/fixture tests этого не заменяют.

Baseline comparison текущего локального real run показывает red → red, lag 30.83% → 30.83%, C 2 → 2, H 9 → 9 и direct diff 0. Это сравнение planning/source состояния, а не доказательство применённого обновления. Получение roadmap само по себе ожидаемо не меняет исходный checkout.

## AGENTS.md

Создан корневой AGENTS.md: карта модулей, evidence/checkpoint/repair инварианты, Draft deliverables и аудит, точные точки входа тестов, ограничения sanitization для untracked файлов, Windows editing policy.

Для публикации описан push-github-branch-and-tag.ps1 как публичная обёртка над push-branch-and-tag.ps1: автоматический patch bump, отдельные source/version commits, валидация release commit, широкое staging, отдельные branch/tag push и retry с тем же явным тегом. Отдельно отмечено, что helper не запускает Python suite, а задача проверки или документации сама по себе не означает разрешения на публикацию.

Production-код не изменялся. Созданы AGENTS.md и этот отчёт; в одном тесте нейтрализован комментарий для public sanitization. Release/push/tag этой проверкой не запускались.
