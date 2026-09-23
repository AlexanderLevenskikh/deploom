# DepLoom - Валидация №5: effective policy, closure-безопасность, staleness-входы, supervisor-изоляция, Fast/Deep из Desktop (G1-G5)

База сравнения: `9d234e3` (v0.2.121) -> HEAD (готовится v0.2.122).
Среда: Windows, Python 3.14.3, Node v24.18.0; npm-registry офлайн (проверки через локальные tarball); финальный регресс - полный набор `tests/` (945 тестов).

## 0. Метод и фиксация «до»

- G1-G5 зафиксированы failing-регрессом заранее: до правок regression run `Ran 11 tests, FAILED (failures=4)` по `tests/test_revalidation_5_policy_gates.py`:
  1. G2 closure: `targetClosureFromRoadmap(...,'yellow',80,1)` с `scope_lag_unknown=2`/`security_unknown=2` и без executable-действий возвращал **`reached=true`**; после фикса `reached=false`.
  2. G2 verdict: свежий npm audit с C0/H0/lag100 и `report.policy={targetLevel:'yellow'}` (без lagPolicyMonths/minLagOkPct/High) возвращал **ACCEPTED**; после фикса - **UNKNOWN** (обязательные поля) / REMEDIATION_REQUIRED (известное невыполнение).
  3. G3 keep-current: manifest с intentJson `{uuid:'keep-current'}` + совпадающими численными критериями возвращал **stale=false** (intentJson терялся при `storedPolicyFromSnapshot`); после фикса stale=true с причиной «политики keep-current/required изменились...».
  4. G3 nested: manifest без `inputFilesByProject` показывал stale=false после правки корневых settings (фиксированный список не видел корневые файлы).

  G1/G4/G5: Python-части правок применены до запуска failing-прогона (общий рабочий репозиторий), поэтому для них «до»-форма зафиксирована в самих тестах и в `docs/DRAFT_MODES_REVALIDATION_5_AND_FOLLOWUP_2026-09-23.md` (известные расхождения: `lag_threshold_months=12` при запросе 3; green при M+L<=20; отсутствие изоляции worker после timeout; отсутствие product-mode транспорта).
- После правок: regression `Ran 11 tests, OK` (0.99s); полный набор 945 тестов OK (skipped=4); все `check:*`, оба tsc-сборки и sanitization зелёные.

## 1. G1/P1 - выбранные lag-месяцы и численные C/H/M/L ограничения реально управляют планом (done)

- Канон: `effective_acceptance_policy(project_name=None)` - приоритет `DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT` (per-project map, generate-all) > `DEPLOOM_ACCEPTANCE_POLICY_JSON`; default `maxKnownHigh=1` (паритет с TS `DEFAULT_ACCEPTANCE_POLICY`), `maxKnownModerate/Low=20/20`.
- `main()` пинит `DEPLOOM_BASELINE_LAG_POLICY_MONTHS` + `DEPLOOM_BASELINE_MAX_KNOWN_CRITICAL/HIGH/MODERATE/LOW` из effective policy - snapshot/hash/planner видят одну и ту же цель.
- `_draft_local_inventory_rows` и `analyze_project` используют `effective_lag_policy_months(project.name, override)` вместо fallback 12: inventory при `{lagPolicyMonths:3}` даёт `row.lag_threshold_months=3` (тест `test_g1_probe_lag_policy_months3_gives_3_not_12`).
- Health-green: `compute_project_health` применяет числовые `maxKnownModerate/maxKnownLow` (при наличии в policy) вместо legacy-правила (M+L)<=20; legacy-ветка сохранена для CLI-only прогонов без policy. Тест `test_g1_green_health_respects_ml_zero_limits`: зеленый пресет M=0/L=0 при известном Moderate не даёт green.
- Generate-all: `generateAllPolicyEnv(workspace)` собирает `DEPLOOM_ACCEPTANCE_POLICY_BY_PROJECT` (все проекты) и сливает с глобальной policy; каждый проект получает свою (тест `test_g1_generate_all_keeps_each_projects_policy`, паритет Python/Node).

## 2. G2/P1 - closure не закрывает цель при неизвестном security; пропуск обязательных policy-полей -> UNKNOWN (done)

- `target-closure.ts`: новый независимый гейт `(rawHealth.security_unknown ?? rawHealth.unknown ?? 0) > 0 -> reached=false` (undefined-safe для legacy health без поля unknown), добавлен в yellow и green достижения; `securityUnknown` попадает в результат. Тест `test_g2_closure_never_reached_with_unknown_security` гоняет собранный production-модуль.
- `acceptance-policy.ts`: `policySchemaGaps` - пять обязательных полей `report.policy` (targetLevel, minLagOkPct, lagPolicyMonths, maxKnownCritical, maxKnownHigh) -> пропуск любого даёт UNKNOWN (unverifiable), а не дефолт. Пропуск M/L -> UNKNOWN (недоверие), при наличии - численное сравнение (M/L mismatch -> UNKNOWN). Тест `test_g2_incomplete_policy_snapshot_is_unbound_unknown` (реальный producer -> JSON -> Node verdict).
- `manual_dependency_audit.py`: `build_report` публикует всю schema (5 обязательных + условные maxKnownModerate/maxKnownLow из `--max-known-moderate/--max-known-low`; main.ts audit job передаёт их); полный совпадающий report остаётся ACCEPTED (check-acceptance-policy.mjs YELLOW80/GREEN100 содержат все 5 полей).

## 3. G3/P1 - staleness: keep-current/required + реально разрешённые входы + всегда видимый stale (done)

