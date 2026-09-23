# Валидация №4: v0.2.120 и следующее задание

2026-09-23. HEAD `f408a69`, изменения `76376b8`/`ed68197`, база сравнения `3fccb91` (v0.2.119). Приложение в рамках проверки не изменялось. Ниже — самостоятельное задание на оставшиеся исправления; предыдущие переписки для выполнения не нужны.

## Вердикт и закрытые проблемы

**Предыдущий блокер Windows-импорта Draft устранён.** Production writer пишет UTF-8/LF и хеширует байты; production Node reader принимает реальные READY/PARTIAL, в том числе Unicode и короткие Windows-пути. Тесты повреждения, чужой identity и cross-run path проходят. Не надо заново переделывать этот участок без новой причины.

Также подтверждены исправления: 0% больше не нормализуется в 80%; inventory всех проектов собирается перед scan; неизвестная security попадает в plan/prompt; обычная строка с неизвестной security больше не становится зелёной; полностью неизвестный lag не показывается как 100%; keep-current сохраняет участие в health; baseline получает параметры цели и в Draft, и в Verified; медленный JSON body теперь прерывается; удаление lockfile обнаруживается по содержимому; failed start не закрывает диалог.

**Принимать всю задачу пока нельзя.** Остаются реальные разрывы критериев приёмки, deadline, staleness и UI lifecycle. Три режима по-прежнему не реализованы как три различных контракта. Итоговое утверждение предыдущего отчёта о сквозной готовности сильнее подтверждений.

## Что запускалось

- `npm run build` в `desktop`: PASS, включая свежую компиляцию production reader до тестирования.
- `.\.venv\Scripts\python.exe -m unittest tests.test_draft_baseline_contract tests.test_draft_flow_integration_physical tests.test_block_psi53_search_depth_ux tests.test_block_vh_baseline_intent`: **46 tests OK, 25.563 s**. Включает реальную границу Python→файлы→Node. Локальный slow-server напечатал ConnectionAbortedError после ожидаемого прерывания клиентом; suite прошёл.
- В `desktop`: `node scripts/check-baseline-intent.mjs`, `check-acceptance-policy.mjs`, `check-human-flow.mjs`, `check-ui-lifecycle.mjs`, `check-target-closure.mjs`: все пять PASS.
- Независимые поведенческие пробы production-модулей acceptance, target-closure, staleness; реальной normalizeBaselineIntent, извлечённой из TypeScript; Python health и HTTP-клиента с локальным медленным сервером. Результаты ниже.

Границы: полный регрессионный suite в этой проверке не запускался; живой Electron UI и физическая миграция зависимостей не запускались. Выводы по React lifecycle основаны на коде, не на наблюдении окна. Успех writer→reader не означает, что нажатие в Desktop и автооткрытие уже проверены сквозным тестом.

## F1 — P1: единая политика всё ещё распадается по пути к приёмке

**Файлы:** `desktop/src/components/BaselineIntentDialog.tsx:136`, `desktop/electron/main.ts:408,1834,2383`, `desktop/electron/acceptance-policy.ts:69,114,157`, `desktop/electron/target-closure.ts:101`, `desktop/src/components/FlowWorkspace.tsx:53,301,340`.

Подтверждено исполнением production-кода:

| Проба | Текущий результат |
|---|---|
| Нормализовать UI-shaped intent: top-level green/100, вложенный acceptancePolicy C0/H1 | intent остаётся green, но acceptancePolicy становится `{maxKnownCritical:0,maxKnownHigh:1,targetLevel:'yellow'}`, без процента |
| Полный свежий npm audit C0/H1, без lag evidence, с полученной политикой | `ACCEPTED`, `evidenceComplete=true` |
| Явная green/100 policy и audit lag=100, H=1 | `ACCEPTED` — green не заставляет H=0 |
| Полные известные данные lag=80 при yellow90 | `UNKNOWN`, `evidenceComplete=false`, хотя данные известны и критерий просто не достигнут |
| Roadmap status=yellow, lag=8/10, actions=[], requested yellow90 | `reached=true`, одновременно `neededBeyondCurrentPlan=1`, `planCanReachYellow=false` |

Причины:

1. Диалог сохраняет targetLevel/minLagOkPct на верхнем уровне, а `readAcceptanceVerdict` читает только вложенный acceptancePolicy. Нормализатор не объединяет их.
2. Lag gate применяется только **если** audit прислал lagOkPct/compliancePct. При отсутствии поля критерий пропускается. В `manual_dependency_audit.py` нет генерации этих полей; свежесть audit по hash/времени не доказывает актуальность библиотек.
3. Green не фиксирует High=0; M/L-критерии также не проходят через общую policy.
4. Closure параметризовали для расчёта дефицита, но `reached` всё ещё доверяет старому цвету roadmap и отсутствию actions, не проверяя новый порог.
5. `generate`/`generate-all` всё ещё не передают сохранённую target policy генератору. Исправление передачи только baseline недостаточно: последующая регенерация может вернуть default80.
6. `runBaselineIntent` передаёт выбранный target, но обычные stage actions и startAutopilot используют оставшуюся константу `target='yellow'`. Green теряется на следующем шаге.

**Задание:** один нормализованный TargetPolicy и одна identity через UI→baseline/Draft→generate→audit evidence→closure/acceptance→autopilot/release. Выбрать единственное место хранения, поддержать миграцию прежнего intent. Обязательный неизвестный критерий даёт UNKNOWN; известное невыполнение — REMEDIATION_REQUIRED. Не выдавать ACCEPTED при отсутствующем доказательстве lag. Связать lag evidence с теми же dependency inputs и policy, что и security evidence. Пересчитать reached по текущим критериям, а не прежнему цвету. Green preset: 100% по явно описанной lag-policy, C=0/H=0; M/L явно задать численно. Уточнить в UI lag-порог (например 12 месяцев), а не только процент.

**Приёмка:** UI intent green100 действительно доходит в acceptance как green100/H0; yellow80→yellow90 меняет verdict на одинаковом наборе 8/10; regenerate и autopilot сохраняют цель; missing lag=UNKNOWN; known80<90=REMEDIATION_REQUIRED; H1 при Green не принимается. Нельзя одновременно возвращать reached=true и shortfall>0. Тестировать реальный UI-shaped payload и обычный audit producer, а не только вручную сконструированную policy с дополнительными полями.

## F2 — P1: покрытие данных и цвет Yellow ещё дают ложное достижение цели

**Файл:** `dependency_live_roadmap_generator.py:5352–5460`; потребитель `target-closure.ts`.

Прямые production-пробы:

- 10 пакетов: один известный актуальный, девять с неизвестными lag/security → `lag_ok_pct=100`, `total=1`, `lag_unknown=9`, `status=yellow`.
- Актуальный пакет с `H:10` → `status=yellow` при продуктовом default High≤1.

Исправлена крайность 0 известных пакетов, но при частичном покрытии доля всё ещё считается лишь среди известных. Это допустимо как отдельно подписанная метрика «из исследованных», но не как доказательство ≥80% всех пакетов. Unknown теперь блокирует Green, однако Yellow продолжает выглядеть достигнутым даже без данных, необходимых для его критериев.

**Задание:** разделить «оценка исследованной части» и «цель доказанно достигнута». Общий scope, известная часть, unknown и исключения должны быть отдельными числами. Unknown нельзя автоматически считать compliant или исключать из знаменателя общей цели. Применять High-limit из F1, не произвольное `high>0 => yellow`. Сохранить правильное новое поведение keep-current и explicit exclusion. Если жёлтый также используется как визуальный цвет неизвестности, обязательный отдельный machine-readable verdict должен исключать его трактовку как выполненной Yellow-цели.

**Приёмка:** 1 известный/9 unknown не закрывает Yellow80; 8 известных compliant/2 unknown не закрывает security-gate без нужных доказательств; High0/1/2 и custom-limit согласованы в health, closure и acceptance. Не ограничиваться тестом `status != green`.

## F3 — P1: hard deadline исправлен только для части HTTP-операций

**Файл:** `dependency_live_roadmap_generator.py:2262,2299,2553,2721` и вызовы тяжёлого planning.

Независимые реальные HTTP-пробы без npm/OSV:

| Сценарий | Budget | Время | Результат |
|---|---:|---:|---|
| HTTP-заголовок передаётся понемногу, байт каждые 100 ms | 2 s | **3.523 s** | DraftBudgetExceeded лишь после получения всех заголовков |
| `fetch_bytes`, тело 26 байт по одному каждые 150 ms | 2 s | **3.786 s** | `None`, deadline exception поглощён |

