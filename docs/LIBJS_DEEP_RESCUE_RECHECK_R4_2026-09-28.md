# Повторная приёмка R4 — 28.09.2026

Вердикт: прежние локальные дефекты в основном устранены; автоматический repair пока НЕ ПРИНЯТ.

Проверены текущие незакоммиченные изменения поверх baf6359. Production-код не менялся в ходе приёмки. Генератор SHA256 <redacted:опaque-reviewer-fingerprint>; desktop/electron/main.ts SHA256 <redacted:opaque-reviewer-fingerprint>.

## Что закрыто относительно R3

- Optional peers: исходный контрпример теперь возвращает совместную когорту [a,b]; a@2+b@2 проходит hard-model. Неудачные одиночные шаги больше не являются единственным вариантом.
- Feasible: и низкоуровневый report, и наружный solver_statuses_out теперь возвращают feasible, без переименования в optimal.
- Draft context: production-shaped sourceCommit=12345678 сохраняется; packageManager=yarn@1.22.22 сохраняет версию; runtime явно помечен как объявленное требование, например node 18 (declared:engines.node).
- Метрики: vulnerablePackages=1 для одного пакета с шестью findings; findingsOccurrences=6 отдельно; advisories/nodes=null, поскольку аудит ещё не выполнен.
- Overrides: в коде исправлены передача iteration config в project/adaptive checks и запись карты в следующий incumbent. Новый тест проверяет resolver/project вызовы после seed из config; полная реальная materialization/restart цепочка остаётся вне этой приёмки.
- Составной test, policy=0, template без зависимости от личного ignored checklist и ранее исправленные unknown/partial переходы сохранены.

## Оставшиеся блокирующие дефекты repair

### P1 — producer и Desktop consumer используют разные пути

`dependency_live_roadmap_generator.py:26396` задаёт progress path `<settings_base>/.dependency-roadmap/state/baseline-verification-progress.json`, который передаётся в verify loop (`:26853`). `_persist_repair_requests` (`:10279`) пишет в родитель progress path: `.dependency-roadmap/state/repair-requests.json`.

`desktop/electron/main.ts:4553` читает `<workspace>/.dependency-roadmap/artifacts/runs/<job.id>/repair-requests.json`. Это другой путь даже при совпадении settings_base и workspace. Не добавлен перенос/публикация файла между этими местами.

Поэтому реальный Baseline-запрос не попадает в новый Desktop consumer. Кроме того, новый код расположен внутри уже запущенного runGroupAgentSession: сам по себе запрос Baseline не запускает repair job и не возвращает его результат в Baseline.

Исправить: единый явный контракт пути/ссылки на артефакт и идентичности run; передавать ссылку из Baseline в диспетчер. Тест должен создавать запрос через настоящий Python producer и находить его через настоящий Desktop consumer, затем запускать repair, без ручного копирования в удобный для теста каталог.

### P1 — любой зелёный batch закрывает чужие repair-запросы того же проекта

`desktop/electron/main.ts:4554–4556` фильтрует requests только по project.name и удаляет каждый совпавший requestId. Не сверяются mode, fingerprint, assignment, source snapshot, overrides, policy или выполненные команды. Импортированный findOpenRepairRequest здесь вообще не используется.

Пример: проект имеет запросы yellow/a@2 и green/b@3. Успешный batch обновляет только c. При доступном файле оба запроса будут удалены и помечены resolved, хотя a/b не проверены. Ошибка становится активной сразу после исправления пути из предыдущего пункта.

Исправить: закрывать только конкретный запрос, для которого есть свежая проверка полного соответствующего cumulative состояния. Успех произвольного batch не доказательство исправления остальных запросов. Тест: два запроса одного проекта, разные режимы/версии/snapshots; один успешный checkpoint не удаляет второй и не удаляет несовпадающий первый.

### P1 — Python repair-resolved зависит от resolver-green, а не обязательно project-green

`dependency_live_roadmap_generator.py:14090–14105`: в diagnostic режиме неструктурная project-ошибка оставляет result.ok от resolver равным True. Сразу затем ветка `if result.ok` удаляет durable request и эмитирует repair-resolved (`:14113–14120`).

Подтверждено отдельным production-loop probe с подменой verifier: resolver ok=True, project ok=False, repairFileStillExists=False и событие repair-resolved. Результат сохранён в diagnostic-repair.json. Таким образом, после смены snapshot тот же tuple всё ещё не проходит project-команду, но запрос считается исправленным. Сохранить resolver-green как частичный результат допустимо; закрывать source/config repair на этом основании нельзя.

Исправить: для resolution требовать относящийся к запросу project_result.ok и полное совпадение контекста проверки. При diagnostic failure или отключённых checks оставить запрос открытым. Добавить негативный тест: resolver passes, project fails, snapshot changed → request remains open; позитивный: обе стадии проходят → закрывается только соответствующий request.

## Проверки и ограничения

- 120 unittest-тестов A–F и выбранных regressions прошли.
- 5 отдельно запущенных проверок composite test, alias, policy=0 и draft context/publisher прошли.
- 7 тестов G прошли в изолированном окружении (source snapshot заменён хешем тестового package.json, registry/executable подменены; physical verifier подменён самими тестами). Итого 132 теста прошли. Дополнительный diagnostic-repair probe воспроизвёл ложное закрытие запроса при красной project-проверке; он не включён в этот счётчик.
- Electron TypeScript compile, check-migration-verification.mjs и check-repair-handoff.mjs прошли.
- Независимые helper probes подтвердили optional peers, feasible, production-shaped sourceCommit/runtime и единицы метрик; результаты в probes.json.
- Python тесты запускаются с workspace-temp адаптером из-за недоступных mode-0700 директорий tempfile в Windows sandbox. Изолированные control-flow проверки дополнительно подменяют source snapshot/registry/executable и physical verifier; они не являются реальной миграцией.
- `git diff --check` не обнаружил whitespace errors, только предупреждения о нормализации CRLF.
- Реальную миграцию ЦЕЛЕВОГО_ПРОЕКТА я не запускал. Проверенный REAL_RUN_ACCEPTANCE по-прежнему содержит незаполненные checkpoint/before-after/restart/first-useful-upgrade.

Локальные материалы: `.dependency-roadmap/audit-reviews/recheck-r4-2026-09-28/`. Новый check-repair-handoff проверяет парсер/чтение/удаление файла и helper matching; не проверяет фактический producer path, runGroupAgentSession или основание для resolution.

## Минимальный следующий пакет

1. Один адресуемый producer→consumer repair contract; реальный dispatch и возврат результата.
2. Запрет resolution по одному project.name или resolver-green; закрытие только по подходящему project-green evidence.
3. Негативные интеграционные тесты: чужой batch, другой mode/snapshot, failing project checks и два одновременно открытых запроса.
4. После этого реальный изолированный ЦЕЛЕВОГО_ПРОЕКТА run с полезным checkpoint и restart, фактическими метриками и developer-артефактами.

Уже исправленные пункты R3 переделывать не требуется. Оставшаяся работа сосредоточена в жизненном цикле repair и реальном подтверждении результата.
