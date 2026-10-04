# Индекс документации DepLoom

Это индекс **действующих контрактов** и способ отделить их от исторических отчётов.
Актуальные контракты — это то, что описывает поведение текущей реализации на `master`
и на что стоит ссылаться при разработке и ревью. Исторические отчёты ниже — разовые
ревизии/эксперименты на конкретных версиях; они зафиксировали факт на свою дату и их
содержимое **не** является доказательством того, что сегодняшняя реализация так работает
(см. `AGENTS.md`).

## Актуальные контракты

| Документ | Что описывает |
| --- | --- |
| `README.md` | Публичный обзор продукта, платформы, установка, запуск, публикация, поддержка пакетных менеджеров. |
| `HOW_IT_WORKS.md` | Простыми словами: baseline, solver, greedy, группы, накопительная миграция, верификация. |
| `settings.schema.json` | Схема настроек проекта и Desktop; источник истины для путей/режимов. |
| `docs/ITERATIVE_MIGRATION_CONTROL_LOOP.md` | Текущий управляющий цикл накопительной миграции, фиксы аудита B1–B7, bounded GC и результаты валидации. |
| `docs/ITERATIVE_MIGRATION_DEMO.md` | Профильная интеграционная демонстрация через настоящий Node coordinator + Python CLI. |
| `docs/MIGRATION_VALIDATION.md` | Контракт валидации миграции: явные профили, fail-closed, не-пересъёмка baseline, сохранение старых падений. |
| `docs/ITERATIVE_REPAIR_RECOVERY.md` | Ремонт исходников/конфига в изолированном checkout, typed feedback, ре-верификация на свежих bytes. |
| `docs/STORAGE_COHORT_REVIEW.md` | Хранение trial/checkpoint bytes, владельцы, GC и бюджеты когорт. |
| `docs/ITERATIVE_MIGRATION_IMPLEMENTATION_STATUS.md` | Хронология реализации итеративной миграции (changelog-формат; датированные записи ниже в нём — факты на дату). |
| `docs/ITERATIVE_MIGRATION_ACCEPTANCE.md` | Приёмочные сценарии итеративной миграции. |
| `tests/`, `desktop/scripts/check-*.mjs` | Машинные контракты: unit/regression/acceptance и Desktop behaviour checks. |

## Исторические отчёты и разовые задачи

Эти файлы фиксируют состояние на свою дату/версию и не являются текущими контрактами.
Их не следует использовать как доказательство поведения сегодняшней реализации.

- **Специфичные версии ревью итеративной миграции:** `ITERATIVE_MIGRATION_REVIEW_2026-09-29.md`,
  `ITERATIVE_MIGRATION_REVIEW_LAUNCH_UX_2026-10-01.md`, `ITERATIVE_MIGRATION_REVIEW_V02153_2026-09-29.md`,
  `ITERATIVE_MIGRATION_REVIEW_V02154_2026-09-30.md`, `ITERATIVE_MIGRATION_REVIEW_V02154_POSTFIX_2026-09-30.md`,
  `ITERATIVE_MIGRATION_PLAN_2026-09-29.md`, `ITERATIVE_MIGRATION_AGENT_TASK_2026-09-29.md`,
  `ITERATIVE_MIGRATION_FINAL_AGENT_TASK.md`, `ITERATIVE_MIGRATION_DESKTOP_FOLLOWUP_TASK.md`.
- **Draft modes revalidation (серия):** `DRAFT_MODES_AUDIT_AND_AGENT_TASK_2026-09-22.md`,
  `DRAFT_MODES_REVALIDATION*_2026-09-2X.md` (отчёты и followup), `DRAFT_GREEN_GOAL_FOLLOWUP_2026-09-25.md`,
  `DRAFT_CANDIDATE_WINDOW_FOLLOWUP_2026-09-24.md`, `DRAFT_PARTIAL_PLAN_REAL_RUN_FOLLOWUP_2026-09-24.md`,
  `DRAFT_MODES_REVALIDATION_10/11/12_REPORT_2026-09-24.md`.
- **Deep audit/rescue (серия `LIBJS_DEEP_*`):** `LIBJS_DEEP_AUDIT_2026-09-27.md`,
  `LIBJS_DEEP_RESCUE_AGENT_TASK_2026-09-27.md`, `LIBJS_DEEP_RESCUE_RECHECK*_2026-09-28.md`.
- **Repair dispatch acceptance (серия):** `REPAIR_DISPATCH_ACCEPTANCE_R6/R7/R8_2026-09-28.md`.
- **Verified baseline followups (исторические):** `VERIFIED_BASELINE_DEEP_DIRTY_CHECKOUT_FOLLOWUP_2026-09-25.md`,
  `VERIFIED_BASELINE_FAST_PLATEAU_FOLLOWUP_2026-09-25.md`, `VERIFIED_PARTIAL_GRAPH_SOURCE_CHURN.md`,
  `POST_ROADMAP_AUDIT.md`.
- **Прочее историческое:** `MIGRATION_FLOW.md`, `DEMO_24_PACKAGES.md`, `DEPENDENCY_GRAPH.md`,
  `dependency-flow-article.md`, `project-topology.md`, `REAL_RUN_ACCEPTANCE_TEMPLATE.md`,
  `verification-observability.md`, `verification-substrate-v2.md`.
- **QA-эксперименты 2026-10-02:** `QA_2026-10-02_ITERATIVE_USABILITY.md`,
  `QA_2026-10-02_PROJECT_SUBMODULE_SCOPE.md`, `QA_2026-10-02_RESTART_AND_FIXED_INPUTS.md`.
- **Дата-папки `docs/2026-*/`** не публикуются (`.gitignore`) — локальные прогоны аудита на дату.

## Обновление индекса

После изменения контрактного документа обновите таблицу «Актуальные контракты».
Разовый отчёт, описывающий конкретную ревизию/эксперимент, добавляйте в «Исторические отчёты»,
а не в актуальные контракты.