Это время отдельных production-методов, не полное время Desktop. Ответы можно удлинить и тем же способом превысить default15 s.

Причины: `requests.get(stream=True)` всё равно блокируется до окончания заголовков, а socket read timeout не ограничивает всю их передачу. В `fetch_bytes` остался `iter_content(chunk_size=64*1024)` с проверкой после получения chunk; `except Exception` превращает DraftBudgetExceeded в обычное отсутствие данных. Этот метод используется для registry tarball/type-evidence. Проверки перед/после CPU-heavy planning также не доказывают ограничение длительности самой операции.

**Задание:** абсолютный deadline для всего run, включая DNS/connect/headers/body/decompression, retries и planning; для непрерываемых участков — ограниченный по времени worker с supervisor и сохранённым inventory. Не считать пару `(connect,read)` hard deadline. Во всех helpers сохранять отдельную семантику DraftBudgetExceeded. Не вводить более длинные таймауты как «решение». Пользователь должен получить локальный предварительный план до завершения обогащения, а PARTIAL — по бюджету с измеренным резервом на публикацию.

**Приёмка:** JSON trickle (уже проходит), headers trickle, binary/tarball trickle, hanging endpoint, CPU-heavy solve; отдельно terminal artifact и его открытие. При заданном budget измерять весь путь до доступного результата. Зафиксировать допустимый небольшой overhead и проверить его; не ожидать пятиминутного stall abort.

## F4 — P1: неполная инвалидация результата и ложная «свежесть»

**Файлы:** `desktop/electron/draft-artifact-reader.ts:73,106,129–169`; `main.ts:712,6309`; `FlowWorkspace.tsx:74`; Python partial publish около `22253,22320`.

Реальные вызовы production staleness вернули `stale=false` при:

- изменении допустимого High1→High0;
- отсутствующем `inputHashRecorded`;
- добавлении/изменении `.dependency-roadmap/settings.project.json`.

Список входных файлов не содержит project/local settings, dashboard policy и других влияющих настроек; проверяются только targetLevel/minLagOkPct/policies. Если inputHash отсутствует (`undefined`), ветка обнаружения отсутствующей identity пропускается. Для partial во время planning/finalization новый `draft_input_hashes` не передаётся: writer может снова пересчитать hash при публикации и привязать план к изменившимся данным.

Дополнительно: `draftResultForProject` возвращает snapshot без вычисления stale. Карточка `draftResultFresh` проверяет runId/ack, но не актуальность входов. Полная staleness вычисляется лишь при открытии промпта; `staleReason` в PromptPreviewDialog не отображается.

**Задание:** полный canonical input/policy fingerprint, общий с планировщиком; missing identity означает непроверенную свежесть, не fresh. Сохранять снимок исходных данных и identity во всех READY/PARTIAL ветках. Вычислять и передавать stale/reason также для карточки; отделить «новый результат запуска» от «соответствует текущим входам». Исторический результат оставлять доступным с понятной причиной устаревания. Учитывать nested-project layout, settings, dashboard overrides и все численные acceptance-ограничения.

**Приёмка:** изменить High, lagMonths, exclusion, project/local settings; удалить inputHash; изменить manifest при планировании, затем вызвать PARTIAL на разных фазах. Во всех случаях правильный stale/reason виден и в карточке, и в preview; raw artifact истории не переписывается.

## F5 — P2: auto-open и периодический прогресс не закончены

**Файлы:** `FlowWorkspace.tsx:75–96,579,594–607`, `useDependencyFlow.ts` (ack/progress), `LiveDataClient.progress`.

По коду:

1. Ack перенесли в hook, но автооткрытие его **не вызывает**. Закрытие preview тоже только сбрасывает `draftPromptPreview`. Поэтому новый объект details или remount может снова открыть тот же run. Вызов ack есть лишь у отдельной кнопки «Принято». Отчёт о one-shot auto-open не соответствует связям вызовов.
2. Timer берёт `progress.elapsedSec`, делит секунды на 1000 и на каждом тике выставляет одно и то же значение. React может вообще не перерисовывать компонент, пока backend молчит. Отображение использует зафиксированный backendElapsed; остаток бюджета также заморожен. Даже stale-индикатор после 8 s зависит от случайного внешнего rerender.
3. Регулярного backend heartbeat для Draft нет: это события на границах операций. Поля elapsed/budget в редких событиях не создают heartbeat.
4. completed/total/pct заменяются следующими сетевыми событиями с null; после inventory почти99% могут смениться0% или исчезнуть. Не показано, что это разные измеряемые этапы. completed зависимостей эмитируется до окончания обработки.

