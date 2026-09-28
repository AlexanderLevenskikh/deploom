# Накопительная миграция: план реализации

Дата: 29.09.2026. Исследована реализация ca67872 (v0.2.149). Это проект поведения, а не утверждение о готовности. Имена новых API/модулей ниже — предложения. Перед реализацией сверить текущий HEAD, AGENTS.md и параллельные изменения.

## 1. Цель

Перестроить исполнение с «полный Baseline → весь agent stage» на повторяемую транзакцию:

**Последний проверенный проект → небольшой кандидат → установка → адаптация агентом → независимая проверка накопленного состояния → новый checkpoint.**

N не задаётся заранее: цикл работает до выбранной цели, остановки, исчерпания бюджета или отсутствия доступных действий. На остановке пользователь получает последний проверенный результат и список оставшегося.

Главная метрика — время до первого полезного PROJECT_VERIFIED checkpoint с реальным изменением зависимости. Resolver success, sealed source snapshot, сообщение агента и пустой шаг не считаются полезным обновлением.

Первая версия: один active candidate на проект, существующий package manager, одна выбранная target policy, последовательное принятие групп. Без переписывания solver, спекулятивных параллельных merge и нового UI с нуля. Существующие Draft/ручные CLI сохраняются; автоматическая публикация не добавляется.

## 2. Существующие части и разрывы

| Часть | Что переиспользовать / изменить |
| --- | --- |
| `block_psi_progressive_baseline.py::plan_progressive_extension` | Уже расширяет incumbent атомарной группой. Базой сделать source + exact dependency state последнего project-verified checkpoint; не требовать сначала большого глобального успешного assignment. |
| `dependency_live_roadmap_generator.py`, solve/verify loop | Diagnostic project failure может оставить resolver-result успешным. Новый execution path обязан сразу передать candidate агенту, не принимать resolver-green как checkpoint и не ждать конца yellow/green/default либо повторного repair tuple. |
| `baseline_constraint_verifier.py`, `verification_proof.py`, `source_snapshot.py`, `resolved_dependency_state.py` | Физическая установка, изоляция и identities. После source repair заново проверить исправленные bytes; старое project proof недействительно. |
| `baseline_repair_handoff.py`, Desktop `repair-dispatch.ts`, `repair-checkout.ts`, `baseline-repair-result.ts` | Exact requests, изоляция, раннее session persistence. Расширить typed feedback агента. |
| `main.ts::baselineRepairReVerificationSpec` | Сейчас повторяет Baseline с restart. Нужен verify-exact-candidate без пересчёта всех режимов и смены версий после ремонта. |
| `runMigrationAgentLoop`, `runAutonomousMigrationStage`, `runMigrationAgentIteration` в main.ts | Agent/recovery/residual/cumulative primitives подключить адаптерами к одному coordinator. Не оставлять вложенный автономный цикл со вторым budget. |
| `dependency_compatibility_evidence.py` | Candidate/control reproduction для обратного сигнала solver; сохранить scoped authority, не доверять prose. |
| `block_v_recovery.py`, `block_psi_anytime.py` | Journal, locking, accounting. Старый BestVerifiedIncumbent без project evidence нельзя автоматически повысить до нового checkpoint. |
| `desktop/electron/materialization-proof.ts`, `resolved-state-proof.ts`, Executor guards | Для раннего repair нужен отдельный ограниченный candidate capability. Не подделывать полный ProofEnvelope и не ослаблять release guards. |

Это изменение момента передачи управления и предмета доказательства, а не новый внешний цикл вокруг capture-baseline.

## 3. Термины и запуск

- **Исходный замер / Baseline**: начальный snapshot, политика, аудит и контрольные проверки.
- **Планировщик обновлений**: следующий небольшой шаг от checkpoint.
- **Итеративная миграция**: coordinator planner/materializer/agent/verifier/audit.
- **Checkpoint**: исходники, конфиги, manifest, lock и evidence одного точного cumulative состояния.
- **Resolver candidate**: устанавливаемые версии; работоспособность проекта пока не доказана.

Yellow/green — цели политики. В активном execution рассчитывать выбранную цель, не требовать три полных физических прогона. Default разрешает выбор цели, это не уровень выше green. Старые internal keys/CLI flags сохранить где возможно; прежде всего исправить UI-подписи и объяснения.

Ручное «Построить план» не запускает агента неожиданно. Явная миграция / существующий Автопилот выполняет цикл. git.push и текущие release gates сохраняют смысл.

## 4. Начальное состояние C0

