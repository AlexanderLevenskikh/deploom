# Повторная приёмка исправлений v0.2.154 (HEAD 70456e1)

Предыдущие P1.1/P1.2 **по заявленной форме** исправлены: agent-gate обновляет ТЗ, stale больше не блокирует кнопки и dispatch; поиск при заданном Node перебирает альтернативы и проверяет engines до install. Исходники/релиз не изменялись этой приёмкой. Полную готовность не подтверждаю по нижеприведённым причинам.

## P1. Новый поиск может понизить уже установленную зависимость

`iterative_migration.py::_discover_targets`: при несовместимом latest перебирает последние 12 версий и выбирает первую с известным совместимым engines.node, **не проверяя, что candidate новее текущей версии**. Версия с неизвестными engines пропускается. Воспроизведение текущей функции с синтетическими registry ответами: current=8.0.0, latest=9.0.0 (engines>=22), 8.0.0 (unknown), 7.0.0 (engines>=18), Node 20.11.0 → `targets={'sample': '7.0.0'}`, статус `discovered-compatible`. Процесс миграции не должен сам понижать пакет и объявлять это полезным обновлением.

Исправить: допускай только строго более новые версии по той же npm-semver модели; unknown не трактуй как incompatible, но не выбирай её как verified-compatible без evidence. Если в bounded окне нет подтверждённой совместимой версии **новее текущей**, сохрани текущую и невыполненную цель/причину, не выдавай downgrade или ложную complete. Добавь regression текущая/равная/более старая и prerelease. Запусти тест через begin→plan-next, а не только pure helper.

## P1. ТЗ может не обновиться, а агент получит старый текст

`desktop/electron/main.ts:7754`: `refreshIterativeTaskArtifact()` возвращает boolean по коду `export-task`; `flow:iterative:drive` (agent-gate) и `flow:iterative:agent` этот результат игнорируют. В agent-handler после неудачного export читается `readIterativeCurrentTask(runDir)` и используется его текст либо пустая строка. Это может отправить старое ТЗ из C0 для кандидата C1 или пропустить содержательное ТЗ вообще. Ремонтный prompt содержит отдельную exact assignment, но старая постановка может ей противоречить. Попытка не должна начинаться с невалидным task scope.

Исправить: различать отсутствие exportable state и операционную ошибку записи/генерации; на dispatch требовать актуальный task manifest с run/policy/checkpoint/candidate identity и проверенными content hashes, либо строить самодостаточный точный prompt из durable state без task артефакта. Если export не удался, сохранить checkpoint и вернуть recoverable ошибку; не отправлять старый текст. Тестировать отказ записи/ненулевой exit export между C1 и следующим agent-gate.

## Отдельное ограничение runtime/evidence

`build_run_config` разрешает requested Node до target discovery, но `_discover_targets(..., runtime_env=None, node_version=effectiveVersion)` запускает npm metadata probes через ambient npm/PATH. При разных локальном и выбранном Node это не тот же исполняемый runtime, который затем используется для install/verify. В приватном registry с manager-specific настройками результаты могут различаться. Передавай выбранный runtime_env в discovery/probes; тестируй реальный child `node -v`/manager и источник metadata при отличающемся PATH. Это часть ранее требуемого D3.1.

## Проверки и границы

- `test_iterative_target_discovery.py`: 8 OK; `npm run check:iterative-runner`: OK. Код обоих прежних исправлений прочитан.
- Точечное воспроизведение downgrade выполнено на production `_discover_targets` с подменёнными только ответами registry.
- Отчёт агента заявляет 1510 Python OK, production-fast 64 OK и synthetic physical acceptance; они не покрывают downgrade, failure of task refresh и live provider.
- Реальный проект/live-agent pilot, физический install под выбранным Node и статус GitHub Actions тега этой приёмкой не подтверждены. Публикация master/tag была заявлена агентом и есть в локальной истории, но CI/release artifacts отдельно не установлены.

Принимать всю миграцию после исправления P1 и сквозного Desktop acceptance: пустой workspace→C0→candidate→agent→C1→следующий candidate→C2, включая export failure/restart и проверку Node под фактическим child process.
