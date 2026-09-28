# Задание агенту: реализовать накопительную миграцию DepLoom

Прочитай корневой AGENTS.md и весь `docs/ITERATIVE_MIGRATION_PLAN_2026-09-29.md`. Это задание на реализацию, не на очередной аудит или переписывание плана. План исследован на ca67872; сверяй текущий HEAD и сохраняй параллельные/unrelated изменения.

## Пользовательский результат

На реальных проектах полный Baseline слишком долго ищет комбинацию и упирается в таймауты до первого полезного обновления. Требуется цикл:

`Ck → план небольшой группы → exact materialization → source/config repair агентом → независимая cumulative verification → Ck+1 → следующий шаг`.

Не нужно сначала завершать большие yellow, green и default планы. Агент участвует в каждой требующей адаптации итерации и возвращает planner структурированную обратную связь. При неудаче следующего шага пользователь сохраняет рабочий Ck и видит остаток.

## Обязательные требования

1. Один coordinator, один candidate на проект, одна выбранная policy; сначала одна атомарная cohort. Baseline оставить названием исходного замера; процесс — «Итеративная миграция».
2. Checkpoint включает source/config/manifest/lock и exact evidence. Следующий candidate строится от него, включая уже сделанные агентом правки.
3. Resolver success, слова агента, snapshot и отсутствие новых ошибок не равны PROJECT_VERIFIED. Не ослабляй gates. COMPLETE требует подтверждённой policy и независимого audit.
4. Исходный RED диагностируется отдельно. Bootstrap repair не меняет версии; исходные ошибки не становятся dependency constraints.
5. При project failure передавай candidate агенту сразу, включая diagnostic branch. Не ждать повторного tuple, конца mode или terminal полного Baseline.
6. Агент получает physically materialized точные версии в isolated trial, не старый checkout с текстовым списком targets. Dependency state immutable для агента, source/config адаптируется в разрешённом scope; ослабление checks запрещено.
7. Сохрани proof guards. Для early repair введи отдельный candidate capability, который не проходит release/project-green gates. Не подделывай полный ProofEnvelope.
8. После repair проверяй тот же exact candidate на новых source bytes. Не запускай весь Baseline заново вместо проверки исправления.
9. Feedback: REPAIRING, READY_FOR_VERIFY, NEEDS_COHORT_EXPANSION, NEEDS_ALTERNATIVE, INCONCLUSIVE, INFRA_BLOCKED. Привязка к run/candidate/base checkpoint/attempt обязательна; stale reply отвергается.
10. «Могу исправить» управляет scheduling, не proof. «Не смог»/timeout не создают вечный nogood. Companion/alternative подтверждает planner, constraint — deterministic scoped evidence. Переиспользуй dependency_compatibility_evidence.py, не доверяй prose.
11. При replan checkpoint не теряется и принятые версии не откатываются молча. Независимая группа может продолжиться после bounded deferral; deferred остаётся в целях и denominator.
12. Acceptance атомарен/идемпотентен; bytes живут независимо от trial. SessionId сохраняется до завершения агента. Resume текущей, в том числе последней, попытки не списывает новую. Один writer и budget ledger; нет вложенных бесконечных retries.
13. Durable step result — основной транспорт. stdout tail не определяет acceptance. Проверяй schemas/identities/path containment.
14. Partial результат доступен из UI и восстанавливается. Только C0 не считается полезным upgrade. Audit UNKNOWN не превращается в нули/COMPLETE.
15. Сохрани Draft и ручное построение плана без неожиданных agent runs. Новый цикл подключи к явной миграции/Автопилоту; git.push policy не расширяй.

## Порядок реализации

Веди `docs/ITERATIVE_MIGRATION_IMPLEMENTATION_STATUS.md`: этап, commit, реализованное поведение, команды/результаты проверок, открытые пункты и следующий шаг. Маленькие локальные commits допустимы; unrelated файлы не включать. Не заканчивай задачу после scaffold, документа или первой helper fixture. При прерывании сессии оставь точный handoff, не объявляй всю работу завершённой.

### A. Контракты и фундамент

Проверь существующие modules из таблицы плана. Определи versioned Run/Candidate/Checkpoint/Feedback, authority matrix, операции plan-next/materialize/verify-exact/validate-feedback. Coordinator выдели из main.ts; Python остаётся владельцем deterministic planner/verifier. Переиспользуй primitives, не вводи второй solver и независимое конкурирующее хранилище истины.

