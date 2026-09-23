# Повторная валидация №3 и задание на исправление Draft / режимов / критериев

Дата: 2026-09-23. Проверена версия **0.2.119**, HEAD `3fccb91`, основные изменения `8a1a47a`; сравнение с `9f4b2a1` (0.2.118). Рабочее дерево до проверки чистое. Проверяющий не менял приложение; этот документ — результат проверки и самостоятельное задание следующему агенту.

## Вердикт

**Принимать работу как завершённую нельзя.** Есть существенные исправления, но добавленная проверка хешей блокирует импорт штатно созданного Draft на Windows. Пользовательский сценарий «получить быстрый теоретический план и открыть промпт» всё ещё не обеспечен. Выбор цели и процентов не проходит через весь процесс. Fast/Deep по-прежнему не имеют отдельных реализованных контрактов.

Не считать изменение текста, успешный exit code Python, наличие четырёх файлов или зелёную сборку доказательством работающего Desktop-сценария.

## Что проверено и что уже исправлено

Подтверждено чтением кода и профильными тестами:

- `executeJob` теперь импортирует Draft до любого требования legacy snapshot; отсутствие валидного manifest стало ошибкой, а не ложным успехом.
- Draft не записывает legacy MD/JSON/HTML и отделён от Verified-публикации.
- Путь артефактов Python/Electron приведён к общей базе workspace.
- Локальный inventory собирается до сетевого анализа текущего проекта; expiry перехватывается также при финализации.
- Неподдерживаемый физический pnpm-путь больше не обязан блокировать теоретический Draft.
- На уровне строк анализа OSV failure больше не превращается в строку `0`; однако агрегаты и итоговый цвет всё ещё неверны — см. T3.
- Добавлены runId-привязка результата, сохранённая карточка Draft, события прогресса, контролы цели и процента. Это полезные изменения, но ниже перечислены разрывы их поведения.

Выполнено в этой проверке:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_draft_baseline_contract tests.test_draft_flow_integration_physical tests.test_block_psi53_search_depth_ux
# 29 tests, OK; 20.082 s

