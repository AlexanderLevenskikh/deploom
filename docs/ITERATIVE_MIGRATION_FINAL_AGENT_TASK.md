# Итоговое задание агенту: завершить накопительную миграцию, Desktop и контракт окружения

Это задание на реализацию и приёмку, не на очередной план. Прочитай AGENTS.md, `docs/ITERATIVE_MIGRATION_REVIEW_2026-09-29.md`, основной план `ITERATIVE_MIGRATION_PLAN_2026-09-29.md`, основное задание `ITERATIVE_MIGRATION_AGENT_TASK_2026-09-29.md` и дополнительное `ITERATIVE_MIGRATION_DESKTOP_FOLLOWUP_TASK.md` (все в docs/). Этот документ объединяет незакрытые требования и добавляет явное окружение Node/CI. Прежние требования к authority, audit, scope, recovery и deliverables сохраняются. Приёмка исследовала 052327f (v0.2.151); сверяй фактический HEAD, номера строк могли измениться. Не переписывай уже рабочее ядро и сохраняй параллельные изменения.

## Пользовательский результат

В обычном Desktop пользователь выбирает проект, policy и при желании версию Node, запускает миграцию и получает первый полезный verified checkpoint до решения всей задачи. Далее цикл самостоятельно повторяется: небольшая группа → физическая материализация → ремонт выбранным агентом при необходимости → независимая cumulative verification → следующий checkpoint. Неудача следующей группы не уничтожает результат; агент умеет запросить другую комбинацию или companion. Перезапуск восстанавливает тот же run/attempt/session. ТЗ и содержательные отчёты доступны в Desktop без открытия HTML dashboard. Подтверждённые обновления совместимы с заявленным runtime; локальная проверка на другой версии Node не выдаётся за доказательство работоспособности CI.

## Часть A. Закрыть R1–R4: реально работающий Desktop-цикл

1. R1: добавь production begin из Desktop, передающий project-dir, targets, полную policy, registry/package-manager/check settings. В пустом workspace не нужны заранее подготовленные run.json/task или ручной CLI. Подключи обычный migration/Autopilot; внутренние шаги можно оставить в диагностике, но пользователь не обязан нажимать materialize/precheck/verify по очереди.
2. Координатор должен работать независимо от наличия экспортированного ТЗ. Состояния миграции и подготовки артефакта раздельны. Ошибка экспорта не запускает миграцию заново и не отменяет checkpoint.
3. R2: согласуй Python phase/candidate.stage с Desktop decision. Успешный MATERIALIZED ведёт в precheck, а не повторно materialize. Реальный выход Python должен служить входом следующего Desktop решения в интеграционном тесте.
4. R3: missing/stale/failed task допускает просмотр состояния и повторный экспорт. Stale запрещает передачу старого задания новому candidate, но не regeneration. Кнопки восстановления доступны и при отсутствии task. После C1 и C2 не нужен внешний CLI.
5. R4: возобновляй VERIFYING на том же exact candidate/source/runtime identity. Устрани щели между записью checkpoint, переключением active pointer, очисткой candidate/repair requests и UI acknowledgement. Acceptance идемпотентен, принятый checkpoint и бюджет не теряются.
6. Для ошибок/отмены/timeout гарантируй выход UI из busy и конкретное действие восстановления. Запоздалый ответ прошлого run/candidate не обновляет текущий экран и артефакт.

## Часть B. Закрыть R5–R7: взаимодействие с агентом и recovery

1. Используй выбранный workspace provider/model через существующие adapters. Не запускай opencode безусловно. Проверяй exit code/timeout/transport failure, а не наличие stdout.
2. Агент возвращает versioned structured feedback: REPAIRING, READY_FOR_VERIFY, NEEDS_COHORT_EXPANSION, NEEDS_ALTERNATIVE, INCONCLUSIVE, INFRA_BLOCKED. Проверяй run/candidate/base checkpoint/attempt/runtime identities и proposed scope. Не преобразовывай любой ответ в READY_FOR_VERIFY. Ответ «могу исправить» не является proof.
3. Companion/alternative подтверждает planner. UNKNOWN/timeout/неудача агента не создают вечный nogood. Bounded deferral сохраняет цель в denominator и позволяет перейти к независимой группе от последнего checkpoint.
4. Сохраняй sessionId/provider/database и состояние dispatch до ожидания результата. После restart продолжай ту же in-flight сессию/последнюю попытку. Durable lease бери до первого await, ключ включает workspace/project/run; двойной IPC не создаёт двух писателей или агентов.
5. Bootstrap RED получает отдельный изолированный version-neutral repair. Не редактируй исходный checkout. После ремонта захвати новые bytes C0 с согласованными source/manifest/lock/runtime identities; следующие группы строятся именно от них.
6. Проверь фактический diff trial, включая удалённые и запрещённые файлы. Нельзя доверять списку changedFiles агента или исключать manifest/lock из контроля только потому, что их нельзя менять. Не ослабляй проверки и не подменяй source guard.

