# Draft Modes: Revalidation #7 — закрытие N1–N7

Дата: 2026-09-23. База: v0.2.124. Статус: **все пункты закрыты, полный регресс зелёный** (Python 981 OK skipped=4, Desktop 45/45 check-скриптов, сборка и браузерный DOM-E2E ок).

## Итог

| Пункт | Приоритет | Суть | Статус |
|---|---|---|---|
| N1 | P1 | Диалог всегда добавлял `budgetMinutes:30` → обычный выбор режима превращался в явный override; сохранение делало «сохранённый legacy-бюджет» из дефолта | закрыт |
| N2 | P1 | Fast-гейт объявлял новой безопасной версией первую без severity, игнорируя повторно-уязвимые новые версии | закрыт |
| N3 | P2 | Fast требовал удаления всех High, хотя policy позволяет один (N2 31/32/33, 56/57/58 и т.п.) | закрыт |
| N4 | P1/P2 | `compute_project_health` считает status/`lag_needed_for_yellow`/проекции по исследованному total, а не по полному scope | закрыт |
| N5 | P2 | `target-closure.ts` трактует отсутствующие critical/high/security как «чисто» | закрыт |
| N6 | P0 | Окно диалога BaselineIntentDialog обрезает кнопки запуска (grid 6 строк + overflow:hidden) | закрыт |
| N7 | P1 | «Начать заново» выглядело сломанным: `execute(…restart)` открывал decision-dialog, не запуская restart | закрыт |

Все негативные сценарии **сначала зафиксированы падающими** (`tests/test_revalidation_7_issues.py`, 19 тестов, первый прогон красный), Python-фиксы N2/N3/N4 написаны до правок моделей (TDD), затем реализована Desktop-сторона и DOM-E2E.

---

## N1 — Диалог не должен делать из дефолта override (P1)

Корень: `BaselineIntentDialog.buildIntent()` всегда писал `budgetMinutes: 30`; `main.ts::baselineIntentHasPersistedBudgetMinutes` определяла «сохранённый бюджет» по presence, а Desktop-case брал числовое поле как явный. Цепочка «выбрал Fast → сохранил → в следующий раз 30-минутный override» была незаметной и делала режимные бюджеты (fast=300s/2, deep=3600s/12) мёртвыми.

Решение — единый чистый модуль `desktop/electron/baseline-intent.ts` (импортируется и renderer-ом, и main-процессом, один источник правды):

- `normalizeBudgetField(raw)` → `{budgetMinutes, budgetMinutesExplicit}`: флаг authoritative; legacy v1 с бюджетом → explicit (миграция); v2 без флага: только число ≠ 30 — реальный выбор (миграция сохранённого старого файла); иначе дефолт НЕ explicit; clamp 5..240.
- `handleDialogBudget({edited, planExplicit, shownMinutes, planBudgetMinutes})`: нетронутое поле на дефолтном плане возвращает `{}` (бюджета в intent НЕТ), редактирование/повторное открытие explicit-плана возвращает `{budgetMinutes, budgetMinutesExplicit:true}`.
- `attemptsForMinutes(minutes)` — дорогие попытки = clamp(ceil(min/10), 2, 8).

Задействовано:
- `main.ts`: `normalizeBaselineIntent` возвращает `...budgetField`; `baselineIntentHasPersistedBudgetMinutes` считается по флагу/миграции (`normalizeBudgetField`), а не по presence; Desktop-case (`rawInputExplicit = normalizeBudgetField(input.baselineIntent).budgetMinutesExplicit`) → `baselineBudgetOverride.explicit` только для реального выбора.
- Renderer: `src/types.ts` — `budgetMinutesExplicit?: boolean`; `src/data/baselineIntent.ts` — `normalizeBaselineIntentPlan` сохраняет флаг; `FlowWorkspace` prepare-plan сохраняет `budgetMinutesExplicit`; диалог — state `budgetEdited`, buildIntent через `handleDialogBudget`, рядом Fast/Deep подпись фактически применяемого лимита (дефолт: «Без выбора бюджета применяются режимные лимиты Fast 300с/2, Deep 3600с/12»; при выборе: «Применяемый лимит: N мин (Ns · K дорогих попыток)»).
- `check-h5-budget-priority.mjs` расширен N1-контрактами: 6 поведенческих сценариев `normalizeBudgetField`/`handleDialogBudget` + source-контракты (dialog использует общий модуль и `budgetEdited`, data сохраняет флаг, main выводит explicitness через `normalizeBudgetField`).

