# Проверка отчёта о закрытии R5 — R6, 28.09.2026

Проверен HEAD c245e0a, реализация 1dc588a + 26c06f1. Вердикт: существенный прогресс подтверждён; полная приёмка и project-green результат не подтверждены. Производственный код и запущенный real run не изменялись.

## Что подтверждено

- Desktop больше не закрывает repair по именам пакетов/общему regression-pass: прежние дефекты этого пути устранены, resolution оставлен Python verifier.
- Добавлены dispatch, machine result, prompt, повторный Baseline, durable bookkeeping и DI-фикстура.
- В real run.log есть recovery-resumed, interrupted=true, completedIteration=2 (13:44:29Z).
- Yellow finalized в 14:07:43Z и green finalized в 14:59:28Z. Оба имеют SEARCH_BUDGET_EXHAUSTED_WITH_INCUMBENT, changedDependencyCount=50.
- При чтении лога default iteration 5 ещё выполнялась. Финальный terminal/roadmap в просмотренных данных не подтверждён. Не останавливали и не перезапускали процесс.

## P1: исправленный dirty snapshot отвергается обычным source guard

`desktop/electron/baseline-repair-result.ts` запрещает агенту commit/stash/branch, просит редактировать working tree. `main.ts:6311` запускает агента в обычном project.path. `baselineRepairReVerificationSpec`, `main.ts:6293`, копирует прежнюю Baseline-команду, изменяя только RESUME=restart и RECOVERY_PROOF_REUSE=0.

`dependency_live_roadmap_generator.py:1858–1889` — ensure_source_checkout проверяет dirty tree до fetch и выдаёт SOURCE_CHECKOUT_DIRTY. Повторный capture-baseline проходит тот же guard. В settings фактического real run sourceCheckoutGuard=true.

Следствие для обычной source/config правки отслеживаемого файла: агент выполняет задачу и оставляет dirty tree; следующая авторитетная команда отклоняет этот tree до проверки исправления. Фейковый Baseline CLI в DI-тесте не запускает настоящий source guard и не доказывает работоспособность адаптера main.ts.

Нужно: явный контракт изолированного repair snapshot/checkout, который допускается на повторную verification с новой source identity и сохраняет исходный checkpoint. Не отключать глобально source guard и не заставлять агента обходить политику commits. Тест должен действительно изменить tracked source в git-фикстуре и пройти production source-preflight/materialization путь.

## P1: заявленное mid-agent restart-resume не реализовано сквозным сохранением сессии

`repair-dispatch.ts`, runBaselineRepairCycles: durable state записывается до вызова dispatchRepairAgent, но новый agentSessionId сохраняется только ПОСЛЕ завершения await dispatchRepairAgent. `main.ts`, runBaselineRepairAgent: sessionId возвращается только после await executeCommand.

При остановке приложения во время первой работающей repair-сессии её id ещё не записан в dispatch-файл. На восстановлении используется previousDispatch.agentSessionId, а он пуст. Следующий запуск начнёт новую сессию. Наличие другого job/session state само по себе это не исправляет: новый адаптер передаёт id из dispatch-state и перезаписывает job.agentSessionId.

Нужно сохранять sessionId и фазу эпизода сразу при их появлении, до завершения агента; после restart различать running-agent и verification-pending. Тест: остановить реальный dispatcher после выдачи sessionId, но до возврата агента; восстановить из файла, а не заранее вручную заполненного состояния.

Дополнительно cycle/лимит попыток: цикл for attempt=1..maxCycles начинается заново после restart, а state.cycle в обычных повторных попытках не увеличивается. Для обещанного durable лимита учитывать израсходованные попытки в сохраняемом состоянии.

## P1: real acceptance ошибочно называет resolver incumbent project-verified

В REAL_RUN_ACCEPTANCE указаны TIME_TO_FIRST_PROJECT_VERIFIED=66.8 мин и project verified на yellow/green. Но run.log для принятых кандидатов содержит:

`dependency assignment is resolver-green; project migration is expected: project preflight failed: yarn flow:check, yarn lint, yarn build, yarn test:unit, yarn test`

Такие сообщения есть для yellow, green и default. Значит, в этих точках принят resolver-green результат с открытой миграцией проекта. Это полезное улучшение относительно прежнего solver unknown, но не доказательство рабочего обновления с зелёными project checks. Само имя mode=green также не является результатом проверки проекта.

Нужно исправить acceptance:

- 66.8 мин — завершение yellow с incumbent, не доказанный time-to-first-project-verified;
- project-verified и safe first-useful-upgrade оставить NOT_PROVEN до логов успешных обязательных команд для одного точного cumulative состояния;
- отличать resolver checkpoint, source snapshot и project-green checkpoint;
- не считать RED → learned constraint доказательством исполненного source/config repair;
- before/after, реальные cohorts и developer-файлы завершить по факту. Обычный capture-baseline не доказывает, что агентские developer-документы появятся автоматически при terminal.

Реальный запуск выполнен напрямую Python CLI (`launch_real_run.py`), поэтому он сам по себе не подтверждает Desktop dispatch/agent/return маршрут.

## Проверки этой приёмки

- Electron TypeScript compile прошёл; check-repair-handoff прошёл.
- check-repair-dispatch в этом окружении упал в Scenario A: fake baseline must produce a terminal envelope on run 1. Причина сбоя дочернего fake CLI этим сообщением не раскрыта; не классифицируем как доказанный дефект production dispatcher. Заявленную полностью зелёную фикстуру здесь воспроизвести не удалось.
- Полный заявленный Python-прогон 1408 тестов в этой проверке не повторялся.
- Изучены production adapters, source guard, state persistence и фактические события real run; terminal не ожидался часами и сторонний запуск не изменялся.

## Что делать дальше

1. Исправить dirty-snapshot re-verification contract и mid-agent restart persistence; проверить их через реальные boundary adapters.
2. Исправить названия/статусы real acceptance по фактам лога, сохранив подтверждённый solver/recovery прогресс.
3. Довести настоящий cumulative результат до обязательных зелёных project checks, сохранить identity/команды/exit codes, actual before/after и оба developer-документа.
4. Лишь после этого закрывать полную приёмку и решать о следующем release bump. Сам по себе terminal долгого solver-run не закрывает перечисленные пункты.