Зафиксировать source revision, dirty-tree policy, manifest/lock, настройки, runtime/package manager и command policy. Работать в изолированной копии, не менять пользовательский checkout. Выполнить контрольную установку, обязательные checks и начальный аудит либо честно отметить audit UNKNOWN.

Если checks зелёные — C0 технически проверен, но это ещё не полезный upgrade. Если исходный проект RED, классифицировать infrastructure / существующий source defect / unknown. Не обучать dependency constraints на исходной поломке. Ремонтируемый дефект допускает отдельный bounded bootstrap repair без смены версий и последующую проверку.

Если зелёный C0 недостижим — BLOCKED_BASELINE с diagnostics и сохранёнными bytes. Существующий режим «нет новых регрессий» допустим только как отдельная явно выбранная политика и отдельный статус; автоматически считать его PROJECT_VERIFIED запрещено. Основная приёмка этой доработки требует зелёных обязательных gates.

## 5. Один владелец и узкие операции

Предлагаемый coordinator — отдельный Desktop `iterative-migration.ts`; main.ts остаётся runtime/IPC adapter. Python владеет deterministic planner/materializer/verifier. Агент меняет source/config выданного trial и возвращает структурированное предложение.

Предлагаемые операции CLI/API (их ещё нужно реализовать/выделить):

- `plan-next(checkpoint, policy, ledger, budget)` → candidate / bounded no-candidate / доказанная недостижимость в указанной области.
- `materialize-candidate(candidate)` → exact lock/resolved state и resolver evidence либо typed failure.
- `verify-candidate(candidate, repairedSnapshot)` → проверки того же assignment; без выбора других версий.
- `validate-feedback(candidate, proposal)` → scoped constraint, scope revision proposal либо inconclusive.
- `audit-checkpoint(checkpoint, policy)` → независимое evidence точного dependency state.

Выделять существующие primitives из generator, не копировать большой solve loop. Каждый шаг возвращает versioned durable result с identity и полными artifact refs. stdout/stderr — progress, а не единственный транспорт. Обрезка последних 6 KB вывода не должна терять handoff. Python не ждёт человеческого диалога и не управляет agent provider.

## 6. Машина состояний

| Фаза | Действие / переход |
| --- | --- |
| BASELINING | Исходная проверка → READY(C0), BOOTSTRAP_REPAIR или BLOCKED_BASELINE. |
| PLANNING | Выбрать шаг от Ck → MATERIALIZING либо terminal с Ck. |
| MATERIALIZING | Установить exact versions → PRECHECK; resolver failure → VALIDATING_FEEDBACK; infra → bounded retry. |
| PRECHECK | Screening/диагностика. Repairable RED → REPAIRING сразу. Screening PASS не заменяет итоговую verification. |
| REPAIRING | Агент адаптирует source/config, либо предлагает scope/alternative. |
| VERIFYING | Полный cumulative trial после последних правок → CHECKPOINTING либо repair/feedback/unknown. |
| VALIDATING_FEEDBACK | Проверить предложение, обновить scoped ledger → новый candidate от Ck. |
| CHECKPOINTING | Сохранить immutable bytes/proof, атомарно принять Ck+1, привязать аудит/метрики. |
| READY | Проверить политику: COMPLETE либо следующая PLANNING. |

PROJECT_VERIFIED — свойство checkpoint; COMPLETE — выполнение всей цели с аудитом. Терминалы: COMPLETE, PARTIAL_VERIFIED, STOPPED_WITH_CHECKPOINT, BLOCKED_BASELINE, BLOCKED_INFRA, NO_VERIFIED_UPGRADE. Solver timeout/no-candidate не равен UNSAT; UNSAT требует proof с явной областью.

## 7. Контракты и identities

Ниже обязательная семантика, не требование буквального написания полей. Определить schemas и Python↔Node tests прежде UI.

**RunState:** schemaVersion, runId, workspaceId, projectId, generation, targetPolicyHash, initialSnapshotKey, activeCheckpointId, activeCandidateId, phase, toolEpochs, budgetLedger, leaseOwner. Один writer на проект; legacy Autopilot и новый coordinator не могут одновременно менять cumulative checkout.

**Checkpoint:** checkpointId, parentCheckpointId, sourceSnapshotKey/locator, manifestHash, lockfileHash, resolvedStateKey, full assignment/overrides, environmentKey, commandSetHash/verificationPolicyHash, projectProofRefs, auditEvidenceRefs/status, acceptedDelta. Сохранить source/config bytes независимо от disposable trial. Родитель неизменяем.

