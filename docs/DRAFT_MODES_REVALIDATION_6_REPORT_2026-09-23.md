# Draft Modes: Revalidation #6 — закрытие H1–H5 и контроль G4

Дата: 2026-09-23. База: v0.2.123. Статус: **все пункты закрыты, полный регресс зелёный**.

## Итог

| Пункт | Приоритет | Суть | Статус |
|---|---|---|---|
| H1 | P1 | Fast-гейт считает goal по всему активному scope (агрегированные C/H/M/L + знаменатель lag = весь scope, unknown ≠ confirmed) | закрыт |
| H2 | P1 | health-гейты жёлтого/планировочный резерв используют per-project `minLagOkPct`, а не глобальные 80% | закрыт |
| H3 | P2 | custom-groups.json входит в fingerprint Draft (`inputFilesByProject`, byte-lockstep с Node-ридером) | закрыт |
| H4 | P2 | Closue: цвет сам по себе не доказательство — `reached=false` + `insufficientData`; green не игнорирует явный ненулевой High | закрыт |
| H5 | P2 | Desktop не подменяет заявленные mode-бюджеты Fast/Deep плоским override без явного/сохранённого бюджета | закрыт |
| G4 | — | Тест изоляции worker усилен — worker реально стартует до timeout | закрыт |

Все негативные сценарии **сначала зафиксированы падающими тестами** (17 новых тестов, из них 10 падали до фиксов), затем реализованы фиксы.

---

## H1 — Fast-гейт по полному scope (P1)

`_candidate_satisfies_fast_policy` (`dependency_live_roadmap_generator.py:5562`):

- **Security агрегируется по всему активному scope** в единицах acceptance policy (C/H/M/L), а не по строке. Один allowance `maxKnownHigh=1` не «покрывает» два пакета с H:1. Превышение агрегата над лимитом снимается только реальным per-row evidence: версия кандидата ≥ `min_no_*` (плановое значение из registry/OSV). Недостаточные данные остаются unknown и блокируют.
- **Lag считается по всему активному scope**: строки с неизвестным lag-target не считаются выполненными и **не исключаются из знаменателя**. 1 подтверждённый из 10 при цели 80% — это 10%, не «100% исследованной части».
- Кандидат очищает finding по строке только когда выбрана безопасная версия (has_safe_target + current_meets_target).

Проверено (все сценарии из отчёта + Low + положительные границы):

- 2×H:1 при maxKnownHigh=1, безопасной версии нет → `False` (было `True`).
- Green, maxKnownModerate=0, установлен M:1 (нет safe no-vuln target) → `False` (M/L не проверялись → было `True`); то же для L с maxKnownLow=0.
- 1/10 lag-confirmed при 80% → `False` (было `True`: 1/1).
- Положительные границы: 2×H:1, оба очищены кандидатом → `True`; L:1 внутри лимита → `True`.

**Verified-оркестратор** (контракт stop-события): `fast_policy_satisfied(False)` → None (первый физически прошедший, но не соответствующий кандидат **не** даёт `progressive-fast-policy-satisfied`); `fast_policy_satisfied(True)` при `strategy="fast"` → `VERIFIED_POLICY_SATISFIED_FAST`; Deep всегда None (сохраняет incumbent). Сама вставка номера события в реальном цикле `_continue_after_verified_candidate` уже вызывает `_verified_assignment_satisfies_policy` = `_candidate_satisfies_fast_policy(...)` на verified-assignment текущего incumbent — покрыто контрактным тестом без запуска z3/физ. сборки (по допущению отчёта в Draft физику не запускаем).

---

## H2 — Per-project пороги health (P1)

Заменён глобальный `EFFECTIVE_MIN_LAG_OK_PCT` на per-project `effective_acceptance_policy(project)["minLagOkPct"]`:

- `health_yellow_ratio(project_name=None)` — порог проекта;
- `health_planning_ratio(project_name=None)` — порог проекта + 5 (резерв);
- `compute_project_health` — `yellow_required`/`yellow_plan_required` и красный рубеж `lag_pct < project_min_pct` (причина `<{pct}%`);
- единственный внешний caller резерва в планировщике (`required_ratio_count(health.total, health_planning_ratio(project))`) также передаёт проект.

Проверено (задаётся `DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT`, как в generate-all): app-a=80% и app-b=90% при 8/10 → у A `lag_needed_for_yellow=0`, у B `=1` и status **red** с причиной «<90%» (было: у B нужного количества не хватало, статус не был red). Резерв: 90% → (95,100) → required 10/10.

---

## H3 — Custom-groups в fingerprint Draft (P2)

`draft_input_files_for(spec, settings_sources, dashboard_state, groups_config_path)` — добавил `groups_config_path` в полный набор входов (вызывается в `main()` при чтении локального inventory, на момент фиксации input identify). Правка `custom-groups.json`:

- меняет hash и делает result stale с причинами;
- неизменные входы остаются fresh;
- имя файла уходит в манифест `inputFilesByProject` и Node-ридер пересчитывает байт-лоцкептом (проверен паритет hash Python↔Node через `draftInputHashFromFiles`).