## N2 — Точная версия вместо «первой безопасной» (P1)

Корень: `_candidate_satisfies_fast_policy` при превышении агрегата сравнивал кандидата с `min_by_vuln` (первая версия без severity). Новая версия могла снова содержать severity — `min_by_vuln` находила более старую «безопасную», а реальный кандидат (новый) считался удовлетворяющим.

Решение: `_severity_at_candidate_version(row, planned, severity)`:
- `planned == current` → `current_vulns` (измерение установленной версии);
- иначе evidence-мап `vuln_evidence_by_version` (введён на `DependencyRow`, заполняется при построении rows: `{v: vuln_summary(entries) for v, entries in (vulns or {}).items()}`; в `row_json` НЕ сериализуется);
- отсутствие evidence для ТОЧНОЙ версии → `None` (unknown) → гейт возвращает `False` (никогда не «безопасно по догадке»); для VERIFIED-цикла Fast-стоп не эмитится.

Проверено: exact-safe → satisfied; evidence есть только у кандидата → unknown; кандидат новее «первой безопасной», но сам уязвим → False (было True); stop-событие Fast не эмитится на unknown.

## N3 — Остаток ≤ лимита (P2)

Корень: гейт требовал убрать ВСЕ превышающие строки (попакетно), хотя policy допускает H=1 в целом по scope (примеры лимитов 31/32/33, 56/57/58 из отчёта).

Решение: после точной оценки каждой строки агрегат НЕ снимается при `remaining > limit`; `remaining` = сумма значений ТОЧНОЙ версии по превысившим строкам; `remaining ≤ limit` → satisfied (частичный фикс, оставляющий ровно разрешённый High). Проверено: 2×H:1, обе очищены → True; одна очищена, вторая оставляет 1 (лимит 1) → True; оставляет 2 (лимит 1) → False; границы через пакеты.

## N4 — Здоровье по полному scope (P1/P2)

Корень: `compute_project_health` и производные (status, `lag_needed_for_yellow`, `yellow_plan_required`, проекции) считались по исследованному `total`; unknown-строки выпадали из знаменателя. Пример: 1/10 confirmed при 80% → «жёлтый с lag_needed=0», при этом Fast-гейт и closure уже говорят красное.

Решение: целевые показатели над `scope_total` (все active-строки):
- `yellow_required = ceil(scope_total * pct/100)`, `yellow_plan_required = ceil(scope_total * (pct+5)/100)`, `green_required = scope_total`;
- `lag_needed_for_yellow = max(0, yellow_required − scope_lag_ok)`;
- проекции (`yellow_projected_*`, `yellow_plan_shortfall`, `green_projected_*`) — numerator по `active_rows`+`removed_closed`, знаменатель `scope_total`; `scope_lag_pct` — доля полного scope;
- status: **red**, когда `scope_lag_pct < project_min_pct` (причина «<N% по полному scope», с вариантом учёта `lag_unknown`); green требует `lag_bad==0 && lag_unknown==0`; yellow — иначе по scope-процентам;
- исследованное (`total`, `lag_ok_pct`) остаётся честной диагностикой («research share»), не знаменателем цели;
- `total==0 && lag_unknown>0` → прежний T3-контракт: `insufficient_data` + жёлтый + «недостаточно данных» (никогда не «красный по измерению» и не «100%») — 9/10 unknown = insufficient-data yellow с честным `lag_needed=8`, а не ложное зелёное/красное.

Обновлены старые (ошибочные) контракты: `tests/test_dependency_roadmap.py` (1/10 → **red**, 4/6 при неизвестных → **red** с `scope_lag_pct=66.7`); dashboard JS (Python-блоб, `projection.scope_total ?? projection.total`).