**Candidate:** candidateId, runId, baseCheckpointId, policyHash, cohortId, fullAssignment, delta, atomicClosure, allowedSourceScope, resolverContextKey, materializationRefs, stage, attemptId, agentSessionId, feedbackRevision, budgetConsumed. Full assignment включает принятые версии Ck; transitive changes отражены в canonical lock/evidence. Агент не меняет manifests/lock/overrides вне разрешённого действия контроллера.

Отдельный **CandidateMaterializationEnvelope** даёт право ремонтировать source/config на exact dependency state, но НЕ удовлетворяет ProjectProofEnvelope/release/publish gate. Это устраняет требование «сначала project-green, иначе агент не запускается» без подделки authority.

**AgentFeedback:** schemaVersion, run/candidate/baseCheckpoint/attempt IDs, observedSnapshotKey, kind, reason, changedFiles, diagnosticsRefs, proposedScope/constraints. Проверять schema, identity, path containment. Stale/чужой/дублированный terminal не меняет checkpoint.

| kind | Значение |
| --- | --- |
| REPAIRING | Промежуточная оценка ремонтируемости/heartbeat; не proof и не продление deadline. |
| READY_FOR_VERIFY | Правки готовы; verifier проверяет самостоятельно. |
| NEEDS_COHORT_EXPANSION | Конкретный companion и основание; planner проверяет pins/scope и создаёт новую revision. |
| NEEDS_ALTERNATIVE | Предложение версии/ограничения; solver authority возникает только после deterministic validation. |
| INCONCLUSIVE | Не удалось доказать; bounded deferral, не вечный nogood. |
| INFRA_BLOCKED | Инструменты/доступ/ресурсы; не менять версии ради инфраструктурной ошибки. |

Не требовать отдельной платной «оценочной» сессии агента: REPAIRING можно сообщить внутри обычной repair-сессии.

## 8. Малые группы и обратная связь

Первый candidate — одна минимальная полезная атомарная группа поверх C0. Не ждать решения всех желаемых обновлений. В минимальном вертикальном срезе размер всегда один; далее после двух успешных итераций можно пробовать две, затем до четырёх независимых групп. После неудачи уменьшать. Это настраиваемая эвристика для пилота, не обещание времени.

Считать атомарные группы, а не жёстко ограничивать число пакетов: peer/compatibility closure может быть большой. Не разрезать обязательную компоненту ради лимита. Для большой компоненты — bounded search и явное объяснение, независимые группы могут продолжаться.

Приоритет: user constraints и Critical/High remediation, полезные предпосылки следующих шагов, затем lag benefit с меньшей стоимостью. Не оптимизировать только количество обновлений. Deferred остаётся в целях и health denominator.

Принятые версии зафиксированы по умолчанию. Если следующий шаг требует их пересмотра, оформить новый candidate revision от Ck; проверить весь trial, не теряя Ck до успешного принятия. Агент не может незаметно изменить immutable candidate.

Для NEEDS_COHORT_EXPANSION проверить companion, package scope, pins и budget; создать новую identity. Перенос полезного patch из прежнего trial — явная операция с conflict checking и полной повторной verification.

Чистая resolver несовместимость даёт constraint только в пределах доказательства. Project API/config RED сначала направляется на repair. Structural signature сама по себе не доказывает универсальную несовместимость версии.

Для NEEDS_ALTERNATIVE переиспользовать candidate/control reproduction. Если контроль невалиден (например, исправленный под новый API код закономерно несовместим со старым API), оба варианта красные или результат нестабилен — inconclusive, а не глобальный nogood.

Ledger индексировать по checkpoint + assignment + source/environment/command identities. Agent failure и timeout временно откладывают trial, но не навсегда запрещают пакет. Повтор того же candidate без изменения причины ограничить.

## 9. Проверка, аудит, acceptance

PRECHECK может остановиться на первой содержательной ошибке, раньше передав управление агенту. Итоговый VERIFYING должен проверить весь cumulative trial всеми обязательными командами после последней правки. Независимо успешные группы не доказывают успешность их объединения.

Зафиксировать command policy: легитимная адаптация config разрешена, удаление тестов, ослабление assertions и пустая замена script — нет. Изменение проверочных конфигов сохраняется в evidence и проходит действующие guards. Список gates не брать бесконтрольно из изменённого агентом manifest.

После source/config edit старый project proof недействителен. Resolver/prepared cache reuse допустим по совпадающим dependency/runtime/lifecycle keys; изменение install scripts может инвалидировать reuse. Проверять соответствующую identity, а не только версии пакетов.

Протокол принятия: immutable snapshot/proof → проверка hashes/доступности → durable prepared record → атомарный active pointer с expected parent/generation → durable accepted event. Recovery идемпотентно заканчивает транзакцию. Trial можно удалить только после сохранения доступного нового checkpoint. Нельзя удалить единственную копию repair source и оставить ссылку на неё в proof.

