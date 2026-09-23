# Валидация №5: текущие правки и оставшееся задание

Дата: 2026-09-23. Проверены **незакоммиченные изменения поверх `f408a69` / v0.2.120**. Новый release commit отсутствует. Это проверка рабочего дерева, а не уже выпущенной версии. Существующие изменения приложения и тестов не редактировались; добавлен только этот документ.

## Вердикт

Исправлено существенно больше, чем в предыдущей итерации. **Основной блокер выдачи файлов Draft на Windows остаётся закрытым; новые проверки проходят.** Однако полную готовность принимать рано: остались ошибки фактического применения политики, определения достижения цели, staleness и подключения режимов. При исправлении deadline появилась гонка с продолжающим работать потоком.

Это самостоятельное задание на пять конкретных групп проблем. Уже исправленные части не нужно переделывать заново.

## Проверки и подтверждённый прогресс

Выполнено:

```powershell
# desktop
npm run build
node scripts/check-baseline-intent.mjs
node scripts/check-acceptance-policy.mjs
node scripts/check-human-flow.mjs
node scripts/check-ui-lifecycle.mjs
node scripts/check-target-closure.mjs
node scripts/check-progressive-contract.mjs
# PASS: сборка и все 6 проверок.

# Корень репозитория
.\.venv\Scripts\python.exe -m unittest tests.test_draft_baseline_contract tests.test_draft_flow_integration_physical tests.test_f1_acceptance_evidence_chain tests.test_block_psi_anytime tests.test_block_phi_execution_modes tests.test_dependency_roadmap
# 134 tests, 55.408 s, OK.
```

В stderr HTTP fixtures есть ожидаемые ConnectionAbortedError после отключения клиента; итог suite успешный.

Дополнительно выполнены независимые пробы production Node-модулей acceptance/closure/staleness и production Python-функций inventory/supervisor.

Что закрыто или улучшено:

- Windows byte-hash writer→production reader и защита от чужих артефактов сохраняются.
- UI-shaped target policy теперь объединяется с acceptancePolicy; stage actions используют сохранённый target.
- Audit producer формирует lag evidence и policy snapshot; missing lag не принимается автоматически, известное невыполнение численного порога получает REMEDIATION_REQUIRED.
- Scope-проценты добавлены отдельно от исследованной части; прежний пример «1 известный/9 неизвестных = цель Yellow80» устранён через scope gate.
- Ограничение ожидания headers/binary/CPU теперь вынесено в supervisor; профильные deadline-тесты проходят. Безопасность состояния после timeout отдельно не обеспечена — G4.
- Missing input identity, изменение High и settings внутри project.path обнаруживаются. Но появился пропуск keep-current — G3.
- По коду автооткрытие теперь вызывает ack после получения prompt; timer продолжает elapsed между событиями; появился backend heartbeat.
- Добавлены реальные локальные library fixtures и проверки install/build/test. Старую претензию «проверяется только cwd» ко всему новому набору больше не предъявлять.
- Fast имеет новую внутреннюю ветку остановки и отдельные helper-бюджеты. Но UI до неё пока не доходит — G5.

**Границы:** полный Electron UI и сквозной production Fast/Deep в этой проверке не запускались. 134 теста — выбранный набор, не весь репозиторий. Поведенческие выводы ниже подтверждены указанными пробами; UI-связи и transport проверены статически. Нельзя на основании этого отчёта утверждать, что весь Desktop FLOW уже пройден.

## G1 — P1. Выбранные lag-месяцы и численные ограничения не управляют планом полностью

**Файлы:** `dependency_live_roadmap_generator.py:4481,21737,5560` и `policy_snapshot_from_env`; `desktop/electron/main.ts` (`goalPolicyEnv`, `actionCommands`); `BaselineIntentDialog.tsx` (`buildIntent`).

Теперь можно выбрать 3/6/9/12 месяцев. Audit получает `--lag-months`, но planner по-прежнему вычисляет `lag_threshold_months` как package override либо **12**, не читая выбранные месяцы из `DEPLOOM_ACCEPTANCE_POLICY_JSON`.

**Выполненная проба:** передать policy `{lagPolicyMonths:3, maxKnownHigh:0, targetLevel:'yellow', minLagOkPct:80}`, создать ProjectSpec с одной dependency и вызвать production `_draft_local_inventory_rows(project,{})`. Получено **`row.lag_threshold_months=12`**, при запросе 3. В основном `analyze_project` используется тот же fallback12.

High/M/L тоже не составляют единую планировочную policy: health по-прежнему допускает Green при суммарном M+L≤20, тогда как UI теперь задаёт Green M=0/L=0. Наличие этих значений в snapshot/hash не означает применения к выбору targets. `generate-all` передаёт одну policy выбранного проекта для всех проектов — значения других проектов теряются.

**Исправление:** прочитать и нормализовать всю effective policy в planner; применить общий lag-порог с явно заданным приоритетом per-package override. Использовать согласованные High/M/L при выборе targets, оценке кандидата и health. Для generate-all передавать политики по проектам либо выполнять отдельные задания, сохраняя identity каждого проекта. Не менять требования пользователя молча: если зелёный пресет ужесточён до M/L=0, это должно быть явно видно и одинаково работать во всех слоях.

