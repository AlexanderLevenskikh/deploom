Повторная проверка DepLoom 0.2.118 и задание на исправление — 22 сентября 2026

Проверены изменения `59a296b..9f4b2a1`: основной implementation commit `972d76e`, последующие release commits, текущий HEAD `9f4b2a1`. Рабочее дерево перед проверкой чистое. Предыдущее задание: `docs/DRAFT_MODES_AUDIT_AND_AGENT_TASK_2026-09-22.md`.

Вердикт: работу целиком НЕ принимать. Это частичное исправление Draft с полезными новыми артефактами, но с блокирующими дефектами интеграции и существенными невыполненными требованиями. Реальная выдача частичного Draft через Desktop всё ещё ломается. Увеличивать таймауты или объявлять задачу выполненной по зелёному build нельзя.

**1. Что действительно исправлено**

- Python теперь отдельно записывает result.json, plan.json, prompt.md, summary.md в runs/<runId>/draft. Промпт можно создать без экспорта из HTML/DOM; это проверено выполнением кода.
- Добавлены runId, workspaceId/projectId, snapshot настроек из env, hash промпта/плана, указатель на последний Draft в Desktop state и новый IPC чтения Draft.
- Старый watcher по mtime/size/seenRunning удалён.
- Source checkout guard отключён для Draft, lockfile update запрещён через allow_update=False, release intelligence по умолчанию отключён. Сохраняется запрет продвижения Draft по verified FLOW и публикации ProofEnvelope.
- Часть ошибок registry переводится в неизвестные metadata. Контролируемый offline-прогон с читаемым manifest сохранил промпт и JSON.
- Добавлен load_tests: свободные функции Draft-тестов теперь собираются unittest. Прежний пробел CI для этого файла устранён.
- materialize.py отказывает при существующем примере без --overwrite и проверяет границы удаляемого каталога при явной перезаписи.

Это следует сохранить. Ниже перечислены оставшиеся дефекты, а не предложение начать реализацию заново.

**2. Результаты выполненных проверок**

| Проверка | Результат и предел доказательства |
| --- | --- |
| npm run build в desktop | PASS: Electron/React TypeScript и Vite production build |
| unittest: test_draft_baseline_contract, test_block_phi_execution_modes, test_baseline_progress | 22 tests PASS, включая 11 Draft-тестов; это не сквозной Desktop FLOW |
| check-human-flow, check-progressive-contract, check-baseline-intent, check-ui-lifecycle | PASS; преимущественно source-contract проверки, часть ещё закрепляет старые требования |
| Python main: deadline до сканирования | exit 0, DRAFT_PARTIAL создан, legacy JSON отсутствует; plan.projects=[], metadata.total=0 при одной зависимости в manifest |
| Python main: deadline 150 мс, контролируемый scan 350 мс | Завершение за 453 мс, DRAFT_PARTIAL; deadline не прерывает активный этап |
| Python main: metadata unavailable | За 133 мс сохранён DRAFT_READY с unknown=1. Это stubbed network, не benchmark реального registry |
| analyze_project: OSV unavailable при доступных metadata | current_vulns="0", min_no_critical=min_no_vuln="10.0.0" — воспроизведена ошибочная подмена неизвестного нулём |
| Python main: корректный pnpm manifest + pnpm-lock.yaml | exit 2, PACKAGE_MANAGER_PNPM_UNSUPPORTED, результата Draft нет |
| Реальная функция executeJob, зависимости заменены тестовыми заглушками: partial без legacy | importDraftResult не вызывается; job-finished.exitCode=1, PROJECT_ARTIFACT_SNAPSHOT_FAILED |
| executeJob: legacy есть, manifest отсутствует | job-finished.exitCode=0 без draftResult — ложный успешный статус |
| Реальная функция readDraftResultArtifact на manifest с другой identity | Принят manifest с другим runId/workspaceId/projectId, без обязательных путей/хешей |
| Разрешение пути при settings.project.json в корне workspace | Writer: .dependency-roadmap/artifacts; reader: artifacts — подтверждено несовпадение |
| Browser: http://127.0.0.1:5173/?demo=1, 1280×720 | UI загружается, Technical details раскрываются, Capture Baseline → Start открывает диалог; нет framework overlay и console errors/warnings |

