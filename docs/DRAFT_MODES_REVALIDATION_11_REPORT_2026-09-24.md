# Draft Modes: Revalidation #11 — инкрементальная атомарная публикация при частичной обработке (R11)

Дата: 2026-09-24. Целевая версия: v0.2.129. Статус: **все пункты закрыты, полный набор проверок зелёный** (Python 1017 OK skipped=4, Desktop build, 45/45 check-скриптов, oxlint 0 errors, public-sanitization OK).

## Проблема

| Id | Приоритет | Описание | Статус |
|---|---|---|---|
| R11 | P1 | Draft на реальном 77-пакетном проекте (диагностический прогон run-5fadef80, версия v0.2.128) публикует «нет данных» после успешной обработки части пакетов: 11 пакетов полностью обогащены (registry metadata + OSV), на 12-м дедлайн сработал в `network-read`; в опубликованном `plan.json` `metadataKnown=0/76`, `securityKnown=0/76`, `unknown-security=77`, `proposed=0`, все строки `<registry unavailable>` | Решено |

Диагностика (анонимизированная): `deadlineSeconds=130.5` (`15 + 1.5×77`), частичный результат `DRAFT_PARTIAL`, `elapsedMs≈129511`. Даже завершённые пакеты были опубликованы как `unknown-metadata`, хотя их строка уже была полностью обогащена. Registry был доступен — запросы к нему никогда не доходили до первого пакета, «registry unavailable» для всей выборки — ложь.

**Корневая причина:** `_draft_local_inventory_rows` создаёт 77 строк ДО сети; `analyze_project` накапливает `rows` в локальном списке и возвращает его только после обработки ВСЕГО проекта; `main` присваивает `rows_by_project[project.name] = enriched` только после возврата. При `DraftBudgetExceeded` на пакете 12 функция не возвращается вовсе, `_publish_draft_and_exit` публикует пред-сетевой inventory и помечает каждую строку как registry-unavailable. Усугубления: `_deadline_bounded_read` читал многомегабайтный registry JSON по байту (`iter_content(chunk_size=1)`, ~0.46 с CPU на 1 MiB); Draft дополнительно выполнял tarball/type/runtime-пробки, что укладывалось в «15 секунд на 77 пакетов» только теоретически.

## Изменения

1. **Инкрементальная атомарная публикация.** В `analyze_project` добавлен параметр `draft_sink`: каждая полностью обработанная строка (`DependencyRow`, commit целиком) публикуется в снапшот проекта немедленно — стабильный ключ (package, kind), замена строки целиком, без half-строк; поздний network-reader после таймаута не может мутировать опубликованный снапшот (публикация артефактов атомарна, порядок строк детерминирован). При дедлайте/ошибке публикуются завершённые строки + inventory для остальных; `draft_scan_state` (`pending` → `attempted` перед сетью → `done`) и `draft_unknown` (`registry-failed`/`osv-unavailable`).
2. **Причины неизвестности разведены** (`_draft_metadata_unknown_reason`, `_draft_scan_status_counts`): `not attempted before deadline`, `interrupted` (запрос был в полёте при дедлайне), `registry request failed/HTTP`, `OSV unavailable`, `unrated`. «Registry unavailable» больше не выдаётся для пакетов, до которых сканирование не дошло. В `plan.json` — счётчики `not-attempted / interrupted / registry-failed / osv-unavailable / unrated`, блок `scan` по проектам; в `result.json` — `metadata.processed / processedTotal / pending / interrupted / registryFailed / osvUnknown`; в summary и prompt (RU/EN) — строка «Обработано X/Y (не начаты: N, прерваны deadline: M, ...)». Excluded-пакеты по-прежнему показываются отдельно.
3. **Скорость Draft:** теоретические (PLANNING_ONLY) цели вычисляются из registry metadata для каждой строки сразу в `analyze_project` (`_apply_draft_theoretical_targets`: yellow/green/default из `min_no_critical/min_no_high/lag_update_target_for_row/min_no_vuln`) и попадают в `plan.json` даже на abort-пути; tarball/type/runtime задекларированы как planning-only (`status="planning-only"`, `type_declarations_ok=None`, `runtime_entrypoint_ok=None`, `provides_own_types=True`, `is_installable=True`) без сетевых пробок; запрет foreign-registry target и честный OSV-unavailable сохранены; результат каждой задачи коммитится из main-потока.
4. **Бюджет и чтение.** Сохранены per-request таймауты и мягкий бюджет; `_deadline_bounded_read` читает блоками 64 KiB под абсолютной отсечкой супервизора (`run_supervised("network-read", ...)`), и при срабатывании дедлайна немедленно рвёт сокет (закрытие `SocketIO`, минуя `BufferedReader.close()`, который блокируется на lock читающего потока, и минуя `requests`-`response.close()`, дренирующий тело ~16 с) — drip-body тесты T4 остаются зелёными. На исчерпании бюджета — полезный `DRAFT_PARTIAL`; READY выдаётся только когда весь доступный scope обработан (77/77).
5. **Failing-first интеграционный тест** `tests/test_revalidation_11_incremental_draft.py` (3 + 1 регрессионный тест, герметичный mock-registry+OSV через `DEPLOOM_OSV_QUERY_BATCH`/`DEPLOOM_OSV_VULN`):
   - до фикса (запуск на чистом HEAD): `metadataKnown=0`, DRAFT_PARTIAL «обработано 0» — производственный симптом воспроизведён герметично;
   - после фикса: `metadataKnown=3` (= числу завершённых), в `plan.json`/prompt теоретические targets, у slow-hanging и не начатых пакетов разные причины, поздний worker не меняет артефакты (sha256 стабилен), счётчики `not-attempted=1`, `interrupted=1`;
   - большой packument (2.5 МБ) читается в пределах бюджета.
   - + регрессионный тест `DraftFinalAssignmentPlanningOnlyTests`: полный Draft-путь не должен падать в финальной проверке присвоения.
