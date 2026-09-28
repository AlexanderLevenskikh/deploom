# Повторная приёмка deep rescue — 28.09.2026

Вердикт: частичные исправления подтверждены, полная приёмка НЕ ПРОЙДЕНА.

Проверен HEAD baf6359 (v0.2.143) и незакоммиченные изменения в генераторе, Desktop gates и тестах A–G. Это повторная проверка после передачи первых замечаний. Исходный запрос относится к libjs; переименование локальных фикстур/артефактов в tsapp само по себе не подтверждает приёмку libjs.

## Что уже исправлено

1. **Полностью unknown больше не объявляется доказанным успехом.** Независимый запуск настоящего `resolve_peer_compatibility_with_verification` с подменой результата solver возвращает пустой набор подтверждённых назначений; физических проверок нет и ложного SAT_PROVEN нет.
2. **Частично решённая задача сохраняет полный scope и цель.** В независимом сценарии сначала проверяется small-c@2 с big-a/b@1. Затем, без повторной глобальной оптимизации, проверяется совместная когорта big-a/b@2. Только после второй успешной проверки появляется VERIFIED_TARGET_COMPLETE. Первый результат — VERIFIED_GOOD_ENOUGH / SOLVER_UNKNOWN, unresolved-пакеты перечислены.
3. **Повторяемый repair-кандидат больше не проверяется до safety limit.** Сценарий с успешным resolver и падающей project-командой даёт одну resolver-проверку и одну project-проверку; следующие два предложения того же кандидата пропускаются. Это исправляет повторные проверки, но ещё не реализует автоматический repair.
4. **Стартовые severity/lag-метрики берутся из scan rows.** На одном пакете с C:1 H:2 журнал теперь содержит C=1, H=2 и lag=1/1, 100%, совпадая с планом. Прежняя ошибка с обнулением известных findings в штатном publisher устранена. Единицы securityCounts ещё неверны — см. R3.
5. Инструкции draft о промежуточных отчётах, комментариях к изменениям и двух developer-документах сохранены. Composite test в Desktop gates сохраняется.

Эти сценарии проверяют реальный управляющий код с подменой solver/physical verifier/окружения. Они НЕ являются доказательством реальной установки или успешной миграции libjs.

## Оставшиеся замечания

### R1 — P1: durable repair request не подключён к исполнителю

Код: `dependency_live_roadmap_generator.py:10218`, `:13390–13439`; `baseline_repair_handoff.py`.

Теперь сохраняется `repair-requests.json`, эмитируется REPAIR_REQUIRED и выполняется `break`. В Desktop Electron нет потребителя этого файла/события, который запускает source/config repair и возвращает исправленный cumulative snapshot на проверку. `RepairHandoffStore.resolve()` по-прежнему не вызывается из production orchestration.

Воспроизводимый результат: повторная проверка действительно прекращается, но функция возвращает пустой verified result. Миграция не проходит путь candidate → repair → verify → accept.

Тест `test_verify_after_repair_accepts_the_same_tuple` не моделирует этот переход: он сразу запускает новый вызов с успешной подменой verifier. В тесте нет первого провалившегося запуска, изменения исходников, потребления repair request и связи двух состояний.

**Исправление:** подключить запрос к реальному Desktop Executor; передавать source/lock/assignment/override identity и команды; сохранять repair-состояние; после исправления проверять cumulative snapshot и лишь затем принимать checkpoint. При невозможности repair сохранять лучший подтверждённый результат и продолжать независимые когорты. Не ограничиваться JSON и комментариями о будущем потребителе.

**Приёмочный тест:** реальный обработчик получает repair-запрос, применяет минимальную правку фикстуры, повторно проверяет её и сохраняет checkpoint. Второй запуск после restart использует корректное состояние; один и тот же сломанный tuple не зацикливается.

### R2 — P1: resolver overrides не закрепляются в следующей когорте

Код: `dependency_live_roadmap_generator.py:12922`; `block_psi_progressive_baseline.py:95`.

`incumbent.resolver_overrides` хранит пары `(name, version)`, но передаётся как `fixed_names=tuple(...)`. Получатель ожидает имена и превращает пару в строку. В результате pin имени `a` отсутствует.

