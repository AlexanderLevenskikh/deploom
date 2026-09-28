# Аудит Deep и Draft на примере libjs — 27 сентября 2026

Вывод: ситуацию можно исправить, но очередного увеличения timeout недостаточно. Сейчас первый полезный результат зависит от полного оптимизационного solve. Накопительный механизм существует, однако стартует слишком поздно. Нужен цикл «зафиксировать исходное состояние → получить небольшой проверенный шаг → сохранить его → расширять следующей когортой», с агентом для миграции кода до окончательного отказа от подходящего набора зависимостей.

Это аудит и проект задания, а не заявление об исправлении программы. Production-код и проекты Kontur не изменялись; установка зависимостей, повторная миграция libjs и обращения к корпоративному registry не запускались.

## 1. Что проверено

- Репозиторий DepLoom: `715c9c29e583f98db675aea2420da4ae92d79e41`, VERSION `0.2.142`.
- Вставленный пользователем текст ошибки.
- `C:/Users/levenskikh/Desktop/Kontur/dependency-roadmap-template/.dependency-roadmap/artifacts/runs/f3d9ce6c-d255-4783-9894-fea15b976057/activity.log`.
- `state/baseline-verification-progress.json`, настройки проекта, сохранённый intent libjs.
- `C:/Users/levenskikh/.deploom/diagnostics/deploom-failure-20260927T025830Z-43888.json`.
- Реальный draft `runs/run-05666e4d-b30f-4b15-88b7-e72be075edf3/draft/prompt.md` для Info.UI, генератор Draft и путь чтения в Desktop. Draft для libjs среди найденных run-scoped manifests отсутствует; чужой draft не выдаётся за libjs.
- Исходники solver, progressive planner, verification, prompt generation и manual audit.
- Установленные `resources/tool/{dependency_live_roadmap_generator.py,peer_solver_z3.py,block_psi_progressive_baseline.py,manual_dependency_audit.py}` совпадают с workspace после нормализации окончаний строк. Первоначально разные byte hashes объясняются CRLF/LF.

Локальные воспроизводимые probes: `.dependency-roadmap/audit-reviews/libjs-deep-2026-09-27/probe.py`; результаты — `probe-results.json`, тесты — `tests.txt`.

## 2. Что произошло в запуске

| Наблюдение | Значение |
|---|---|
| Intent | deep, AUTONOMOUS / BACKGROUND, searchMode AUTO |
| Явный бюджет | 75 минут |
| Цель | yellow, lag >= 80%, 12 месяцев, Critical <= 0, High <= 1 |
| Старт UI | 02:52:21 UTC / 07:52:21 местное время |
| Старт core | 02:52:26 UTC |
| Начало solve-and-verify | 02:57:48 UTC |
| Managed direct inputs | 133, fixed inputs 0 |
| Peer graph | 71 компонента, 5052 кандидата всего |
| Компонента, на которой остановились | 53 пакета, 2865 кандидатов, 4402 constraints |
| Z3 | одна попытка, timeoutMs 30000, elapsedMs 33990 |
| Результат | unknown, reasonUnknown «no reason given» |
| Refinements / дорогие проверки / incumbent | 0 / 0 / null |
| Конец | 02:58:31 UTC, exit 3, EXACT_SOLVER_UNKNOWN |

Около 6 минут прошло от старта UI, но собственно solve-and-verify занял около 42 секунд. Более пяти минут ушло на подготовку/данные до него. До физической проверки кандидата дело не дошло. Отсутствие решения не доказано. Диагностика содержит `confirmedTimeout=false`: близость elapsed к лимиту не позволяет переименовать unknown в подтверждённый timeout.

Наблюдавшийся отказ относится к стадии выбора версий. Он ещё ничего не говорит о том, пройдут ли Flow, lint, build и tests libjs после изменений.

## 3. Основные находки

### P1. Глобальный optimize блокирует получение первого проверенного шага

