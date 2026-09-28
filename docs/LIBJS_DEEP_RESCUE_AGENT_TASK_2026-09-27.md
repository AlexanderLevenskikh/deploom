# Задание агенту: рабочая накопительная миграция DepLoom и полноценный Draft

Реализуй задание по последовательным частям A–F. Сначала прочитай соседний `ЦЕЛЕВОГО_ПРОЕКТА_DEEP_AUDIT_2026-09-27.md` и проверь актуальность ссылок относительно текущего HEAD. Отчёт основан на `715c9c29e583f98db675aea2420da4ae92d79e41`, VERSION 0.2.142; не откатывай последующие пользовательские изменения.

Цель: на реальном проекте получить проверенный небольшой результат, накопительно улучшать его, переживать unknown/timeout/плохую когорту и давать агенту весь контекст для миграции кода. Глобальный оптимум не должен быть предварительным условием первой практической попытки. Выполнение задания не считается завершённым после улучшения сообщения об ошибке или добавления новых маркеров в source.

Соблюдай Windows AGENTS.md: без apply_patch, без удаления пользовательских изменений; bounded edits или проверенный git apply; diff --check и узкая валидация. Внешние проекты и исходные артефакты используй для чтения; реальные installs/repair выполняй в предназначенном для теста изолированном checkout внутри разрешённого workspace. Не меняй registry, Git refs и lockfile исходного ЦЕЛЕВОГО_ПРОЕКТА ради эксперимента. Не публикуй приватные исходники/metadata в публичный fixture.

Общий критерий: proof остаётся строгим; поиск может быть незавершённым; полезный проверенный результат не теряется. Заявление «цель достигнута» требует актуальных install/check/security/lag evidence для одного cumulative snapshot.

## Часть A. Зафиксировать воспроизведение и наблюдаемое поведение

**Входные материалы**

- Лог: `C:/Users/user/Desktop/projectbase/dependency-roadmap-template/.dependency-roadmap/artifacts/runs/f3d9ce6c-d255-4783-9894-fea15b976057/activity.log`.
- Диагностика: `C:/Users/user/.deploom/diagnostics/deploom-failure-20260927T025830Z-43888.json`.
- Intent: `.dependency-roadmap/desktop/baseline-intent/ЦЕЛЕВОГО_ПРОЕКТА-ab016216ea9e.json` в workspace template.
- Исходный проект: `C:/Users/user/Desktop/projectbase/ЦЕЛЕВОГО_ПРОЕКТА`.
- Локальные probes этого аудита: `.dependency-roadmap/audit-reviews/ЦЕЛЕВОГО_ПРОЕКТА-deep-2026-09-27/`.

Собери fixture/replay без live registry там, где возможно: package/version domains, constraints/requirements, frozen policy, runtime/manager, source/lock identities. Исходный лог содержит размеры, но не полный IR; не объявляй синтетические 53 пакета точным replay ЦЕЛЕВОГО_ПРОЕКТА. Если IR не сохранён, экспортируй его при контролируемом повторении или честно используй отдельные synthetic и real сценарии. Закрытые данные оставь локально; для CI подготовь обезличенный эквивалент проблемной структуры.

Добавь поведенческие тесты production selector/orchestrator:

1. Первая большая компонента возвращает unknown, независимая маленькая доступна: поиск обязан дойти до проверки малого кандидата.
2. В очереди уже есть fallback, новый global solver недоступен: queued кандидат проверяется без предварительного solve.
3. Первый безопасный результат найден, следующая когорта не прошла: результат сохранён, следующая независимая когорта рассматривается.
4. Interrupted run: продолжение восстанавливает правильный checkpoint; изменившийся source/lock/policy инвалидирует старую authority.

Моки допустимы для unknown, таймера и сетевого отказа, но selector/state transitions должны быть production. Source-string assertions не заменяют эти тесты.

**Готово, когда:** есть повторяемый failing test для барьера ЦЕЛЕВОГО_ПРОЕКТА, baseline замеров и список конкретных переходов, которые предстоит изменить. Сохрани run IDs, время scan/solve/install/check, размер domain, число попыток, time-to-first-materialized и time-to-first-project-verified.

