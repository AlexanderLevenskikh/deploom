# R7: повторная приёмка repair dispatch, 28.09.2026

Проверены HEAD 55fa96a (v0.2.145) и текущие незакоммиченные исправления R6. Вердикт: приёмка не закрыта. Исправления source guard и раннего сохранения sessionId полезны, но сквозной Desktop repair всё ещё имеет два подтверждённых блокера. Production-файлы в этой проверке не изменялись.

## P1 — repair settings меняет корень durable state

Места: desktop/electron/main.ts:6705, desktop/electron/repair-checkout.ts:121; dependency_live_roadmap_generator.py:1615, :26469, :10355.

Desktop сохраняет копию settings в <workspace>/.dependency-roadmap/state/repair-settings-*.json. buildRepairSettingsFile меняет только projects[].path. Python settings_workspace_base определяет workspace по каталогу settings, причём специальная обработка есть только для непосредственной папки .dependency-roadmap.

Фактически проверено вызовом production settings_workspace_base:

- обычный settings: .dependency-roadmap/state/repair-requests.json;
- repair settings: .dependency-roadmap/state/.dependency-roadmap/state/repair-requests.json.

Повторный verifier не читает исходный repair handoff и пишет в другую папку; Desktop readOpenRequests продолжает читать исходную. Даже успешная проверка не закрывает исходные запросы; цикл может исчерпать попытки. Относительные history/cache/groups paths тоже меняют смысл.

Исправление: сохранить исходный workspace base как явный контракт либо разместить repair settings так, чтобы base остался прежним; корректно сохранить семантику всех относительных путей. Не копировать и не удалять запросы для имитации закрытия.

Приёмочный тест: создать обычный workspace/settings + открытый handoff; построить repair settings production-функцией; пройти Python path resolution и verifier; проверить, что оба процесса читают один durable state и исходный exact request закрывается авторитетным результатом.

## P1 — restart незавершённого агента расходует новую попытку

Место: desktop/electron/repair-dispatch.ts:364–382.

Раннее сохранение sessionId добавлено. Однако phase=agent-running обрабатывается как новый dispatch: attempt=attemptsUsed+1. При attemptsUsed=maxCycles цикл вообще не входит в тело и выставляет exhausted, хотя сохранённая сессия ещё не завершена.

Воспроизведено на скомпилированном production dispatcher, с записью и повторным чтением state с диска:

    phase=agent-running, attemptsUsed=3, maxCycles=3, agentSessionId=persisted-session
    => outcome=exhausted, dispatchCalls=0, attemptsUsed=3

Также verification-pending вычисляет номер attemptsUsed+1, хотя новая попытка не начинается.

Исправление: различать начало новой попытки, resume текущего агента и resume его verification. Продолжение текущей попытки не должно увеличивать счётчик; последняя незавершённая попытка должна возобновляться даже при исчерпанном лимите новых попыток.

Приёмочные тесты: kill/resume после раннего session callback, до возврата агента, отдельно на первой и последней разрешённой попытке; повторные restart той же попытки; restart в verification-pending. Восстанавливать записанное реальным dispatcher состояние, а не только вручную подготовленную сессию.

## P2 — repair dispatch fixture не соответствует новому API

Место: desktop/scripts/check-repair-dispatch.mjs:231.

Фикстура вызывает runBaselineRepairCycles без нового обязательного ensureRepairCheckout (также отсутствуют originalPath/sourceBranch). После успешного Electron TypeScript compile запуск вне sandbox завершается:

    TypeError: input.ensureRepairCheckout is not a function

В sandbox тот же тест останавливался раньше с fake baseline must produce a terminal envelope on run 1; эту раннюю ошибку нельзя считать доказательством дефекта parser. Повтор вне sandbox раскрыл отдельную воспроизводимую ошибку контракта фикстуры.

Исправление: обновить все сценарии под текущий API; добавить production checkout/settings adapter и реальные boundary assertions из двух пунктов выше. Повторить compile и check:repair-dispatch.

## Real run: terminal появился, project-green по-прежнему не доказан

В изолированном real-run-2026-09-28 теперь есть terminal: run.log сообщает baseline captured, writing roadmap artifacts и done; total elapsed=8181.6s (около 136.4 мин). Roadmap JSON/Markdown/HTML записаны.

В логе 14 сообщений resolver-green; project migration is expected с провалами обязательных project checks. Последние сообщения default перечисляют flow:check, lint, build, test:unit, test. Это не подтверждает зелёный обновлённый проект.

REAL_RUN_ACCEPTANCE всё ещё IN PROGRESS и содержит ошибочное TIME_TO_FIRST_PROJECT_VERIFIED=66.8 мин. MIGRATION_REPORT и DEVELOPER_UPGRADE_GUIDE в проверенном workspace не найдены. Финализация CLI не создала их автоматически.

Нужно обновить acceptance по готовым артефактам: отделить resolver incumbent от project-green; заполнить измеримые before/after и cohorts, а неподтверждённые поля явно оставить NOT_PROVEN. Документы и сквозной Desktop repair/return подтвердить отдельным исполнением, а не фактом завершения solver.

## Проверки

- npx tsc -p tsconfig.electron.json: PASS.
- npm run build: PASS вне sandbox (Electron TypeScript, frontend TypeScript, Vite).
- npm run check:repair-handoff: PASS.
- Python test_source_checkout_repair_capture.py: 4/4 PASS вне sandbox.
- npm run check:repair-dispatch: FAIL вне sandbox, ensureRepairCheckout is not a function.
- Проба restart на последней попытке: подтверждает преждевременный exhausted.
- Проба Python settings_workspace_base: подтверждает расхождение durable state paths.
- git diff --check: PASS (предупреждение о будущей нормализации CRLF в main.ts).
- Полный Python suite в этой приёмке не повторялся.

В рабочем дереве три изменённых production-файла и два новых незакоммиченных файла реализации/тестов. Тег v0.2.145 этих исправлений R6 не содержит. После устранения блокеров нужно зафиксировать полный набор файлов и проверять конкретный commit.

## Порядок работы для агента

1. Закрыть единый workspace/state контракт repair settings и проверить его сквозным тестом.
2. Исправить resume текущей попытки без дополнительного расхода бюджета; проверить kill на последней попытке.
3. Обновить repair fixture и добиться зелёного compile + handoff + dispatch + source-capture tests.
4. Исправить real acceptance по завершённому прогону; отдельно подтвердить project-green и наличие двух developer-документов.
5. Зафиксировать изменения целиком; release не использовать как свидетельство приёмки незакоммиченного кода.