6. **Приёмка на реальном 77-пакетном проекте** (артефакты — в scratch, отчёт анонимизирован; прогон тем же запуском, что и production: `--draft-baseline --mode draft` + окружение Desktop):
   - **Run A (жёсткий дедлайн 20 с):** `DRAFT_PARTIAL`, `processed=17/77`, `metadataKnown=17/76`, `pending=59`, `interrupted=1`, `registryFailed=0`, `osvUnknown=0`, `proposed=14`; prompt «Обработано 17/77 (не начаты: 59, прерваны deadline: 1)»; данные завершённых пакетов сохранены и различимы — ровно производственный сценарий, но с честной частичностью.
   - **Run B (бюджет 300 с):** `DRAFT_READY`, `processed=77/77`, `metadataKnown=76/76`, `securityKnown=75` (1 пакет без OSV-покрытия — внутренняя зависимость, честный unknown), `proposed=10`, `unknown=1`. `DRAFT_READY` получен только при полной обработке обработанного scope.

В ходе приёмки обнаружен и исправлен вторичный дефект полного Draft-пути: финальная проверка доказанного присвоения (`enrich_registry_target_evidence`, `_candidate_registry_installable`, `validate_final_peer_assignment`) трактовала planning-only evidence как `REGISTRY_TARGET_UNAVAILABLE` и падала с `FINAL_PROVEN_ASSIGNMENT_REGISTRY_DRIFT` для первого же planned-обновления. В Draft планируемые версии принимаются как metadata-кандидаты (PLANNING_ONLY), физическая installability остаётся за агентом.

## UI

Desktop: `DraftResultSnapshot.metadata` расширен (processed/processedTotal/pending/interrupted/registryFailed/osvUnknown/metadataKnown/metadataTotal) в `draft-artifact-reader.ts`, `main.ts`, `src/types.ts`; в карточке Draft (`FlowWorkspace.tsx`) при `DRAFT_PARTIAL` выводится строка «обработано X/Y · метаданные M/N · не начаты · прерваны deadline · registry failed · OSV недоступен».

## Регрессия

- Python: 1017 тестов OK (skipped=4) — включая старые Draft/T4/C2 тесты и новый R11-набор.
- Desktop: `npm run build` ОК, 45/45 check-скриптов, oxlint 0 errors (10 warnings — все до изменений).
- `scripts/check-public-sanitization.py`: OK.