## Часть B. Улучшить Draft как самостоятельный полезный результат

Эта часть независима от solver и должна быть первой пользовательски полезной поставкой.

**Где работать:** `build_draft_prompt`, сборка plan/result/policy snapshot, `desktop/electron/draft-artifact-reader.ts`, путь Desktop чтения/copy/export/send prompt. Расширенный Dashboard prompt изучи как источник уже существующих контрактов; общие элементы вынеси в небольшие shared builders, не копируй всё JS-тело в Python.

### B1. Контекст и порядок действий

Генерируй RU и EN prompt с:

- точными project path, artifact root, tool path, source snapshot/branch/commit, package manager/version, runtime, registry без credentials;
- политикой yellow/green, lagMonths/minLagOkPct и C/H/M/L limits; не теряй допустимое значение 0 через `or default`;
- commands discovery и результатом проверки доступности команд; для ЦЕЛЕВОГО_ПРОЕКТА учитывать отдельный `test:build`, входящий в `test`;
- алгоритмом: измерить исходное → выбрать ограниченную когорту → install → repair кода/конфига → checks → пересчитать actual metrics → checkpoint → следующий шаг;
- продолжением с последнего cumulative результата, а не с первоначального package.json;
- `NOT_VERIFIED / PLANNING_ONLY` до фактической верификации и ссылками на evidence для последующих утверждений.

Временный search-deferred можно пересматривать внутри разрешённого migration scope. Явный user-excluded/keep-current запрещено обходить. Добавление нового прямого package, удаление или смена resolver override требует явной записи изменения плана и применимой проверки scope; отсутствие optional peer нельзя автоматически считать поводом для установки.

### B2. Промежуточные итоги

Prompt требует короткий отчёт после исходного измерения, после каждой принятой когорты, после значимого blocker, при остановке и в финале. Во время длинной операции — стадия и elapsed, без выдуманного процента завершения.

Формат, например:

`Шаг 3: ESLint/tooling; принято 2/7 запланированных когорт; actual Lag OK 72/110 = 65.5% (цель 80%); C/H/M/L/U = …; source = OSV/direct, coverage = …; canonical audit = …; checks = …; checkpoint = …; следующий шаг = …`.

Это пример формата, не данные ЦЕЛЕВОГО_ПРОЕКТА. Числа брать только из evidence. Отделять три величины: актуальность библиотек, покрытие исследования и долю выполненного текущего плана. Если состав плана изменился, показывать смену знаменателя. Не считать отложенный пакет успешно обновлённым, не улучшать проценты исключением неудобных строк. Строго различать actual и projected; unknown/stale сохранять явно.

Добавь машинный `migration-progress.json` и читаемый журнал. Для каждой точки: timestamp, source/manifest/lock hashes, policyHash, accepted cohort IDs, actual health, audit engine/accuracy/coverage, commands/exits, checkpoint identity, remaining/deferred. Security counts имеют явную единицу: packages / advisories / nodes. Изменённые definitions/denominators нельзя тихо сравнивать.

### B3. Конкретный аудит вместо «OSV/outdated»

Prompt должен содержать реально доступную команду штатного helper, с безопасным quoting путей под используемую shell. Пример PowerShell (генератор обязан подставить реальные значения вместо плейсхолдеров):

```powershell
python "<tool-root>/manual_dependency_audit.py" `
  --project-dir "<candidate-project>" `
  --project-name "ЦЕЛЕВОГО_ПРОЕКТА" `
  --registry "<configured-registry>" `
  --target-level yellow `
  --lag-months 12 `
  --min-lag-ok-pct 80 `
  --max-known-high 1 `
  --yarn-audit-engine auto `
  --audit-workspace "<run-artifacts>/audit-step-003" `
  --json-out "<run-artifacts>/audit-step-003.json" `
  --md-out "<run-artifacts>/audit-step-003.md"