Первичные ошибки build (spawn EPERM) и шести тестов (доступ к Windows Temp) были ограничениями песочницы. Повтор с разрешённым доступом завершился успешно; их не записываем в дефекты продукта.

Обычный browser URL без demo=1 ожидаемо показывает DESKTOP_BRIDGE_UNAVAILABLE, поскольку Electron preload в браузере отсутствует. Для визуального smoke явно включён существующий demo-режим. Это НЕ доказательство работающего Electron IPC, реальной установки пакетов, внешнего агента или release. Desktop end-to-end не запускался. Browser проверка охватывает desktop viewport; mobile не проверялся, продукт — Desktop.

Локальные диагностические скрипты и результаты, созданные в разрешённом каталоге вне репозитория (пути к временным файлам профиля опущены для публичной сдачи; см. `docs/DRAFT_MODES_REVALIDATION_REPORT_2026-09-23.md`):

- `revalidate_draft.py`
- `revalidate_draft_more.py`
- `revalidate_desktop.cjs`
- `desktop-probe-results.json`
- `draft-validation-fixtures/`

Скрипты — диагностические стенды; фикстуры создаются с отказом при повторном существовании каталогов. Для регулярного CI перенести подтверждённые сценарии в изолированные тесты с собственным lifecycle. В первом скрипте pnpm-проба имела конфликт менеджера и npm lock, а OSV stub был неполон: эти два предварительных результата не используются как доказательство. Второй скрипт воспроизводит оба случая на корректных входах.

**3. Блокирующие и существенные дефекты**

**R1 · P0. Частичный Draft отвергается Desktop до импорта результата.**

Код: `desktop/electron/main.ts:5882–5917`, `dependency_live_roadmap_generator.py:21031–21070`.

_publish_draft_and_exit записывает новый формат артефактов и выполняет SystemExit(0), поэтому нижний блок записи legacy roadmap.{md,json,html} не выполняется. executeJob для любого baseline сначала делает snapshotProjectArtifacts и бросает PROJECT_ARTIFACT_SNAPSHOT_FAILED, если legacy JSON отсутствует. Ветка importDraftResult находится после этого. На новом проекте типичный timeout/partial закончится ошибкой вместо показа существующего промпта. Если старый legacy отчёт уже есть, поведение зависит от случайно сохранившегося предыдущего запуска.

Отдельно: при отсутствии result.json код только пишет строку в log и сохраняет exitCode=0, включая уведомление об успехе. Это нарушает новый контракт «процесс успешно завершился только при доступном результате».

Исправить: отдельная ветка финализации Draft до любых snapshot/cleanup Verified. Читать и валидировать result manifest конкретного run; partial является доступным пользовательским результатом. Отсутствующий/невалидный manifest — typed FAILED с причиной и путём. Не требовать и не переиспользовать legacy roadmap для Draft. Событие завершения должно содержать runId независимо от наличия результата.

Приёмка: настоящий сценарий Electron subprocess → DRAFT_PARTIAL → import → job-finished → видимый prompt на чистом workspace без любых старых артефактов. Отдельно проверить нулевой exit с отсутствующим manifest, отмену и сбой записи.

**R2 · P0. Заявленный короткий deadline не ограничивает работу и может оставить запуск без артефакта.**

Код: `dependency_live_roadmap_generator.py:20512–20539`, `21547`, `21593`, `21625`, `21642`, `21718`; сетевые методы `2383` и далее, analyze_project `4070` и далее.

DeadlineClock проверяется только на границах крупного scan/planning. HTTP requests, retries, ожидание ThreadPoolExecutor, tarball/type inspection и solver не получают remaining budget. В контролируемом тесте бюджет 150 мс не прервал scan 350 мс; результат пришёл за 453 мс. В production сохранены per-request timeout=30 секунд и retries. На практике draft может ждать дольше 15 секунд многократно. Ограничение candidate cap не устраняет скачивание/проверку registry артефактов и расчёт всех yellow/green/default.

Проверка deadline_clock.check("draft-plane") находится вне обработчиков DraftBudgetExceeded: истечение времени после final-targets, но до публикации, выбросит исключение без partial-result. Общая обработка допускает partial лишь в двух try-блоках, не при всех допустимых ошибках обогащения/solver.

