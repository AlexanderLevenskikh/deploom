# Повторная приёмка R3 — 28.09.2026

Вердикт: НЕ ПРИНЯТО целиком. Есть подтверждённые улучшения; основные ограничения накопительной миграции и автоматического repair остаются.

Проверен HEAD baf6359 вместе с текущими незакоммиченными правками. SHA256 генератора при завершении проверки: `4E8099A80D3D9589DBFE06FB5C6A400C1343D81A96947EB6ABD668D9C0E40210`. Production-код в ходе приёмки не изменялся.

## Статус предыдущих замечаний

| Замечание | Результат R3 |
|---|---|
| R1: автоматический repair | Открыто: production-потребителя запроса по-прежнему нет |
| R2: сохранение overrides | Частично: формат имён и resolver-вызов исправлены; project-вызов и следующий incumbent теряют overrides |
| R3: единицы security metrics | Частично: один уязвимый пакет теперь считается одним; resolved nodes пока ложно равны нулю |
| R4: контекст draft | Частично: registry, manager, scripts, команды и 0% исправлены; sourceCommit/runtime ещё теряются |
| R5: optional peer cohorts | Открыто: исходный контрпример всё ещё воспроизводится |
| R6: feasible/optimal | Частично: низкоуровневый статус исправлен, наружу снова выходит optimal |
| R7: composite test в Python | Закрыто на уровне discovery: test с дополнительной стадией сохраняется |
| R8: ignored checklist в CI | Исправлена зависимость: template перенесён в docs, локальное evidence необязательно. Новый template нужно включить в коммит |

Ранее исправленные all-unknown, сохранение полного scope и подавление повторных repair-проверок не регрессировали в изолированных проверках G.

## Что ещё нужно исправить

### 1. P1 — overrides теряются между resolver, project verification и checkpoint

`dependency_live_roadmap_generator.py:13433`: `_iteration_verify_config` теперь содержит incumbent overrides, и resolver получает этот config. Нормализация пар в fixed_names также исправлена.

Но полный project preflight в `:13898` всё ещё получает исходный `config`, а не `_iteration_verify_config`. Вызов `record_verified_incumbent` в `:13978` не передаёт resolver_overrides. Значение по умолчанию в `:12837` — None; новый incumbent в `:12855` сохраняет пустой набор. При финализации `:13030` прежняя карта overrides удаляется, если в новом incumbent она пуста.

Следствие: даже успешно проверенное независимое direct-обновление может потерять ранее принятый transitive override; resolver и project checks могут проверять разные materialized состояния.

Исправить передачу **одной и той же** карты overrides во все verification paths, cache/proof identity и record/checkpoint/finalization. Тест должен начинаться с incumbent с override, принять независимую когорту и проверить сохранность override после project verify, checkpoint, следующей когорты и restart. Одного теста формата fixed_names недостаточно.

### 2. P1 — repair всё ещё заканчивается файлом запроса

`dependency_live_roadmap_generator.py`, `_persist_repair_requests` и ветка `repair-required-terminal`: запрос сохраняется, режим завершается. В `desktop/electron` отсутствуют потребители `repair-requests.json`, `cohort-repair-required`, `repair-required-terminal`. Сквозное выполнение source/config repair → повторная проверка → принятие cumulative checkpoint не добавлено.

Пройденный `test_verify_after_repair_accepts_the_same_tuple` не подтверждает автоматический repair: он сразу запускает сценарий с успешной подменой verifier. Нужен настоящий обработчик запроса и двухэтапный тест с изменением source snapshot, а не независимый успешный запуск.

### 3. P2 — optional peers: исправлен другой сценарий, исходный всё ещё сломан

`dependency_live_roadmap_generator.py:7544–7590`; новый тест `D4PresentOptionalPeersBindTheMovingCohort.test_mutually_satisfiable_optional_joint_pair_stays_independent` закрепляет неправильное ожидание.

Свежий production-helper probe:

- Исходно a@1, b@1. Цель a@2, b@2.
- a@2 требует optional b@^2; b@2 требует optional a@^2.
- Closure возвращает `[a]`, `[b]`.
- Попытка a@2+b@1: PEER_CONFLICT.
- Попытка a@1+b@2: PEER_CONFLICT.
- Совместный a@2+b@2: hard-model не находит конфликта.
- После двух одиночных отказов planner не предлагает совместный вариант.

