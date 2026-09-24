# Draft Modes: Revalidation #12 — честная scope-проекция цели (policy-гейт vs плановый запас, post-plan security, goal verdict) + не скрываемые candidate-версии (R12)

Дата: 2026-09-24. Целевая версия: v0.2.130. Статус: **все пункты закрыты (исходные R12 + аудит F1–F4), полный набор проверок зелёный** (Python 1029 OK skipped=4, Desktop build, 45/45 check-скриптов, oxlint 0 errors, public-sanitization OK, CI и Release v0.2.130 зелёные после фикса VERSION BOM).

## Проблема (R12, исходная)

| Id | Приоритет | Описание | Статус |
|---|---|---|---|
| R12 | P1 | В v0.2.129 Draft обработал 77/77 зависимостей реального 77-пакетного проекта и выпустил DRAFT_READY, но план не приближался к цели yellow (80% lag-OK): активный scope 76, lag OK 12, lag blockers 63; предложено 10 обновлений, **53 из 63 lag blockers — `no-target`**; проекция после плана 21/76 (27,6%) против требуемых 61/76 (разрыв 40) и планового буфера 65/76 (разрыв 44); 66 строк «нет безопасного target», 1 Critical и 5 High | Решено |

**Корневая причина:** Draft по умолчанию использует `draft-max-candidates=3`, а `versions_from_current` обрезал список до первых трёх версий **начиная с установленной** ДО вычисления `min_by_lag`/`min_by_vuln`. Для старых зависимостей первые три версии обычно тоже старые, поэтому `min_by_lag` писал «не найден installable target в configured registry», хотя свежие версии были в том же packument (metadata уже прочитана в этом же запуске, `latest` известен). Усечение поиска маскировало реально существующие цели.

## Аудит приёмки (review R12) — F1–F4

| Id | Приоритет | Замечание аудита | Фикс |
|---|---|---|---|
| F1 | P1 | Порог политики подменялся плановым запасом: `post_health.get("yellow_required")` всегда пуст (у ProjectHealth нет такого поля), fallback подставлял `yellow_plan_required` (+5 п.п.), и prompt писал «требуется 65/76 (80%)», а shortfall считался от 65 | В `ProjectHealth` добавлено `yellow_required` и оно возвращается из `compute_project_health`. Требования и shortfall разделены: `postPlanPolicyRequired`/`postPlanPolicyShortfall` (гейт пользователя) и `postPlanReserveRequired`/`postPlanReserveShortfall` (+5 п.п., чётко подписан как «плановый запас»). `postPlanShortfall` = shortfall ПО ПОЛИТИКЕ, никогда по запасу. Prompt: «требуется по политике: 61/76 (80%); плановый запас (политика +5 п.п.): 65/76» |
| F2 | P1/P2 | UI показывал `postPlanLagOk / (postPlanLagOk + postPlanShortfall)` → 74/74 вместо 74/76; агрегат `postPlanLagOkPct` был суммой процентов проектов (2×50% → 100%) | Знаменатель — весь активный scope: `postPlanLagOk / postPlanScopeTotal` (74/76, 97.4%). Агрегат — взвешенная доля `sum(projectedLagOk)/sum(scopeTotal)` (юнит: 8/10 + 25/50 → 33/60 = 55.0%, не 130). В манифест добавлен `perProject` — у каждого проекта свои числа, Desktop рендерит view выбранного проекта |
| F3 | P1 | «yellow достижим» доказывался только по lag; нет проекции security на точных выбранных версиях; отсутствие OSV трактовалось как «чисто» | Добавлена проекция по ТОЧНЫМ target-версиям для всех активных строк (`_projected_row_security` на `_severity_at_candidate_version`): `postPlanCritical/High/SecurityKnown/SecurityUnknown/SecurityTotal`. Вердикт цели `postPlanGoal` = `feasible` / `unknown` / `blocked` по `effective_acceptance_policy` (maxKnownCritical/maxKnownHigh), публикуется в result.json/prompt/UI. Текущие и projected C/H показываются рядом. `DRAFT_READY` — статус сканирования, отдельно от достижимости цели |
| F4 | P2 | Сигнал усечения перегружен: все 62 truncated-строки (включая 61 actionable proposed) попадали в «требуют уточнения»; дефолтный лимит назывался «явным выбором пользователя» | В unknowns остаются только targetless/неоценённые (статусы unknown-metadata/unknown-security/candidate-search-truncated/blocked/no-target). Разделены счётчики `candidateTruncated` (все строки с усечённым поиском) / `candidateTruncatedTargetless` (без target) / `candidateTruncatedProposed` (с конкретным target, actionable несмотря на caveat). В prompt — отдельное примечание «поиск кандидатов ограничен лимитом для N из M proposed строк — target требует проверки tarball/registry», а не намёк на явный выбор пользователя |

