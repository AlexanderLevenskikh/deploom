# Приёмка аудита и исправлений DepLoom (обновлено 2026-10-12)

Исходная ревизия: `4c40716` (v0.2.218). Документ описывает изменения,
подготовленные к следующему релизу, и фактически проверенные границы.
Исторические результаты не заменяют проверки текущей реализации.

## C2: планировочный registry cache

`registry_metadata_cache.py` сохраняет packument по registry/package с TTL и
provenance. Кеш опционален, выключен без пути; стандартный TTL — 600 секунд.
Latest/version/engines/history читаются из одного packument. В контролируемом
сценарии повторного запуска число HTTP GET сокращается с 4 до 2.
Install/verifier и vulnerability audit не используют планировочный кеш.
Для Modern Yarn сохранён точный `yarn-inventory`, без подмены npm-lock bridge.
Ошибки создания каталога и записи кеша не превращают успешный registry fetch
в инфраструктурную ошибку. Модульные проверки: 14 тестов.

## C3: polling, live-state и журнал

UI poll кеширует только committed manifests и containment refs, на 15 секунд.
Fingerprint учитывает OID refs и SHA-256 содержимого prompt, а также project,
HEAD/branch, status/worktree listing и stat prompt. Completion-state, runtime,
текущий manifest при отсутствии merged ref и содержимое/dirty state остальных
worktree читаются заново. Ошибка дешёвого Git-запроса исключает cache hit.
Execution/integration guards по умолчанию обходят UI cache. Manifests читаются
по captured OID; изменение refs во время probes делает snapshot недоверенным.

`check-git-facts-cache.mjs` проверяет настоящий production reader против
временного Git-репозитория и worktree: повторный poll не повторяет immutable
probes, completion/runtime/dirty state обновляются, ошибка status даёт
`trustworthy=false`, authoritative read выполняет probes заново.

Renderer пакетирует события каждые 32 мс; итоговый буфер ограничен 1000 строками.
Durable backend log сохраняется отдельно (батчи 250 мс / 64 KB); виртуализация
не изменяет Copy и durable evidence. Rejected refresh сохраняет последний
payload и показывает ошибку с возможностью повторного опроса.

Raw-журнал использует измеряемую высоту сообщений, prefix sums и bounded
window. Измерения ResizeObserver пакетируются через animation frame; ширина
панели инвалидирует измерения. Проверены 1000 длинных сообщений: последняя
строка видна, возврат наверх работает, отрицательного счётчика нет.

Graph/dashboard загружаются по запросу. Последняя сборка: main 533.19 kB
(gzip 156.48), graph 27.32 kB (8.67), dashboard 1.46 kB (0.66).
Исторический main до lazy split: 555.64 kB (161.99). Первый переход загружает
соответствующий chunk; первый экран его не запрашивает. Предупреждение Vite
о main >500 kB остаётся и не скрывается.

## C4: copy/hash/install и isolation

Root/monorepo topology fixtures проверяют dependency roots, полный copy и
sealed manifest. Cold/warm fixture сравнивает существующий full-copy fallback
и guarded clone против одного prepared snapshot. При 152 и 1262 payload files
warm сохраняет 31 junction и не копирует dependency payload; cold/seal проходят
все файлы. Это измерение существующего guarded path, а не нового переписанного
алгоритма. На маленьких деревьях warm может оказаться медленнее cold.

Добавлен `tests/acceptance/test_prepared_install_measurements.py`: настоящий
npm registry на loopback с fixture tarball, настоящий npm install и production
`verify_assignment`, project check и exact proof reuse. Два topology-сценария:
обычный проект и два участника монорепозитория с одним исходным root lockfile.
Параллельные установки идут в отдельные verification clones; install roots
различны, исходные manifests и общий lockfile побайтно неизменны.

Представительный прогон (время зависит от диска/нагрузки):

| Fixture | Cold installs | Warm installs | Cold | Warm |
| --- | ---: | ---: | ---: | ---: |
| Root | 2 | 0 | 4.14 s | 2.05 s |
| Monorepo, 2 members | 4 | 2 | 5.14 s | 5.11 s |

Гейт проверяет сокращение install calls и isolation; wall time не является
условием успешности. Оставшиеся installs в monorepo выполняются и не объявлены
пропущенными. Измерения не доказывают ускорение произвольного пользовательского
проекта. Install/verifier/source/proof guards не ослаблялись.

Per-path mutex `restore_resolved_dependency_state` сериализует write+replace+
assert между потоками одного процесса. Он не объявляется межпроцессной
блокировкой и не защищает следующий install. `os.replace` не допускает torn
bytes, hash assertion обнаруживает наблюдаемый DRIFT; это не гарантия исключения
писателей. Isolation установок подтверждается отдельным физическим тестом.

## UI и discovery

Журнал доступен при 1080×700. Главный CTA находится в верхней utility row;
сценарий и действие берутся из того же состояния iterative panel. Есть явная
кнопка возврата в workspace overview. Dialog focus trap пересчитывает доступные
контролы при каждом Tab, учитывает скрытые/disabled элементы и возвращает фокус.
Добавление проекта использует bounded async discovery с depth/entry/time limits,
явный выбор кандидата и защиту от устаревшего ответа. Folder picker инвалидирует
прежний scan, а ответы сопоставляются с request epoch вместо захваченного старого
path. До завершения discovery submit закрыт; очистка path снимает scanning state. Package path и repository
path остаются разными координатами; исходный root lockfile не подменяется.

## Проверки перед публикацией

- Targeted Python: 18 тестов кеша, resolved-state и physical install — OK.
- Desktop: lint 0 ошибок / 18 предупреждений; build — OK.
- `python run_tool_tests.py --suite production-fast`: 64 теста — OK.
- python run_tool_tests.py --suite all: 1983 теста за 1554.985 s — OK (skipped=4).
- python run_tool_tests.py --suite storage-delivery: 1 физический тест за 368.946 s — OK; small/medium/large завершены с audit PASS.
- Desktop inventory: 71/71 contract checks — OK; после последних исправлений повторно прошли проверки Git cache, log batching и interaction contracts. Релизный helper повторяет полный inventory на точном release commit.
- Browser QA: 8 комбинаций RU/EN × light/dark × 1080×700/1440×940 — PASS. Дополнительно проверены 1000 многострочных логов, первый переход на lazy-экраны, modal focus/Escape и picker с запоздавшим ответом discovery; ошибок React нет.

Реальная миграция пользовательского проекта не запускалась. Фикстуры полностью
создаются во временных директориях. Статус GitHub CI и сборки installers после
публикации следует проверять отдельно от локальных гейтов.