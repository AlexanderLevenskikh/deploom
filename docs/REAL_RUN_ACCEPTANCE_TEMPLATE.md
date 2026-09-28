# REAL RUN ACCEPTANCE — шаблон чек-листа (tracked source of truth)

Эталонный шаблон чек-листа приёмки изолированного реального прогона. Заполненный
экземпляр хранится рядом с материалами конкретного прогона и игнорируется Git
(`.dependency-roadmap/audit-reviews/<product>-deep-<date>/REAL_RUN_ACCEPTANCE.md`);
данный файл — tracked источник, по которому единичный тест проверяет, что чек-лист
воспроизводим и не вырождается в обещание без измерений.

Изолированный реальный прогон на машине пользователя, после synthetic/fixture
покрытия (parts A–G, `tests/test_audit_2026_09_27_deep_rescue_part_{a..g}.py`).
**Никаких заранее обещанных процентов продукта или времени: не обещаем.** Бюджет и
контрольный сценарий зафиксированы ниже; результат измеряется фактически.

## Зафиксированные настройки (тот же intent, что в аудите)

- productMode: `deep`, controlMode: `AUTONOMOUS`, executionMode: `BACKGROUND`, searchMode: `AUTO`
- budgetMinutes: `<ФАКТИЧЕСКОЕ_ЗНАЧЕНИЕ>`
- acceptancePolicy: targetLevel `yellow`, lagPolicyMonths `12`, minLagOkPct `80`, maxKnownCritical `0`, maxKnownHigh `1`
- manager: `yarn`, lockfile: `yarn.lock`, source: origin/master@<SHA>
- discovered checks: `yarn flow:check`, `yarn lint`, `yarn build`, `yarn test:unit`
  (composite `test` = `yarn test:unit && yarn test:build` не теряет `test:build` — Part F/R7 gate fix;
  для продукта явные verificationCommands из настроек имеют приоритет — это конфигурация владельца)
- isolation: изолированный checkout, без изменения registry/Git refs/lockfile исходного продукта;
  installs/repair только в изолированном checkout

## Контрольный сценарий (что должно измениться относительно аудита)

1. Большая peer-компонента больше НЕ роняет весь прогон: per-component tolerant verified
   путь (Part C), неизвестная большая компонента записывается честно (status=unknown,
   terminalStatus=SOLVER_UNKNOWN), независимый малый SAT-candidate физически проверяется.
2. Полностью закреплённые (residual/override) компоненты решаются без свежего глобального
   solve (Part C short-circuit), отчёт честный (pinned/proof=feasible, не optimal — Part G R6).
3. Миграционные когорты — версиеспецифическая delta-closure (Part D); overrides — pin, не блок;
   resolver overrides переносятся в следующую когорту (Part G R2).
4. Project-kind провал (config/API) — repair handoff к Executor, НЕ dependency nogood (Part E/G R1):
   тот же набор версий может пройти после repair (candidate → repair → cumulative verify → checkpoint → restart).
5. Draft/прогресс: migration-progress.json + migration-journal.md, NOT_VERIFIED/PLANNING_ONLY,
   фактический execution context, audit command, честные security-единицы (Part B/G R3/R4).

## Шаги

1. Обновить checkout до фикса, соответствующего проверяемой версии (см. релизный тег).
2. Взять срез входов: `package.json`, `yarn.lock`, `HEAD` (SHA), settings проекта — сохранить
   в `snapshot/` рядом, чтобы legacy/current сравнение шло на одинаковых входных данных.
3. Запустить прогон через Desktop (baseline-intent файл) ИЛИ эквивалентным CLI-вызовом,
   с `--run-id` и `--workspace-id`, с тем же intent.
4. Дождаться остановки (бюджет/plateau/verified). Не отменять ради зелёного: restart-сценарий
   проверяется отдельно.
5. Заполнить метрики ниже; прикрепить ссылки логов и оба developer-файла.

## Метрики (заполнить фактическими значениями)

- scanElapsedSeconds / registryPrefetchElapsedSeconds / sourceCaptureElapsedSeconds
- solveAndVerifyWallSeconds; per-stage: scan/solve/install/check/repair times
- timeToFirstCheckpoint (секунды от старта до первого сохранённого checkpoint)
- timeToFirstProjectVerified (секунды до первого project-verified результата)
- checked / accepted / deferred когорт (counts по когортам; deferred c конкретным reason)
- actual before/after lag: denominator/numerator, C/H/M/L/U по источникам (OSV/canonical/bridge), coverage
- результат после restart: verified состояние не теряется, source identity не подменяется
- оба developer-файла (MIGRATION_REPORT + DEVELOPER_UPGRADE_GUIDE), ссылки на командные логи
- **First useful upgrade** обязан быть ненулевым, если фикстура/данные содержат доступное
  безопасное обновление; неизменённый source floor этот критерий не закрывает
- legacy/current vs исправленный подход на ОДИНАКОВЫХ metadata/lock/runtime (если практически возможно)

## Если real run заблокирован (доступ/runtime/registry)

Отметить явно: **STATUS: INCOMPLETE** + причина в этом файле; сохранить готовые fixes и
evidence; НЕ называть задачу полностью закрытой по unit tests. После разблокировки повторить шаги.

## Статус

- [ ] TIME_TO_FIRST_CHECKPOINT: …
- [ ] TIME_TO_FIRST_PROJECT_VERIFIED: …
- [ ] COHORTS checked/accepted/deferred: …
- [ ] LAG before/after (den/num, C/H/M/L/U, coverage): …
- [ ] RESTART: …
- [ ] FIRST_USEFUL_UPGRADE: …
- [ ] DEV_FILES + LOG LINKS: …
- [ ] STATUS: DONE / INCOMPLETE (причина)