---

## H4 — Честный UNKNOWN при отсутствии доказательств (P2)

`desktop/electron/target-closure.ts`:

- `hasLagEvidence = goalLagPct !== undefined` — закрытие НИКАКОЙ цели невозможно без измеренной доли lag (генератор всегда пишет `lag_ok_pct`/`scope_lag_pct`); `received=false`, добавлен флаг `insufficientData`.
- Сообщение `targetClosureMessage` при `insufficientData` прямо объясняет: «есть только цвет, но нет численных показателей lag/scope; цвет не доказательство — пересоберите отчёт».
- **green больше не игнорирует явный ненулевой High** (`greenHighClear = high===undefined || high===0`): legacy-противоречие `status:green + high>0` → не ложный успех.

Проверено: repro `{project_health:{demo:{status:'green'}}, projects:{demo:[]}}` → `reached=false`, `insufficientData=true`, причина в сообщении; `{status:'yellow',lag_ok_pct:80}` → по-прежнему reached; `{status:'green',lag_ok_pct:100,high:2}` → reached=false. `check-target-closure.mjs` (все существующие сценарии, включая best-effort и legacy) зелёный.

---

## H5 — Приоритет бюджета: mode defaults / явный / сохранённый (P2)

Desktop больше **не подменяет** заявленные режимы бюджета автоматикой на обычном запуске:

- Новый чистый модуль `desktop/electron/baseline-budget.ts` → `baselineBudgetOverride(...)`: 
  - явный per-run бюджет (поле в raw `input.baselineIntent`) → override для обоих режимов;
  - иначе сохранённый legacy-бюджет (поле в raw persisted intent, `baselineIntentHasPersistedBudgetMinutes`) → override;
  - иначе пусто → **engine остаётся на mode-default: fast=300s/2, deep=3600s/12**.
- `main.ts` (baseline case): keys `DEPLOOM_BASELINE_AUTOMATIC_BUDGET_SECONDS` / `MAX_EXPENSIVE_ATTEMPTS` эмитируются только через условный spread. `DEPLOOM_BASELINE_BUDGET_MINUTES` (информационный, в манифест) — всегда.
- Проверка: `desktop/scripts/check-h5-budget-priority.mjs` — 4 сценария helper + source-контракт main.ts (импорт, детекция persisted-бюджета, условный spread, единственное вхождение env-ключа).
- Engine: при отсутствии override — fast=300s, deep=3600s, attempts 2/12 (Python guard); при наличии override — значения engine берут его (1800s/3).

---

## G4 — Усиленный тест изоляции

`test_g4_timed_out_worker_mutations_never_reach_published_rows`: старый deadline 0.2s срабатывал **до** старта worker (finalize reserve уже съедал всё). Теперь:

- `DeadlineClock(1.3)` → net-budget положительный: worker **стартует** (Event `started`), 
- supervisor даёт `DraftBudgetExceeded`, пока worker заблокирован (Event `proceed`),
- worker **завершается после** timeout (Event `done`), пишет в свою копию,
- `shared["demo"]` остаётся прежним (current_version=1.0.0) — публикация читает консистентный pre-deadline снимок (G4).

---

## Верификация

- **Unit/source**: 17 новых тестов `tests/test_revalidation_6_issues.py` (+ G4 в `tests/test_revalidation_5_policy_gates.py`); все 10 негативных сценариев сначала падали, затем зелёные.
- **Cross-language**: parity hash Python↔Node для H3 (`draftInputHashFromFiles` по `inputFilesByProject` с группами), `_electron_dist` пересобирает dist при необходимости.
- **Subprocess**: `check-target-closure.mjs`, `check-h5-budget-priority.mjs`, `check-acceptance-policy.mjs`, `check-progressive-contract.mjs`, `check-baseline-intent.mjs` — все зелёные; полный набор `run_tool_tests.py --suite all` → **1129 OK (skipped=4)**.
- **Electron UI**: изменения в main.ts/env и target-closure проверены на уровне скомпилированных модулей + source-контрактов и полной сборки (`npm run build`: `tsc -p tsconfig.electron.json` + `tsc -b` + vite build, oxlint 0 errors). Интерактивный UI-флоу электрона автоматическим e2e не гоняется (честное ограничение раунда — UI поверх существующих команд не менялся).
- CI: новый check-скрипт подключён в `desktop/package.json` (`check:h5-budget-priority`) и в `desktop` job `.github/workflows/ci.yml`; inventory-тест `test_desktop_check_inventory` зелёный.

## Затронутые файлы

- `dependency_live_roadmap_generator.py` (H1, H2, H3)
- `desktop/electron/target-closure.ts` (H4)
- `desktop/electron/main.ts`, `desktop/electron/baseline-budget.ts` (H5)
- `desktop/scripts/check-h5-budget-priority.mjs`, `desktop/package.json`, `.github/workflows/ci.yml`
- `tests/test_revalidation_6_issues.py`, `tests/test_revalidation_5_policy_gates.py`
- `docs/DRAFT_MODES_REVALIDATION_6_REPORT_2026-09-23.md`