## Часть C. Закрыть R8–R10: audit, Desktop-артефакты, документы

1. После принятия checkpoint запускай audit в предусмотренных точках и обязательно перед COMPLETE: manual_dependency_audit.py с текущей полной policy и точным окружением. Сохраняй JSON/Markdown, lag numerator/denominator/coverage, C/H/M/L/unknown. Не заменяй на bare yarn audit, не превращай UNKNOWN в нули.
2. Finish — идемпотентный durable terminal transition. Различай COMPLETE, PARTIAL, BLOCKED, CANCELLED. Совпадение targets само по себе не заменяет project verification/audit. Повторный шаг завершённого run не пишет отчёты бесконечно.
3. Экспорт из структурированного результата без HTML; один канонический контракт/builder или явный versioned adapter для старых Baseline/Draft. При достаточных старых данных получай ТЗ без многочасового rerun. При недостаточных — перечисли недостающие поля без выдуманной evidence.
4. В Desktop доступны preview/copy/save/dispatch/retry. Исправь также затронутый прежний preview/copy путь. Узкий typed preload/IPC, проверка sender/payload, sandbox/contextIsolation сохранены. «Скопировано» только после успеха; fallback — выделяемый текст/сохранение файла. Проверяй путь кнопка→preload→IPC→clipboard, а не только устройство отдельно.
5. Комплект task+manifest публикуется атомарно с content hashes и identities; проверяй их при чтении/dispatch. Старый комплект доступен как история, не становится заданием другого candidate.
6. MIGRATION_REPORT.md содержит все фактически принятые изменения, old/new, breaking changes, адаптации кода/конфигов, проверки, audit, окружение, остаток и evidence. DEVELOPER_UPGRADE_GUIDE.md содержит реальные новые возможности, ограничения и рекомендации для дальнейшей разработки. Заглушка «допишем позже» не проходит приёмку.
7. Объясняй неочевидные правки кода/конфигов; для JSON/lock пояснения по file/key в отчёте, не невалидные комментарии. Выбирай активный checkpoint по identity/parent chain, не по лексикографической сортировке C9/C10. Документы доступны и при partial/stop, не меняют незаметно verified source bytes.

## Часть D. Новое требование: явное окружение Node и совместимость CI

### D1. Проблема и границы

Пользовательский CI выполняет install под Node 20.11.0; выбранный пакет требует engines.node `^22.18.0 || >=24.11.0`. Install падает до запуска тестов. В том же логе есть несовместимое resolution для es5-ext и предупреждение о некорректной Git SSH URL. Это разные причины: текущий fatal — engine mismatch; остальные предупреждения должны быть видимы отдельно. Приватные URL/имена из пользовательского лога не переносить в публичные docs/tests/commits. Используй synthetic fixtures.

Не устраняй проблему через --ignore-engines, engine-strict=false, удаление engines, произвольное ослабление resolutions или незаметное повышение Node. Node приложения Electron и Node проекта/CI — разные runtime.

### D2. Настройка и UX