Повторный probe с production-форматом `(('a','1'),)` выбирает a@2 при incumbent a@1. Это нарушает обещание сохранять override при накопительной миграции. Кроме того, нужно проверить перенос самой карты overrides в обычный `verify_assignment` и следующий `record_verified_incumbent`: закрепление имени не заменяет передачу resolver state.

**Исправление/тест:** отдельно сохранять прямое assignment и resolver overrides; передавать имена в нужном формате, а карту overrides — в resolver и checkpoint. Начать с физически проверенного incumbent с transitive override, принять независимое direct-обновление и проверить, что override сохранился в materialized manifest, verifier arguments и восстановленном checkpoint.

### R3 — P2: количество уязвимых пакетов равно количеству findings

Код: `dependency_live_roadmap_generator.py:24162`; тест `PartGInitialMetricsFromScan` в `tests/test_audit_2026_09_27_deep_rescue_part_g.py`.

`vulnerablePackages = critical + high + moderate + low`. Один пакет с C:1 H:2 M:3 становится шестью уязвимыми пакетами. Новый тест прямо закрепляет ошибочное значение 6 при двух строках, из которых уязвима одна. `advisories` и `nodes` остаются нулевыми при не начавшемся аудите; отсутствие измерения нельзя показывать как измеренный ноль.

**Исправление:** считать уникальные уязвимые пакеты отдельно от findings/advisories/nodes; неизвестные измерения представлять явно как unknown/null с источником и coverage. Не выдавать агрегат direct scan за resolved-tree audit. Добавить проверку: один уязвимый пакет, шесть findings, audit ещё не выполнен.

### R4 — P2: фактический draft не получает execution context

Код: `dependency_live_roadmap_generator.py:25031–25046`, вызовы `:25665`, `:26589`; порог `:24248`.

Верхнеуровневые вызовы `publish_draft_result` не передают execution_context. Fallback пытается читать отсутствующее поле `ProjectSpec.package_manager` и ищет commit/head вместо реального `sourceCommit`. Registry/runtime/команды туда не попадают.

Повторная публикация с packageManager=yarn@1.22.22, sourceCommit=12345678 и scripts даёт:

- commit —;
- Package manager —; runtime —; registry —;
- команды проверки «не переданы»;
- заданный minLagOkPct=0 в заголовке превращается в 80% из-за `or "80"`.

Это снижает воспроизводимость аудита и может оставить агенту команду без корпоративного registry.

**Исправление/тест:** собрать контекст на настоящем входе pipeline, нормализовать sourceCommit, package manager/runtime, registry без credentials, команды и input hashes. Проверять результат реального publisher, не только `build_draft_prompt` с вручную заполненным ctx. Проверить 0 как допустимое числовое значение.

### R5 — P2: optional peer, который уже установлен, теряет атомарность

Код: `dependency_live_roadmap_generator.py:7540–7560` (не изменён в повторной правке).

Closure пропускает optional peers целиком. Optional разрешает отсутствие пакета; если он присутствует, его версия всё ещё должна быть совместима. Существующий hard-model проверяет такой peer.

Сценарий предыдущей проверки остаётся применим к неизменённому коду: a@2 требует optional b@^2, b@2 требует optional a@^2; исходно оба @1. Closure отдаёт две одиночные когорты. Каждая конфликтует, хотя совместный a@2+b@2 допустим.

**Исправление/тест:** учитывать present optional peers; отсутствующие необязательные peers не добавлять искусственно. Строить delta относительно текущего verified incumbent, а не всегда исходного row.current_version. Проверить совместное обновление и следующий шаг после уже принятой когорты.

### R6 — P2: pinned feasible всё ещё помечается optimal

Код: `dependency_live_roadmap_generator.py:8163` (не изменён).

Проверка `assignment_issue` доказывает допустимость заданной точки, а отчёт возвращает `status="optimal"`. Прежний probe показал residual a@1.5 с этим статусом при настоящем optimum a@2 в том же домене. Флаги pinned/proof не делают статус optimal корректным.

**Исправление/тест:** отдельный feasible/pinned статус, согласованный с consumers; оптимальность только после её доказательства. Не направить новый статус в ветку unresolved без физической проверки готового кандидата.

### R7 — P2: Python verification теряет test:build

Код: `baseline_constraint_verifier.py:579–580`.

