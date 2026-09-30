# Приёмка v0.2.154 после завершения работ

Проверен чистый HEAD `562b411`. Отчёт агента о закрытии 7 пунктов отражает реальные изменения в исходниках; тесты отдельных механизмов зелёные. Полная приёмка пользовательского сценария пока не пройдена. Реальный частный проект и live provider не запускались в этой приёмке.

## P1. Цикл застревает на первом агенте и после checkpoint

`desktop/electron/iterative-migration.ts:103`: отсутствие task даёт `stale=true, NO_TASK`. `flow:iterative:begin` создаёт run/C0 и автоматически вызывает drive, но не экспортирует task. При исходном RED или первом RED candidate drive возвращает `agent-gate`. В `IterativeTaskPanel.tsx:250–297` ветка `!task` НЕ содержит кнопки экспорта/продолжения, а «Исправить агентом» имеет `disabled={... || Boolean(snapshot?.stale)}`. `main.ts:7753` тоже отвергает agent, если task stale. Получается закрытый цикл: агент требует ТЗ; экспорт доступен только когда ТЗ уже есть. `task_staleness` на пустом каталоге фактически возвращает NO_TASK (проверено через собранный модуль).

После успешной C1 старый task привязан к C0 и закономерно stale. В ветке с task `IterativeTaskPanel.tsx:348` отключает «Продолжить» при `snapshot.stale`. Кнопка «Переэкспортировать» там есть, но необходимая ручная операция разрывает заявленный автоматический цикл; если она не выполнена, следующий шаг заблокирован. Состояние task не должно запрещать coordinator. Перед agent gate надо автоматически собирать актуальное ТЗ из durable state (или показать работающую кнопку экспорта и разрешить repair, привязанный к exact run/candidate/checkpoint). После acceptance Ck автоматически обновлять task либо продолжать coordinator без него. Тестировать новый workspace без task, C0 RED, C1→C2 со stale task и restart на gate.

## P1. Runtime discovery выбирает latest и откладывает вместо совместимой альтернативы

`iterative_migration.py:_discover_targets` при пустом roadmap запрашивает `dist-tags.latest` для каждой прямой зависимости. `build_run_config` вызывает discovery до разрешения requested Node и передаёт `runtime_env=None`. `_engine_deferred_targets` позднее видит несовместимый с Node latest и убирает цель из текущего candidate, записав deferral. Поиск более ранней версии, удовлетворяющей lag policy и `engines.node`, в новом итеративном пути отсутствует. Для сценария CI Node 20.11.0 и latest, требующего Node 22/24, пользователь получает отложенную цель, хотя в registry может существовать пригодная версия. Текст «yellow lag policy» в docstring discovery не реализован: latest — это не граница допустимого лага.

Исправить bounded выбор совместимой версии из registry metadata с точным Node и policy; если подходящей версии нет или evidence неизвестна, честно отложить с причиной. Не возвращать false compatibility и не подменять выбранный runtime. Проверить на synthetic registry с latest incompatible + предыдущая compatible, а также unknown metadata и без roadmap.

## Приёмка и доказательства

Реально присутствуют: supervisor, повторный precheck после materialize, resume VERIFYING, detection forbidden mutations, session resume args, typed feedback, audit перед finish, Node selector, exact engines range matcher, отдельный clipboard IPC. Старые замечания нельзя считать все закрытыми end-to-end, пока не пройден пользовательский сценарий.

Локально повторены: `test_iterative_target_discovery.py` — 4 OK; `test_iterative_engine_precheck.py` — 6 OK; Desktop `check:iterative-runner` и `check:iterative-agent` — OK. Они проверяют компоненты по отдельности, не проводят UI→begin→drive→agent→checkpoint→drive. Датированный acceptance-док подтверждает scripted физическую фикстуру, но сам фиксирует отсутствие real-agent pilot и реального install под выбранным Node. Пользовательский CI после тега здесь не проверялся; публикация релиза не является доказательством этих границ.

Следующий обязательный тест: от чистого workspace без roadmap/task, с заданным Node, прогнать через production Desktop entrypoints до первого agent gate и C1, затем до C2 без ручного экспорта/внутренних шагов. Отдельно проверить исходный RED, restart и наличие совместимой альтернативы к engine-incompatible latest. Для real pilot — изолированная копия согласованного проекта и ограниченный бюджет.