- `storedPolicyFromSnapshot` сохраняет ОБА snapshot: merge `acceptancePolicyJson` (численная политика) и top-level `intentJson` (keep-current/required) - больше не теряется (тест `test_g3_keep_current_changes_make_run_stale_with_both_snapshots`).
- Fingerprint по реально прочитанным файлам: Python `draft_input_files_for` = фиксированный набор + `settings_sources` (то, что вернул `read_merged_settings`) + `dashboard_state_path`; манифест получает `inputFilesByProject` (форма `{name}` - сериализация приведена к контракту Node-ридера); Node `draftInputHashFromFiles` re-hashит ровно эти имена (byte-lockstep `name\0bytes\0`).
- Nested layout: `package.json` в `<ws>/frontend`, корневые settings вне project.path - hashed абсолютным именем; правка корневых settings делает run stale (тест `test_g3_nested_layout_settings_change_is_detected_via_resolved_inputs`: before stale=false -> after stale=true).
- Численные критерии сравниваются per-dimension и только когда текущая политика их задаёт (отсутствие поля у старого клиента не считается изменением), т.е. без ложных stale при том же уровне/проценте; реальное изменение лимитов по-прежнему инвалидирует run (F4 acceptance сохранён).
- UI: предупреждение stale показывается всегда, когда `draftResult.stale`, независимо от свежести запуска/ack (FlowWorkspace).
- Cross-language physical-тесты F4/T5 (real Python writer -> files -> production Node reader) green; probe `draft_reader_probe.mjs` теперь пробрасывает `artifact.inputFilesByProject` в staleness (как production `main.ts`).

## 4. G4/P1 - supervisor изолирует продолжающего работать planner (done)

- Новый `run_supervised_planning(phase, deadline, fn, rows_by_project)`: worker получает **глубокую копию** `rows_by_project`; результат сращивается обратно атомарно ТОЛЬКО при успехе до deadline (`copy.deepcopy(working)` назад); на `DraftBudgetExceeded` общие rows не тронуты - поздние записи late-worker уходят только в его private-копию, которую никто не читает.
- Все планировочные вызовы в `main()` переведены на `run_supervised_planning` (lambdas принимают `working`); но dispatch без дедлайна (verified-режимы/тесты с собственным clock) вызывает `fn(rows_by_project)` напрямую.
- Исходный `run_supervised` сохранён для deadline-тестов (`DraftDeadlineHardBoundTests` не тронуты).
- Тест `test_g4_timed_out_worker_mutations_never_reach_published_rows`: worker, разбуженный после timeout, пишет в свою копию; shared rows после паузы остаются неизменными.

## 5. G5/P1 - Fast/Deep подключены из Desktop; fast-стоп оценивает фактический verified-кандидат (done)

- Продуктовый mode транспортируется целиком: `BaselineIntent.productMode: 'fast'|'deep'` (types + UI state в `BaselineIntentDialog`, segmented toggle без native `<select>`, dirty/reset, buildIntent), `normalizeBaselineIntent` его сохраняет.
- `DEPLOOM_MODE` теперь ставится для ВСЕХ baseline-прогонов: draft -> 'draft', verified -> `effectiveIntent.productMode ?? 'deep'`; Python `main()` принимает `--mode draft/fast/deep/verify` и закрепляет его в env (F6-контракт сохранён: `test_block_phi_execution_modes` зелёный).
- Fast-стоп оценивает фактического verified-кандидата (не долю совпадения с desired): security-unknown -> не удовлетворяет; C>maxKnownCritical / H>maxKnownHigh должны закрываться `min_no_critical`/`min_no_high`; lag-доля compliant/total >= minLagOkPct (по `lag_compliance_target_for_row`). Тесты:
  - `test_g5_fast_stop_never_satisfied_by_remaining_critical` - 8/10 desired matches, оставшийся Critical -> fast НЕ стопается;
  - `test_g5_fast_stop_satisfied_when_candidate_covers_critical` - кандидат закрывает Critical -> стоп;
  - `test_g5_fast_stop_satisfied_when_goal_already_complete` - цель уже достигнута -> стоп.
- Бюджеты fast=2/300s, deep=12/3600s сохранены; legacy `verify` mapping не сломан.

## 6. Регресс и известные ограничения

- Полный прогон корня: `Ran 945 tests, OK (skipped=4)` (из них 11 - новый regression G1-G5; физические cross-language F4/T5 и producer->acceptance chain - часть набора).
- `desktop`: `tsc -p tsconfig.electron.json` green, `tsc -b` (renderer) green; check-скрипты ALL OK: check-acceptance-policy, check-target-closure, check-progressive-contract, check-interaction-contracts, check-baseline-intent, check-flow-state, check-human-flow, check-ui-shell, check-i18n; sanitization `scripts/check-public-sanitization.py` OK.
- Инфраструктурные починки (CI Linux): кросс-платформенный запуск tsc в тестах (`shutil.which('npx.cmd') or 'npx'` - раньше `npx.cmd` падал на runner'е); probe пробрасывает `inputFiles`; манифест сериализует `inputFilesByProject` как `{name}` (контракт Node-ридера). Структурный `test_proof_handoff_firewall` обновлён под имя `run_supervised_planning` (семантика контракта не изменена).
- Честные границы: живого Electron-окна в этом окружении не запускалось; UI-связи и transport подтверждены на source-уровне (check:* инспектируют production-код), tsc и cross-language tests green. G1/G4/G5 «до»-форма не зафиксирована failing-прогоном в CI (Python-правки применены до capture), расхождения зафиксированы в followup-документе и тестами-регрессиями.
- Офлайн-среда: npm-registry недоступен; аудит/OSV-evidence мокаются; real-fixture install/build/test проверены на локальных tarball (сохранены из ревалидации №4).