При deadline до первого scan публикуется пустой plan.projects=[] и 0 неизвестных при реально существующем uuid в package.json. Это JSON-файл, но не полезный план исходных зависимостей. До сети нужно гарантированно собрать локальный inventory; затем обогащать строки, сохраняя все неизвестные.

Исправить: маленький самостоятельный путь Draft с первым локальным результатом; единый deadline с момента принятия job. Передавать остаток времени HTTP/solver/worker или выполнять обогащение в управляемом отменяемом процессе. Публиковать initial/partial inventory до тяжёлых операций, иметь общий finalizer для deadline/cancel/допустимых ошибок. Убрать обязательные tarball, type checks, три варианта и точный solver из пути первого Draft. Не объявлять успехом пустой inventory из-за того, что сканирование не началось.

Приёмка: slow HTTP, slow solver, retry/backoff, зависший future, истечение непосредственно перед publish; артефакт с полным локальным списком зависимостей выдаётся за выбранный срок плюс ограниченный документированный запас финализации. Проверять от клика до доступного результата, не только elapsedMs внутри Python.

**R3 · P1. При недоступности OSV неизвестное состояние уязвимостей становится нулём.**

Код: `dependency_live_roadmap_generator.py:4296–4310`.

Новый catch VulnerabilityEvidenceUnavailable устанавливает vulns={}. Затем vuln_summary(vulns.get(current, [])) возвращает "0", а min_by_vuln использует пустой список как отсутствие уязвимостей. При воспроизведении current_vulns="0" и текущая версия становится одновременно no-critical/no-vuln target. Это не доказанный обход независимого release audit: дефект затрагивает planning/health/prompt и может дезинформировать пользователя/внешнего агента.

Дополнительно offline-план при неизвестной registry metadata пишет в prompt «Lag OK: 100.0% (0/0), C/H/M/L: 0/0/0/0», хотя ниже признаёт неизвестный пакет. _row_metadata_known считает только факт наличия latest, а не полноту OSV/lockfile. DRAFT_READY сейчас публикуется даже для полностью неизвестных metadata; критерий READY/PARTIAL не учитывает фактическую полноту.

Исправить: типизированные состояния UNKNOWN/COMPLETE для metadata и security; UNKNOWN не передавать как пустой массив подтверждённых findings. Не вычислять safe target на недоступном evidence. Показывать «неизвестно» и покрытие вместо нулевых C/H и фиктивных 100%; частичный статус производить из реальной полноты, не только исключения deadline. Сохранить fail-closed независимого audit/release.

Приёмка: registry доступен, OSV timeout/429/5xx; ни plan, ни prompt, ни summary/health не содержат утверждений zero-vulnerabilities/no-vuln/полной актуальности на неизвестных данных. Аналогично проверить отсутствующий lockfile и unknown severity.

**R4 · P1. Результат исчезает из интерфейса после смены вкладки/перезапуска; свежесть не привязана к запущенному run.**

Код: `desktop/src/components/FlowWorkspace.tsx:54–78`, `215–226`, `390–404`; `desktop/src/App.tsx:89`; `desktop/src/hooks/useDependencyFlow.ts:454–479`.

Карточка отображается только если локальный Set launchedDraftProjects содержит имя проекта. После размонтирования FlowWorkspace (Graph/Dashboard → FLOW), перезапуска приложения или acknowledge карточка скрывается. Постоянной кнопки повторного открытия именно последнего Draft вне этого условия нет. Комментарий о restart-safe доступности «через карточку» не соответствует условию JSX.

Set добавляется до onRun и не содержит runId/workspaceId. Уже сохранённый старый draftResult может показаться свежим сразу при новом запуске, даже если этот запуск ошибся. runAction по-прежнему поглощает ошибку старта; новый runId ответа IPC не используется для маршрутизации UI. Prompt автоматически не открывается: кнопка «Создать Draft и показать промпт» приводит только к карточке с ещё одной кнопкой. openDraftPrompt читает результат текущего выбора, а не конкретного run; защита от смены проекта во время await отсутствует.

Исправить: API runAction возвращает jobId/runId либо ошибку; состояние запуска и результата вынести в устойчивое хранилище по workspaceId/projectId/runId. Отделить одноразовое уведомление от постоянной доступности сохранённых результатов. После готовности открыть ожидаемый prompt; после acknowledge оставить «Последний Draft / История». Не открывать старый результат как новый. Ошибки старта и чтения не превращать в «artifact не найден» без исходной причины.

