# Повторная проверка №8: v0.2.125

Дата: 2026-09-24. Проверен `1c8abbc` / тег `v0.2.125`. Код приложения не менялся; этот документ фиксирует оставшийся сценарий после отчёта №7.

GitHub connector подтвердил оба указанных run: [CI master 35915332050](https://github.com/AlexanderLevenskikh/deploom/actions/runs/35915332050) — все 4 jobs `completed/success`; [Release 35915337321](https://github.com/AlexanderLevenskikh/deploom/actions/runs/35915337321) — все 7 jobs `completed/success`, включая `publish-release`. Локально прошли `npm run build`, 66 Python-тестов (`test_revalidation_7_issues`, `test_revalidation_6_issues`, `test_revalidation_5_policy_gates`, `test_draft_flow_integration_physical`, `test_f1_acceptance_evidence_chain`) и 6 профильных Node checks. `npm run lint` завершился с 0 errors и предупреждениями. Полный 981-тестовый набор локально повторно не запускался.

## B1 — P1. Fast может объявить достигнутой policy после появления новой уязвимости

**Место:** `dependency_live_roadmap_generator.py:5633–5651`, `_candidate_satisfies_fast_policy`. N2 научил гейт брать OSV evidence точной версии кандидата, но этот путь выполняется **только если сумма findings исходных версий уже превышала лимит**:

```python
overstated = [r for r in active if parse_vuln_counts(r.current_vulns).get(severity, 0) > 0]
aggregate = sum(... for r in overstated)
if aggregate <= limit:
    continue  # выбранные версии по этой severity вообще не оцениваются
```

**Прямое воспроизведение production функцией:** один пакет, исходная `1.0.0` имеет `C:0;H:0;M:0;L:0`, цель обновления по lag — `2.0.0`. В `vuln_evidence_by_version` для **выбранной** `2.0.0` записано `H:1`, policy `maxKnownHigh=0`, `minLagOkPct=80`. `_candidate_satisfies_fast_policy([row], {'pkg':'2.0.0'}, {'pkg':'1.0.0'}, {'pkg'}, 'app')` возвращает **true**. Если evidence `2.0.0` отсутствует вовсе, возвращает **true** снова. Оба ответа неверны: первая версия нарушает лимит, вторая не доказана безопасной. Сборка/физический PASS кандидата этого не исправляет.

Та же причина пропускает рост severity, когда исходный суммарный уровень находится *внутри* разрешённого лимита: например, текущий High=1, лимит High=1, кандидат получает High=2.

**Исправить:** для каждого активного пакета и каждой severity C/H/M/L сначала определить count **точно выбранной версии**, включая пакеты с нулём findings до обновления. При отсутствии OSV для выбранной версии Fast не вправе объявить цель достигнутой. Затем агрегировать counts по всем активным пакетам и сравнить с лимитами policy. Не использовать исходный aggregate как условие пропуска оценки кандидата. Сохранить исправление N3: если после частичного обновления остаток ровно равен допустимому лимиту, Fast должен останавливаться.

**Приёмка:** зафиксировать как failing-first тесты 0→H:1 при лимите0, H:1→H:2 при лимите1, 0→unknown, а также положительные 0→0 и 2×H:1→остаток H:1 при лимите1. Проверить вызов этого гейта на **реальном verified assignment** перед `progressive-fast-policy-satisfied`, не только `BaselineAnytimeState.fast_policy_satisfied(boolean)`. Deep и последний verified incumbent не должны деградировать; Draft по-прежнему не запускает physical verification.

## B2 — P2, качество UI-регрессии: dev-harness не показывает основную кнопку

Проверка N6 на `http://127.0.0.1:5174/e2e-harness.html`, viewport 800×600, 200 пакетов: `.baseline-intent-scroll` имеет `scrollHeight=11678`, `clientHeight=383`; primary расположен в viewport (`top≈544`, `bottom≈576`), кликается, консоль без ошибок. Путь N7 в harness показывает restart notice и кнопку, submit даёт `resume=restart`. Это подтверждает DOM-компоновку и frontend-вызов `onSubmit`; настоящий Electron job/сброс checkpoint этим не проверен.

При этом [скриншот из отчёта №7](../assets/revalidation-7-dialog-fixed.png) и наш повторный снимок показывают footer **без видимой основной кнопки**. В DOM кнопка `Start autonomously` есть, но её computed style в harness — `color: rgb(255,255,255)`, `background: rgba(0,0,0,0)`: белый текст на белом фоне. Причина тестовой среды: `desktop/src/e2e/dialog-harness.tsx` импортирует `App.css`, но не `index.css`; `.button.primary` использует `var(--blue)` из `index.css`. Production `desktop/src/main.tsx` импортирует `index.css`, поэтому **это не доказанный баг релизного окна**. Но скриншот и DOM-проверка «кнопка в viewport» не доказывают визуальную доступность CTA в настоящем приложении.

**Исправить тест:** подключить в harness те же базовые стили/тему, что в основном приложении; проверить цвет фона, контраст и попадание кнопки в viewport в RU/EN, коротком окне и при масштабировании. После этого получить скриншот, на котором подпись primary действительно видна. Отдельно пройти реальный Electron: «Начать заново» → prepare-dialog → submit → новый job c `DEPLOOM_BASELINE_RESUME=restart` → видимый прогресс/артефакт; отмена сохраняет прежний результат. Не выдавать submit заглушки harness за такой прогон.

**Итог:** семь заявленных исправлений в основном присутствуют и адресные регрессии проходят, но B1 блокирует утверждение «Fast достигает policy корректно». B2 ограничивает доказательность UI-скриншотов и оставляет живой Electron-сценарий непроверенным.