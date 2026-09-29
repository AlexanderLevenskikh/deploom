# Приёмка основного и дополнительного задания

Проверен HEAD `052327f` (v0.2.151), implementation до `3d694c0`. Исходники не менялись при этой приёмке. Вердикт: оба задания целиком НЕ приняты. Рабочий Python vertical slice и экспорт артефактов есть; полноценный Desktop-путь и recovery не завершены.

## Блокеры

### R1 — P1: Desktop не создаёт первый run и первый task

`desktop/electron/main.ts:7384` возвращает NO_RUN, если run.json отсутствует; handler begin с project-dir/targets/config отсутствует. `IterativeTaskPanel.tsx:170` при отсутствии task показывает только сообщение, а кнопки шага и экспорта находятся в ветке уже существующего task. `taskStaleness` считает отсутствие task устареванием и блокирует status/step. Нужны заранее подготовленные CLI run и export, иначе пользователь не начинает новый сценарий. Обычный migration/Autopilot не становится накопительным от наличия этой панели.

Исправить: явный production begin из Desktop с текущими policy/targets/settings, затем coordinator ведёт шаги; task/export не является предусловием работы coordinator. Проверить старт из пустой .dependency-roadmap без ручного CLI/HTML.

### R2 — P1: после успешного materialize драйвер вызывает materialize повторно

`iterative_migration.py:1295` сохраняет phase=MATERIALIZING, candidate.stage=MATERIALIZED. `iterative-runner.ts:148` выбирает materialize по этой фазе. `_materialize_locked` принимает только PLANNED. Воспроизведено production decision-функцией на фактической форме выходного состояния и Python guard: CANDIDATE_STAGE_NOT_PLANNED: MATERIALIZED.

Исправить совместный автомат phase/stage, следующий шаг успешной материализации — precheck. Добавить тест, который получает результат настоящего Python materialize, передаёт его Desktop decision и выполняет выбранную CLI-команду; ручной вызов precheck в физическом тесте эту ошибку обходит.

### R3 — P1: checkpoint делает ТЗ stale и запирает продолжение

`main.ts:7387` блокирует шаг при taskStaleness; принятие C1 меняет activeCheckpointId, что делает экспорт C0 stale. `IterativeTaskPanel.tsx:247` отключает «Переэкспортировать» именно при snapshot.stale. При отсутствии task экспорт вообще не виден. UI предлагает recovery, которым нельзя воспользоваться.

Исправить: stale запрещает dispatch старого scope, но не чтение состояния, продолжение независимого coordinator или регенерацию ТЗ. Retry должен быть доступен при missing/stale/failed. Проверить C0→C1→C2 без внешнего CLI.

### R4 — P1: restart во время verify-exact не восстанавливается

`iterative_migration.py:1517` допускает только PRECHECKED/REPAIRING, затем сохраняет candidate.stage=VERIFYING до долгой проверки. После остановки процесса драйвер вызывает verify-exact снова и получает CANDIDATE_STAGE_NOT_READY_FOR_VERIFY: VERIFYING. Воспроизведено вызовом текущего guard на сохранённом состоянии. В таком же состоянии остаётся инфраструктурная неудача проверки.

Исправить resume VERIFYING с точным candidate/source identity и учётом бюджета; acceptance должен быть идемпотентен на границах записи checkpoint, переключения указателя и очистки candidate. Нужен kill ВНУТРИ verify/acceptance, не только отдельные subprocess между успешно завершёнными шагами.

### R5 — P1: агент не возвращает структурированную альтернативу

`main.ts:7539–7558`: вызов всегда запускает opencode, даже при другом workspace.agent; exit code не проверяется, любой непустой output превращается в READY_FOR_VERIFY. Prompt просит невозможность/альтернативу описать словами, но typed feedback от агента не читается. NEEDS_ALTERNATIVE/NEEDS_COHORT_EXPANSION/INFRA_BLOCKED недостижимы через этот production handler. Ошибка запуска с текстом тоже может выглядеть как завершённый ремонт.

Исправить: использовать выбранный provider и реальный structured response с проверкой tuple, kind, предложений и статуса процесса. Текст агента не доказательство green. Проверить отказ провайдера, timeout, невозможность ремонта, companion request и продолжение независимой группы.

### R6 — P1: restart repair создаёт новую агентскую сессию

`main.ts:7440–7466,7537–7545`: на вызов создаётся новый временный сервер/БД, в buildOpenCodeAgentArgs передаётся undefined sessionId. В новом пути sessionId не сохраняется до ожидания результата. Set защиты только в памяти; добавляется после await status, то есть два параллельных запроса успевают пройти проверку до захвата.

Исправить: durable dispatch lease, сохранить session/provider/database/attempt до ожидания, восстановить тот же in-flight run. Блокировку брать до await и привязать к workspace+project/run. Проверить двойной IPC и рестарт на последней repair attempt без второй сессии.