Приёмка: Draft → Graph → FLOW; Draft → restart; acknowledge → reopen; повторный запуск с предыдущим результатом; ошибка нового запуска; два workspace с одинаковыми именами проектов; переключение выбранного проекта во время чтения промпта.

**R5 · P1. Draft всё ещё перезаписывает текущий Verified roadmap/dashboard.**

Код: `desktop/electron/main.ts:5882–5891`, snapshotProjectArtifacts; завершающая запись legacy отчётов в Python.

Новые draft-файлы изолированы, но успешный путь main() после publish продолжает записывать обычные MD/JSON/HTML, а executeJob копирует их в общий projectArtifactCache. Текущий projectRoadmapPath/projectDashboardPath будет читать planning-only отчёт вместо прежнего Verified. Защита proven-dependency-state не защищает UI-кэш и источник следующего prompt export.

Исправить: Draft должен завершаться собственной публикацией и импортом, не обновляя latestVerifiedRoadmap/latestVerifiedDashboard/latestVerifiedPrompt/checkpoint. Для просмотра отдельного Draft отчёта использовать отдельный путь и явный переключатель. Не «чинить» R1 обязательной генерацией legacy отчётов: это оставит R5.

Приёмка: сохранить действующий Verified; выполнить ready/partial/failed/cancelled Draft; хеши и указатели всех verified артефактов неизменны, результат Draft при этом доступен.

**R6 · P1. Reader и writer расходятся в путях; identity и целостность manifest фактически не проверяются.**

Код: `desktop/electron/main.ts:682–697`, `2288–2300`, `6275–6312`.

Writer всегда получает workspace/.dependency-roadmap/artifacts. draftArtifactsRoot(reader) возвращает dirname(settingsPath)/artifacts. Для поддерживаемого settings.project.json в корне workspace пути различаются: существующий результат не находится.

readDraftResultArtifact проверяет лишь status и непустую строку runId. Он не сравнивает ожидаемые run/workspace/project, не проверяет schemaVersion, обязательные paths/files/hashes/authority. Тест с другим runId/workspaceId/projectId и отсутствующими артефактами проходит как валидный. IPC чтения prompt доверяет путям из manifest, не проверяет их принадлежность каталогу run и hash, возвращает stale=false без сравнения inputHashes с текущими входами.

Исправить: одна функция разрешения artifact root, используемая на запуске и чтении; хранить exact manifest reference принятого job. Валидировать version, identity, обязательные файлы, containment путей и hash. Отдельно вычислять устаревание относительно входов проекта и policy; не выставлять stale=false константой. Передавать явные workspace/project/run параметры IPC, а не определять их по глобальному selectedProject.

Приёмка: settings в корне и в .dependency-roadmap, повреждённый manifest, несовпадающие identity/hashes, отсутствующий prompt, изменение package.json после Draft, переключение workspace во время preview. Для каждого — точный результат или typed error, а не чужой/ложно свежий prompt.

**R7 · P1. Некоторые читаемые проекты по-прежнему не получают Draft.**

Код: `dependency_live_roadmap_generator.py:21443–21472`; `lockfile_consistency.py:1032`.

Draft вызывает ensure_lockfile_consistency с mode=off, но до проверки этого режима функция требует supported authoritative topology. Корректный pnpm manifest + pnpm-lock.yaml завершается PACKAGE_MANAGER_PNPM_UNSUPPORTED с exit 2 и без Draft. Catch разрешает лишь три lockfile-кода. Аналогично нужна проверка Yarn Berry и неоднозначного/отсутствующего lockfile.

Исправить: читать manifest/доступный lockfile для Draft без требования authoritative verification capability. Неподдерживаемый physical manager — ограничение следующего Verified шага, не запрет теоретического плана. Некорректный JSON остаётся явной ошибкой; отсутствие proof-support должно давать полезный planning-only результат.

Приёмка: npm, Yarn Classic, pnpm, Yarn Berry, manifest-only, stale/ambiguous lockfile. Draft не изменяет исходные файлы и явно указывает точность анализа; Verified не получает ложную поддержку менеджера.

