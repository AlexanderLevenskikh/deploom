# Draft Modes: Revalidation #12 — ограничение кандидатов не скрывает актуальные версии; честный candidate-search-truncated (R12)

Дата: 2026-09-24. Целевая версия: v0.2.130. Статус: **все пункты закрыты, полный набор проверок зелёный** (Python 1020 OK skipped=4, Desktop build, 45/45 check-скриптов, oxlint 0 errors, public-sanitization OK).

## Проблема

| Id | Приоритет | Описание | Статус |
|---|---|---|---|
| R12 | P1 | В v0.2.129 Draft обработал 77/77 зависимостей реального 77-пакетного проекта и выпустил DRAFT_READY, но план не приближался к цели yellow (80% lag-OK): активный scope 76, lag OK 12, lag blockers 63; предложено 10 обновлений, **53 из 63 lag blockers — `no-target`**; проекция после плана 21/76 (27,6%) против требуемых 61/76 (разрыв 40) и планового буфера 65/76 (разрыв 44); 66 строк «нет безопасного target», 1 Critical и 5 High | Решено |

**Корневая причина:** Draft по умолчанию использует `draft-max-candidates=3`, а `versions_from_current` обрезал список до первых трёх версий **начиная с установленной** ДО вычисления `min_by_lag`/`min_by_vuln`. Для старых зависимостей первые три версии обычно тоже старые, поэтому `min_by_lag` писал «не найден installable target в configured registry», хотя свежие версии были в том же packument (metadata уже прочитана в этом же запуске, `latest` известен). Это не отказ registry и не недостаток security-информации — усечение поиска маскировало реально существующие цели. Prompt прятала 53 нарушителя в «Нет проверяемого target» с общей классификацией группы вместо причины планировщика.

Локальный синтетический вызов функций подтверждает: версии 1.0.0–1.0.10, первые три (1.0.0–1.0.2) старые — `min_by_lag(12м)` на усечённом списке не находит цель; на полном списке находит свежую версию.

## Изменения

1. **Поиск по полной локально доступной registry metadata.** В `analyze_project` (полная ветка) пространство поиска для lag-целей — ВЕСЬ диапазон `versions_from_current(..., max_candidates=0)`: применяется `registry_structural_candidates` (дешёвый фильтр tarball-URL, конфиденциальный foreign-registry ban сохранён) + `current`. `min_by_lag` (12/9/6/3 месяца) вычисляется по полному диапазону всегда (и на abort-пути через `_apply_draft_theoretical_targets`, который использует те же минимумы строки). Дорогой OSV-запрос остаётся ограниченным: evidence-сеть `[current, *первые N, latest, min_12, min_9, min_6, min_3]` покрывает ровно версии, которые могут стать целями. Дефолтный лимит больше не может скрыть пригодную цель; не-draft путь с явным `--max-candidates` сохранён (лаг-поиск тоже на полном диапазоне).
2. **`min_by_vuln`: отсутствие OSV-данных = unknown, а не clean.** Версия без записи в `vulns_by_version` пропускается (не объявляется «без уязвимостей»), поэтому безопасная версия, скрытая за усечением, никогда не выдумывается как безопасный target.
3. **Честный `candidate-search-truncated`.** Строка получает флаг `candidate_search_truncated` + причину, когда явный лимит оставил версии вне evidence-сети. Для таких строк статус — `candidate-search-truncated` (отдельная причина, НЕ `no-target`, не `registry unavailable`, не доказанное отсутствие installable target), счётчики в `plan.json`/`result.json`, строка попадает в unknowns с человекочитаемым пояснением (сколько версий из скольких не проверено).
4. **Разделение статусов строк** (`_draft_row_status` + `_draft_policy_satisfied`): `ok` (уже удовлетворяют политике: security оценена без C/H/U и lag-OK), `proposed` (конкретный target), `blocked` (теоретический target заблокирован peer/registry условием — compatibility note/cohort), `candidate-search-truncated`, `no-target` (нет безопасного target по полным данным), `unknown-security` (OSV не оценена ИЛИ найденная уязвимость без рейтинга U); метаданно-unknown остаются `unknown-metadata`. Для `no-target` reason — реальная причина планировщика (`_draft_no_target_reason`: какая именно размерность не закрывается, значение `min_lag_*`/`min_no_critical`), а не `row.reason` о группе.
5. **Post-plan проекции.** `build_draft_plan` пересчитывает `yellow/green_projected_lag_ok/pct` и `yellow/green_plan_shortfall` по ИТОГОВЫМ целям плана и публикует их как `postPlanLagOk / postPlanLagOkPct / postPlanShortfall` (в health каждого проекта и агрегаты в `result.json`). Аналогично — `yellow_plan_required`. В prompt (RU/EN) добавлен блок «Цель (projected по этому плану)»: актуально / требуется / projected / shortfall / C-H блокеры; разделы `ok`/`blocked`/`candidate-search-truncated`/`unknown-security`/`no-target` вместо единого «Нет проверяемого target».
6. **`result.json`/summary.** `unknown`/`unknownPackages` = только пакеты, которым реально нужно уточнение (без выбранного target: unknown-security/candidate-search-truncated/blocked/no-target/unknown-metadata); `candidateTruncated` = сколько строк имеет усечённый поиск, `candidateTruncatedTargetless` = из них без target; `noTarget`, `blocked`, `ok`, post-план агрегаты. Summary: «предложено N обновлений; для K пакетов нужно уточнение (security неизвестна: M); поиск кандидатов усечён: X строк».
7. **UI.** `DraftResultSnapshot.metadata`/`DraftResultArtifact.metadata` расширены (noTarget/candidateTruncated/candidateTruncatedTargetless/blocked/ok/postPlanLagOk/postPlanLagOkPct/postPlanShortfall); карточка Draft в `FlowWorkspace.tsx` показывает строку «projected lag-OK … · shortfall N · поиск кандидатов усечён N · без безопасного target N · target заблокирован N».