```

Если есть per-package policy, передавай соответствующий dashboard-state/snapshot. Подставляй policy пользователя, а не константы из примера. Проверяй поддерживаемые helper flags; если нужный threshold не выражается CLI, это нужно явно поддержать/проверить, а не скрывать расхождение.

Объясни непосредственно в prompt:

- Не заменять helper прямым `yarn audit`.
- Yarn `auto` сейчас означает isolated npm-lock-bridge; root yarn.lock остаётся каноническим, package-lock не попадает в исходный проект.
- `accuracy=npm-resolved-approximation` — сверка по другому разрешённому графу, а не точное доказательство безопасности Yarn-графа.
- Для строгой canonical проверки доступен `--yarn-audit-engine yarn-inventory`; неполное inventory/registry response означает unknown, не zero.
- OSV/direct, canonical inventory и npm bridge выводить раздельно. До/после сравнивать один engine и одну единицу подсчёта; хранить время/advisory source, coverage, raw evidence.
- Exit code audit интерпретировать по структурированному результату: findings и failure получения данных — разные исходы.

Сделай mode/accuracy явными в state и итогах. Не меняй default engine глобально незаметно; если исправляешь acceptance bridge, покрой переход совместимости тестами. Approximate complete/trusted нельзя использовать как canonical pass без соответствующего evidence. Аудит запускается как часть явно заданного agent workflow, не скрытым побочным эффектом генерации Draft.

### B4. Три обязательных результата для разработчика

1. **Комментарии в изменённом коде и конфигурации.** Policy `detailed-why-comments`: объяснять нетривиальную причину, новое ограничение API, workaround и условия его удаления. Не писать комментарий к каждой строке, не оставлять временный дневник в source. В JSON и generated lockfile комментарии не добавлять: описать изменение с file/key reference в MIGRATION_REPORT. Передавать policy при resume и в agent handoff.
2. **`docs/dependency-migration/<run-id>/DEVELOPER_UPGRADE_GUIDE.md`.** Главные новые возможности именно фактически обновлённых библиотек, примеры применимых API/паттернов, ограничения runtime/browser/toolchain, breaking changes, что учитывать при дальнейшей разработке. Ссылки на release notes/официальные migration guides для точного диапазона old→new. Не выдавать предположение за найденную возможность и не документировать ещё не установленный target как доступный.
3. **`docs/dependency-migration/<run-id>/MIGRATION_REPORT.md`.** Все direct изменения/удаления/добавления/overrides, существенные transitive/security изменения со ссылкой на полный lockfile diff; old/requested/actual; мотивация, breaking changes, изменения файлов, выполненные шаги и проверки с exits, before/after metrics, engine/coverage/unknown, отложенные когорты и причины, остаточные риски, checkpoint/rollback и ссылки на raw evidence. Полный перечень lockfile changes допустим машинным приложением; он должен быть доступен и связан с отчётом.

Оба файла обновляются по ходу, в финале отражают cumulative состояние. Пути должны совпадать в prompt, state и ответе агента. Вывод текста «документы готовы» без существующих файлов не проходит acceptance. Missing docs/comment coverage — отдельный deliverable status, не фальсификация успешности build или solver proof. Сырые audit workspace/cache/secrets не коммитить в production.

### B5. Проверки и приёмка Draft

- Проверять результат реального `build_draft_prompt` для RU/EN, а не поиск строк в модуле.
- Сгенерировать manifest/plan/prompt; production Desktop reader должен проверить hashes и прочитать новые поля. Подмена/tamper/stale/input mismatch по-прежнему отклоняются.
- Проверить copy/export/send: агент получает ровно run-scoped prompt и актуальные контексты, а не другой dashboard/project prompt.
- Сценарии: пути с пробелами/кириллицей, policy zero, user-excluded vs temporary-deferred, missing helper, unavailable audit, bridge drift, неизвестный severity, прерванный run.
- Known conflicts вывести один раз на когорту с package references; сохранить всю evidence в plan, не обрезать смысл ради размера prompt.

**Готово, когда:** пользователь может взять новый Draft и пройти миграцию с отчётами и нужными документами без угадывания команд аудита.

## Часть C. Получать первый полезный результат без глобального optimum

**Где работать:** `peer_solver_z3.py`, `peer_solver_model.py`, `_run_z3_peer_component`, `resolve_peer_compatibility_with_verification`, `block_psi_anytime.py`, proof/result types.

Раздели виды evidence (имена типов можно адаптировать к существующим):

- feasibility: satisfies hard model / unsat for this scoped domain / unknown;
- optimization: optimal / incomplete / not requested;
- materialization: not run / passed / failed;
- project verification: passed / repair required / incomplete / baseline failure;
- goal acceptance: met / partial / unknown.

`unknown` не даёт ни SAT, ни UNSAT. `sat_unproven` не следует переименовывать в optimal. Если удаётся получить assignment, независимо проверь полный hard model перед использованием; при отсутствии assignment делай bounded SAT/local solve или отдельный bootstrap candidate. Физический install не отменяет peer/environment policy, особенно у Yarn Classic. Любой publishable state нуждается в необходимых hard-model, materialization и project evidence с одинаковой identity.

Сделай единый next-candidate selector:

1. Валидный resumed/current verified checkpoint и queued promising/fallback.
2. Исходное locked состояние как проверяемый floor либо минимальная delta. Существование lockfile само по себе не делает floor verified; измерить исходные checks/health.
3. Небольшие feasibility/cohort шаги, сохраняя остальные версии зафиксированными.
4. Широкая оптимизация как ограниченное улучшение, когда уже есть полезный результат или нет локальной альтернативы.

Не требуй global desired assignment для запуска локальной миграции: различай policy goals, per-package proposed targets и уже доказанный exact assignment. Original, desired и incumbent не должны перезаписывать друг друга.

Нулевой diff может служить проверенной исходной точкой, но не считается выполненным обновлением. Если исходное состояние не прошло checks, сохраняй failure fingerprint как наблюдение; инфраструктура/грязный checkout/сломанный snapshot не должны автоматически разрешать слабую baseline. Исправление исходного кода проходит отдельным контролируемым candidate шагом.

Прокинь единый wall-clock deadline от начала run через scan/model build/solve/install/check/repair. Резервируй время на checkpoint/finalization. На unknown попробуй новую ограниченную стратегию в пределах бюджета; одинаковый solve бесконечно не повторять. Per-solver cap и общий бюджет отображаются отдельно. Чисто эвристическое сужение домена не делает UNSAT доказательством невозможности всего плана: scope proof обязан содержать domain/fixed assignments.

**Приёмка:** replay с unknown первой компоненты достигает install/check другого кандидата; queued fallback не ждёт нового solve; partial результат не получает optimal/goal-met. Бюджет/отмена завершают дерево процессов и сохраняют последний завершённый checkpoint.

## Часть D. Настоящие маленькие когорты и накопление

**Где работать:** `block_psi_progressive_baseline.py`, peer graph/transition helpers, pending queue, incumbent/recovery/proof persistence.

- Отличать potential peer graph (индекс поиска) от atomic migration cohort для конкретных old/new версий.
- Строить version-specific closure: конкретная delta, required companions, ограничения неизменяемых соседей. Проверять входящие и исходящие peer requirements через границу когорты; не разрывать необходимую атомарность.
- Пересчитывать closure при смене выбранной версии; optional peer, workspace/fixed/non-registry packages и removals учитывать явно.
- Начинать с небольшого domain: current, минимально подходящая policy/security версия, совместимая промежуточная версия, proposed target. При неуспехе расширять управляемо, сохраняя информацию о том, что область пока ограничена.
- Проверять progress по реальным policy/security/coverage метрикам и закрытым миграционным шагам, не только расстоянию до одного desired assignment. Промежуточная ступень может быть нужна для следующего major; зафиксировать обоснование и не считать её конечной целью.
- Принятая когорта образует новый cumulative source+manifest+lock snapshot. Последующие шаги идут от него. Вне scope этой delta нет тихих downgrade/reopen; нужное изменение ранее принятого пакета оформляется как отдельная обоснованная миграция.
- Отклонённая B не теряет принятую A; перейти к C. Возврат к B допустим после смены контекста, версии, closure или code repair, с новым fingerprint.
- Overrides/resolutions должны участвовать в identity и следующем candidate. Наличие overrides не должно молча обрывать все последующие direct upgrades, как нынешний ранний return.
- Падение проверки конкретного source/config snapshot не переносить как глобальный запрет на package tuple без предусмотренного доказательства универсальности. Ошибки сети/таймаут не обучают nogood.
- Checkpoint содержит source snapshot/commit, manifests, canonical lock, runtime, policy, overrides, checks/evidence, metrics, queue и deferred reasons. Запись атомарная; публикация только после завершения требуемых checks.

**Приёмка:** A проходит, B падает, C проходит — итог содержит A+C; после restart это же состояние. Проверить большую потенциальную компоненту с маленькой фактической closure, обязательную широкую closure, current→intermediate→target, overrides→следующая когорта, stale cache, cancel на install и на checkpoint.

## Часть E. Подключить repair кода в нужный момент

**Где работать:** существующий Desktop Planner/Supervisor/Executor, migration gates и proof envelopes. Не добавляй ещё один независимый оркестратор поверх всех нынешних путей; выдели общий переход candidate→verify→repair→verify→accept и используй его для основного flow.

Кандидат, который разрешается package manager, но требует изменения API/config, может поступить Executor до наличия глобального окончательного плана. Он получает exact candidate, разрешённый scope, cumulative checkout, old/new versions, release notes references, failing commands и полный diagnostic log, policy комментариев и документы из B.

- Agent чинит source/config в изолированном checkout и возвращает machine-readable outcome.
- Если нужны другие версии/companion packages/overrides — возвращает предложение replan; новый assignment выбирает и проверяет planner. Изменённые версии не скрывать внутри repair.
- Orchestrator самостоятельно запускает установку и необходимые checks после repair; сообщение агента «всё зелёное» не является proof.
- Limit repair по времени/попыткам/реальному улучшению; при неуспехе сохранить диагностику, отложить когорту и продолжить другие.
- Принимать code/config/manifest/lock как единый cumulative результат; proof до изменения кода не переносится на новый snapshot автоматически.
- Сохранить distinctions adaptive resolver-ready и fully project-verified. Для завершённой миграции нужны все применимые обязательные checks. При baseline-red явно показать, что уже было сломано; не снимать requirement молча.
- Недоступный agent не блокирует все deterministic upgrades: вернуть partial с конкретной repair-required когортой.

Проверить сценарий «тот же набор версий падает до config repair и проходит после него»: ранняя версия кода не должна отравить solver cache вечным dependency nogood. Обязательно проверить накопительное объединение двух независимо успешных шагов: несовместимый cumulative merge не принимается.

**Приёмка:** программа проходит тот же практический цикл, который сейчас пользователь делает вручную через Draft; новый project-verified checkpoint сохраняется после code repair, не только после изменения package.json.

## Часть F. UX, метрики и реальное завершение

Состояния отражают результат, например:

- поиск продолжается, уже сохранено N шагов;
- проверен частичный результат, цель пока не достигнута;
- нужен repair конкретной когорты;
- бюджет закончился, есть resumable checkpoint;
- нового проверенного результата нет, сохранён предыдущий;
- цель подтверждена на cumulative snapshot;
- нарушение safety/infrastructure, продолжение невозможно.

Solver unknown одной компоненты не равен красному стопу всей миграции, пока есть безопасные альтернативы и бюджет. Ошибки идентичности, небезопасный Git-state и недостоверное evidence не превращать в partial success. Показывать, что именно было сохранено: исходная точка, resolver-ready либо весь project-verified результат; не объединять их одним двусмысленным «проверено».

Progress: elapsed по стадиям, фактические когорты/checks, actual lag и C/H/M/L/U, coverage, последний checkpoint, следующий шаг. Target/estimated health выводится отдельно. На остановке дать ссылки на логи и оба developer документа. Не советовать «увеличьте бюджет» без указания, какой лимит реально сработал и что изменится.

### Обязательная матрица приёмки

| Сценарий | Ожидаемый результат |
|---|---|
| Global optimize unknown, малый SAT шаг существует | Есть физическая попытка малого шага; unknown не выдаётся за conflict |
| A accepted, B failed, C available | Сохранён A+C, B имеет конкретный deferred/repair reason |
| Большая потенциальная peer-компонента | Версиеспецифические когорты либо объяснённая необходимая атомарность |
| Исправимый config/API break | Repair → повторные checks → новый cumulative checkpoint |
| Intermediate version обязательна | Ступень проверена; конечная цель и оставшаяся работа видны |
| Incumbent с overrides | Следующие когорты сохраняют/явно перепланируют overrides |
| Shutdown/cancel/restart | Нет потери verified состояния и подмены source identity |
| Security endpoint unavailable | Unknown/coverage gap, не 0 vulnerabilities |
| Yarn bridge drift | Approximate evidence отделено от canonical acceptance |
| Prompt RU/EN и Desktop reader | Контекст, audit command, comments, документы доходят до агента |
| ЦЕЛЕВОГО_ПРОЕКТА test script составной | Не теряется test:build при выборе test:unit |
| Невыполнимая политика/закрытый scope | Частичный результат и scoped blocker; нет ложного goal-met |

После synthetic/fixture тестов проведи изолированный real run ЦЕЛЕВОГО_ПРОЕКТА с зафиксированными настройками и snapshot. Отдельно сравни legacy/current и исправленный подход на одинаковых входных metadata/lock/runtime, если это практически возможно. Не использовать свежесть registry как скрытое отличие benchmark.

В приёмочном отчёте: scan/solve/install/check/repair times; time-to-first-checkpoint и time-to-first-project-verified; число checked/accepted/deferred когорт; actual before/after lag denominator/numerator, C/H/M/L/U по источникам, coverage; что получилось после restart; оба итоговых developer-файла; ссылки на командные логи. First useful upgrade обязан быть ненулевым, если test fixture содержит доступное безопасное обновление; неизменённый source floor не закрывает этот критерий.

Не обещай заранее конкретный процент ЦЕЛЕВОГО_ПРОЕКТА или время на его hardware. До реализации зафиксируй измеримый бюджет и контрольный сценарий; после предъяви фактические результаты. Если реальный run блокирован доступом/runtime/registry, прямо отметь незавершённую real acceptance, сохрани готовые fixes и evidence; не называй задачу полностью закрытой по unit tests.

## Что не принимать за решение

- Только увеличение timeout с 30 до 300 секунд.
- Только fallback из Deep в Draft без физически проверяемого следующего шага.
- Unknown, sat_unproven или неполное install evidence, переименованные в optimal/verified.
- Ослабление peer checks, скрытые legacy-peer-deps для установки исходного проекта, удаление required checks ради зелёного статуса.
- «Все новые версии сразу», после чего агент должен исправлять весь проект без checkpoints.
- Удаление сложных пакетов из знаменателя/скрытый exclude ради достижения 80%.
- Только новые маркеры/строковые тесты без исполняемого сценария.
- Документы с обещанными targets вместо реально установленного cumulative результата.

## Как сдавать каждую часть

После каждой части предоставь: что изменено, сценарий до/после, узкие тесты и ограничения, актуальный список следующих шагов. Не останавливайся на описании плана, когда реализация этой части разрешена. Перед следующей частью проверь diff и сохранность предыдущего поведения. Обновляй единый delivery checklist A–F, не переписывай историю тестов как будто все части уже завершены.

Рекомендуемая граница поставок: B отдельно; затем C с тестами A; затем D; затем E; затем F с real acceptance. Не смешивай это с общим redesign интерфейса или переписыванием всего генератора.