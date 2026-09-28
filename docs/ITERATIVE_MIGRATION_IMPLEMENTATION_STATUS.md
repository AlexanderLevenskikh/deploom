# Итеративная миграция DepLoom — статус реализации

Дата начала: 2026-09-29. План: `docs/ITERATIVE_MIGRATION_PLAN_2026-09-29.md`.
Исследованный для плана HEAD: `ca67872` (v0.2.149). Задание: `docs/ITERATIVE_MIGRATION_AGENT_TASK_2026-09-29.md`.

Этот файл — живой журнал реализации: этап, commit, реализованное поведение, проверки, открытые пункты, следующий шаг.

## Решения по архитектуре (рекорд, не обещание)

- Python остаётся владельцем детерминированных planner/materializer/verifier и durable-состояния. Новый модуль `iterative_migration.py` выделяет primitives из существующего generator/verifier, а не копирует solve-цикл.
- Новый execution path — отдельные bounded шаги поверх одного durable run directory; Desktop-координатор решает, когда вызывать шаг, и запускает repair-агента.
- Проверенные primitives для переиспользования: `plan_progressive_extension` (plan-next), `verify_assignment` (authoritative materialize+verify), `materialize_private_tree` / `source_snapshot` durable containers (isolated trial/checkpoint bytes), `_apply_assignment` (exact manifest application), `build_repair_request` (diagnostics handoff), `manual_dependency_audit.build_report` (audit).
- Trial агента — физически материализованный isolated workspace с точными версиями (install с exact spec), независим от verifier trial. Acceptance authority — всегда `verify_assignment` на repaired bytes (verify-exact), никакой «слова агента» или stdout-хвоста.
- C0 — «Исходный замер»: durable source snapshot + manifest/lock hashes + контрольная проверка (control verify) + classification RED. Baseline остаётся названием исходного замера; процесс называется «Итеративная миграция».

## Этапы

(заполняется по мере реализации; см. commits ниже)

## Журнал

### 2026-09-29 — рекогносцировка и контракты (в работе)

Прочитаны ключевые модули: `block_psi_progressive_baseline.py`, `dependency_live_roadmap_generator.py` (main/CLI/Draft/baseline snapshot), `baseline_constraint_verifier.py` (verify_assignment, config, materialize/apply), `verification_proof.py`, `source_snapshot.py`, `resolved_dependency_state.py`, `baseline_repair_handoff.py`, `dependency_compatibility_evidence.py`, `block_v_recovery.py`, `block_psi_anytime.py`, `manual_dependency_audit.py`, `run_tool_tests.py`; Desktop: `main.ts`, `repair-dispatch.ts`, `repair-checkout.ts`, `baseline-repair-result.ts`, `materialization-proof.ts`, `resolved-state-proof.ts`, `migration-verification.ts`, `planner-session.ts`, `agent-session.ts`, `acceptance-policy.ts`, `preload.cts`, `desktop/src` (flow screen, i18n).

Ключевые находки (подробный inventory — в рекогносцировке агентов):
- `verify_assignment` = единственный authoritative verifier; trial disposable, source snapshot sealed.
- `plan_progressive_extension` = ровно тот «небольшой шаг от incumbent» из плана; `assignment_fingerprint` — exact identity.
- Repair handoff desktop уже читает `repair-requests.json`; typed feedback агента необходимо расширить (сейчас `repaired|partial|blocked`).
- `npm_config_cache` наследуется в install env (block_vex_storage.package_manager_cache_environment) → физический тест может быть офлайн через `npm cache add <tarball>` + `--offline`.
- Все `*.py` репозитория бандлятся в packaged tool (`electron-builder.yml` extraResources) — новый модуль попадёт в релиз автоматически.

Открытые пункты: (см. следующий шаг)

### Следующий шаг
1. Написать `iterative_migration.py` (schemas/identities/authority matrix, begin/plan-next/materialize/precheck/verify-exact/feedback/status/finish/audit, ledger, budget/lease).
2. Юнит-тесты схема/identity/stale-rejection + cross-language fixture.
3. Desktop-координатор `iterative-migration.ts`, check-скрипт, opt-in маршрут.
4. Физический acceptance test (offline npm cache) + scripted agent.
5. Прогон `run_tool_tests.py --suite all` + `production-fast`, Desktop lint/build/`check:*`.
6. `docs/ITERATIVE_MIGRATION_ACCEPTANCE.md`.