## Failing-first

`tests/test_revalidation_12_candidate_scope.py` — 5 тестов:
- юнит: `versions_from_current(cap=3)` прячет свежую версию от `min_by_lag`, полный список находит;
- **до фикса (чистый HEAD):** «Draft на дефолтных настройках находит теоретический target за первой тройкой» → FAIL (`status=no-target`), «явный лимит помечает candidate-search-truncated» → FAIL (`status=no-target`) — производственный симптом воспроизведён герметично;
- **после фикса:** оба теста зелёные; `proposed=1` с target `1.0.7`, `no-target=0`; усечённый случай → `candidate-search-truncated=1`, причина «candidate search truncated …» в unknowns;
- сквозная согласованность: 3 lag-блокера на полном диапазоне → `proposed=3`, `postPlanLagOk=3`, `postPlanShortfall=0`, prompt «shortfall: 0»;
- сквозной блокер: пакет с Critical, безопасная версия за пределом покрытия → статус `candidate-search-truncated`, Critical остаётся в health, причина в unknowns, lag-проекция по-прежнему честна.

Герметичный `_MockRegistry` (packument + tarball + OSV querybatch/vulns через `DEPLOOM_OSV_QUERY_BATCH`/`DEPLOOM_OSV_VULN`) — без внешней сети.

## Приёмка на реальном 77-пакетном проекте

Повторный прогон тем же production-инвокативным запуском (`--draft-baseline --mode draft`, дедлайн `15 + 1.5×77 = 130.5 c`, политика yellow/80/12, maxKnownCritical=0, maxKnownHigh=1, Dashboard policy с теми же исключениями). Артефакты — в scratch, имена пакетов/пути/registry в отчёте не публикуются.

| Метрика | R11 (v0.2.129) | R12 (v0.2.130) |
|---|---|---|
| Статус | DRAFT_READY | DRAFT_READY |
| Обработано | 77/77 | 77/77 |
| Метаданные известны | 76/76 | 76/76 |
| **proposed** | **10** | **62** |
| **no-target** | **66** | **0** |
| ok (уже соответствуют политике) | — (маскировались no-target) | 12 |
| candidate-search-truncated (всего строк с усечённым поиском) | — | 62 (из них без target — 1) |
| unknown-security | 1 | 1 |
| excluded | 1 | 1 |
| unknown (нужно уточнение) | 1 | 2 (1 — усечённый поиск без target + cohort-блокировка, 1 — security неизвестна) |
| Lag blockers с назначенной целью | 10/63 | **62/63** (без цели — только 1, cohort-блокирован) |
| Projected lag OK (post-plan) | 21/76 (27,6%) | **74/76 (97,4%)** |
| Shortfall против планового буфера 65/76 | 44 | **0** |
| Critical / High | 1 / 5 | 1 / 5 |
| elapsedMs | ≈95 609 | 105 868 (в пределах бюджета 130,5 c) |

Итог по цели: 74 ≥ 65 (плановый буфер) ≥ 61 (80% gate) — предложенный план способен закрыть yellow-цель; единственная незакрытая lag-строка — конкретная cohort-блокировка (честный `candidate-search-truncated`/blocked с причиной), а не «нет проверяемого target». 1 Critical и 5 High остаются на текущих версиях и видны в блоке цели prompt/UI как C/H блокеры.

## Регрессия

- Python: **1020 тестов OK** (skipped=4) — включая все старые Draft/C1/T3/T4/C2/peer-наборы и новый R12-набор (5 тестов).
- Desktop: `npm run build` ОК, 45/45 check-скриптов, oxlint 0 errors (10 warnings — все до изменений).
- `scripts/check-public-sanitization.py`: OK.