`dependency_live_roadmap_generator.py:12848–12943`: при отсутствии incumbent вызывается `resolve_peer_compatibility`; `EXACT_SOLVER_UNKNOWN` пробрасывается наружу. Путь `partial_on_incomplete` предусмотрен для Draft, но не решает bootstrap Verified.

`peer_solver_z3.py:65–143`: используется `Optimize(priority="lex")`, 8 основных objectives из `_build_peer_optimization_model` (`generator:7881`) и ещё по одному лексикографическому tie-break objective на пакет. Для компоненты из 53 пакетов это 61 objective. Кроме существования допустимой комбинации код требует закрытия всех оптимизационных bounds; `sat_unproven` тоже не возвращает assignment.

Лексикографический порядок objectives соответствует документации Z3: [Combining Objectives](https://microsoft.github.io/z3guide/docs/optimization/combiningobjectives/). Предположение, что именно tie-breaks составляют основную долю времени libjs, требует benchmark/IR replay; в этом аудите это не измерялось.

Смысл проблемы: доказательство оптимальности плана сделано входным условием практической проверки. Для полезной миграции достаточно конкретного кандидата, удовлетворяющего hard constraints, после чего всё равно нужны install и проверки проекта. «Feasible», «optimal», «resolver verified», «project verified» и «цель достигнута» должны быть разными фактами. Unknown нельзя объявлять успехом, но он не должен запрещать другой проверяемый следующий шаг.

### P1. Накопительная миграция начинается только после incumbent

`generator:12560–12567`: `_next_progressive_extension()` возвращает None, пока нет incumbent или desired assignment; при resolver overrides тоже возвращает None.

`generator:12997–13002`: desired assignment заполняется уже после успешного получения candidate assignment.

`generator:14211–14278`: bootstrap floor rescue зависит от повторного project failure/predicate либо последнего прогнозируемого слота. При остановке в peer-planning этих событий нет. Rescue не достигается.

Это объясняет, почему наличие progressive-кода и зелёных unit-тестов не помогает именно этому запуску.

### P1. Готовый fallback всё равно ждёт solver

`generator:12884` вызывает solver, а `pending_promising_assignments.pop(project, mode)` находится на `12943`. При unknown до `pop` выполнение не доходит. Даже ранее подготовленный fallback может оказаться недоступен за тем же барьером.

Нужен единый selector кандидатов: восстановленный checkpoint/очередь → небольшой шаг от incumbent/исходного состояния → ограниченный новый solve. Любой queued candidate заново проверяется на совпадение source/policy/lock/constraints; очередь не даёт права обходить safety.

### P1. Потенциальная peer-компонента превращается в атомарную когорту

`generator:7450`: `_potential_peer_graph` объединяет peer-связи всех версий домена.

`generator:12542`: `_progressive_atomic_groups` берёт компоненты именно этого графа и добавляет learned edges. Это консервативная группировка для поиска, но слишком грубая единица практического обновления. Связь, существующая у какой-то старой или будущей версии, не означает необходимости менять оба пакета в данном шаге.

`block_psi_progressive_baseline.py:74`: planner переводит всю группу сразу к одному desired assignment. Промежуточные версии в его API отсутствуют. Если такой полный fingerprint запрещён, группа пропускается. Это локальное свойство planner; другие ветки программы умеют искать версии, но этот progressive-путь сам их не исследует.

Runtime probe: заблокированная группа A/B/C не даёт следующего шага; с одиночными группами выбирается A, остальные сохраняются. Probe не доказывает, что произвольное разбиение peer-графа безопасно: production должен делать version-specific closure и проверять границу с зафиксированными пакетами.

### P1. Deep budget не меняет 30-секундный входной барьер

`_peer_solver_backend_options` берёт timeout из `timeoutMs` / legacy `shadowSolverTimeoutMs`; здесь в settings стоит 30000. `_run_z3_peer_component` повторяет solve при registry refinement уже найденного optimal assignment, а не адаптирует unknown к крупной компоненте.

75 минут общего бюджета не превращаются в иной стартовый алгоритм. В этом запуске оставшееся время просто не использовано. Автоматическое повторение той же глобальной задачи не устраняет проблему; нужны bootstrap, feasibility, локальные шаги и ограниченный escalation.

`deploom_failure.py:372` маркирует unknown как user-action-required. Desktop также распознаёт его как non-retry stop (`main.ts:6296`). Текст UI предлагает увеличить бюджет, хотя увеличение только общего бюджета не меняет per-solver cap. Изменить надо переход состояния и подсказку, а не только сообщение об ошибке.

### P1. Полезный verified baseline и завершённая миграция — разные результаты

`generator:13430–13459`: adaptive verification умеет принимать resolver-green assignment с частью project failures как ожидаемую миграцию; structural failures отклоняются раньше. Поэтому нельзя писать, что нынешний incumbent всегда означает успешные все проектные checks.

Агент может чинить код и конфигурацию. Baseline в основном меняет версии/проверяет старый код; Executor подключается позже. Отсюда принципиальное отличие от ручного «draft → агент».

Решение: ранний кандидат → проверка → bounded repair кода/конфига в изолированном checkout → повторная полная проверка → cumulative commit. Ошибка адаптации API не должна автоматически становиться вечным dependency-only nogood. Доказательства и кеш должны учитывать source/config snapshot. Resolver-proof не заменяет project-proof; policy acceptance не заменяет оба.

### P2. Контракт проверок libjs требует дополнительной ревизии

`discover_baseline_project_checks` фактически выбрал `yarn flow:check`, `yarn lint`, `yarn build`, `yarn test:unit`.

В package.json libjs `test` равен `yarn test:unit && yarn test:build`, а `test:build` — `webpack`. Discovery удаляет `test` при наличии `test:unit`, поэтому webpack-ветка test не включена этим механизмом. Нельзя считать весь declared test suite исполненным по одному unit-step.

Текущий checkout также не содержит файла `scripts/release/build.sh`, на который указывает `build`. Это read-only наблюдение о текущем checkout, не доказанная причина запуска 27 сентября: до build он не дошёл. Перед real acceptance требуется сверить ветку/snapshot и реальные команды; не скрывать отсутствующий script и не запускать release-команды наугад.

## 4. Аудит Draft prompt

Источник — `build_draft_prompt`, `generator:23441–23834`. Desktop читает этот файл непосредственно (`desktop/electron/main.ts:6950`); расширенный Dashboard prompt автоматически его не дополняет.

Уже есть: NOT_VERIFIED/PLANNING_ONLY, текущие и projected lag/security, targets, unknown/deferred/conflicts, install и общие checks. Это полезно сохранить.

Отсутствуют или недостаточно конкретны:

1. Последовательность «зафиксировать исходное → малый шаг → cumulative verification → следующая когорта» и checkpoint/resume.
2. Промежуточные отчёты после проверенного шага: actual lag numerator/denominator, процент, C/H/M/L/U, coverage, источник аудита, блокеры. Projected цифры могут выглядеть как обещание без фактического пересчёта.
3. Готовая команда `manual_dependency_audit.py` с путём tool, project-dir, registry, policy, outputs и выбранным engine. Вместо неё только «OSV/outdated».
4. Полный контекст выполнения: реальные project/tool paths, source identity, package-manager и runtime, проверочные команды. Параметр `projects_by_name` в функции сейчас не используется для вывода такого контекста.
5. Policy комментариев к изменённому коду/конфигам. В Dashboard генераторе уже есть `codeCommentPolicy`; Draft его не содержит.
6. Обязательные `DEVELOPER_UPGRADE_GUIDE.md` и `MIGRATION_REPORT.md` с конкретным содержанием и проверкой существования.
7. Разделение user-excluded/keep-current и временно deferred из-за незавершённого поиска. Сейчас просьба не трогать deferred без пересогласования способна законсервировать техническое откладывание.
8. Краткий список когорт с evidence-ссылками. Реальный prompt Info.UI ~33.7 KB повторяет длинные peer-conflict цепочки по многим пакетам; это уменьшает ясность следующего действия.

Регрессионные проверки Dashboard ищут `manual_dependency_audit.py` и policy во всём файле генератора; они не гарантируют присутствие этих инструкций именно в сгенерированном Draft. Runtime probe вызова `build_draft_prompt` подтверждает отсутствие названия audit helper, его project-dir, comment policy и требуемых doc filenames.

## 5. Как правильно описать аудит уязвимостей

В программе есть разные источники, их нельзя сливать в один безымянный счётчик:

- Generator использует OSV для рассматриваемых package/version и считает health/lag. Это не полный npm audit установленного транзитивного дерева.
- `manual_dependency_audit.py` — отдельная проверка. Для Yarn `--yarn-audit-engine auto` сейчас вызывает `npm-lock-bridge`: создаёт isolated package-lock и запускает npm audit там. Прямой `yarn audit` не является default.
- `yarn-inventory` проверяет точные package/version из канонического yarn.lock через npm security endpoint без нового разрешения диапазонов. Это отдельный режим.
- При drift npm bridge код возвращает `accuracy=npm-resolved-approximation` и всё равно может ставить `complete=true`, `trusted=true`. Успешный endpoint-запрос нельзя без уточнения называть доказательством безопасности именно Yarn-графа.

В prompt надо дать штатную команду, объяснить engine/accuracy и сравнивать одинаковый источник/счётчик до и после. Для строгого утверждения о фактическом Yarn graph нужен canonical inventory либо доказанное соответствие bridge; approximate результат сохраняется отдельной сверкой. Не менять default engine молча под видом редактирования текста.

`unknown`, сетевой отказ, неполное покрытие и outdated-result не равны нулю уязвимостей. Различать vulnerable packages, advisories и nodes. Изменение базы advisories между измерениями тоже может менять счётчик независимо от миграции.

## 6. Рекомендуемый порядок спасения

1. Немедленно улучшить Draft: контекст, точный аудит, checkpoints и три запрошенных результата (комментарии + два документа). Это улучшает уже работающий путь.
2. Убрать зависимость первой физической попытки от глобального optimum. Проверять исходное состояние/узкий шаг, употреблять queued fallback до новой оптимизации.
3. Перейти к локальным задачам «incumbent + delta», version-specific peer closure, промежуточным версиям и расширению домена только по необходимости.
4. Встроить bounded code/config repair до отказа от кандидата. Подтверждать каждый cumulative результат заново; сохранять исправленный source snapshot вместе с lockfile.
5. Перевести exhausted/unknown в честный partial/resumable outcome, если есть проверенный результат; показывать остаток и отдельно недостигнутую цель.
6. Принять работу на replay libjs и реальном изолированном запуске, а не только на тестах A/B/C и проверках наличия строк.

Нельзя обещать, что любая цель достижима: могут потребоваться замена заброшенной библиотеки, новый peer package или серьёзная миграция API. Программа должна сохранять достигнутое и выдавать конкретную следующую работу, а не останавливаться до первого шага из-за недоказанного optimum.

## 7. Верификация и ограничения аудита

25 целевых тестов прошли: progressive baseline, iterative cohort intent, peer solver model, audit prompt contract. Отдельная попытка Dashboard comment-policy теста остановилась на PermissionError Windows TEMP при записи report.html; его результат не засчитан как pass и это не объявляется дефектом продукта.

Локальные probes подтвердили unknown → terminal, ограничение широких atomic groups, состав Draft prompt, auto dispatch npm-lock-bridge и соответствие установленного кода. Порядок queue/solver проверен статически; это отдельно отмечено в JSON.

Полный real migration/libjs install и производительность альтернативного solver не проверялись. Отчёт не содержит выдуманного «после», новых процентов или обещания нулевых vulnerabilities. Изменён только набор документов и локальных материалов аудита.

Задание для реализации: [LIBJS_DEEP_RESCUE_AGENT_TASK_2026-09-27.md](LIBJS_DEEP_RESCUE_AGENT_TASK_2026-09-27.md).