## Изменения

1. **Поиск по полной локально доступной registry metadata** (исходный R12): lag/safety-поиск всегда на полном диапазоне `versions_from_current(..., max_candidates=0)` + `registry_structural_candidates` + `current`; дорогая OSV evidence-сеть остаётся ограниченной `[current, *первые N, latest, min_12, min_9, min_6, min_3]` (ровно кандидаты-цели); не-draft путь с явным `--max-candidates` сохранён.
2. **`min_by_vuln`: отсутствие OSV-данных = unknown**, а не clean.
3. **Честный `candidate-search-truncated`** как отдельный статус/счётчик/причина (не `no-target`, не `registry unavailable`).
4. **Разделение статусов строк** (`_draft_row_status` + `_draft_policy_satisfied`): `ok/proposed/blocked/candidate-search-truncated/no-target/unknown-security`; `unknown-metadata` — отдельно; для `no-target` — реальная причина планировщика.
5. **F1: разделение policy-гейта и планового запаса.** `ProjectHealth.yellow_required` (из `compute_project_health`); `build_draft_plan` публикует `postPlanPolicyRequired`/`postPlanReserveRequired`/`postPlanPolicyShortfall`/`postPlanReserveShortfall`; `postPlanShortfall` — по политике; `yellow_plan_shortfall` — по запасу. В prompt RU/EN строка требований подписывает запас отдельно и никогда не выдаёт 65/76 за «(80%)».
6. **F2: взвешенный агрегат и per-project.** `draft_post_plan_aggregate` — `sum(projectedLagOk)/sum(scopeTotal)`; health каждого проекта публикует свои postPlan*-числа; в манифест добавлен `perProject` (postPlan* + counts по статусам + scopeTotal). UI: знаменатель `postPlanLagOk/postPlanScopeTotal` с процентом, per-project view для выбранного проекта.
7. **F3: post-plan security и вердикт цели.** `_projected_row_security(row, target)` по точному target через `_severity_at_candidate_version` (N2-подобное evidence по OSV per version); `_draft_goal_verdict` — feasible/unknown/blocked с `effective_acceptance_policy` проекта; итог по всем проектам — `_draft_aggregate_goal`. Prompt RU/EN: «Security projected на выбранных версиях: Critical X / High Y; OSV-покрытие целей: A/B, без OSV-данных: C» + «Оценка достижимости цели: …». Summary/манифест: `postPlanCritical/High/Security*/Goal`; текущие C/H также показываются.
8. **F4: разгрузка «требуют уточнения».** `unknown`/`unknownPackages` — только строки без выбранного target (см. выше); отдельные счётчики усечения; примечание про лимит поиска для proposed; старый заголовок «Поиск кандидатов усечён (лимит кандидатов)» сохранён для targetless.
9. **Desktop.** Исправлен баг: `buildDraftResultSnapshot` не пробрасывал R12/F1–F3 поля (`postPlan*`, noTarget, candidateTruncated*, blocked, ok) — чипы Draft-карточки никогда не рендерились. Теперь тип `DraftResultSnapshot.metadata` расширен и снапшот копирует все поля; при наличии `perProject[projectId]` выбирается per-project view (числа выбранного проекта). `FlowWorkspace.tsx` показывает «projected lag-OK N/M (p%)» по полному scope, projected C/H на целях, цели без OSV и вердикт цели («цель достижима» / «цель не подтверждена (OSV)» / «цель НЕ достижима этим планом»), плюс чипы усечения/no-target/blocked.
10. **VERSION.** Найден и устранён баг, ломавший CI/Release: `VERSION` содержал UTF-8 BOM (`\xef\xbb\xbf0.2.130`), `cat ../VERSION` ≠ `node -p "require('./package.json').version"` → шаг «Version consistency» desktop-джобы падал, Release v0.2.130 не публиковался. Файл переписан байт-в-байт как `b"0.2.130"` (без BOM, без перевода строки — как в v0.2.129).

## Failing-first