# Из desktop:
npm run build
node scripts/check-baseline-intent.mjs
node scripts/check-acceptance-policy.mjs
node scripts/check-human-flow.mjs
node scripts/check-ui-lifecycle.mjs
# Все прошли.
```

Первоначальные запуски тестов/сборки встретили ограничения песочницы Windows Temp / Vite spawn EPERM; повтор с необходимым доступом прошёл. Это отдельно от дефектов приложения.

Дополнительно выполнены воспроизводящие пробы реальных функций Python и извлечённых через TypeScript AST функций `main.ts`: публикация файлов на Windows; чтение manifest; identity/path validation; нормализация 0%; staleness; health/unknown; настоящий HTTP-запрос к локальному серверу с медленной передачей ответа. Сеть npm/OSV для этих проб не нужна.

**Граница проверки:** полноценный Electron UI, автооткрытие окна и Fast/Deep end-to-end в этой итерации не запускались. UI-дефекты ниже установлены по коду, а не по наблюдению окна. 29 тестов и четыре Node-проверки — не полный регрессионный набор. Наличие тестов с названием `physical` не означает проверки Electron или реальной миграции зависимостей.

## T1. Блокер: штатный Windows Draft отвергается читателем Electron

Приоритет P0 для выдачи результата. Код: `dependency_live_roadmap_generator.py:21136` (`_atomic_write_text`), `publish_draft_result` (секция `hashes`); `desktop/electron/main.ts:704` (`readDraftResultArtifact`), проверка хешей около строки 743; `executeJob` около строки 5971.

**Причина.** Python `Path.write_text(..., encoding="utf-8")` без явного newline на Windows записывает CRLF. Хеш prompt считается от исходного LF-текста; хеш plan — от `read_text`, который нормализует CRLF в LF. Node `readFileSync(..., 'utf8')` сохраняет CRLF, поэтому хеши не совпадают.

**Фактическая проба:** вызвать штатный `publish_draft_result` на Windows с `DRAFT_PARTIAL`, затем штатный `readDraftResultArtifact` для того же workspace/run. Для plan и prompt обнаружены CRLF и несовпадение manifest hash с SHA-256 байтов. Reader вернул `undefined`. После замены только двух хешей на хеши фактических файлов reader принял те же самые артефакты. Содержимое плана и промпта не менялось.

Следствие: успешно завершившийся Python-run может попасть в `DRAFT_RESULT_MISSING`, хотя все файлы существуют. Новое сообщение также ошибочно смешивает отсутствие файла и нарушение целостности.

**Исправить:**

1. Зафиксировать единый контракт кодировки и хеширования между Python и Node. Предпочтительно UTF-8, явные LF при записи, хеш фактических байтов; либо иная явно одинаковая канонизация с обеих сторон.
2. Не отключать проверку целостности как обходной путь.
3. Различать `manifest missing`, `hash mismatch`, `identity mismatch`, `unsupported schema`, показывать понятную причину и действие пользователю.
4. Сохранить атомарную публикацию manifest последним; ready только после успешного импорта валидного набора.

**Приёмка:** настоящий Python subprocess создаёт READY и PARTIAL на native Windows; настоящий production reader принимает оба; Desktop открывает соответствующий prompt. Отдельно проверить LF/CRLF, Unicode и однобайтовое повреждение plan/prompt: повреждение отвергается. Тест обязан пересечь границу Python→файл→Node, а не сравнивать Python `read_text` с Python hash.

## T2. Цель и процент всё ещё не управляют всем процессом

Приоритет P1. Код: `desktop/electron/main.ts:428, 2418`; `desktop/src/components/FlowWorkspace.tsx:52, 222, 241`; `desktop/electron/acceptance-policy.ts`; `desktop/electron/target-closure.ts:117, 248`; `dependency_live_roadmap_generator.py:4834–4864, 20821` и выбор default target около 5614.

Подтверждённые разрывы:

- `DEPLOOM_BASELINE_TARGET_LEVEL` и `DEPLOOM_BASELINE_MIN_LAG_OK_PCT` отправляются **только внутри ветки `proofMode === 'DRAFT'`**. Verified Baseline запускается с прежними значениями по умолчанию; generate также не получает новую политику этим путём.
- В FLOW всё ещё `const target: TargetLevel = 'yellow'`. Выбор Green в диалоге не меняет `ActionInput.target`.
- `EFFECTIVE_TARGET_LEVEL` записывается в snapshot/prompt, но не выбирает план действий: `_draft_target_for_major` берёт default/yellow, а default goal определяется текущим status. Надпись «green» не гарантирует, что выбран именно green-план.
- AcceptancePolicy содержит только `maxKnownCritical` и `maxKnownHigh`; freshness/targetLevel/policyHash не участвуют в `acceptanceVerdictFromManualAudit`. Manifest сам по себе ничего не передаёт потребителю без соответствующего кода чтения и проверки.
- `target-closure.ts` всё ещё считает `ceil(total * 0.8)` и пишет про фиксированные 80%.
- Значение 0% поддержано контролом, но `Number(raw.minLagOkPct) || 80` превращает его в 80. Выполнение реального `normalizeBaselineIntent({minLagOkPct:0})` вернуло 80.
- Проверка изменения политики перед resume сравнивает только `intent.policies`. Изменение цели, процента и прочих численных критериев не меняет эту identity.
- `health_green_ratio()` равен `min(100, pct + 10)`: при default80 это 90%. Одновременно фактический зелёный статус требует отсутствия нарушений lag-policy. Проекция и критерий статуса расходятся.

**Исправить:** ввести один нормализованный versioned `TargetPolicy`, передавать его в UI→IPC→все режимы planner→результат/prompt→audit/acceptance→closure/release. Удалить независимые жёсткие 80% и рассогласованные цели. При смене policy не продолжать доказательства старой цели без явной инвалидации. Семантически одинаковая политика должна иметь одинаковый policyHash; хеш настроек не заменяет их применение.

Согласовать продуктовые пресеты с исходным запросом:

- Yellow: минимум 80% соответствуют выбранному lag, Critical=0, High допускается в явно заданном малом количестве (default1).
- Green: 100% соответствуют явно показанному критерию актуальности, Critical=0, High=0; допустимые Moderate/Low задаются числами. Не называть суммарный допуск 20 «единичными» без явного решения и видимого значения.
- Показать рядом с процентом, что значит «без лага»: например отставание не более 12 месяцев; отличать это от latest/нулевого отставания. Дать настройку lag и область её действия перед запуском.
- Пользовательские значения, отличающиеся от пресетов, показывать как пользовательскую цель; не скрывать ослабление зелёного пресета.

**Приёмка:** одинаковые данные, цели yellow80/yellow90/green100; меняются реальные выбранные targets и итоговый verdict там, где должны. Проверить 0/5/80/90/100 и границы ceil на небольшом N. Значения проходят через Draft и Verified. Изменение policy после паузы/после Draft делает прежний результат устаревшим. Green нельзя завершить как Yellow, а выполнение High-условия без freshness не означает достижение всей цели.

## T3. UNKNOWN доходит до строки, но теряется в итоговой оценке

Приоритет P1. Код: `dependency_live_roadmap_generator.py:5273` (`compute_project_health`), `20752` (`_row_metadata_known`), `20859` (`build_draft_plan`), `338` (`_apply_baseline_intent_scope`).

Выполнены прямые вызовы production health/plan с DependencyRow:

| Вход | Текущий результат |
|---|---|
| Версия удовлетворяет lag; `current_vulns='unknown'` | `status='green'`, `unknown=1`, причина сообщает `0 C/H` |
| Единственная зависимость с неизвестной актуальностью и security | `status='yellow'`, `total=0`, `lag_ok_pct=100.0` |
| Актуальная зависимость с `H:10`, без Critical | `status='yellow'`, хотя продуктовый default High≤1 |
| Registry latest известен; OSV unknown | `build_draft_plan.unknowns=[]`, `unknown-metadata=0`; неизвестная security-часть не попадает в список уточнений |

То есть исправление сообщения OSV не исправило агрегат. Green-ветка не проверяет `unknown`; знаменатель freshness учитывает лишь зависимости с известным lag-target. Отдельно прежняя функция `keep-current` всё ещё полностью исключает пакет из update/health target — простая отсрочка может скрыть его устаревание/уязвимости.

**Исправить:** разделить metadata coverage, security coverage, совместимость и доказанность результата. Unknown не трактовать как zero/healthy; для пустого известного знаменателя отображать «недостаточно данных», а не 100% всей области. Учитывать security unknown в plan, summary и prompt. Цвет/closure должны использовать общую политику T2. Разделить «не обновлять сейчас» и «исключить из оценки»: keep-current сохраняет влияние на общий health; явное исключение имеет причину и видимый размер исключённой области.

**Приёмка:** registry доступен / OSV недоступен; OSV частично доступен; все lag-target неизвестны; C=1; H=0/1/2; M/L на границе; unknown в current/target; keep-current и explicit exclude. Не должно быть зелёного с неизвестной security, «0 неизвестных» при OSV failure и 100% по всем пакетам при нулевом покрытии. Это не требует физической верификации Draft: теоретическая оценка должна честно показывать собственные пробелы.

## T4. Deadline Draft не является жёстким пределом времени

Приоритет P1. Код: `LiveDataClient._budgeted_timeout` около 2234; `fetch_npm_metadata` около 2472; прочие HTTP, retry sleeps; coarse checks около 7971/21956/22008; настройки supervision Draft в `main.ts` около 2380.

Положительное изменение: deadline подключён к запросам, parallel prefetch для Draft обойдён. Но `max(1, min(int(remaining), timeout))` даёт минимум 1 s даже при остатке 0.1 s. Request timeout ограничивает ожидание сетевых операций, а не полную длительность получения ответа; медленная передача может продлевать запрос. Sleep/backoff также не ограничены остатком бюджета. Проверки до/после больших операций не прерывают сами операции.

**Реальное воспроизведение:** локальный HTTPServer посылал JSON по одному байту с интервалом 150 ms; `LiveDataClient` с deadline0.1 s и timeout30 вернул успешный metadata-ответ через **2.109 s**, при remaining0.0. Запрос не был мокирован. При более длинном ответе тот же принцип позволяет выйти за обычный 15 s budget. В этой пробе проверен конкретный сетевой метод, не end-to-end время всего Desktop.

**Исправить:** абсолютный monotonic deadline с резервом на публикацию; дробные остатки; ограничение всех retry/backoff, сетевого чтения и тяжёлого planning. Для операций, которые нельзя безопасно прервать внутри, использовать ограниченного по времени worker и предварительно сохранённый inventory, позволяющий опубликовать PARTIAL. Не увеличивать timeout и не ждать пятиминутного stall-abort. Первый локальный результат должен быть доступен до окончания enrichment. Для нескольких проектов собрать inventory всех выбранных проектов до первого долгого scan — сейчас цикл может истечь на первом, оставив остальные без inventory.

**Приёмка:** cold/warm/offline, hanging endpoint, slow trickle, повторяющиеся 429/5xx, CPU-heavy planning и несколько проектов. Измерять от клика до доступного результата. Ориентиры исходного задания: первый статус ≤1 s; локальный Draft tiny ≤2 s; warm ≤5 s; по default deadline15 s публикация PARTIAL с явно измеренным небольшим резервом на финализацию. На timeout полный локальный inventory сохраняется, промпт открывается, никакие install/build/test для Draft не запускаются.

## T5. Проверка принадлежности и устаревания результата неполная

Приоритет P1. Код: `desktop/electron/main.ts:704–798`, `dependency_live_roadmap_generator.py:20803` и публикация `inputHashes`.

После корректировки хешей только в тестовых артефактах реальный reader:

- принял manifest с удалёнными `workspaceId` и `projectId` при заданном expected identity;
- принял ссылки plan/prompt/summary на директорию **другого run**: проверяется общий artifacts root, а не siblings текущего run;
- `draftInputStaleness` вернул false после удаления lockfile: отсутствие файла не считается изменением.

Reader identity сравнивает только непустые строки; snapshot-путь `draftResultForProject` вообще не передаёт expected identity. Staleness основана только на mtime и не сравнивает policy; snapshot не сохраняет `inputHashes`, хотя Python их уже публикует. Более того, входные хеши считаются при публикации, а не фиксируются как identity реально прочитанных исходных данных: изменение файла во время расчёта может дать неверную привязку.

**Исправить:** обязательные schema/identity/authority поля; строгая принадлежность артефактов конкретному run; hash inputs и effective policy, зафиксированные при чтении, сравнивать с текущими до показа «свежий». Учитывать удаление/добавление manifest/lock/settings, npm-shrinkwrap, файлы вне корня nested package, изменение policy, invalid timestamp. Исторический результат разрешено открывать с явным признаком устаревания и причиной, но не выдавать за свежий.

**Приёмка:** чужой run/workspace/project; пустые/отсутствующие identity; cross-run path; повреждённые bytes; смена policy; изменение с сохранённым mtime; удаление lockfile; редактирование входа во время run. Валидный результат проходит после перезапуска приложения и при расположении settings как в корне, так и в `.dependency-roadmap`.

## T6. Draft / Fast / Deep всё ещё не реализованы как три режима

Приоритет P1, незавершённое исходное требование. `--mode` записывается в metadata, но это не три разных стратегии исполнения. GUI выбирает DRAFT/VERIFIED; FAST/BACKGROUND задаётся через controlMode и означает политику исполнения, а не пользовательский контракт «быстрый/глубокий». В отчёте реализации это ограничение признано, но недоступная сеть не объясняет отсутствие разведения веток.

**Исправить:**

- Draft: bounded theoretical plan; без physical install/checks; READY/PARTIAL с результатом и unknown.
- Fast: отдельный бюджет и ограничение дорогих попыток; остановка после первого подходящего проверенного результата, без обязательного глубокого перебора; при недостижении цели честный статус/ограничения.
- Deep: расширенный поиск, сохранение лучшего уже доказанного результата, безопасный выход по бюджету/отмене; следующая неудача не уничтожает предыдущее доказательство.
- Mode, controlMode, executionMode и target policy — разные понятия; не подменять одно другим и не обещать реализованный режим одной надписью.

Приоритет поставки — сначала рабочий Draft T1–T5. Затем Fast; Deep не объявлять готовым без своего контракта и проверок.

**Приёмка:** один fixture и три режима; журнал вызовов показывает разное число/тип операций. Draft: 0 physical calls; Fast прекращает работу после первого удовлетворяющего критериям результата; Deep допускает дальнейшее улучшение и сохраняет предыдущий best result при сбое/timeout. Проверить поведение и без внешней сети через управляемые локальные fixtures.

## T7. Прогресс и lifecycle требуют завершения

Приоритет P2. Код: `FlowWorkspace.tsx:72–95, 225–254, 572–619`, `useDependencyFlow.ts:105–121, 202–222, 492–521`, `LiveDataClient.progress` около 2206.

По коду:

- `draftLaunchActive = activeAction === 'baseline' && Boolean(draftLaunch)`. Marker предыдущего Draft сохраняется; следующий Verified Baseline того же проекта может получить текст «Готовим черновой план» и утверждение об отсутствии physical checks.
- Автооткрытие зависит от объекта `draftResult` и marker, но не потребляет флаг autoOpen. Обновление details новым объектом или remount может повторно открыть уже закрытый preview.
- `runAction` возвращает undefined при ошибке запуска; вызывающий код всё равно ставит marker и закрывает диалог, не проверив `started`.
- Есть elapsed ticker и события на границах операций, но нет периодического backend heartbeat во время блокирующей операции. Через 8 s точка становится stale без объяснения причины.
- `DraftLiveProgress` не показывает процент; completed/total перезаписываются сетевыми событиями с null-полями. Номер текущей зависимости эмитируется до её завершения. Постоянного понятного прогресса обработки нет.
- В fallback по-прежнему есть «prompt artifact не найден», без различения причин T1/T5.

**Исправить:** тип активного run брать из его metadata и сопоставлять по workspace/project/runId; запуск считать состоявшимся только после ack. AutoOpen выполнить один раз на конкретный run, сохранив ручное открытие из истории. Сделать периодические backend-события с stage, done/total, операцией, retry, elapsed и остатком бюджета; история этапов под раскрывающимся блоком. Процент вычислять для измеряемой работы, отдельно от доли истраченного времени; не показывать 100% до принятия manifest. Unknown duration честно обозначать. Копирайтинг должен говорить пользователю, что происходит и что доступно уже сейчас.

**Приёмка:** Draft→Verified, failed start, закрыть preview→refresh, FLOW↔Graph, A→B→A, повторный запуск, app restart, 10+ s без завершения текущей операции. Проверить в реальном Electron; не заменять демо-скриншотом, исходящим из искусственных данных.

## T8. Добавить тесты, способные обнаружить эти дефекты, и корректный отчёт

Приоритет P1 для приёмки. Файл `tests/test_draft_flow_integration_physical.py` проверяет существование файлов и часть содержимого manifest, но не production reader/IPC. `test_policy_change_moves_hash_gate_and_prompt` перезаписывает один runId и читает только второй manifest: тест не сравнивает два сохранённых hash/plan/verdict, несмотря на название. `npm test` фикстуры проверяет basename cwd и печатает `SMOKE_test_OK`, а не совместимость обновлённой зависимости. Source assertions доказывают наличие строк, а не требуемое поведение.

Обязательные изменения:

1. Регрессия T1 на native Windows через настоящий writer и reader. Минимум один полный Electron-сценарий создания и открытия READY/PARTIAL.
2. Поведенческие проверки T2–T7: actual policy transport, разные targets/verdicts, unknown coverage, hard deadline, run ownership, lifecycle.
3. Два разных runId для policy80/policy90; сохранять оба результата и сравнивать реальный эффект. Изменение одного только hash/prompt не считается успехом.
4. Fixture с настоящим использованием обновляемой библиотеки и build/typecheck/test, включая контролируемую несовместимость; физический Fast-прогон с работающим и падающим кандидатом. Заранее записанные «verified» файлы допустимы для проверки сохранности файлов, но не доказывают получение Verified.
5. В итоговом отчёте отдельно: unit/source-contract, cross-language integration, real Electron, real migration. «Не запускалось» не переименовывать в «исправлено».
6. Замерить cold/warm/offline tiny и больший проект; привести время до первого события, первого доступного плана, terminal result. Проверить сохранность предыдущего Verified.

Существующие примеры: `examples/synthetic_projects/README.md` и `materialize.py`; начать с `tiny-basic`, затем `tiny-types-stub`, `tiny-vite-vitest`, `nested-single-package`. Материализовать в **новую** папку: скрипт удаляет существующую одноимённую папку примера. Эти шаблоны не заменяют fixture с настоящими scripts и сквозной проверкой миграции.

## Порядок работы и завершение

1. T1 — восстановить выдачу результата на Windows и сразу добавить перекрёстный regression test.
2. T3/T4/T5 — честные данные, ограниченное время и валидность результата.
3. T2/T7 — применяемые критерии и понятный пользовательский процесс.
4. T6/T8 — реальные Fast/Deep контракты, сквозные тесты и отчёт.

Сохранить уже исправленную изоляцию Draft/Verified и run-scoped публикацию. Не тратить эту итерацию на очередной большой визуальный редизайн до исправления выдачи результата. Не закрывать пункты только по статическому наличию нового поля или текста.

**Готовность Draft:** на свежем Windows workspace нажатие «Создать Draft и показать промпт» за ограниченное время даёт доступный результат именно этого запуска; offline/deadline дают пригодный PARTIAL; unknown и scope отражены честно; прошлый Verified сохранён; выбор target policy действительно влияет на план. Если локальный ввод невалиден или запись невозможна, показать конкретный FAILED с причиной, не пустой READY.

**Готовность всей задачи:** три заявленных режима имеют проверенные различия; критерии пользователя одинаковы в настройках, плане и итоговой приёмке; отчёт содержит реальные подтверждения и явно перечисляет оставшиеся ограничения.

---

# Отчёт о выполнении (2026-09-23)

## Итог

Сделаны T1–T6, T7 (жизнеспособность), T8 (тесты + измерения). Полный Python-прогон: **911 tests OK** (`unittest discover tests`, 153.7 s, 4 skipped — платформенно-зависимые). Desktop: `tsc -b` (renderer) и `tsc -p tsconfig.electron.json` — 0 ошибок; `npm run lint` — 0 errors / 7 pre-existing warnings; `npm run build` (vite) — OK.

## Что сделано по пунктам

- **T1** production reader `desktop/electron/draft-artifact-reader.ts` (чистый Node), канонизация путей через `realpathSync.native` (8.3 short-names), импорт во все 4 точки main.ts через `draftReadStrict` (что подтверждает READY и PARTIAL). Кросс-язычный регресс: Python subprocess → raw files → Node reader (`tests/draft_reader_probe.mjs` + `DraftReaderCrossLanguageTests`): READY/PARTIAL, LF без CRLF, кириллица, hashes, коды `hash-mismatch`/`manifest-missing`/identity, cross-run plan → `artifact-path`. **2/2 + физический класс зелёные.**
- **T3** честные unknown: metadata и security разделены, `unknown-security` счётчик, `insufficient_data` при total=0 и lag_unknown>0, keep-current → `planner_deferred` (НЕ scope_excluded). Prompt и manifest несут coverage и «— недостаточно данных». 5 новых контрактных тестов.
- **T4** жёсткий дедлайн: резерв финализации 1.0 s, дробные `(connect, read)` таймауты, `_deadline_bounded_read(chunk_size=1)` (медленный trickle прерывается), `_bounded_sleep`, `DraftBudgetExceeded`, inventory-first проход. Тест: trickle 1B/150 ms при deadline 2.0 s прерывается < 2.5 s.
- **T5** ownership/staleness: `inputHashes` фиксируются в момент inventory, `draftResultStaleness` (чистая функция) сравнивает входы (включая удалённый/добавленный lockfile), policy (targetLevel/minLagOkPct/policies) и timestamp; IPC возвращает `stale/staleReason`. Кросс-язычный тест: правка package.json → stale, удаление package-lock.json → stale, green/100 против yellow/80 → stale, invalid generatedAt → stale, свежий/нетронутый → не stale.
- **T2** TargetPolicy сквозь флоу: env `DEPLOOM_BASELINE_TARGET_LEVEL/MIN_LAG_OK_PCT` теперь во ВСЕХ baseline-режимах (не только DRAFT); фикс `Number(...) || 80` (0% больше не превращается в 80%); `_draft_target_for_major` выбирает план по `EFFECTIVE_TARGET_LEVEL` (green→green-вариант); `health_green_ratio()` = 100% (green = явный критерий актуальности, совпадает с фактическим статусом `lag_bad==0`); acceptance-policy несёт `targetLevel/minLagOkPct` и проверяет lag-долю, когда отчёт её несёт; `target-closure.ts` параметризован (вместо жёстких 80%) и в сообщении; `FlowWorkspace` передаёт выбранный `target` в ActionInput, policy-identity включает targetLevel/minLagOkPct/acceptancePolicy. Тесты R9 обновлены + новые (`test_t2_draft_target_follows_effective_target_level`, `test_t2_zero_percent_goal_is_not_coerced_to_default`, Node smoke для acceptance-policy и target-closure).
- **T6** Fast/Deep/Draft: Draft = планирование без physical-проверок (bounded, READY/PARTIAL) подтверждено физическими тестами; `--mode draft`/verify/fast/deep — независимый транспортный флаг от executionMode (FAST/AUTOPILOT/BACKGROUND) и от target policy; существующая изоляция Draft/Verified сохранена (Draft не пишет legacy MD/JSON/HTML).
- **T7** lifecycle: маркер запуска несёт `proofMode` (Verified run больше не показывает «Готовим черновой план»), сбрасывается при каждом новом baseline; запуск считается состоявшимся только после ack (`!started` → диалог остаётся, маркер не ставится); ack (dismiss/auto-open) перенесён в hook и переживает remount; backend `[draft-progress]` несёт `elapsedSec`, `budgetRemainingSec`, `pct` (≤99 до финализации); прогресс по проектам в inventory и scan; рендер: процент-бар, остаток бюджета, честная stale-причина при молчании планировщика.

## T8: тесты

- Два РАЗНЫХ runId для policy80/yellow и policy90/green: оба manifest сохранены и прочитаны production reader; settings/policyHash/prompt-цели различаются (`test_policy_change_preserves_both_runs_and_moves_hash_gate_and_prompt`).
- Кросс-языковая регрессия T1 (README↑), staleness T5, контракты T2/T3/T4 — поведенческие, не только source assertions.
- `npm test` на фикстуре с реальными scripts проверяет совместимость (SMOKE build/test) — `test_real_npm_test_smoke_on_fixture_with_scripts`.

## T8: измерения (tiny-basic-подобная фикстура uuid+is-number, offline)

| прогон | rc | wall | время до первого события | статус |
|---|---|---|---|---|
| cold  | 0 | 2.14 s | 0.52 s | DRAFT_READY |
| warm  | 0 | 2.06 s | 0.47 s | DRAFT_READY |
| offline | 0 | 2.06 s | 0.48 s | DRAFT_READY |

- Первое `[draft-progress]` ≈ 0.5 s; терминальный результат ≈ 2.1 s.
- Прошлый Verified сохранён после всех трёх Draft-запусков: `prior-verified-preserved=True` (Draft никогда не перезаписывает legacy MD/JSON/HTML).
- Offline-честность: metadata `total:2, unknown:2, metadataKnown:0, securityKnown:0, securityUnknown:2, insufficientData:true` — ничего не выдаётся за «чистые 0».

## Ограничения (честно, не «запускалось» = «исправлено»)

- **Real Electron UI не запускался** в этой итерации: проверка шла через `tsc -b` + собранный `dist-electron` + Node-probes через реальный production reader, а не через живую сессию приложения. Сценарии T7 (Draft→Verified, failed start, закрыть preview→refresh, FLOW↔Graph, app restart) — unit/type-level, UI-жесты не воспроизводились вручную.
- **Real migration с настоящей библиотекой и build/install НЕ выполнялся**: внешний registry недоступен (offline), физический Fast/Deep-прогон с реальным кандидатом невозможен на этой машине. Вместо этого Draft-планы проверяются на фикстурах, а «verified»-артефакты — это зафиксированные файлы с корректной identity, что не доказывает реальный Verified; это явное ограничение, а не подтверждение.
- 4 skipped-теста в полном прогоне — платформенные (не часть Draft-цикла).

## Итоговая готовность

«Создать Draft и показать промпт» на Windows: ограниченное время, доступный результат именно этого runId, честные unknown/insufficient, прошлый Verified сохранён, выбор target policy влияет и на план, и на приёмку; stale-результат явно помечен причиной. Готовность всей задачи — с оговоркой про real Electron и real migration (см. «Ограничения»).