**Задание:** ack/autoOpen-consumed по runId при успешном открытии и/или закрытии (не до подтверждённого получения prompt); сохранить ручной доступ из истории. Для времени — единицы seconds/milliseconds согласовать и продолжать monotonic отображение с момента последнего backend события. Heartbeat формировать независимо от блокирующей работы. Разделить stage-progress и общий progress; измеряемые counters сохранять между событиями, не выдавать phase ratio за завершение всего run. Историю этапов и пояснение паузы — под раскрывающимся блоком.

**Приёмка в Electron:** открыть→закрыть prompt→refresh/FLOW↔Graph: повторного автооткрытия нет; 10 s без событий — время продолжает идти и stale появляется предсказуемо; Draft→Verified корректно меняет описание; проценты явно относятся к этапу, terminal100 только после принятия результата. В этой проверке окно не запускалось; эти сценарии нельзя закрыть одними source assertions.

## F6 — P1 для полного объёма: Fast/Deep и сквозная проверка всё ещё отсутствуют

В реализации по-прежнему отдельный Draft и общий Verified-путь. `--mode draft/fast/deep` — транспортное поле; наличие enum не доказывает отдельную стратегию остановки Fast или сохранение лучшего результата при последующих попытках Deep. В отчёте «T6 сделано» описана изоляция Draft/Verified, а не запрошенные три режима. Недоступность внешней сети не объясняет отсутствие отдельных веток и локальных тестов стратегии.

**Задание:** Fast прекращает дорогой поиск после первого удовлетворяющего выбранной policy проверенного результата и имеет свой бюджет/лимит попыток. Deep продолжает улучшение в большем бюджете, сохраняя лучший уже доказанный результат при ошибке/timeout. Draft не выполняет install/checks. Эти контракты должны быть доступны из UI и проверены журналом фактически вызванных операций. Если часть не реализована — прямо обозначить её как незавершённую, не объявлять работу полностью готовой.

Тестовый `build.js` в `tests/test_draft_flow_integration_physical.py:298` всё ещё проверяет лишь basename cwd и печатает `SMOKE_*_OK`. Он не импортирует обновляемую библиотеку. Поэтому утверждение отчёта «scripts проверяют совместимость» неверно. Два policy-run теперь сохраняются отдельно — это исправлено, но тест пока сравнивает settings/hash/prompt, а не изменение итогового verdict всей цепочки.

**Приёмка:** fixture с реально используемой библиотекой и работоспособными build/typecheck/test, контролируемые совместимый/несовместимый кандидаты; Fast/Deep с разным числом операций и сохранением best result. Можно использовать локальный registry/предподготовленные tarballs и одноразовый Git remote. Плюс хотя бы один настоящий Electron сценарий «нажатие→Python→reader→IPC→prompt», READY/PARTIAL, повторный запуск и reopen после рестарта. Отдельно учитывать unit, cross-language, Electron и real migration; результаты одного слоя не выдавать за другой.

## Порядок следующей итерации

1. F1/F2 — единая policy, честный verdict и непротиворечивый closure.
2. F3/F4 — гарантированное время и полная привязка результата к входам.
3. F5 — проверить пользовательский сценарий в живом приложении.
4. F6 — отдельные режимы и реальные fixtures.

Сохранить исправленные writer/reader, раздельную публикацию Draft/Verified, byte hashes и существующие cross-language тесты. Не возвращаться к общему редизайну и не заменять обязательные сценарии дополнительными проверками наличия текста в исходниках.

Итоговый отчёт должен перечислять выполненные команды, конкретные expected/actual для F1–F6, времена до первого/terminal результата и честные незакрытые ограничения. Эта итерация действительно улучшила Draft, но успешный импорт артефактов не закрывает автоматически policy, deadline и UI.