Атомарность нужна из-за несовместимости **переходного** состояния, даже когда итоговая пара совместима. Новая ветка объединяет несовместимые итоговые пары и пропускает именно требуемый случай. Исправить сравнение target одного пакета с current/verified-incumbent другого, учитывая присутствующие optional peers. Сменить ожидание нового теста на одну совместную когорту. Не ослаблять существующий hard-model ради зелёного теста.

### 4. P2 — feasible снова превращается в optimal у вызывающего кода

`dependency_live_roadmap_generator.py:9467`: после получения exact_status=feasible выполняется `diagnostics.update(status="optimal", backend="z3")`. Далее `:9664–9666` этот статус публикуется по пакетам.

Свежий probe с residual a@1.5: `_run_z3_peer_component` возвращает feasible, а `resolve_peer_compatibility(... solver_statuses_out=...)` публикует optimal для a. Передавать исходный статус через всю цепочку. SAT/физическую допустимость допустимо подтверждать отдельно, глобальную оптимальность нельзя выводить из pin.

Связанный тест C1 всё ещё ожидает старый optimal и падает. Обновить его после исправления всех consumers, добавив проверку наружного статуса, а не только return низкоуровневой функции.

### 5. P2 — фактический sourceCommit и runtime отсутствуют в draft context

`dependency_live_roadmap_generator.py:25114`: новый helper читает commit/resolvedHead/head, но снова не читает **sourceCommit**, который production source checkout действительно предоставляет. Runtime helper вообще не заполняет; manager version тоже теряется.

Свежий probe с source_checkout={sourceCommit:12345678}, packageManager=yarn@1.22.22 и engines.node=18 возвращает commit="", packageManager="yarn", без runtime. При этом registry и команды теперь передаются правильно; тест 0% проходит.

Добавить реальное sourceCommit и достоверные runtime/manager version. Если runtime не измерен, различать declared requirement и detected runtime. Тесты должны использовать production shape sourceCommit: текущая фикстура с полем commit не ловит эту ошибку.

### 6. P2 — nodes=0 по-прежнему не является измерением

`dependency_live_roadmap_generator.py:24182–24184`: при наличии scan_health записываются vulnerablePackages, сумма findings в advisories и nodes=0, хотя audit.engine=not-started.

Проверено: один пакет C:1 H:2 M:3 теперь корректно даёт vulnerablePackages=1. Эту часть закрываю. Но число resolved nodes не вычислялось: должно быть null/unknown, а не ноль. Сумма findings по строкам не гарантирует количество уникальных advisories; нужны отдельные единицы/источник или явное имя findingsOccurrences, без притворной дедупликации.

## Выполненная проверка

- 119 unittest-тестов A–F и выбранных регрессий: **118 прошли, 1 упал** — C1 ожидает optimal вместо нового feasible.
- 5 отдельных проверок: composite test/alias, policy=0, builder context и publisher context — **все прошли**. Три module-level функции запущены явно: стандартный unittest discovery их не включает.
- 5 тестов G — **все прошли в изолированном запуске**. Помимо workspace-temp адаптера подменены source snapshot, обнаружение executable и registry client для проверки управляющего цикла без реальной установки. Этот результат не является end-to-end proof или реальной миграцией.
- Итого **128 passed / 1 failed** в перечисленных тестах; independent probes учитываются отдельно.
- Electron TypeScript compile и migration verification/integration-repair script прошли.
- `git diff --check`: осталось замечание о пустой строке в конце `tests/test_audit_2026_09_27_r6_fixes.py:415`.
- Реальная миграция libjs в этой приёмке не выполнялась. Проверенный REAL_RUN_ACCEPTANCE всё ещё содержит незаполненные before/after, checkpoint, restart и first-useful-upgrade.

Материалы: `.dependency-roadmap/audit-reviews/recheck-r3-2026-09-28/`: suite.txt, targeted.txt, g-offline.txt, probes.py, probes.json, локальные harness-файлы. Они диагностические, не CI fixtures.

## Следующий пакет для агента

1. Сначала исправить сквозное сохранение overrides и переходную атомарность optional peers. Использовать именно контрпримеры выше.
2. Подключить автоматический repair к исполнителю и повторной cumulative verification; подтвердить restart.
3. Пронести feasible без переименования; дописать sourceCommit/runtime; закончить единицы метрик.
4. Получить полностью зелёный прогон, включив новые файлы в коммит, затем провести изолированный реальный libjs run с фактическим первым полезным checkpoint и итоговыми артефактами.

Не переделывать уже закрытые пункты: composite test, policy=0, передачу registry/scripts, различение all-unknown и частичного результата. Недостающая работа теперь сосредоточена в перечисленных переходах production pipeline.