## N5 — Числовое evidence по security обязательно (P2)

Корень: `target-closure.ts` трактовал `critical === undefined` и `securityUnknown === undefined` как «чисто» (цвет/отсутствие = доказательство).

Решение: для закрытия ЛЮБОЙ цели требуются численные `critical`, `high` и `security_unknown` (генератор всегда пишет их в `project_health`); отсутствие любого → `insufficientData` с перечнем `missingEvidence` (численные lag/scope, Critical, High, покрытие security) и сообщением, называющим недостающее. После наличия evidence: `criticalClear = critical===0`, `greenHighClear = high===0` (гейт High по-прежнему отдельно через `highClear` для yellow), `securityClear = security_unknown===0`. `PlanCanReachYellow`/best-effort не трогаются.

`check-target-closure.mjs`: все существующие фикстуры дополнены `critical/high/security_unknown` (иначе детальные сообщения — недостижимая ветка), два inline-override High-фикстуры и scopeClosure дополнены, добавлен новый негатив «цвет без evidence → insufficientData с перечислением».

## N6 — Диалог больше не обрезает кнопки (P0)

Корень: диалог — flex-column, но середина (fast-flow, deferred, decision, toolbar, stats, list) не имела общего скролл-контейнера; `.baseline-intent-list` был единственным scrollport (свой overflow:auto + sticky head), всё между ним и footer не скроллилось, `overflow:hidden` на диалоге обрезал footer — при малых окнах кнопки запуска уходили под обрез.

Решение (flex):
- `.baseline-intent-dialog` — flex-column (удалён мёртвый grid-вариант),
- средняя часть обёрнута `<div className="baseline-intent-scroll">` (`flex:1 1 auto; min-height:0; overflow-y:auto; display:flex; flex-direction:column`),
- `.baseline-intent-list` — `flex: 0 0 auto; overflow: visible` (скроллит общий контейнер; sticky-шапка `.baseline-intent-row-head` теперь липнет к scrollport обёртки),
- header/бейджи/footer — `flex: 0 0 auto`.

DOM-измерения (браузерный E2E, см. Верификацию): на 800×600 с 200 пакетами footer `top:506..bottom:589` (viewport 600), primary (137×32) полностью виден, середина скроллится (scrollHeight 5729 / clientHeight 454), sticky head держится у верха при прокрутке.

## N7 — «Начать заново» действительно рестартует (P1)

Корень: `execute(…, 'restart')` попадал в ветку «есть pending decision → открыть decision-dialog», поэтому restart оставался «висящей» кнопкой.

Решение: в `FlowWorkspace.execute` первым проверяется `baselineResume === 'restart'` → `openBaselineIntentDialog('prepare','restart')` (до decision-ветки). Диалог получил prop `resume` и при `restart` показывает янтарный бейдж «Baseline будет начат заново / после запуска оркестрационный checkpoint будет сброшен; exact proof/artifact cache с совпадающей identity останется доступен», а главная кнопка — «Начать заново и запустить» (en: Restart and start). Отмена закрывает диалог и сохраняет прежний запуск. Проверено в DOM-E2E: бейдж виден, кнопка переименована, Cancel → состояние прежнее.

---

## Почему зелёный CI пропустил все семь пунктов

Ни один пункт №7 не был покрыт ни одним существующим контрактом — у старой логики было «поведение по умолчанию», которое тесты не наблюдали:

1. **N1** — ни один check не утверждал, что дефолтный выбор НЕ несёт бюджет; presence-детекция и «всегда budgetMinutes:30» были внутренними допущениями.
2. **N2/N3** — старый гейт использовал эвристику «первая версия без severity»; тестов на «кандидат новее, но снова уязвим» и на «остаток внутри лимита» не было.
3. **N4** — единственный тест знаменателя (`test_dependency_roadmap`) сам утверждал СТАРЫЙ ошибочный контракт (1/10 → yellow) и молча «подтверждал» баг.
4. **N5** — closure трактовала отсутствие полей как «чисто»; ни один тест не проверял «цвет без чисел».
5. **N6/N7** — ни одного DOM-уровня: CSS/JSX-поведение диалога автоматически ничем не мерялось (визуальный контроль — только руками пользователя).