**Приёмка:** один набор registry/OSV данных при lag3 и lag12 даёт соответствующие targets/health; High0/1 и M/L-границы отражаются и в плане, и в audit/acceptance. Два проекта с разными policy после generate-all сохраняют свои критерии. Сравнивать реальные действия/оценки, а не только строку prompt или hash.

## G2 — P1. Closure ещё закрывает цель при неизвестной security; проверка policy snapshot допускает пропуски

**Файлы:** `desktop/electron/target-closure.ts:110–149`, `desktop/electron/acceptance-policy.ts:215–239`.

Выполнены вызовы собранных production-модулей:

1. Roadmap: status=yellow, scope_total=10, scope_lag_ok=8, scope_lag_pct=80, scope_lag_unknown=2, security_unknown=2, unknown=2, C=0/H=0; executable actions отсутствуют. `targetClosureFromRoadmap(...,'yellow',80,1)` вернул **`reached=true`**. Данные о двух неизвестных security-результатах не участвуют в gate.
2. Свежий полный npm audit с C0/H0, lag100 и `report.policy={targetLevel:'yellow'}`; текущая policy требует lag3. `acceptanceVerdictFromManualAudit` вернул **ACCEPTED**: отсутствие lagPolicyMonths/minLagOkPct/High в snapshot не считается несовпадением или отсутствием доказательства.

Первое — оставшийся вариант прежнего F2. Второе — незаконченная привязка evidence к обязательным полям новой политики. Не трактовать отсутствие поля как разрешение воспользоваться значением по умолчанию или пропустить сравнение. Важна также разница между долей заведомо compliant пакетов и полнотой security coverage.

**Исправление:** общий goal verdict с проверкой обязательного security coverage. Closure не должен считать цель достигнутой, если необходимые C/H-данные неизвестны. Для legacy roadmaps отсутствие обязательных чисел означает непроверенную цель, а не доверие одному цвету. Policy snapshot проверять по обязательной versioned схеме; нужны явные значения критериев, реально использованных producer. Audit producer должен публиковать эту схему целиком, включая M/L, если они являются частью цели.

**Приёмка:** 8 compliant/2 security-unknown не дают reached; отсутствие security totals в legacy health не закрывает цель. Пропуск каждого обязательного policy-поля, mismatch lag3/lag12, High/M/L mismatch дают UNKNOWN либо требование обновить evidence. Полный совпадающий report проходит; известное нарушение порога остаётся REMEDIATION_REQUIRED.

## G3 — P1. Регрессия staleness для keep-current и неполные пути входов

**Файл:** `desktop/electron/draft-artifact-reader.ts` (`storedPolicyFromSnapshot`, `draftResultStaleness`, `DRAFT_INPUT_FILENAMES`); Python `draft_input_hash` около 21153.

**Воспроизведено:** при валидном inputHash и совпадающих численных критериях добавить `uuid:'keep-current'` в currentPolicy.policies. Production staleness вернула **false**. Причина: `storedPolicyFromSnapshot` извлекает только acceptancePolicyJson; дальнейший поиск `stored.intentJson` выполняется уже в этом вложенном объекте. Отдельный `settings.intentJson` потерян, поэтому сравнение keep-current/required не выполняется.

**Воспроизведено отдельно:** package.json в `<workspace>/frontend`, settings в `<workspace>/.dependency-roadmap/settings.project.json`; после изменения корневых settings reader возвращает **stale=false**. Hash читает только фиксированные пути относительно project.path. Root-level settings.json, реальные разрешённые пути settings, groups/dashboard вне package-root также не охвачены этим списком.

В карточке осталось условие показа предупреждения `!draftResultFresh && draftResult.stale`: новый результат запуска может быть устаревшим относительно текущих входов, но предупреждение до ack скрыто. «Новый запуск» и «актуальные входы» должны быть независимыми признаками.

**Исправление:** сравнивать численную acceptancePolicy и package intent отдельно, сохраняя оба исходных snapshot. Fingerprint строить по реально прочитанным файлам и разрешённым путям, а не предположению, что всё находится рядом с package.json. Сохранить byte parity Python/Node. Показывать stale всегда, когда он установлен, независимо от fresh/ack.

**Приёмка:** auto→keep-current→required, normal и nested layouts, root settings и custom dashboard/groups paths. Изменение каждого влияющего входа даёт stale с причиной; неизменные данные не дают ложный stale. Отдельный regression на manifest с одновременно заполненными intentJson и acceptancePolicyJson.

## G4 — P1. Supervisor ограничивает ожидание, но не изолирует продолжающего работать planner

**Файл:** `dependency_live_roadmap_generator.py:21043` (`run_supervised`) и supervised planning около 22407–22534.

Новая реализация запускает daemon Thread, делает join с timeout и выбрасывает DraftBudgetExceeded. Поток не останавливается и работает с теми же изменяемыми `rows_by_project`, rows и caches, из которых основной поток начинает строить PARTIAL.