`tests/test_revalidation_12_candidate_scope.py` — 12 тестов (5 исходных R12 + 7 новых `DraftGoalHonestyTests`):
- F1 (76 строк): policyRequired 61, reserveRequired 65, projected 74/76 (97.4%), оба shortfall 0, `no-target=2`, prompt «61/76 (80%)» присутствует, «65/76 (80%)» отсутствует, «плановый запас» присутствует; manifest/perProject-числа совпадают;
- F2 (юнит): `draft_post_plan_aggregate` 8/10 + 25/50 → 33/60 = 55.0 (не 130);
- F3a: текущая Critical устраняется ТОЧНОЙ выбранной версией → projected Critical 0, goal feasible;
- F3b: выбранная версия СНОВА содержит Critical → projected Critical 1, goal blocked;
- F3c: нет OSV-данных у exact target → SecurityUnknown 1, goal unknown (не safe);
- F1 (юнит): оба shortfall по одной строке считаются раздельно;
- F4 (subprocess): proposed-строка с усечённым поиском → `unknowns == []`, `candidateTruncated=1`, `candidateTruncatedProposed=1`, `candidateTruncatedTargetless=0`, в разделе «требуют уточнения» её нет, примечание «поиск кандидатов ограничен лимитом» есть.
**До фикса (чистый HEAD)**: новые тесты давали 6 errors (KeyError postPlan*) + 1 failure; исходные 5 тестов R12 снова ловили симптом (status=no-target). **После фикса**: 12/12 зелёные.

Герметичный `_MockRegistry` (packument + tarball + OSV querybatch/vulns через `DEPLOOM_OSV_QUERY_BATCH`/`DEPLOOM_OSV_VULN`) — без внешней сети.

## Приёмка на реальном 77-пакетном проекте

Повторный прогон тем же production-инвокативным запуском (`--draft-baseline --mode draft`, дедлайн `15 + 1.5×77 = 130.5 c`, политика yellow/80/12, maxKnownCritical=0, maxKnownHigh=1, Dashboard policy с теми же исключениями). Артефакты — в scratch, имена пакетов/пути/registry в отчёте не публикуются.

| Метрика | R11 (v0.2.129) | R12 (v0.2.130, финальный прогон) |
|---|---|---|
| Статус | DRAFT_READY | DRAFT_READY |
| Обработано | 77/77 | 77/77 |
| Метаданные известны / security оценена | 76/76 | 76/76; 75/76 OSV |
| **proposed** | **10** | **62** |
| no-target | 66 | 0 |
| ok (уже соответствуют политике) | — | 12 |
| candidateTruncated / Targetless / Proposed | — | 62 / 1 / 56 |
| unknown-security | 1 | 1 |
| unknown (нужно уточнение) | 1 | 2 (1 — усечённый поиск без target, 1 — security неизвестна) |
| Projected lag OK (post-plan) | 21/76 (27,6%) | **74/76 (97,4%)** |
| Shortfall по политике (61/76) / по запасу (65/76) | 40 / 44 | **0 / 0** |
| Текущие C/H → Projected C/H | 1/5 | **1/5 → 0/3** на точных выбранных версиях |
| Вердикт цели (по security-лимитам maxK C=0/H=1) | — | **blocked: projected High 3 > 1** |
| elapsedMs | ≈95 609 | 103 221 (в пределах бюджета 130,5 c) |

Итог: lag-критерий план закрывает (74 ≥ 65 ≥ 61), но вердикт цели честный — **blocked**: на выбранных версиях остаются 3 High при лимите maxKnownHigh=1 (F3). В отличие от предыдущего отчёта, достижимость yellow НЕ заявляется без проверки security на точных target. 1 Critical уходит на выбранных версиях, 5 High → 3; остаток High и отсутствие OSV-данных у 1 пакета — отдельные задачи, видны в prompt/UI как блокеры и «целей без OSV».

## Регрессия

- Python: **1029 тестов OK** (skipped=4) — включая все старые Draft/C1/T3/T4/C2/peer-наборы и R12-набор (12 тестов: 5 исходных + 7 F1–F4).
- Desktop: `npm run build` ОК, 45/45 check-скриптов, oxlint 0 errors (10 warnings — все до изменений).
- `scripts/check-public-sanitization.py`: OK.
- CI/Release: после фикса BOM в `VERSION`, пересоздания тега v0.2.130 и пере-push — pipeline и Release зелёные; релиз v0.2.130 опубликован.