Поэтому регресс и оставался зелёным: гейт зелёный, пока проверяет то, что проверяет. Новый failing-first набор (+19) это закрывает, включая поведенческие контракты N1 и DOM-прокси для N6/N7.

---

## Верификация

- **Failing-first**: `tests/test_revalidation_7_issues.py` — 19 тестов (N1×3, N2×4, N3×2, N4×6, N5×4); первый прогон подтверждённо падал/ошибался, после фиксов зелёный; + обновлены старые контракты (1/10 → red, 4/6 → red, H1-положительный и G5-фикстуры с evidence-картами, H4-фикстуры с security-полями).
- **Полный регресс**: `unittest discover tests` → **981 OK (skipped=4)**; Desktop: `npm run build` (tsc electron + tsc -b + vite build, oxlint 0 errors) и **45/45** `check:*` скриптов CI-набора, включая `check:h5-budget-priority`, `check:target-closure`, `check:baseline-intent`, `check:ui-shell`, `check:interaction-contracts`, `check:ui-lifecycle`, `check:dashboard-state`.
- **DOM-E2E (N6/N7/N1)**: dev-only харнесс `desktop/e2e-harness.html` + `desktop/src/e2e/dialog-harness.tsx` (не входит в prod-бандл: `vite build` берёт только index.html; tsc/oxlint чистые), сервер `npx.cmd vite --port 5174`, Playwright:
  - 900×640/77 пакетов и 800×600/200 пакетов: footer и primary полностью в viewport (500..589 при 600), середина скроллится, sticky-шапка держится, скриншоты `assets/revalidation-7-dialog-fixed.png`, `assets/revalidation-7-dialog-restart-banner.png`;
  - N1: поле «Minutes» не тронуто → submit `budgetMinutes=none, budgetMinutesExplicit=false`; введено 60 → подпись «Applied budget: 60 min (3600s · 6 expensive attempts)» → submit `budgetMinutes=60, budgetMinutesExplicit=true`;
  - N7: open(restart) → бейдж + кнопка «Restart and start», Cancel → состояние закрыто, submit не происходит.
- **Честные ограничения раунда**: физический Verified-поиск и живой Electron (запуск real-run через `electron .`) НЕ запускались (репо-допущение: в Draft физику не гоняем; UI поверх существующих команд не менялся) — покрыто контрактными тестами на скомпилированных модулях + DOM-E2E харнесса.

## Затронутые файлы

- `dependency_live_roadmap_generator.py` (N2/N3: `vuln_evidence_by_version`, `_severity_at_candidate_version`, переписанный security-цикл; N4: scope-показатели в `compute_project_health`; dashboard JS)
- `desktop/electron/baseline-intent.ts` (новый, N1-модуль), `desktop/electron/main.ts` (N1: normalize/persisted/rawExplicit), `desktop/electron/baseline-budget.ts` (без изменений)
- `desktop/electron/target-closure.ts` (N5)
- `desktop/src/components/BaselineIntentDialog.tsx`, `desktop/src/components/FlowWorkspace.tsx`, `desktop/src/App.css`, `desktop/src/types.ts`, `desktop/src/data/baselineIntent.ts` (N1/N6/N7)
- `desktop/scripts/check-h5-budget-priority.mjs`, `desktop/scripts/check-target-closure.mjs`
- `desktop/e2e-harness.html`, `desktop/src/e2e/dialog-harness.tsx` (dev-only харнесс)
- `tests/test_revalidation_7_issues.py` (новый), `tests/test_revalidation_5_policy_gates.py`, `tests/test_revalidation_6_issues.py`, `tests/test_dependency_roadmap.py`
- `assets/revalidation-7-dialog-fixed.png`, `assets/revalidation-7-dialog-restart-banner.png`
- `docs/DRAFT_MODES_REVALIDATION_7_REPORT_2026-09-23.md`