### R7 — P1: исходный RED не получает корректный bootstrap repair

`iterative-runner.ts:144` направляет BOOTSTRAP_REPAIR сразу в verify-bootstrap; agent handler допускает только decision=agent и требует candidate. Поэтому драйвер повторяет проверку без ремонта. Кроме того, `iterative_migration.py:889–920` проверяет config.projectDir и меняет статус C0, но не создаёт новый source snapshot после исправлений. Следующая материализация использует старые bytes C0.

Исправить отдельный изолированный version-neutral bootstrap handoff и принятие нового снимка с согласованными hashes. Проверить исходный RED→repair→C0 verified→первый upgrade на исправленных исходниках, не в обычном checkout.

### R8 — P1: завершение обходит независимый audit

`iterative-runner.ts:177–179` при совпадении targets вызывает finish. Ни одна ветка decision не возвращает audit. `cmd_finish` только пишет отчёты, не фиксирует завершённое состояние и не проверяет audit/policy. Поэтому READY с достигнутыми версиями снова выбирает finish; audit остаётся UNKNOWN. Даже CLI audit сохраняет в checkpoint лишь status и ссылку на директорию, не полный report с метриками; пользовательская package lag policy туда передаётся как dashboard_state=None.

Исправить audit exact checkpoint с актуальной полной policy, сохранить JSON/Markdown/coverage/vuln evidence, затем вычислить durable complete/partial/blocked. Finish сделать идемпотентным terminal-переходом, доступным также для частичного результата. UNKNOWN не даёт COMPLETE.

## Незакрытые требования дополнительного задания

### R9 — P2: старые Baseline/Draft и исходная кнопка остаются на старом пути

Новый экспорт читает только новый iterative run-config/checkpoints. Адаптера существующего результата Baseline нет; старый prompt-harness продолжает загружать HTML. PromptPreviewDialog и dashboard copyPrompt по-прежнему используют navigator.clipboard. Новая Electron clipboard-функция полезна, но не исправляет автоматически кнопку из сообщения пользователя. Acceptance-документ честно признаёт, что исходный сбой не воспроизведён.

Нужна приёмка старого сохранённого результата без повторного Baseline и рабочего Desktop preview/copy/save/dispatch; общий builder/контракт с прежним путём либо явный versioned adapter. Реальный clipboard probe проверяет устройство, а не нажатие кнопки через preload/IPC.

### R10 — P2: обязательные итоговые документы пока шаблоны

`iterative_migration.py:2085–2177`: MIGRATION_REPORT содержит таблицу checkpoint/версий и hashes. DEVELOPER_UPGRADE_GUIDE содержит таблицу версий и обещание описать возможности/breaking changes позже. Это не требуемые возможности, ограничения, breaking changes, адаптации и рекомендации по фактическим обновлениям. Также checkpoints сортируются строками и active берётся как последний: при C10 и C9 итог может описывать C9.

Нужна агрегация фактических агентских объяснений/release evidence по принятой цепочке, выбор activeCheckpointId, проверка содержимого обоих документов и сохранение на partial/stop. Наличие файлов не является acceptance содержания.

## Проверки этой приёмки

- Python regression `python -m unittest discover -s tests/regression -p 'test_iterative*.py' -q`: 27 PASS.
- Физическая acceptance `python -m unittest discover -s tests/acceptance -p test_iterative_migration_physical.py -v`: 1 PASS, 43.846s (реальные Git/npm/check в изолированной фикстуре).
- Desktop TypeScript компиляция через precheck: успешна.
- `check:iterative-runner`, `check:task-clipboard`: PASS.
- `node scripts/check-iterative-agent.mjs`, `node scripts/check-iterative-migration.mjs`: PASS после разрешённого повторного запуска; первый запуск блокировался sandbox spawnSync python EPERM.
- Точечные воспроизведения R2 и R4: подтверждены указанными ошибками текущих функций.
- Реальный пользовательский проект не запускался; настоящий агент не вызывался. Системный clipboard не перезаписывался ради повторения device probe. Полный CI здесь не повторялся: профильные зелёные тесты уже показывают пробелы интеграционного покрытия.

## Задание на следующий проход

Сначала закрыть R1–R4 одним интеграционным Desktop→Python сценарием. Затем R5–R7 с production provider adapter и durable recovery. Затем R8 и R9–R10. Не подменять автоматический coordinator последовательностью ручных кликов по внутренним CLI-шагам. После каждого исправления добавить тест на описанный failure boundary, а в конце пройти основной сценарий через Desktop без предварительно подготовленного run/task, kill/restart во время работы и ограниченный real-agent pilot. Обновить устаревшие implementation status/acceptance по фактическому HEAD. До этого оба задания считать частично реализованными.