**Прямая проба production helper:** worker ждёт Event, затем меняет общий target. При budget1.1 s helper выбросил timeout примерно через **0.102 s** (с резервом1 s); после возврата управления event отпущен, worker успешно изменил target на `after-timeout`. То есть после timeout записи в общее состояние продолжаются. Это подтверждение отсутствия изоляции; в этой проверке не утверждается, что был получен конкретный повреждённый manifest реального проекта.

Комментарий «процесс сразу завершится, значит worker больше ничего не запишет» не учитывает время вычисления health, формирования plan/prompt и атомарной записи файлов **до** SystemExit. Публикация читает структуры параллельно с их изменением. Даже неизменность Verified-файлов не гарантирует согласованность PARTIAL.

**Исправление:** worker должен работать с отдельным состоянием и возвращать результат, который применяется только при успешном завершении до deadline. Альтернатива — управляемый subprocess с прекращением работы и публикацией заранее зафиксированного snapshot. На timeout запрещено читать mutable state продолжающего работать worker. Обеспечить согласованность health, proposals, summary и input identity одного snapshot. Не ждать неограниченно завершения worker и не увеличивать deadline.

**Приёмка:** worker меняет targets/rows непосредственно после timeout, пока publication намеренно задержана. Опубликованный PARTIAL остаётся неизменным и внутренне согласованным; после terminal result нет поздних изменений результата. Сохранить проходящие tests headers/binary/CPU и короткие deadlines.

## G5 — P1 для полного объёма. Fast/Deep появились в ядре, но не подключены из Desktop; stop rule подменяет цель

**Файлы:** `main.ts` (`ActionInput/BaselineIntent`, `actionCommands`), `BaselineIntentDialog.tsx`; Python `_baseline_product_mode`, `record_verified_incumbent` около 11303 и `_verified_assignment_satisfies_policy` около 11509; `tests/test_draft_flow_integration_physical.py:767`.

Что уже сделано: разные helper-бюджеты, внутренняя Fast-stop ветка, сохранение incumbent. Это прогресс относительно отсутствовавшей стратегии.

Что осталось:

- В Desktop нет передачи product mode fast/deep: DEPLOOM_MODE задаётся только для Draft. Verified идёт в default `verify`, который выбирает deep strategy; FAST/BACKGROUND executionMode не является новым product mode. Пользователь не может запустить эти две стратегии как обещанные режимы.
- `_verified_assignment_satisfies_policy` считает `round(policy_score * total)` и сравнивает с lag-процентом. Но `policy_score` — **доля совпадений с desired_assignment**, не доля пакетов, удовлетворяющих lag/security. Совпадение 8 из10 желаемых обновлений не доказывает Yellow: оставшиеся два могут включать Critical/High-пакет. Физическая совместимость кандидата также не доказывает security/lag goal.
- Новый тест `test_f6_fast_deep_are_real_strategies_not_transport_flags` запускает оба mode через helper, который всё равно добавляет `--draft-baseline`, затем проверяет labels и отдельные helper-бюджеты. Он не исполняет Verified Fast/Deep stop/search loop. Реальные library fixtures полезны, но этот пробел ими не закрыт.

**Исправление:** отдельный product mode в UI/IPC/persistence/command transport, независимо от управления подтверждениями и executionMode. Fast останавливается по оценке фактического verified candidate в общей TargetPolicy, не по проценту совпадения с desired. Deep продолжает разрешённое улучшение, сохраняя лучший проверенный результат. Mode и бюджет должны иметь однозначный приоритет относительно сохранённых legacy env-настроек.

**Приёмка:** запуск из Desktop действительно выбирает fast/deep; журнал показывает разное количество дорогих попыток на одном управляемом fixture. Кандидат с 8/10 desired matches и оставшимся Critical не удовлетворяет Yellow. Кандидат с меньшим числом desired matches, но уже выполненной целью, может завершить Fast. Deep сохраняет incumbent при следующем сбое. Хотя бы один тест должен пройти реальную Verified orchestrator-ветку, а не Draft с названием fast.

## Условия завершения следующей итерации

1. До исправления зафиксировать G1–G5 failing regression-сценариями; после исправления показать их результат.
2. Сохранить работающие Windows artifact reader, deadline ожидания, реальные library fixtures и producer→acceptance тесты.
3. Пройти один настоящий Electron-сценарий: выбрать policy→запустить Draft→получить prompt→закрыть→переключить вкладку→открыть вручную; отдельно PARTIAL и смену входов. Для Fast/Deep — реальный транспорт и orchestrator согласно G5.
4. Итоговый отчёт разделяет: unit/source, cross-language, реальные subprocess/fixtures, Electron UI. Незакрытые пункты остаются явно открытыми. Применение поля в hash или выполнение отдельного helper не означает готовности всего контракта.

Приоритет — G1/G2 (правильный результат), G3/G4 (актуальность и согласованность Draft), G5 (доступные пользователю режимы). Полный визуальный редизайн для этой итерации не требуется.