# Draft v0.2.130: postPlanGoal неверен для зелёной цели

Предыдущие F1–F4 исправлены и адресные проверки проходят. Остался самостоятельный P1-дефект: новая оценка достижимости Draft всегда применяет **yellow**-порог к запуску с `targetLevel=green`.

## Воспроизведение

В `dependency_live_roadmap_generator.py` `build_draft_plan` рассчитывает `post_yellow` и `post_green`, но вызывает `_draft_goal_verdict(post_yellow, yellow_required, ..., effective_acceptance_policy(project))`. `_draft_goal_verdict` сравнивает High с `maxKnownHigh` (для yellow допускается 1). `EFFECTIVE_TARGET_LEVEL` и `green_required` при вердикте не участвуют. Prompt и Desktop пишут «цель достижима», хотя для green действуют 100% lag-OK и 0 High.

Два локальных воспроизведения на текущем HEAD без сетевых запросов:
- `targetLevel=green`, 10 активных пакетов, 8 lag-OK, C/H=0: `green_required=10`, `green_projected_lag_ok=8`, `green_plan_shortfall=2`, но `postPlanGoal=feasible`.
- `targetLevel=green`, 1 lag-OK пакет с H:1 и без target: `postPlanHigh=1`, `postPlanGoal=feasible`, хотя green требует H=0.

Это касается не только формулировки: `postPlanLagOk`, `postPlanPolicyRequired`, shortfall, summary и UI тоже представляют yellow-проекцию как выбранную цель green. При этом `_draft_target_for_major` отдаёт агенту green target, поэтому цифры цели и фактический список изменений могут расходиться.

## Исправление

1. При построении плана явно выбрать активный target level из зафиксированной политики запуска. Для yellow использовать full-scope minLagOkPct и настроенные лимиты C/H; для green — 100% соблюдения package lag-policy, C=0, H=0 и применимые лимиты M/L/unknown из определения green status/acceptance. Не использовать значение yellow `maxKnownHigh=1` как разрешение для green.
2. Считать projected lag и projected security на **том же точном назначении версий**, которое попадает в `proposed` для выбранного уровня. Сохранять отдельно диагностические yellow/green варианты, если нужны, но поля `postPlanGoal`, `postPlanLagOk`, `postPlanPolicyRequired`, shortfall в result/prompt/UI должны соответствовать выбранному уровню. Неполные OSV/lag-данные — unknown, не feasible.
3. В prompt/UI явно показывать `targetLevel` рядом с вердиктом. `DRAFT_READY` остаётся статусом завершённого сканирования и не заменяет вердикт цели. Не менять семантику yellow, подтверждённую в R12.

## Приёмка

- Failing-first тест из первого воспроизведения: green 8/10, C/H=0 → `blocked`, required=10, projected=8, shortfall=2; prompt/UI не говорят «достижима».
- Failing-first тест из второго: green 1/1 lag, H:1 → `blocked`, хотя yellow для того же настроенного лимита может быть допустим.
- Green 100% lag, C/H=0, M/L в пределах политики, полный OSV → `feasible`; неполное OSV → `unknown`; M/L за пределами green policy → `blocked`.
- Парный тест yellow/green на одних строках с разными выбранными target: projection совпадает именно с `proposed`, а verdict/required меняются по выбранному уровню.
- Проверить RU/EN prompt, Desktop metadata/rendering, несколько проектов с разными policy, регресс Python/Desktop и public sanitization. Реальный проект можно проверять обезличенно; не публиковать приватные имена/пути/registry URL.