**R8 · P1. Состояния процесса и регулярный прогресс не реализованы.**

Код: `FlowWorkspace.tsx:334–352`, `App.tsx:89`; diff не добавляет поток progress в карточку.

Во время любого baseline, включая Draft, всё ещё выводятся «Ищем первый проверенный результат» и «физически проверяем проект». Режим активного job не передаётся в эту логику. Под катом остаются стадии полного FLOW и счётчик выполненных команд, а не live операции текущего Draft. Новая карточка содержит технические runId/deadline/authority после завершения, что не заменяет обратную связь во время работы.

Исправить согласно пункту 6 первоначального задания: короткое понятное состояние сверху, лента этапов под «Ход работы», отдельная диагностика; heartbeat 1–2 секунды, run-scoped sequence, текущие пакет/операция/retry, прошедшее время, actual completed/total там, где объём известен. Не путать процент этапа, budget spent и freshness. Не рисовать точный общий процент бесконечного поиска. Сохранённый лучший результат отделить от текущей попытки.

Приёмка: UI демонстрирует изменения состояния на медленном registry и молчащем subprocess; Draft ни разу не объявляется физической проверкой. Смена проекта не смешивает события; 100% означает доступный итоговый артефакт.

**R9 · P1. Быстрый/глубокий режимы и настраиваемые критерии почти полностью остались в прежнем виде.**

BaselineIntentDialog, acceptance-policy, block_psi_anytime и schema цели не изменены. FlowWorkspace содержит const target='yellow'. В диалоге по-прежнему только Control, бюджет от 5 минут, C=0 и High; нет процента актуальности/окна/Yellow-Green-Custom. Это подтверждено Browser DOM и screenshot.

CLI --mode draft/fast/deep добавлен, но фактический Draft-branch определяется args.draft_baseline; поле mode преимущественно записывается в manifest. Само по себе --mode draft не включает обещанный путь. Для fast/deep DeadlineClock(None), старый anytime бюджет всё ещё создаётся внутри for mode (`dependency_live_roadmap_generator.py:10658`, `10759`). Три названия в argparse не являются тремя реализованными режимами.

Старые пороги 80%, M+L≤20 и исключение keep-current из здоровья остались (`346`, `4680`, `5157–5160`). Новая policyHash — hash env-строк, не эффективной единой политики: не включает фактически применённые settings/override lagMonths, registry и область измерения. Prompt содержит hash, но не численные цели пользователя. Acceptance по-прежнему не проверяет freshness/M/L; комментарии о невозможности drift благодаря hash не подкреплены потребителями.

Исправить по пунктам 4 и 7 первоначального задания: продуктовый mode независим от автономности, единый deadline, выбранный вариант вместо трёх обязательных, fast останавливается по своему контракту; единая versioned policy с snapshot/hash проходит UI → planner → prompt → audit/closure/release. Настройки процентов, допустимого отставания, vulnerability limits и области подсчёта реально влияют на план и приёмку. Unknown и keep-current не улучшают метрику молча. Обновить старые source-contract tests, которые запрещают freshness controls, вместо сохранения нежелательной семантики ради PASS.

Приёмка: изменение 80→90% изменяет выбор targets и условие достижения цели; Yellow/Green имеют реальные различия, custom сохраняется; готовность partial не выдаётся за достижение цели. Проверить границы округления, High 0/1/2, unknown, scope, M/L, устаревший audit. Значение слова «нет лага» и конкретизацию «единичные» явно показать в настройках, как предложено в первом документе.

**R10 · P2. Покрытие тестами пока не подтверждает пользовательский результат.**

Новые тесты проверяют DeadlineClock отдельно, offline prefetch отдельно, publish отдельно и наличие строк. Они не вызывают полную цепочку main → executeJob → UI и не покрывают пограничные случаи R1–R9. Full build PASS ожидаем при таких логических ошибках.

Новые настоящие build/typecheck/test fixtures не добавлены; четыре synthetic manifests по-прежнему без scripts. Это позволяет проверить зависимости, но не полный процесс миграции/проверки/release. Требование примера полного FLOW из первого задания открыто.

**4. Порядок выполнения повторного задания агентом**