1. Добавь необязательное поле «Node.js для проекта / CI», ПО УМОЛЧАНИЮ ПУСТОЕ. Пусто означает «не задано пользователем», а не «совместимо с любым Node» или автоматический выбор latest. Обратная совместимость старых settings обязательна.
2. Размести настройку рядом с параметрами проекта/миграции до запуска. Поддержи UI, сериализацию settings/schema, backend/CLI, восстановление и импорт/экспорт настроек. Предпочтительный scope — project; если архитектура требует workspace default, предусмотрен явный project override с понятным effective value.
3. Можно реализовать select/combobox: обнаруженные локальные версии/пути + ручной ввод точной версии. Не вшивай список «актуальных» Node в код. Различай установленную версию и доступную только для установки; сеть для каталога не обязательна. При выборе major/alias до запуска разреши его в точную версию и сохрани её. Невалидный ввод отклоняется понятно.
4. Отдельно показывай requested runtime, реально обнаруженный executable/version/package-manager и evidence CI. Подсказки из package.json engines, packageManager, .nvmrc/.node-version, tool configs, Docker/CI можно предлагать, но не заполнять пустое поле без выбора пользователя. Для конфликтов и CI matrix не выбирать удобную версию молча: показать источники и конкретное расхождение.
5. Пустая настройка не ломает существующий сценарий и не требует установки Node. Используй разрешённый текущим проектом runtime, но явно сообщай, где и под чем проверено; без подтверждённого совпадения не заявляй CI compatibility. Явно заданная, но недоступная версия даёт ENVIRONMENT_UNAVAILABLE/действие настройки, а не fallback на PATH.

### D3. Реальное исполнение в выбранном окружении

1. Сделай общий runtime resolver/launcher для install, precheck, authoritative verification, audit bridge и project checks, включая процессы агента, когда он запускает проверки. Сначала version probe фактического executable; PATH/CWD/env должны давать тот же Node дочерним npm/yarn/script процессам. Не достаточно добавить текст версии в prompt или env-поле без применения.
2. Учитывай Windows cmd shims, выбранный package manager и существующие runtime managers. Поддержку managers делай явно обнаруживаемой. Не меняй глобальный Node, пользовательский PATH или CI автоматически. Если runtime нужно подготовить, следуй принятой в проекте модели установки с явным действием пользователя; ручной путь должен работать без неё.
3. Runtime contract хранит requested/effective Node, версию package manager, платформу/архитектуру и параметры, значимые для проверки. Включи его в версии схем, candidate/checkpoint/verification/cache identities. Один executable path недостаточен: его содержимое/версия может измениться. Старое evidence без runtime не считать доказательством нового окружения.
4. Смена Node инвалидирует релевантную verification/cache/task evidence и требует повторной проверки от сохранённого checkpoint. Последний результат остаётся доступен как проверенный в старом окружении; не удаляй его и не продолжай старую сессию под новым runtime молча.
5. В ТЗ агенту явно передай runtime/manager, как получить его, запрет менять runtime/CI/dependency state вне согласованного scope. Если требуемая версия недоступна — INFRA_BLOCKED, а не перебор версий пакетов.

### D4. Planner и диагностика

1. Проверяй engines.node прямых target-кандидатов до дорогой материализации по semver из registry metadata (с учётом project registry), включая диапазоны с ||, минимальные minor/patch, prerelease semantics. Сопоставляй с точной effective версией, не только major. Не пиши самодельное сравнение строк.
2. Несовместимый candidate исключается только в контексте заданного runtime; planner ищет совместимую альтернативу либо явно откладывает цель, сохраняя lag-policy unmet. При отсутствии engine metadata фиксируй unknown coverage, не подтверждённую совместимость. Отсутствие engines у пакета не равно ошибке metadata.
3. Транзитивные несовместимости подтверждаются resolved graph/install evidence. Структурируй ошибку: пакет/версия, требуемый диапазон, requested/effective Node, источник metadata/команда/exit code. Сохрани checkpoint, bounded retry/alternative; не запирай UI и не создавай глобальный nogood для других Node.
4. Если target требует повысить Node, покажи два осознанных пути: сохранить окружение и искать совместимые версии либо отдельно согласовать миграцию runtime/CI. Самовольная смена CI не разрешается. Физический green под локальным Node не доказывает green на другой CI OS/Node; область evidence явно ограничена.
5. Resolutions warnings показывай отдельно: проверь источник pin и диапазоны потребителей, предложи согласованное изменение/блокер. Не удаляй resolution без понимания причины и новой проверки lock/compatibility.
6. Некорректную Git dependency URL диагностируй отдельно с безопасным отображением. Если формат можно исправить, сохрани host/repo/ref/protocol semantics, provenance и приватность; не меняй registry/source или Git ref автоматически. Warning не должен маскировать fatal engine error.