Desktop исправлен, Python discovery нет: если существуют test:unit и test, последний удаляется без проверки содержимого. Повторный probe для `test = yarn test:unit && yarn test:build` возвращает только `yarn test:unit`.

**Исправление/тест:** одинаковое правило для обоих исполнителей — удалять только точный дублирующий alias. Проверять фактическое выполнение дополнительной стадии.

### R8 — P2: тест приёмки зависит от ignored локального файла

Код: `tests/test_audit_2026_09_27_deep_rescue_part_f.py:177–184`.

Путь заменён с libjs на tsapp, но проблема осталась: тест требует `.dependency-roadmap/audit-reviews/tsapp-deep-2026-09-27/REAL_RUN_ACCEPTANCE.md`, который игнорируется Git. На чистом checkout этот тест не воспроизводится. Содержимое checklist по-прежнему состоит из незаполненных пунктов, а наличие строк не подтверждает real run.

**Исправление/тест:** tracked fixture/spec либо генерация из tracked источника; CI должен проходить без личной `.dependency-roadmap`. Реальный acceptance evidence хранить отдельно от unit-test предусловий.

## Границы проверки

- Повторно прошли 47 тестов A–F с адаптером создания временных директорий внутри workspace. Адаптер заменяет только tempfile.mkdtemp: Python 3.14 mode 0700 создаёт недоступные каталоги в данном Windows sandbox.
- Обычный запуск G упирается в PermissionError tempfile; запуск с адаптером оказался зависим от подготовки SourceSnapshot и был остановлен. Дополнительный изолированный прогон с подменой source/registry завершился четырьмя падениями из-за чтения stream events из файла последнего состояния. Во время проверки другой агент изменил G, заменив этот reader на перехват eprint. Текущая новая версия G здесь не сертифицирована; ошибки прежнего reader не приписываются исправленному production-циклу.
- Независимый acceptance_probe повторён на свежих изменениях: partial, all_unknown, repair, actual publisher, override tuple и Python command discovery. Solver/verifier и source capture подменены; реальные пакеты не устанавливались.
- Дополнительно прошли 64 теста progressive baseline, peer model, proof handoff и draft baseline contract. Итого 111 успешно пройденных тестов A–F и выбранных регрессий в этой повторной проверке. Независимый probe не входит в этот счётчик.
- Electron TypeScript compile и `check-migration-verification.mjs` прошли.
- `git diff --check` прошёл.
- Реальную миграцию libjs в этой проверке не запускал. В проверенном REAL_RUN_ACCEPTANCE нет заполненных time-to-first-checkpoint, actual before/after, restart и first-useful-upgrade. Обещание работоспособности на реальном проекте пока не подтверждено.

Локальные воспроизводимые материалы: `.dependency-roadmap/audit-reviews/recheck-2026-09-28/` — acceptance_probe.py, probe-results.json, af-tests.txt, тестовые publisher-артефакты. Это локальные диагностические материалы, не переносимые CI fixtures.

## Что передать агенту дальше

1. **Сохранить подтверждённые исправления unknown/partial и suppression повторной проверки.** Добавить к ним production-path тесты, которые не зависят от глобального source checkout и не требуют реального registry.
2. **Закрыть R2, R5 и R7:** сохранение overrides, корректная атомарность, одинаковая полнота команд. Без этого нельзя доверять накоплению когорт.
3. **Закрыть R1 сквозным сценарием:** запрос → Executor → правка → cumulative verify → checkpoint → restart. Только после этого назвать automatic repair готовым.
4. **Закрыть R3/R4/R6/R8:** честные единицы, фактический prompt context, feasible vs optimal, воспроизводимый CI.
5. **Провести изолированный real libjs run** на фиксированных source/lock/settings. Сохранить первый ненулевой полезный checkpoint, фактические метрики до/после, команды/exit codes, repair/accepted/deferred когорты и восстановление после остановки. Приложить оба developer-документа и доказательства их соответствия принятым изменениям.

Полная приёмка: реальная миграция должна последовательно сохранять подтверждённые улучшения, переживать сложную/unknown компоненту и repair без потери результата, а отчёты должны точно объяснять достигнутое и оставшиеся ограничения. Одних зелёных unit tests и нового repair JSON недостаточно.