Добавь cross-language identity/schema tests и один физически materialized candidate. Укажи адаптированные existing functions. Новые API-названия из плана — предложения, не уже существующие команды.

### B. Первый вертикальный цикл

Production adapters: исходный PASS → A принят → B требует config/code repair → агент исправляет → A+B проходят → C2 сохранён → следующий plan-next получает C2. Дать opt-in Desktop route, не только helper test.

Новый execution path не ждёт полного Baseline перед первым агентом и не повторяет все modes после ремонта. Diagnostic RED не позволяет принять resolver-only candidate. Сначала продемонстрируй этот работающий срез, затем расширяй возможности.

### C. Feedback и альтернативы

Typed feedback, companion/scope revision, deterministic validation, scoped nogoods и anti-loop ledger. Покажи C rejected/inconclusive после C2, сохранение C2 и принятие независимого D. Infra/unknown/agent timeout не отравляют solver constraints.

### D. Recovery/budgets

Atomic checkpoint, lease, phase/session recovery, cancel и единый accounting. Проверь kill до/после acceptance и на последней repair attempt. Сохраняй результат, завершая только собственные процессы; переиспользуй supervisor/recovery primitives.

Defaults и adaptive cohort size из плана сделать конфигурируемыми. Deadline не сбрасывается с Python-вызовом и heartbeat. Дубликаты запросов не выполняют acceptance второй раз.

### E. Обычный UI и документы

Подключи новый путь к стандартной миграции/Autopilot. RU/EN подписи: «Исходный замер», «Планировщик обновлений», «Итеративная миграция». Внутренние keys сохраняй для совместимости где возможно. UI показывает фазу, группу, последний checkpoint, metrics/coverage и partial result без фиктивного общего процента.

Prompts требуют промежуточные итоги, lag num/den, C/H/M/L/unknown; audit через manual_dependency_audit.py и текущую policy, не bare yarn audit. Комментарии к неочевидным правкам; MIGRATION_REPORT.md и DEVELOPER_UPGRADE_GUIDE.md по фактически принятым upgrades. На stop/timeout сохранять доступные документы. Не инвалидировать незаметно proof, добавляя docs после final verification.

### F. Физическая и реальная приёмка

Выполни матрицу плана. Physical fixture запускает реальные Git/install/check commands через production adapters. Scripted agent допустим для воспроизводимых tests, но не заменяет pilot с настоящим агентом.

Pilot — изолированная копия согласованного реального проекта и явный ограниченный budget. Не прерывай чужой run и не меняй исходный checkout. Если проект/provider/допустимый budget нельзя однозначно определить из контекста, уточни недостающие данные, продолжая независимую реализацию и тесты. Недоступность runtime — BLOCKED, не «всё принято».

Создай `docs/ITERATIVE_MIGRATION_ACCEPTANCE.md` с санитизированными выводами; приватные артефакты — в ignored run workspace. Запиши first useful PROJECT_VERIFIED checkpoint, actual before/after, accepted/deferred, commands/exit codes, source/lock/policy/proof identities, recovery и оба developer-документа. Отдели synthetic, physical и real-agent evidence.

## Проверки и окончание

При разработке — узкие meaningful tests. Итог: Python production-fast и all через run_tool_tests.py; Desktop lint/build и затронутые check:* по актуальному CI. Включи новые tests в канонический runner/CI. Не ограничивайся source-text assertions и свободными функциями, которые unittest не собирает.

Соблюдай Windows editing policy: apply_patch запрещён; temp diff + git apply --check/git apply либо bounded exact-once edit с сохранением encoding/line endings. После правок — git diff --check и просмотр diff, включая untracked.

Sanitization выполнить после финальных docs/tests; проверить новые файлы, ещё не видимые tracked scan. Не публиковать private project identifiers, персональные пути и raw logs в публичной истории.

Задание разрешает реализацию, локальные проверки и смысловые commits, но само по себе не просит release. Если пользователь отдельно уже разрешил публикацию, используй push-github-branch-and-tag.ps1 согласно AGENTS.md, без ручного tag/version обхода и без SkipValidation. Release не заменяет real acceptance.

В финале: поведение обычного Desktop маршрута, implementation SHA, команды проверок, результаты physical/real pilot, ссылки на статус/приёмку и ограничения. Если выполнен только этап, так и назвать.

Критерий завершения всей доработки: первое полезное обновление появляется ДО решения всей миграции; следующий repair накапливается поверх него; неудача третьей группы и restart не уничтожают достигнутое; planner продолжает именно от сохранённого проекта; это подтверждено реальным исполнением.