Независимый аудит — через manual_dependency_audit.py по exact dependency state и текущей политике. При недоступном evidence код можно сохранить как PROJECT_VERIFIED с audit=UNKNOWN; это не POLICY_SATISFIED/COMPLETE и не разрешение release. Соблюдать действующую fail-closed acceptance policy, не ослаблять её ради продвижения. Known policy regression не скрывать хорошим счётчиком direct upgrades.

Before/after — исходное состояние → текущий checkpoint, отдельно delta последнего шага. Не создавать «before» из только что полученного «after». Метрики: C/H/M/L, unknown/coverage, lag num/den/threshold. Deferred не исчезает из знаменателя.

## 10. Restart, stop, budgets

- SessionId сохранять при появлении, до завершения агента. Resume только по совпадающим provider/model/scope/snapshot identities; завершённая и прерванная сессии различаются.
- Восстановление учитывает planning, running-agent, verification-pending, checkpoint-pending. Resume не списывает текущую попытку второй раз, включая последнюю разрешённую.
- Один run budget + step/repair/infra budgets. Все тяжёлые операции получают remaining deadline и существующие hard timeouts. Новый CLI вызов, feedback и heartbeat не сбрасывают deadline.
- Резервировать время на materialize/repair/verify по измеренной стоимости: planning не должен потратить весь run до первой проверки. При недостатке бюджета сохранить честный partial, не начинать заведомо незавершаемый шаг.
- Стартовые defaults: одна cohort, до двух завершённых repair attempts на candidate revision, до двух retries одинаковой transient infra ошибки. Общий budget наследовать от настроек пользователя. Всё конфигурируемо и durable; не увеличивать лимиты скрыто.
- Исключить перемножение внутреннего и внешнего retry loops. Одинаковая ошибка без новых inputs — повод сменить действие/отложить trial, а не повторить весь Baseline.
- Cancel завершает собственное дерево процессов и сохраняет checkpoint и resumable trial. Не применять destructive reset к пользовательскому checkout.
- Если сохранён только C0, сообщить «проверенное обновление ещё не получено». При C1+ вернуть полезный partial без требования завершить глобальный план.

## 11. UI и документы

Показать цель и фазы: «Исходная проверка → Выбор шага → Обновление и адаптация → Проверка → Сохранено». Для итерации — группа, суммарно принятые изменения, последний checkpoint, remaining/deferred, бюджет и причина ожидания.

Не рисовать фиктивный процент всей миграции при неизвестном числе будущих кандидатов. Разделить lag/coverage проценты, число принятых изменений и progress текущей операции. REPAIRING не означает «проверено», PARTIAL_VERIFIED не означает «всё завершено».

После каждого checkpoint сохранять machine ledger и сводку. На finish/stop/timeout выдавать документы по принятым изменениям:

- MIGRATION_REPORT.md: old/new, source/config adaptation, commands/exit codes, audit before/after, declined/deferred и причины, ссылки на checkpoint/evidence.
- DEVELOPER_UPGRADE_GUIDE.md: доступные после реальных upgrades возможности, breaking changes, runtime/toolchain ограничения, официальные migration references. Нет upgrades — написать это. Агент недоступен — обозначить недостающую содержательную часть, не выдумывать её.
- Комментарии к неочевидным source/config правкам. JSON/lockfiles объяснять через file/key references в отчёте, не вставлять невалидные комментарии.

Документы сначала хранить в run artifacts вне immutable source snapshot. Если включать их в deliverable Git tree, получить новую source identity и выполнить требуемую для неё verification; old proof не должен ссылаться на другую итоговую ревизию. Retention логов/отчётов не зависит от удаления trial.

## 12. Этапы реализации

| Этап | Изменение | Условие завершения |
| --- | --- | --- |
| A. Контракты | Инвентаризация APIs, schemas, authority matrix, exact verification API, тонкие adapters. | Python↔Node round-trip, rejection stale identities, один исполняемый materialized candidate. Одним дизайн-документом этап не закрывать. |
| B. Вертикальный цикл | C0 → A → C1 → B с config repair → C2; opt-in Desktop route. | A+B физически проходят, B строится от C1; после repair нет пересчёта трёх modes. Первая демонстрация пользователю. |
| C. Обратная связь | Typed feedback, revisions, companion expansion, bounded alternatives, scoped evidence. | Неподходящий C не ломает C2; независимый D принят; timeout/prose не создают глобальный nogood. |
| D. Durable lifecycle | Atomic acceptance, session/phase recovery, budgets, lease/cancel/retention. | Kill в каждой фазе без потери checkpoint, двойного списания/acceptance или второго writer. |
| E. Продуктовый маршрут | Обычные Autopilot/ручная миграция, selected target, RU/EN UI, partial export, документы. | Стандартный маршрут не ждёт full legacy Baseline перед первым агентом. |
| F. Реальная приёмка | Изолированный реальный проект, настоящий provider/materializer/verifier/audit, bounded pilot. | First useful checkpoint, следующий repair и restart доказаны, результат доступен, документы существуют. |