## Часть E. Обязательная матрица приёмки

Используй production adapters. Unit-тесты важны, но одной функции decision и ручного последовательного CLI недостаточно.

1. Desktop с пустым run/task → C0 → materialize → RED → выбранный агент → C1 → следующая группа → C2 без dashboard/ручных CLI-команд.
2. После C2 недостижимая группа → typed feedback/deferral → независимая группа принимается; checkpoint сохранён.
3. Kill во время verify, до/после pointer switch и на последней agent attempt; один durable dispatch, нет второй сессии/повторного acceptance, бюджет не сбрасывается.
4. Bootstrap RED → изолированный repair без смены зависимостей → новый C0 → upgrade использует исправленные bytes.
5. Missing/stale task, ошибка диска/export timeout/clipboard rejection, смена candidate при открытом preview: retry доступен, busy снят, старый scope не отправляется.
6. Старый достаточный Baseline JSON без рабочего HTML → ТЗ в Desktop без rerun; недостаточный JSON → диагностируемая ошибка. Реальный UI copy проверен, пользовательские clipboard-данные не логируются.
7. Node пустой: старые settings читаются, effective runtime указан, CI compatibility не придумана.
8. Node 20.11.0 + synthetic package с engines `^22.18.0 || >=24.11.0`: ранний понятный отказ/совместимая альтернатива; нет ignore-engines и ложного green. Отдельно unit boundaries: 22.17.x против 22.18.0, 24.10.x против 24.11.0, OR range.
9. Явно выбранный доступный Node: реальные install и nested project script выводят именно выбранную версию, даже при другом PATH Node. Synthetic fixture package без обращения к приватному registry. Недоступный runtime даёт environment blocker без изменения глобального окружения.
10. Transitive engine mismatch, unavailable metadata, resolution conflict и malformed synthetic Git URL имеют разные diagnostics; runtime change не переиспользует старую verification/constraints как актуальные.
11. CI matrix/конфликт конфигураций: не заявлять проверку всех окружений по одному локальному runtime. Проверка под совместимым Node, но другой OS имеет соответствующее ограничение evidence.
12. Независимый audit с policy/UNKNOWN/coverage и обязательными JSON/Markdown; COMPLETE только после gates. Оба developer-документа содержательные; C10 выбирается по active identity, не как строковый максимум.
13. Ограниченный real-agent pilot на изолированной копии согласованного проекта после synthetic/physical checks. Не прерывай чужой run и не запускай миграцию в обычном checkout. При недостающих provider/budget/project данных уточни их, продолжая независимую работу; недоступный pilot фиксируется как ограничение, не как PASS.

## Порядок работы и финальная сдача

Веди implementation status с таблицей R1–R10 и D1–D4: статус, commit, production path, тест/результат, ограничения. Начни с общего runtime контракта, который влияет на identities; затем закрой A, B, C и подключи runtime execution/planner/UI, сохраняя небольшие проверяемые этапы. Не откладывай интеграционный Desktop тест до конца и не объявляй задачу завершённой после scaffold/сборки.

Выполни проверки по AGENTS.md: профильные tests при итерации, финально `python run_tool_tests.py --suite all` и `--suite production-fast`, Desktop lint/build и затронутые check:* по CI, diff-check, sanitization полного изменения включая новые документы. Включи новые проверки в canonical runner/CI. Sandbox EPERM отличай от дефекта продукта. Не ослабляй проверки ради green. Отдельно зафиксируй synthetic, physical, UI/real clipboard и real-agent evidence.

Финальный ответ: SHA, как запустить основной сценарий в Desktop, как задать/оставить пустым Node, что происходит при engine mismatch, таблица закрытых R/D, команды и результаты, ссылки на acceptance/отчёты, ограничения реального pilot. После восстановления/ошибки пользователь сохраняет результат и видит следующий доступный шаг. Это критерий готовности, а не просто зелёный helper test.

Публикация релиза этим заданием не разрешается. Если пользователь отдельно разрешил публикацию, используй push-github-branch-and-tag.ps1 по AGENTS.md с проверкой всего рабочего дерева; не включай чужие изменения и не обходи validation.