1. R1 + R5: выделить отдельную финализацию Draft, убрать обязательный legacy snapshot и любые изменения verified-артефактов. До следующего UI-изменения доказать end-to-end выдачу partial на чистом workspace.
2. R2 + R3 + R7: локальный inventory до сети, управляемый deadline/отмена, честные unknown, bounded enrichment, Draft для читаемых менеджеров. Никаких ложных no-vuln targets или 100% на неизвестных данных.
3. R4 + R6: строгая run identity, единое разрешение путей, валидация manifest/files/hash, persistent result access и автоматическое открытие ожидаемого prompt. Не смешивать состояние уведомления и существование результата.
4. R8: подключить настоящие события процесса в простой интерфейс, проверить медленные/молчащие этапы.
5. R9: реализовать три режима и критерии цели от UI до приёмки. Это обязательная часть исходного задания; «пока исправлен только Draft» не является завершением всей работы.
6. R10: добавить интеграционные сценарии, полноценный маленький build/test проект, реальные smoke/измерения и обновить документацию.

Не требуется повторять аудит вместо исправлений. Начать с регрессионных поведенческих тестов на воспроизведённые причины; применять изменения узко, сохраняя действующие guards verified FLOW. Использовать Git/Windows editing policy репозитория. Не удалять unrelated изменения и не публиковать очередной release как доказательство исправности.

**5. Минимальная матрица приёмки исправления**

| Сценарий | Обязательный результат |
| --- | --- |
| Fresh workspace, deadline до/во время scan | Полный список локальных зависимостей с unknown + partial prompt в Desktop, без legacy JSON |
| Истечение перед publish / HTTP / solver / ожиданием future | Соблюдён общий deadline; результат не пропадает; дочерняя работа завершена |
| exit 0, manifest отсутствует/битый | Typed FAILED, без успеха/уведомления «готово» |
| Повреждены run/project/workspace identity, paths или hashes | Отказ с конкретной причиной; чужой/повреждённый prompt не показывается |
| OSV unavailable, metadata available | UNKNOWN, safe targets не выдуманы; нигде не показано 0 vulnerabilities как факт |
| Все metadata unavailable | Полный inventory, покрытие 0/N, unknown freshness/security, полезный partial prompt |
| Draft после Verified: ready/partial/cancel/fail | Все verified pointers/files/proofs/checkpoints сохранены |
| Повторный Draft, вкладки, restart, acknowledge | Правильный результат постоянно доступен, старый не выдаётся за новый |
| root settings.project.json и .dependency-roadmap/settings.project.json | Одинаково работающая публикация/импорт |
| pnpm/Yarn Berry/manifest-only | Draft доступен; отсутствие verified support ясно обозначено |
| Fast первый кандидат PASS / FAIL / timeout | Контракт stop/budget выполнен; точный уровень проверенности, fallback не объявлен PASS |
| Deep PASS → следующая попытка FAIL | Лучший verified result сохранён, цель и текущий прогресс отображены отдельно |
| Пользователь меняет policy | Реально меняется план и приёмка, пороги присутствуют в prompt, policyHash соответствует эффективным значениям |
| UI slow run | Различимые этапы и heartbeat, корректные счётчики, отсутствие текста про физическую проверку Draft |
| Реальный минимальный проект с scripts | Есть recorded npm/Yarn smoke и результаты install/build/typecheck/test, отдельно от mock тестов |

**6. Формат сдачи агентом**

Дать таблицу R1–R10: исправлено / не исправлено / ограничение с конкретным доказательством. Приложить результаты integrated tests, Desktop сценария partial на чистом workspace, cold/warm/offline timings, пример prompt/manifest, сохранность Verified до/после, UI screenshots живого процесса и повторного открытия результата. Отдельно назвать непроверенные сценарии и не считать demo UI тестом Electron backend.

Текущая проверка не меняла код приложения. Добавлен только этот документ; диагностические файлы находятся вне Git-репозитория.

Снимок проверенного диалога (demo UI, 1280×720; показывает текущие поля и отсутствие настроек freshness/трёх режимов) выполнен для v0.2.118 и сохранён в локальной диагностической папке (в публичную сдачу не включается; свежие UI-снимки после исправлений — `.playwright-mcp/flow-after-tab-return.png` и `.playwright-mcp/r10-flow-live.png`).