A→B→C последовательно. D до rollout, E/F не заменяются helper tests. Маленькие смысловые commits. Оценку длительности уточнить после B: главная неопределённость — proof guards и materialization, а не UI. Не обещать точные часы до первого вертикального среза.

## 13. Приёмочная матрица

1. Исходный PASS; A обновляется без source repair → C1 с новыми версиями, lock и полными checks PASS.
2. B требует config adaptation: тот же assignment RED до ремонта и GREEN после; C2 содержит A и source fix B.
3. C отвергнут/inconclusive → C2 доступен; D даёт C3; C остаётся в остатке и отчёте.
4. A и B отдельно PASS, вместе RED → cumulative acceptance запрещён, прежний checkpoint сохранён.
5. Companion proposal → проверенная scope revision, не мутация старого candidate; полный verify новой группы.
6. Агент меняет lock/pin вне scope либо ослабляет check → guard отклоняет, checkpoint неизменен.
7. Diagnostic/off project checks → resolver-only не принимается. Diagnostic RED сразу идёт на repair; execution verification не отключена даже при planning checks=off.
8. Нет provider/auth/runtime → typed blocker/partial; нет фиктивного PASS или бесконечного retry.
9. Kill после sessionId, на последней попытке, во время verify, до/после active pointer update → идемпотентное восстановление, верный budget и accepted ID.
10. Policy/source/environment/commands изменились → reuse инвалидируется в нужной области, артефакты сохранены. Stale agent reply отвергнут.
11. Длинный stdout, обрезанный tail, exit=0 без корректного durable result → acceptance запрещён.
12. Исходный RED → нет ложного green C0 и learned dependency constraints; bootstrap repair отдельный.
13. Timeout до upgrade и после C2 → разные статусы, checkpoint восстанавливается с диска.
14. Registry/network failure → bounded retry, нет nogood; достигнутое сохранено.
15. Settings, Windows short/long paths, очистка trial → один durable state; accepted bytes и документы не исчезают.
16. Policy не достигнута / audit UNKNOWN → partial code checkpoint допустим, COMPLETE/release запрещены; before/after честны.
17. Production Desktop adapter проходит весь маршрут: DI дополняется физическим Git/install/check test со scripted agent и отдельным pilot с реальным агентом.

Fixtures должны иметь настоящие install/check commands и source adaptation. Не ограничиваться проверками строк исходников, искусственным удалением handoff JSON и mock verifier, который всегда PASS.

## 14. Метрики реального пилота

Сравнивать одинаковые source revision, manifest/lock, environment, registry conditions, policy и budget. Записать tool SHA, запуск, run/candidate/checkpoint identities, полные diagnostics. Не обещать заранее проценты успеха или минуты.

Измерять time-to-first-candidate, time-to-first-PROJECT_VERIFIED-upgrade, время materialize/agent/verify/audit, accepted/attempted/deferred, policy gain, повторы без изменения inputs, число full-plan recomputations, recovery latency/repeated work и пиковый диск.

Обязательный порядок evidence: candidate A → repair/verify A → C1 → candidate B. C1 появился до глобального исчерпания целей и остался после последующей неудачи.

Недоступный project/provider — BLOCKED с причиной, не synthetic pass вместо real acceptance. Не прерывать чужой активный run ради пилота. Приватные логи сохранять в ignored workspace, публичные результаты санитизировать.

## 15. Совместимость и rollout

Новый формат имеет schemaVersion. Legacy resolver incumbent без project proof — только seed-предложение со свежей проверкой, не checkpoint. Legacy state не удалять; несовместимое continuation объяснять и сохранять артефакты.

Feature flag допустим на A–D; к E новый цикл должен быть доступен из обычного продукта. Release не закрывает F. При разрешённой публикации — скрипт из AGENTS.md без обхода checks; Python suites выполнять отдельно.

Итог: хотя бы одно полезное, воспроизводимое проверенное обновление реального проекта получается до решения всей миграции; последующее планирование идёт от него, а failure/restart не уничтожают достигнутое.
